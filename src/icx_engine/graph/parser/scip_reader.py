"""
SCIP (Source Code Intelligence Protocol) optional integration.

SCIP indexers produce compiler-grade cross-file references with zero false positives.
Detects installed SCIP indexers, runs them during graph build, reads .scip protobuf
output, converts to ICX graph edges.

EVERYTHING IS OPTIONAL. If no SCIP indexer is installed, returns empty edges.
Zero breaking changes to existing behavior.

Auto-installed indexers (requires Node+npm or Go on PATH):
  Python/TypeScript/JavaScript: Node + npm (auto-installed by scip_manager)
  Go: Go toolchain (auto-installed by scip_manager)

Manual-only indexers (PATH detection only):
  Java/Kotlin: https://sourcegraph.github.io/scip-java/
  Ruby:        gem install scip-ruby

Edge type: scip_reference  (confidence: 0.95)
"""
import logging
import os
import subprocess
import shutil
import sys
import time
from pathlib import Path
from collections import defaultdict

_log = logging.getLogger(__name__)

_INDEXERS: dict[str, tuple[str, list[str]]] = {
    "python":     ("scip-python",     ["index", "--project-name", "icx-graph", "."]),
    "typescript": ("scip-typescript", ["index", "--infer-tsconfig"]),
    "javascript": ("scip-typescript", ["index", "--infer-tsconfig"]),
    "java":       ("scip-java",       ["index"]),
    "kotlin":     ("scip-java",       ["index"]),
    "go":         ("scip-go",         ["."]),
    "ruby":       ("scip-ruby",       ["index", "."]),
}


# ---------------------------------------------------------------------------
# Minimal protobuf wire-format parser for SCIP
# No generated code or external dependency needed.
# Wire types used by SCIP: 0 = varint, 2 = length-delimited (string/message)
# ---------------------------------------------------------------------------

