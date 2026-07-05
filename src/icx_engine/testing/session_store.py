from __future__ import annotations

import asyncio
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


def _iter_session_checkpoints(db_path: Path) -> list[tuple[str, dict]]:
    """Yield (thread_id, checkpoint_dict) newest-first using langgraph's own reader.

    The checkpoints table is written by AsyncSqliteSaver, which stores each
    checkpoint as a serialized BLOB (not JSON) and does not have a `ts` column.
    Reading it correctly therefore requires langgraph's serializer rather than
    raw SQL. Returns [] if the table has not been created yet or on any error,
    preserving the best-effort contract callers rely on.
    """
    from langgraph.checkpoint.sqlite import SqliteSaver

    conn = sqlite3.connect(str(db_path))
    try:
        saver = SqliteSaver(conn)
        try:
            tuples = list(saver.list(None))
        except Exception:
            # No checkpoints table yet, or an unreadable DB - treat as empty.
            return []
        out: list[tuple[str, dict]] = []
        for t in tuples:  # langgraph yields newest checkpoint_id first
            try:
                tid = t.config["configurable"]["thread_id"]
            except Exception:
                continue
            checkpoint = t.checkpoint if isinstance(t.checkpoint, dict) else {}
            out.append((tid, checkpoint))
        return out
    finally:
        conn.close()


def list_active_sessions(db_path: Path) -> list[dict]:
    if not db_path.exists():
        return []
    try:
        rows = _iter_session_checkpoints(db_path)
    except Exception as exc:
        _log.warning("Failed to list sessions: %s", exc)
        return []

    seen: set[str] = set()
    result: list[dict] = []
    for tid, checkpoint in rows:  # newest-first -> first seen per thread wins
        if tid in seen:
            continue
        seen.add(tid)
        cv = checkpoint.get("channel_values", {}) if isinstance(checkpoint, dict) else {}
        result.append({
            "session_id": tid,
            "status": cv.get("status", "unknown"),
            "iteration": cv.get("iteration", 0),
            "file_paths": cv.get("file_paths", []),
            "run_id": cv.get("run_id"),
        })
    return result


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
    """Delete sessions whose most recent checkpoint is older than `days`.

    The checkpoints table has no timestamp column; the wall-clock time lives
    inside each checkpoint's `ts` field. We read the newest checkpoint per
    thread, and delete threads whose newest `ts` is older than the cutoff.
    A thread whose timestamp cannot be determined is never deleted (safe:
    uncertainty never evicts a possibly-active session). Returns the number of
    threads deleted.
    """
    if not db_path.exists():
        return 0
    try:
        rows = _iter_session_checkpoints(db_path)
    except Exception:
        return 0
    if not rows:
        return 0

    from datetime import datetime, timedelta, timezone

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    newest_ts: dict[str, datetime | None] = {}
    for tid, checkpoint in rows:  # newest-first -> first seen per thread is newest
        if tid in newest_ts:
            continue
        ts_raw = checkpoint.get("ts") if isinstance(checkpoint, dict) else None
        parsed: datetime | None = None
        if ts_raw:
            try:
                parsed = datetime.fromisoformat(ts_raw)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
            except Exception:
                parsed = None
        newest_ts[tid] = parsed

    to_delete = [tid for tid, ts in newest_ts.items() if ts is not None and ts < cutoff]
    if not to_delete:
        return 0

    from langgraph.checkpoint.sqlite import SqliteSaver

    conn = sqlite3.connect(str(db_path))
    purged = 0
    try:
        saver = SqliteSaver(conn)
        for tid in to_delete:
            try:
                saver.delete_thread(tid)
                purged += 1
            except Exception as exc:
                _log.warning("Failed to purge session %s: %s", tid, exc)
    finally:
        conn.close()
    return purged
