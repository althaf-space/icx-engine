"""Shared javalang parse cache for the build subprocess.

All Java resolvers (lombok, java_symbols, spring, jpa, jaxrs, java_clients)
parse the same files. Without a cache, each resolver does a full javalang
pass over every .java file - for a 3000-file project that means 18000+
pure-Python parses. This module caches the tree after the first parse so
each file is only parsed once per build subprocess.

A per-file timeout prevents any single pathological file from stalling
the entire build. Files that exceed the timeout are stored as None and
all resolvers silently skip them.
"""
from __future__ import annotations

import queue
import threading
from pathlib import Path

_cache: dict[str, object] = {}
_lock = threading.Lock()

_TIMEOUT_SECONDS = 6.0
_MAX_FILE_BYTES = 150_000


def _parse_worker(source: str, result_q: "queue.Queue[object | None]") -> None:
    try:
        import javalang
        result_q.put(javalang.parse.parse(source))
    except Exception:
        result_q.put(None)


def get_tree(path: Path, source: str | None = None) -> object | None:
    """Return cached javalang parse tree for path, or None on failure/timeout."""
    key = str(path)
    with _lock:
        if key in _cache:
            return _cache[key]

    if source is None:
        try:
            raw = path.read_bytes()
        except OSError:
            with _lock:
                _cache[key] = None
            return None
        if len(raw) > _MAX_FILE_BYTES:
            with _lock:
                _cache[key] = None
            return None
        try:
            source = raw.decode("utf-8")
        except UnicodeDecodeError:
            with _lock:
                _cache[key] = None
            return None

    result_q: queue.Queue[object | None] = queue.Queue()
    t = threading.Thread(target=_parse_worker, args=(source, result_q), daemon=True)
    t.start()
    t.join(_TIMEOUT_SECONDS)

    try:
        tree = result_q.get_nowait()
    except queue.Empty:
        tree = None

    with _lock:
        _cache[key] = tree
    return tree


def clear() -> None:
    """Release all cached trees. Call between builds if reusing the process."""
    with _lock:
        _cache.clear()
