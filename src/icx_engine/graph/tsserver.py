"""
tsserver lifecycle for icx graph extraction.

Manages a private tsserver install under ~/.icx/tsserver/. Re-detects the
user's active Node on every build. If Node version drift is detected, kills
any tracked tsserver subprocesses, removes the install, and reinstalls
under the current Node so the npm-resolved typescript package matches.

Cross-platform: Linux, macOS, Windows.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ICX_HOME = Path.home() / ".icx"
TSSERVER_DIR = ICX_HOME / "tsserver"
VERSION_FILE = TSSERVER_DIR / "node-version.json"
PID_FILE = TSSERVER_DIR / "pids.json"
LOCK_FILE = ICX_HOME / "tsserver.lock"

_INSTALL_TIMEOUT_SECONDS = 300
_NODE_VERSION_TIMEOUT_SECONDS = 5
_TERMINATE_GRACE_SECONDS = 3
_RMTREE_RETRY_ATTEMPTS = 5
_LOCK_TIMEOUT_SECONDS = 120


def ensure_tsserver() -> Path | None:
    """
    Return the path to a usable tsserver binary, installing or reinstalling
    as needed. Returns None when Node or npm is unavailable, or when the
    install fails for any reason - callers fall back to tree-sitter.
    """
    node = shutil.which("node")
    npm = shutil.which("npm")
    if not node or not npm:
        return None

    current_version = _detect_node_version(node)
    if not current_version:
        return None

    ICX_HOME.mkdir(parents=True, exist_ok=True)

    try:
        from filelock import FileLock, Timeout
    except ImportError:
        # filelock missing: proceed without serialization. Concurrent invocations
        # may race; user should install filelock for safety.
        return _ensure_under_lock(npm, current_version)

    try:
        with FileLock(str(LOCK_FILE), timeout=_LOCK_TIMEOUT_SECONDS):
            return _ensure_under_lock(npm, current_version)
    except Timeout:
        _log("Another icx process is installing tsserver. Falling back to tree-sitter.")
        return None


def _ensure_under_lock(npm: str, current_version: str) -> Path | None:
    binary = _binary_path()

    if binary.exists() and VERSION_FILE.exists():
        stored_version = _read_stored_version()
        if stored_version == current_version:
            return binary
        _log(
            f"Node version changed ({stored_version} -> {current_version}). "
            f"Reinstalling tsserver to match."
        )
        _kill_tracked_tsserver()
        _force_rmtree(TSSERVER_DIR)

    TSSERVER_DIR.mkdir(parents=True, exist_ok=True)
    _log(f"Installing tsserver under Node {current_version} (one-time, ~40 MB)...")

    try:
        result = subprocess.run(
            [npm, "install", "typescript",
             "--prefix", str(TSSERVER_DIR),
             "--silent", "--no-audit", "--no-fund"],
            capture_output=True,
            text=True,
            timeout=_INSTALL_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        _log("tsserver install timed out. Falling back to tree-sitter.")
        return None
    except OSError as exc:
        _log(f"tsserver install could not start: {exc}. Falling back to tree-sitter.")
        return None

    if result.returncode != 0:
        last_err_line = (result.stderr or "").strip().splitlines()[-1:] or ["unknown error"]
        _log(f"tsserver install failed: {last_err_line[0]}")
        _log("Falling back to tree-sitter (lower TS accuracy).")
        return None

    try:
        VERSION_FILE.write_text(
            json.dumps({
                "full": current_version,
                "installed_at": datetime.now(timezone.utc).isoformat(),
            }),
            encoding="utf-8",
        )
    except OSError:
        pass

    return binary if binary.exists() else None


def _binary_path() -> Path:
    """
    Resolve the tsserver entrypoint inside the local install. The package
    path is identical across operating systems; the npm-generated .bin/
    shims differ by OS, so we go directly to the package.
    """
    return TSSERVER_DIR / "node_modules" / "typescript" / "bin" / "tsserver"


def _detect_node_version(node_path: str) -> str | None:
    """Run `node --version` and return its output, e.g. 'v20.10.0'."""
    try:
        result = subprocess.run(
            [node_path, "--version"],
            capture_output=True, text=True,
            timeout=_NODE_VERSION_TIMEOUT_SECONDS,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    out = (result.stdout or "").strip()
    return out if out.startswith("v") else None


def _read_stored_version() -> str | None:
    try:
        data = json.loads(VERSION_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    val = data.get("full")
    return val if isinstance(val, str) else None


def record_tsserver_pid(pid: int) -> None:
    """
    Callers invoke this immediately after spawning a tsserver subprocess.
    The recorded PIDs are used to kill leftover processes before a
    version-drift reinstall.
    """
    if not TSSERVER_DIR.exists():
        return
    entries: list[dict] = []
    if PID_FILE.exists():
        try:
            entries = json.loads(PID_FILE.read_text(encoding="utf-8"))
            if not isinstance(entries, list):
                entries = []
        except (OSError, json.JSONDecodeError):
            entries = []
    entries.append({"pid": int(pid), "started_at": time.time()})
    try:
        PID_FILE.write_text(json.dumps(entries), encoding="utf-8")
    except OSError:
        pass


def _kill_tracked_tsserver() -> int:
    """
    Terminate previously spawned tsserver subprocesses. PIDs are sanity-checked
    against their current command line before killing, so reused OS PIDs that
    no longer belong to a tsserver are left alone.
    """
    if not PID_FILE.exists():
        return 0
    try:
        entries = json.loads(PID_FILE.read_text(encoding="utf-8"))
        if not isinstance(entries, list):
            entries = []
    except (OSError, json.JSONDecodeError):
        _unlink_quiet(PID_FILE)
        return 0

    try:
        import psutil
    except ImportError:
        killed = _kill_native_fallback(entries)
        _unlink_quiet(PID_FILE)
        return killed

    target_marker = str(TSSERVER_DIR).lower()
    terminated: list = []

    for entry in entries:
        pid = entry.get("pid")
        if not pid:
            continue
        try:
            proc = psutil.Process(int(pid))
            cmdline = " ".join(proc.cmdline()).lower()
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError):
            continue
        if "tsserver" not in cmdline or target_marker not in cmdline:
            continue
        try:
            proc.terminate()
            terminated.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    if terminated:
        _, still_alive = psutil.wait_procs(terminated, timeout=_TERMINATE_GRACE_SECONDS)
        for proc in still_alive:
            try:
                proc.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

    _unlink_quiet(PID_FILE)
    return len(terminated)


def _kill_native_fallback(entries: list) -> int:
    """Best-effort kill via OS-native tools when psutil is unavailable."""
    import signal as _signal
    killed = 0
    for entry in entries:
        pid = entry.get("pid")
        if not pid:
            continue
        pid = int(pid)
        try:
            if sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(pid)],
                    capture_output=True, check=False, timeout=10,
                )
                killed += 1
            else:
                os.kill(pid, _signal.SIGTERM)
                time.sleep(0.2)
                try:
                    os.kill(pid, _signal.SIGKILL)
                except ProcessLookupError:
                    pass
                killed += 1
        except (OSError, ProcessLookupError, subprocess.TimeoutExpired):
            continue
    return killed


def _force_rmtree(path: Path) -> None:
    """
    Cross-platform recursive delete with retries. Windows holds file handles
    briefly after a process exits and npm marks some files read-only, so
    a single rmtree call may fail; retry with chmod-on-error.
    """
    if not path.exists():
        return

    def _on_error(func, target, _exc_info):
        try:
            os.chmod(target, stat.S_IRWXU | stat.S_IRWXG | stat.S_IRWXO)
            func(target)
        except OSError:
            pass

    last_exc: OSError | None = None
    for attempt in range(_RMTREE_RETRY_ATTEMPTS):
        try:
            shutil.rmtree(path, onerror=_on_error)
            if not path.exists():
                return
        except OSError as exc:
            last_exc = exc
        time.sleep(0.3 * (attempt + 1))

    if path.exists():
        raise OSError(
            f"Could not remove {path} after multiple attempts. "
            f"Delete it manually and rerun. Last error: {last_exc}"
        )


def _unlink_quiet(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def repair_tsserver() -> None:
    """
    User-facing reset: kill any tracked tsserver, then delete the install
    directory so the next `icx graph build` performs a clean reinstall.
    """
    _kill_tracked_tsserver()
    _force_rmtree(TSSERVER_DIR)


def _log(msg: str) -> None:
    print(f"[icx graph] {msg}", file=sys.stderr)
