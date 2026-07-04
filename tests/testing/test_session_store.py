from __future__ import annotations
import asyncio
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from icx_engine.testing.session_store import (
    spawn_bg_poll,
    get_bg_result,
    mark_bg_done,
    cancel_bg_poll,
    _BG_TASKS,
    _BG_RESULTS,
)


def setup_function():
    _BG_TASKS.clear()
    _BG_RESULTS.clear()


def test_spawn_bg_poll_registers_task():
    with patch("icx_engine.testing.session_store._poll_worker", new_callable=AsyncMock):
        spawn_bg_poll("ui-test-123")
    assert "ui-test-123" in _BG_TASKS


def test_get_bg_result_returns_none_before_done():
    result = get_bg_result("nonexistent-run-id")
    assert result is None


def test_mark_bg_done_removes_task():
    _BG_TASKS["run-x"] = MagicMock()
    _BG_RESULTS["run-x"] = {"run_state": "completed"}
    mark_bg_done("run-x")
    assert "run-x" not in _BG_TASKS


def test_cancel_bg_poll_cancels_task():
    mock_task = MagicMock()
    _BG_TASKS["run-y"] = mock_task
    cancel_bg_poll("run-y")
    mock_task.cancel.assert_called_once()
    assert "run-y" not in _BG_TASKS


async def test_poll_worker_stores_result_on_completion():
    from icx_engine.testing.session_store import _poll_worker

    mock_client = AsyncMock()
    mock_client.get_run_status = AsyncMock(side_effect=[
        {"state": "running", "counters": {"pass": 2, "fail": 0}},
        {"state": "completed", "counters": {"pass": 5, "fail": 0}},
    ])

    with patch("icx_engine.testing.session_store._make_client", return_value=mock_client):
        with patch("icx_engine.testing.session_store._POLL_INTERVAL", 0):
            await _poll_worker("ui-test-done")

    assert "ui-test-done" in _BG_RESULTS
    assert _BG_RESULTS["ui-test-done"]["run_state"] == "completed"


def test_bg_results_capped_at_max():
    """_set_bg_result evicts oldest entries past _BG_RESULTS_MAX (finding R3)."""
    from icx_engine.testing.session_store import _set_bg_result, _BG_RESULTS_MAX
    for i in range(_BG_RESULTS_MAX + 50):
        _set_bg_result(f"run-{i}", {"run_state": "completed"})
    assert len(_BG_RESULTS) == _BG_RESULTS_MAX
    # Newest retained, oldest evicted.
    assert get_bg_result(f"run-{_BG_RESULTS_MAX + 49}") is not None
    assert get_bg_result("run-0") is None


async def test_poll_worker_stores_error_on_run_lost():
    from icx_engine.testing.session_store import _poll_worker
    from icx_engine.testing.client import MagikRunLost

    mock_client = AsyncMock()
    mock_client.get_run_status = AsyncMock(side_effect=MagikRunLost("gone"))

    with patch("icx_engine.testing.session_store._make_client", return_value=mock_client):
        with patch("icx_engine.testing.session_store._POLL_INTERVAL", 0):
            await _poll_worker("ui-lost")

    assert "ui-lost" in _BG_RESULTS
    assert _BG_RESULTS["ui-lost"].get("error")


# ---------------------------------------------------------------------------
# SQL-bearing session management: list_active_sessions / cancel_session /
# purge_old_sessions against a real temp sqlite db (no mocking the database).
# ---------------------------------------------------------------------------

import json as _json
import sqlite3 as _sqlite3
from pathlib import Path as _Path


def _seed_checkpoints_db(db_path: _Path, rows: list[tuple]) -> None:
    """rows: list of (thread_id, checkpoint_json_str, metadata_str, ts_str)."""
    conn = _sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "CREATE TABLE checkpoints (thread_id TEXT, checkpoint TEXT, metadata TEXT, ts TEXT)"
        )
        conn.executemany(
            "INSERT INTO checkpoints (thread_id, checkpoint, metadata, ts) VALUES (?, ?, ?, ?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()


def _chk(status: str, iteration: int, files: list[str], run_id: str | None) -> str:
    return _json.dumps({
        "channel_values": {
            "status": status,
            "iteration": iteration,
            "file_paths": files,
            "run_id": run_id,
        }
    })


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
    # Two rows for thread A (newest first via ORDER BY checkpoint DESC) + one for B.
    _seed_checkpoints_db(db, [
        ("A", _chk("running", 2, ["x.jsx"], "run-A2"), "{}", "2026-01-02"),
        ("A", _chk("running", 1, ["x.jsx"], "run-A1"), "{}", "2026-01-01"),
        ("B", _chk("completed", 5, ["y.jsx"], None), "{}", "2026-01-03"),
    ])
    sessions = list_active_sessions(db)
    by_id = {s["session_id"]: s for s in sessions}
    assert set(by_id) == {"A", "B"}            # deduped to one entry per thread
    assert by_id["A"]["run_id"] == "run-A2"    # newest checkpoint wins (DESC)
    assert by_id["A"]["iteration"] == 2
    assert by_id["A"]["file_paths"] == ["x.jsx"]
    assert by_id["B"]["status"] == "completed"


def test_cancel_session_deletes_and_reports(tmp_path):
    from icx_engine.testing.session_store import cancel_session
    db = tmp_path / "c.db"
    _seed_checkpoints_db(db, [("A", _chk("running", 1, [], None), "{}", "2026-01-01")])
    assert cancel_session("A", db) is True       # row existed -> deleted
    assert cancel_session("A", db) is False      # already gone
    assert cancel_session("missing", tmp_path / "nope.db") is False  # no db


def test_purge_old_sessions_deletes_by_age(tmp_path):
    from icx_engine.testing.session_store import purge_old_sessions
    db = tmp_path / "p.db"
    _seed_checkpoints_db(db, [
        ("old", _chk("running", 1, [], None), "{}", "2000-01-01 00:00:00"),
        ("new", _chk("running", 1, [], None), "{}", "2999-01-01 00:00:00"),
    ])
    purged = purge_old_sessions(db, days=7)
    assert purged == 1                            # only the ancient row
    assert purge_old_sessions(tmp_path / "nope.db", days=7) == 0  # no db