def _read_varint(data: bytes, pos: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while pos < len(data):
        b = data[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, pos
        shift += 7
    return result, pos


def _iter_fields(data: bytes):
    """Yield (field_number, wire_type, value) from a serialized protobuf message.

    value is int for wire_type 0, bytes for wire_type 2.
    Unknown wire types are skipped.
    """
    pos = 0
    length = len(data)
    while pos < length:
        tag, pos = _read_varint(data, pos)
        if pos > length:
            break
        field_number = tag >> 3
        wire_type = tag & 0x7
        if wire_type == 0:
            value, pos = _read_varint(data, pos)
            yield field_number, 0, value
        elif wire_type == 2:
            size, pos = _read_varint(data, pos)
            value = data[pos:pos + size]
            pos += size
            yield field_number, 2, value
        elif wire_type == 1:
            pos += 8
        elif wire_type == 5:
            pos += 4
        else:
            break


def _parse_scip_index(raw: bytes) -> list[tuple[str, list[tuple[str, int]]]]:
    """Parse SCIP Index binary. Returns list of (relative_path, [(symbol, roles), ...]).

    SCIP proto field numbers used:
      Index.documents     = 2  (repeated message)
      Document.relative_path = 1  (string)
      Document.occurrences   = 2  (repeated message)
      Occurrence.symbol       = 2  (string)
      Occurrence.symbol_roles = 3  (int32, 1=definition)
    """
    docs: list[tuple[str, list[tuple[str, int]]]] = []
    for fn, wt, val in _iter_fields(raw):
        if fn != 2 or wt != 2:
            continue
        # Document message
        rel_path = ""
        occs: list[tuple[str, int]] = []
        for dfn, dwt, dval in _iter_fields(val):
            if dfn == 1 and dwt == 2:
                rel_path = dval.decode("utf-8", errors="replace")
            elif dfn == 2 and dwt == 2:
                symbol = ""
                roles = 0
                for ofn, owt, oval in _iter_fields(dval):
                    if ofn == 2 and owt == 2:
                        symbol = oval.decode("utf-8", errors="replace")
                    elif ofn == 3 and owt == 0:
                        roles = oval
                if symbol:
                    occs.append((symbol, roles))
        if rel_path:
            docs.append((rel_path, occs))
    return docs


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def detect_scip_indexers(languages: list[str]) -> dict[str, str]:
    """Return {language: binary} for indexers found on PATH."""
    result = {}
    for lang in languages:
        cfg = _INDEXERS.get(lang.lower())
        if cfg and shutil.which(cfg[0]):
            result[lang] = cfg[0]
    return result


# Synchronous timeout cap for all non-background languages.
# After 3 minutes the sync run is killed and falls back to a background daemon
# so the build is not blocked. TypeScript/JavaScript always go background and
# never reach this path.
_SCIP_TIMEOUTS: dict[str, int] = {
    "java": 180,
    "kotlin": 180,
}
_SCIP_DEFAULT_TIMEOUT = 180

# TypeScript/JavaScript always launch as background daemons, never blocking
# the build. scip-typescript can take 5-30 min depending on project size
# (ts.createProgram loads all node_modules type definitions before indexing).
# Build completes immediately; SCIP result lands in cache when daemon finishes.
_BACKGROUND_ON_LAUNCH: frozenset[str] = frozenset({"typescript", "javascript"})

# Sentinel: returned when a background daemon was launched (not a Path or None).
BACKGROUND_SPAWNED: object = object()

# Lock older than this is assumed crashed; removed on next build attempt.
_LOCK_STALE_SECONDS: int = 7200  # 2 hours


def _lock_file(scip_file: Path) -> Path:
    return scip_file.parent / (scip_file.stem + ".building")


def _tmp_file(scip_file: Path) -> Path:
    return scip_file.parent / (scip_file.name + ".tmp")


def _spawn_background_scip(
    run_cmd: list[str],
    project_path: str,
    scip_file: Path,
    env: dict | None,
) -> None:
    """Detach scip indexer as a daemon. Returns immediately.

    Atomic write protocol so concurrent builds never read a partial file:
      1. Create typescript.building (lock) - prevents duplicate spawns
      2. Write to typescript.scip.tmp via --output
      3. On success: rename .tmp -> .scip atomically
      4. finally: remove lock and .tmp

    If a force build deletes .tmp mid-run, rename fails cleanly, lock is
    removed, and the next build spawns a fresh daemon.
    """
    lock = _lock_file(scip_file)
    tmp = _tmp_file(scip_file)
    cmd_with_output = run_cmd + ["--output", str(tmp)]

    _inner_run = (
        "    r = subprocess.run(cmd, cwd=cwd, stdout=subprocess.DEVNULL,"
        " stderr=subprocess.DEVNULL, creationflags=0x08000000)"
        if os.name == "nt" else
        "    r = subprocess.run(cmd, cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)"
    )
    script = "\n".join([
        "import subprocess, shutil",
        "from pathlib import Path",
        f"lock = Path({str(lock)!r})",
        f"tmp = Path({str(tmp)!r})",
        f"dst = Path({str(scip_file)!r})",
        f"cmd = {cmd_with_output!r}",
        f"cwd = {project_path!r}",
        "lock.touch()",
        "try:",
        _inner_run,
        "    if r.returncode == 0 and tmp.exists():",
        "        shutil.move(str(tmp), str(dst))",
        "finally:",
        "    lock.unlink(missing_ok=True)",
        "    try:",
        "        tmp.unlink(missing_ok=True)",
        "    except OSError:",
        "        pass",
    ])

    kwargs: dict = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if env:
        kwargs["env"] = env
    if os.name == "nt":
        kwargs["creationflags"] = (
            subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        )
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen([sys.executable, "-c", script], **kwargs)
        _log.debug("scip background daemon spawned -> %s", scip_file)
    except OSError as exc:
        _log.debug("scip background spawn failed: %s", exc)


def run_scip_indexer(
    language: str,
    project_path: str,
    cache_dir: Path,
    cmd: list[str] | None = None,
    extra_env: dict | None = None,
    use_cache: bool = False,
) -> "Path | object | None":
    """Run SCIP indexer for language.

    Returns:
        Path             - .scip file path (cache hit or sync success)
        BACKGROUND_SPAWNED - background daemon launched; no edges this build,
                             result cached when daemon finishes
        None             - unavailable, failed, or timed out

    TypeScript/JavaScript always go background (never block the build):
      - use_cache=True + .scip exists -> return Path instantly
      - otherwise -> spawn daemon via --output, return BACKGROUND_SPAWNED

    Java/Kotlin/others run synchronously with timeout.

    stdout/stderr are DEVNULL to prevent pipe-hold hangs on Windows when
    build tools fork child processes that inherit pipe handles.
    """
    if any(c in project_path for c in ("\n", "\r", "'")):
        _log.debug("scip %s: project_path contains unsafe characters, skipping", language)
        return None

    if cmd is None:
        cfg = _INDEXERS.get(language.lower())
        if not cfg or not shutil.which(cfg[0]):
            return None
        binary, args = cfg
        run_cmd = [binary] + args
    else:
        run_cmd = cmd

    scip_file = cache_dir / f"{language}.scip"
    env = {**os.environ, **extra_env} if extra_env else None

    # TypeScript/JavaScript: always background, zero build-time cost.
    if language.lower() in _BACKGROUND_ON_LAUNCH:
        lock = _lock_file(scip_file)

        # Stale lock: daemon likely crashed, clean up and allow respawn.
        if lock.exists():
            try:
                lock_age = time.time() - lock.stat().st_mtime
                if lock_age > _LOCK_STALE_SECONDS:
                    _log.debug("scip %s: stale lock (%dh old), removing", language, int(lock_age // 3600))
                    lock.unlink(missing_ok=True)
                else:
                    _log.debug("scip %s: daemon running (lock %ds old), skipping spawn", language, int(lock_age))
                    return BACKGROUND_SPAWNED
            except OSError:
                pass  # Lock removed between exists() and stat() - proceed normally

        # Cache hit: complete .scip exists, no active daemon writing it.
        if use_cache and scip_file.exists() and not lock.exists():
            _log.debug("scip %s: returning cached %s", language, scip_file.name)
            return scip_file

        _spawn_background_scip(run_cmd, project_path, scip_file, env)
        return BACKGROUND_SPAWNED

    # Synchronous path: Java/Kotlin/Go/Python/Ruby.
    if use_cache and scip_file.exists():
        _log.debug("scip %s: returning cached %s", language, scip_file.name)
        return scip_file

    _timeout = _SCIP_TIMEOUTS.get(language.lower(), _SCIP_DEFAULT_TIMEOUT)
    default = Path(project_path) / "index.scip"
    try:
        r = subprocess.run(
            run_cmd,
            cwd=project_path,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=_timeout,
            **({"env": env} if env is not None else {}),
        )
        if r.returncode != 0:
            _log.debug("scip %s exited %d", language, r.returncode)
            return None
        if default.exists():
            shutil.move(str(default), str(scip_file))
            return scip_file
        _log.debug("scip %s: index.scip not found after run", language)
        return None
    except subprocess.TimeoutExpired:
        # Sync run hit the 3-minute cap; hand off to background daemon so the
        # build is not blocked. SCIP edges land in cache when daemon finishes.
        _log.debug("scip %s: timed out after %ds, spawning background daemon", language, _timeout)
        _spawn_background_scip(run_cmd, project_path, scip_file, env)
        return BACKGROUND_SPAWNED
    except (FileNotFoundError, OSError) as exc:
        _log.debug("scip %s: subprocess error %s", language, exc)
        return None
    finally:
        try:
            default.unlink(missing_ok=True)
        except OSError:
            pass


def read_scip_edges(
    scip_file: Path,
    nodes: list[dict],
    project_path: str | None = None,
) -> list[dict]:
    """Parse .scip file -> ICX graph edges (scip_reference, confidence=0.95).

    project_path should be the absolute project root (forward-slash form).
    Node source_file paths are absolute; SCIP emits relative paths.
    We strip the project prefix so lookups match.
    """
    if not scip_file or not scip_file.exists():
        return []

    # Normalise project root to a forward-slash prefix we can strip
    _proj_prefix = ""
    if project_path:
        _proj_prefix = project_path.replace("\\", "/").rstrip("/") + "/"

    node_by_file: dict[str, list[dict]] = defaultdict(list)
    for n in nodes:
        sf = (n.get("source_file") or n.get("file") or "").replace("\\", "/")
        if not sf:
            continue
        # Strip project prefix so absolute paths become relative
        if _proj_prefix and sf.startswith(_proj_prefix):
            sf = sf[len(_proj_prefix):]
        if sf:
            node_by_file[sf].append(n)

    edges = []
    try:
        docs = _parse_scip_index(scip_file.read_bytes())

        symbol_to_file: dict[str, str] = {}
        for rel_path, occs in docs:
            norm = rel_path.replace("\\", "/")
            for symbol, roles in occs:
                if roles & 1:  # Definition role
                    symbol_to_file[symbol] = norm

        seen: set[tuple[str, str]] = set()
        for rel_path, occs in docs:
            src_file = rel_path.replace("\\", "/")
            src_nodes = node_by_file.get(src_file, [])
            if not src_nodes:
                continue
            for symbol, roles in occs:
                if roles & 1:
                    continue  # skip definitions
                tgt_file = symbol_to_file.get(symbol)
                if tgt_file and tgt_file != src_file:
                    key = (src_file, tgt_file)
                    if key not in seen:
                        seen.add(key)
                        tgt_nodes = node_by_file.get(tgt_file, [])
                        if tgt_nodes:
                            edges.append({
                                "source": src_nodes[0]["id"],
                                "target": tgt_nodes[0]["id"],
                                "source_file": src_file,
                                "target_file": tgt_file,
                                "relation": "scip_reference",
                                "type": "scip_reference",
                                "confidence": 0.95,
                                "resolver": "scip",
                                "fix_confidence_delta": 0.0,
                                "resolution_weight": 0.0,
                            })
    except Exception as exc:
        _log.debug("scip parse error in %s: %s", scip_file, exc)

    return edges
