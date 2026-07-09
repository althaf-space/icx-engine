"""Generic LSP JSON-RPC 2.0 client over stdio.

Handles Content-Length framing, background reader thread, request/response
routing, and per-request timeouts. Language-agnostic - works with any
LSP server (typescript-language-server, pyright-langserver, jdtls, etc.).
"""
from __future__ import annotations

import json
import logging
import queue
import subprocess
import sys
import threading
from pathlib import Path
from urllib.parse import unquote, urlparse

_log = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 3.0
_INIT_TIMEOUT = 30.0


def _kill_tree(proc: "subprocess.Popen") -> None:
    """Kill a process and all its children.

    On Windows, Popen.kill() only terminates the root JVM process; child
    processes spawned by jdtls (gradle daemons, classpath resolvers) survive
    and hold file locks that block the next graph build. /T kills the tree.
    """
    try:
        import psutil
        try:
            root = psutil.Process(proc.pid)
            children = root.children(recursive=True)
        except psutil.NoSuchProcess:
            return
        for child in children:
            try:
                child.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        try:
            root.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    except ImportError:
        if sys.platform == "win32":
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    capture_output=True,
                )
            except OSError:
                pass
        else:
            try:
                proc.kill()
            except OSError:
                pass


class Location:
    __slots__ = ("path", "line", "character")

    def __init__(self, path: str, line: int, character: int) -> None:
        self.path = path
        self.line = line
        self.character = character

    def __repr__(self) -> str:
        return f"Location({self.path!r}, line={self.line}, char={self.character})"


def uri_to_path(uri: str) -> str:
    """Convert a file:// URI to an OS-native path string."""
    parsed = urlparse(uri)
    path = unquote(parsed.path)
    if path.startswith("/") and len(path) > 2 and path[2] == ":":
        path = path[1:]
    return path.replace("/", "\\") if "\\" in path or (len(path) > 1 and path[1] == ":") else path


