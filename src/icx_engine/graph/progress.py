"""Cross-process progress channel for graph builds.

Subprocess writes newline-delimited JSON events to a file path. Parent
process tails the file and forwards events to a renderer (Rich Progress
in the CLI, no-op in MCP/background contexts).

Event schema:
    {"stage": str, "current": int, "total": int, "message": str, "ts": float}

Stages are strings rather than enum members so the producer and the
consumer can evolve independently without a shared import.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Callable


STAGES: tuple[str, ...] = (
    "scan",
    "ast",
    "lsp",
    "scip",
    "framework",
    "llm",
    "louvain",
    "export",
)

STAGE_LABELS: dict[str, str] = {
    "scan": "Scan files",
    "ast": "AST extract",
    "lsp": "LSP resolve",
    "scip": "SCIP resolve",
    "framework": "Framework resolve",
    "llm": "LLM enrich",
    "louvain": "Community detect",
    "export": "Export graph",
}


class ProgressEmitter:
    """Producer-side handle. Used inside the build subprocess."""

    def __init__(self, path: str | None) -> None:
        self._path = path
        self._fh = None
        if path:
            try:
                self._fh = open(path, "a", encoding="utf-8", buffering=1)
            except OSError:
                self._fh = None

    def emit(
        self,
        stage: str,
        *,
        current: int = 0,
        total: int = 0,
        message: str = "",
    ) -> None:
        if self._fh is None:
            return
        event = {
            "stage": stage,
            "current": current,
            "total": total,
            "message": message,
            "ts": time.time(),
        }
        try:
            self._fh.write(json.dumps(event) + "\n")
        except OSError:
            pass

    def close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None


def new_progress_path() -> str:
    """Allocate a fresh progress file path inside the system temp dir."""
    fd, path = tempfile.mkstemp(prefix="icx-build-progress-", suffix=".jsonl")
    os.close(fd)
    return path


def tail_events(
    path: str,
    on_event: Callable[[dict], None],
    stop_predicate: Callable[[], bool],
    *,
    poll_interval: float = 0.1,
) -> None:
    """Block, tailing `path` and invoking `on_event` for each line.

    Returns when `stop_predicate()` returns True and no buffered lines remain.
    Safe to call from a background thread.
    """
    pos = 0
    while True:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                fh.seek(pos)
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        on_event(json.loads(line))
                    except json.JSONDecodeError:
                        continue
                pos = fh.tell()
        except OSError:
            pass
        if stop_predicate():
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    fh.seek(pos)
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            on_event(json.loads(line))
                        except json.JSONDecodeError:
                            continue
            except OSError:
                pass
            return
        time.sleep(poll_interval)


def safe_unlink(path: str | None) -> None:
    if not path:
        return
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass
