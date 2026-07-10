from __future__ import annotations
import asyncio
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from icx_engine.testing.session_store import list_active_sessions
















# ---------------------------------------------------------------------------
# SQL-bearing session management: list_active_sessions / cancel_session /
# purge_old_sessions against a REAL langgraph AsyncSqliteSaver-created db.
#
# The DB these helpers read in production is written by AsyncSqliteSaver, whose
# `checkpoints` table stores each checkpoint as a serialized BLOB (not JSON) and
# has no `ts` column. Seeding with the real saver guarantees the tests validate
# production behavior, not a fabricated schema.
# ---------------------------------------------------------------------------

import sqlite3 as _sqlite3
from pathlib import Path as _Path


def _seed_real_db(db_path: _Path, puts: list[tuple[str, dict]]) -> None:
    """puts: ordered list of (thread_id, channel_values). Each entry is one
    checkpoint written via the real AsyncSqliteSaver; later puts for the same
    thread are newer (higher checkpoint_id)."""
    import aiosqlite
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    from langgraph.checkpoint.base import empty_checkpoint

    async def _seed() -> None:
        conn = await aiosqlite.connect(str(db_path))
        try:
            saver = AsyncSqliteSaver(conn)
            await saver.setup()
            for tid, channel_values in puts:
                cfg = {"configurable": {"thread_id": tid, "checkpoint_ns": ""}}
                chk = empty_checkpoint()
                chk["channel_values"] = dict(channel_values)
                await saver.aput(cfg, chk, {"source": "loop"}, {})
        finally:
            await conn.close()

    asyncio.run(_seed())


def _cv(status: str, iteration: int, files: list[str], run_id: str | None) -> dict:
    return {"status": status, "iteration": iteration, "file_paths": files, "run_id": run_id}


def test_list_active_sessions_missing_db_returns_empty(tmp_path):
    from icx_engine.testing.session_store import list_active_sessions
    assert list_active_sessions(tmp_path / "nope.db") == []


def test_list_active_sessions_missing_table_returns_empty(tmp_path):
    from icx_engine.testing.session_store import list_active_sessions
    db = tmp_path / "empty.db"
    _sqlite3.connect(str(db)).close()  # db exists, no checkpoints table
    assert list_active_sessions(db) == []


def test_list_active_sessions_dedups_by_thread_id_and_parses_channel_values(tmp_path):
    from icx_engine.testing.session_store import list_active_sessions
    db = tmp_path / "s.db"
    # Thread A written twice (iteration 1 then 2 -> 2 is newest); thread B once.
    _seed_real_db(db, [
        ("A", _cv("running", 1, ["x.jsx"], "run-A1")),
        ("A", _cv("running", 2, ["x.jsx"], "run-A2")),
        ("B", _cv("completed", 5, ["y.jsx"], None)),
    ])
    sessions = list_active_sessions(db)
    by_id = {s["session_id"]: s for s in sessions}
    assert set(by_id) == {"A", "B"}            # deduped to one entry per thread
    assert by_id["A"]["run_id"] == "run-A2"    # newest checkpoint wins
    assert by_id["A"]["iteration"] == 2
    assert by_id["A"]["file_paths"] == ["x.jsx"]
    assert by_id["B"]["status"] == "completed"


def test_cancel_session_deletes_and_reports(tmp_path):
    from icx_engine.testing.session_store import cancel_session
    db = tmp_path / "c.db"
    _seed_real_db(db, [("A", _cv("running", 1, [], None))])
    assert cancel_session("A", db) is True       # row existed -> deleted
    assert cancel_session("A", db) is False      # already gone
    assert cancel_session("missing", tmp_path / "nope.db") is False  # no db


def test_purge_keeps_fresh_and_deletes_old(tmp_path):
    from icx_engine.testing.session_store import purge_old_sessions, list_active_sessions
    db = tmp_path / "p.db"
    _seed_real_db(db, [
        ("s1", _cv("running", 1, [], None)),
        ("s2", _cv("completed", 3, [], None)),
    ])
    # Freshly-written sessions must NOT be purged by the 7-day cutoff.
    assert purge_old_sessions(db, days=7) == 0
    assert {s["session_id"] for s in list_active_sessions(db)} == {"s1", "s2"}
    # A cutoff in the future (days=-1 -> cutoff = tomorrow) makes every session
    # "old", exercising the real delete path.
    assert purge_old_sessions(db, days=-1) == 2
    assert list_active_sessions(db) == []


def test_purge_missing_db_returns_zero(tmp_path):
    from icx_engine.testing.session_store import purge_old_sessions
    assert purge_old_sessions(tmp_path / "nope.db", days=7) == 0
