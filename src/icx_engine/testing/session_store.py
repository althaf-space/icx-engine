from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

from icx_engine.testing.client import MagikClient, MagikUnreachable, MagikRunLost

_log = logging.getLogger(__name__)

_POLL_INTERVAL = 30
_MAX_RETRIES = 3
_RETRY_BACKOFF = [5, 15, 45]
_TERMINAL_STATES = {"completed", "failed", "cancelled"}

# Run-id keyed registries for background tasks
_BG_TASKS: dict[str, asyncio.Task | None] = {}
_BG_RESULTS: dict[str, dict[str, Any]] = {}
_BG_RESULTS_MAX = 100


def _set_bg_result(run_id: str, value: dict[str, Any]) -> None:
    """Store a poll result, evicting oldest entries past _BG_RESULTS_MAX so a
    long-lived MCP server does not accumulate results unbounded."""
    _BG_RESULTS[run_id] = value
    while len(_BG_RESULTS) > _BG_RESULTS_MAX:
        _BG_RESULTS.pop(next(iter(_BG_RESULTS)), None)


def _make_client() -> MagikClient:
    from icx_engine.config_manager import ConfigManager
    cfg = ConfigManager.load()
    return MagikClient(base_url=cfg.magik_base_url, api_key=cfg.magik_api_key)


async def _poll_worker(run_id: str) -> None:
    client = _make_client()
    retries = 0
    try:
        while True:
            await asyncio.sleep(_POLL_INTERVAL)
            try:
                snap = await client.get_run_status(run_id)
                retries = 0
            except MagikRunLost as exc:
                _set_bg_result(run_id, {"error": str(exc), "run_state": "lost"})
                return
            except MagikUnreachable as exc:
                retries += 1
                if retries > _MAX_RETRIES:
                    _set_bg_result(run_id, {"error": str(exc), "run_state": "unreachable"})
                    return
                await asyncio.sleep(_RETRY_BACKOFF[min(retries - 1, len(_RETRY_BACKOFF) - 1)])
                continue
            except Exception as exc:
                retries += 1
                if retries > _MAX_RETRIES:
                    _set_bg_result(run_id, {"error": str(exc), "run_state": "error"})
                    return
                continue

            if snap["state"] in _TERMINAL_STATES:
                _set_bg_result(run_id, {
                    "run_state": snap["state"],
                    "run_counters": snap.get("counters", {}),
                })
                return
    finally:
        await client.aclose()


def spawn_bg_poll(run_id: str) -> None:
    """Schedule a background poll task for run_id.

    If called from within a running event loop the task is created immediately.
    If called from a sync context (no running loop) the run_id is still
    registered in _BG_TASKS so callers can detect the spawn, but no real Task
    is created - node_poll's own loop will handle polling directly.
    """
    if run_id in _BG_TASKS:
        return
    coro = _poll_worker(run_id)
    try:
        loop = asyncio.get_running_loop()
        task: asyncio.Task | None = loop.create_task(coro)
    except RuntimeError:
        # No running event loop in this thread - close the coroutine to avoid
        # ResourceWarning and store None as a placeholder.
        coro.close()
        task = None
    _BG_TASKS[run_id] = task


def get_bg_result(run_id: str) -> dict | None:
    return _BG_RESULTS.get(run_id)


def mark_bg_done(run_id: str) -> None:
    _BG_TASKS.pop(run_id, None)
    _BG_RESULTS.pop(run_id, None)


def cancel_bg_poll(run_id: str) -> None:
    task = _BG_TASKS.pop(run_id, None)
    if task is not None:
        task.cancel()


def list_active_sessions(db_path: Path) -> list[dict]:
    if not db_path.exists():
        return []
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        table_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='checkpoints'"
        ).fetchone()
        if not table_exists:
            return []
        rows = conn.execute(
            "SELECT thread_id, checkpoint, metadata FROM checkpoints ORDER BY checkpoint DESC"
        ).fetchall()
        seen: set[str] = set()
        result: list[dict] = []
        for row in rows:
            tid = row["thread_id"]
            if tid in seen:
                continue
            seen.add(tid)
            try:
                chk = json.loads(row["checkpoint"] or "{}")
                channel_values = chk.get("channel_values", {})
                result.append({
                    "session_id": tid,
                    "status": channel_values.get("status", "unknown"),
                    "iteration": channel_values.get("iteration", 0),
                    "file_paths": channel_values.get("file_paths", []),
                    "run_id": channel_values.get("run_id"),
                })
            except Exception:
                result.append({"session_id": tid, "status": "unknown"})
        return result
    except Exception as exc:
        _log.warning("Failed to list sessions: %s", exc)
        return []
    finally:
        conn.close()


def cancel_session(session_id: str, db_path: Path) -> bool:
    if not db_path.exists():
        return False
    conn = sqlite3.connect(str(db_path))
    try:
        cursor = conn.execute("DELETE FROM checkpoints WHERE thread_id = ?", (session_id,))
        conn.commit()
        return cursor.rowcount > 0
    except Exception as exc:
        _log.warning("Failed to cancel session %s: %s", session_id, exc)
        return False
    finally:
        conn.close()


def purge_old_sessions(db_path: Path, days: int = 7) -> int:
    if not db_path.exists():
        return 0
    conn = sqlite3.connect(str(db_path))
    try:
        cursor = conn.execute(
            "DELETE FROM checkpoints WHERE ts < datetime('now', ?)",
            (f"-{days} days",),
        )
        conn.commit()
        return cursor.rowcount
    except Exception:
        return 0
    finally:
        conn.close()