class LSPClient:
    """Minimal blocking LSP client. Not thread-safe for callers - use from one thread."""

    def __init__(self, cmd: list[str], root: Path, timeout: float = _DEFAULT_TIMEOUT) -> None:
        self._cmd = cmd
        self._root = root
        self._timeout = timeout
        self._proc: subprocess.Popen | None = None
        self._seq = 0
        self._pending: dict[int, queue.Queue] = {}
        self._lock = threading.Lock()
        # Separate lock for stdin writes: reader thread may respond to server
        # requests from _read_loop, so _send can be called from two threads.
        self._send_lock = threading.Lock()
        self._reader: threading.Thread | None = None
        self._running = False
        self._consecutive_timeouts = 0

    @property
    def consecutive_timeouts(self) -> int:
        return self._consecutive_timeouts

    def start(self) -> bool:
        try:
            self._proc = subprocess.Popen(
                self._cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except (OSError, FileNotFoundError) as exc:
            _log.debug("LSP server start failed: %s", exc)
            return False

        self._running = True
        self._reader = threading.Thread(target=self._read_loop, daemon=True, name="lsp-reader")
        self._reader.start()

        result = self._request("initialize", {
            "processId": self._proc.pid,
            "rootUri": self._root.as_uri(),
            "capabilities": {
                "textDocument": {
                    "definition": {"dynamicRegistration": False},
                },
            },
        }, timeout=_INIT_TIMEOUT)

        if result is None:
            _log.debug("LSP initialize timed out or failed")
            self._running = False
            return False

        self._notify("initialized", {})
        return True

    def _read_loop(self) -> None:
        proc = self._proc
        if not proc or not proc.stdout:
            return

        while self._running:
            header_bytes = b""
            while self._running:
                try:
                    ch = proc.stdout.read(1)
                except OSError:
                    return
                if not ch:
                    return
                header_bytes += ch
                if header_bytes.endswith(b"\r\n\r\n"):
                    break

            content_length = 0
            for raw_line in header_bytes.decode("utf-8", errors="replace").split("\r\n"):
                if raw_line.lower().startswith("content-length:"):
                    try:
                        content_length = int(raw_line.split(":", 1)[1].strip())
                    except ValueError:
                        pass

            if content_length <= 0:
                continue

            body = b""
            remaining = content_length
            while remaining > 0 and self._running:
                try:
                    chunk = proc.stdout.read(remaining)
                except OSError:
                    return
                if not chunk:
                    return
                body += chunk
                remaining -= len(chunk)

            try:
                msg = json.loads(body.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                continue

            msg_id = msg.get("id")
            msg_method = msg.get("method")

            if msg_id is not None and msg_method is not None:
                # Server-initiated request: respond null (method-not-found for unknown).
                self._send({"jsonrpc": "2.0", "id": msg_id, "result": None})
            elif msg_id is not None:
                with self._lock:
                    q = self._pending.get(msg_id)
                if q is not None:
                    q.put(msg)

    def _send(self, msg: dict) -> None:
        if not self._proc or not self._proc.stdin:
            return
        body = json.dumps(msg).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
        with self._send_lock:
            try:
                self._proc.stdin.write(header + body)
                self._proc.stdin.flush()
            except (BrokenPipeError, OSError):
                pass

    def _request(self, method: str, params: dict, timeout: float | None = None) -> object:
        with self._lock:
            self._seq += 1
            seq = self._seq
            q: queue.Queue = queue.Queue()
            self._pending[seq] = q

        self._send({"jsonrpc": "2.0", "id": seq, "method": method, "params": params})

        try:
            response = q.get(timeout=timeout if timeout is not None else self._timeout)
            self._consecutive_timeouts = 0
            return response.get("result")
        except queue.Empty:
            self._consecutive_timeouts += 1
            _log.debug("LSP timeout on %s (seq=%s, consecutive=%d)", method, seq, self._consecutive_timeouts)
            return None
        finally:
            with self._lock:
                self._pending.pop(seq, None)

    def _notify(self, method: str, params: dict) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc else None

    def did_open(self, path: Path, language_id: str) -> None:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return
        self._notify("textDocument/didOpen", {
            "textDocument": {
                "uri": path.as_uri(),
                "languageId": language_id,
                "version": 1,
                "text": text,
            }
        })

    def did_close(self, path: Path) -> None:
        self._notify("textDocument/didClose", {
            "textDocument": {"uri": path.as_uri()}
        })

    @staticmethod
    def _parse_definition_result(result: object) -> list["Location"]:
        """Normalize a textDocument/definition result into Location objects.
        Shared by definition() and definition_batch() so both parse identically."""
        if not result:
            return []
        if isinstance(result, dict):
            result = [result]
        if not isinstance(result, list):
            return []
        locations: list[Location] = []
        for item in result:
            if not isinstance(item, dict):
                continue
            uri = item.get("uri") or item.get("targetUri", "")
            rng = (
                item.get("targetSelectionRange")
                or item.get("targetRange")
                or item.get("range")
                or {}
            )
            start = rng.get("start", {})
            if uri:
                locations.append(Location(
                    uri_to_path(uri),
                    start.get("line", 0),
                    start.get("character", 0),
                ))
        return locations

    def definition(self, path: Path, line: int, character: int) -> list[Location]:
        """Query definition. line/character are 0-indexed (LSP convention)."""
        result = self._request("textDocument/definition", {
            "textDocument": {"uri": path.as_uri()},
            "position": {"line": line, "character": character},
        })
        return self._parse_definition_result(result)

    def definition_batch(
        self,
        requests: list[tuple[Path, int, int]],
        window: int = 12,
        abort_after_consecutive_timeouts: int | None = None,
    ) -> list[list[Location]]:
        """Pipelined textDocument/definition. Sends up to `window` requests before
        draining their replies, overlapping the round-trips instead of blocking one
        query at a time - uses the LSP server's worker threads / idle CPU cores.

        Results are returned in the SAME order as `requests` (matched by request
        id), so the caller builds an identical edge set to a serial loop - only
        faster. Windowed draining preserves the circuit breaker: after
        `abort_after_consecutive_timeouts` consecutive timeouts the remaining
        requests are abandoned (empty result), matching the serial abort.
        """
        results: list[list[Location]] = [[] for _ in requests]
        n = len(requests)
        i = 0
        while i < n:
            if (abort_after_consecutive_timeouts is not None
                    and self._consecutive_timeouts >= abort_after_consecutive_timeouts):
                break
            chunk = requests[i:i + window]
            pending: list[tuple[int, queue.Queue]] = []
            for (path, line, character) in chunk:
                with self._lock:
                    self._seq += 1
                    seq = self._seq
                    q: queue.Queue = queue.Queue()
                    self._pending[seq] = q
                self._send({
                    "jsonrpc": "2.0", "id": seq, "method": "textDocument/definition",
                    "params": {"textDocument": {"uri": path.as_uri()},
                               "position": {"line": line, "character": character}},
                })
                pending.append((seq, q))
            for offset, (seq, q) in enumerate(pending):
                try:
                    response = q.get(timeout=self._timeout)
                    self._consecutive_timeouts = 0
                    results[i + offset] = self._parse_definition_result(response.get("result"))
                except queue.Empty:
                    self._consecutive_timeouts += 1
                finally:
                    with self._lock:
                        self._pending.pop(seq, None)
            i += window
        return results

    def shutdown(self) -> None:
        self._running = False
        if self._proc:
            try:
                self._request("shutdown", {}, timeout=5.0)
                self._notify("exit", {})
                if self._proc.stdin:
                    self._proc.stdin.close()
                self._proc.wait(timeout=15)
            except Exception:
                _kill_tree(self._proc)

    def __enter__(self) -> "LSPClient":
        return self

    def __exit__(self, *args) -> None:
        self.shutdown()
