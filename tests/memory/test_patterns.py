from __future__ import annotations
import uuid
from unittest.mock import MagicMock, patch

from icx_engine.memory.schema import MemoryEntry


def _make_entry(**kwargs) -> MemoryEntry:
    defaults = dict(
        id=str(uuid.uuid4()),
        issue_key="PROJ-1",
        project_key="PROJ",
        source_type="jira",
        issue_type="Bug",
        summary="Auth fails",
        problem_description="JWT expired",
        impact="",
        resolution_note="Updated TTL",
        files_changed=["src/auth/token.py"],
        resolution_confirmed=True,
        saved_at="2026-05-12T10:00:00Z",
        tags=[],
        work_item_type="bug",
        pattern_used="",
    )
    defaults.update(kwargs)
    return MemoryEntry(**defaults)


def _mock_embeddings():
    mgr = MagicMock()
    mgr.embed.return_value = [0.1] * 768
    mgr.ensure_ready.return_value = None
    mgr.check_ready.return_value = None
    return mgr


# -- detect_patterns unit tests ------------------------------------------------

def test_detect_returns_empty_below_min_entries():
    from icx_engine.memory.patterns import detect_patterns

    entries = [_make_entry(issue_key=f"PROJ-{i}", id=str(uuid.uuid4())) for i in range(2)]
    assert detect_patterns(entries) == []


def test_detect_frequent_file_above_threshold():
    from icx_engine.memory.patterns import detect_patterns

    hot = "src/auth/token.py"
    entries = [
        _make_entry(issue_key="PROJ-1", files_changed=[hot]),
        _make_entry(issue_key="PROJ-2", id=str(uuid.uuid4()), files_changed=[hot]),
        _make_entry(issue_key="PROJ-3", id=str(uuid.uuid4()), files_changed=[hot]),
        _make_entry(issue_key="PROJ-4", id=str(uuid.uuid4()), files_changed=["src/other.py"]),
    ]
    patterns = detect_patterns(entries)
    ff = [p for p in patterns if p["pattern_type"] == "frequent_file"]
    assert len(ff) == 1
    assert hot in ff[0]["evidence"]["file"]
    assert ff[0]["evidence"]["count"] == 3


def test_detect_no_frequent_file_below_threshold():
    from icx_engine.memory.patterns import detect_patterns

    entries = [
        _make_entry(issue_key="PROJ-1", files_changed=["src/auth/token.py"]),
        _make_entry(issue_key="PROJ-2", id=str(uuid.uuid4()), files_changed=["src/other.py"]),
        _make_entry(issue_key="PROJ-3", id=str(uuid.uuid4()), files_changed=["src/another.py"]),
        _make_entry(issue_key="PROJ-4", id=str(uuid.uuid4()), files_changed=["src/yet_another.py"]),
    ]
    patterns = detect_patterns(entries)
    assert not any(p["pattern_type"] == "frequent_file" for p in patterns)


def test_detect_dominant_tag():
    from icx_engine.memory.patterns import detect_patterns

    entries = [
        _make_entry(issue_key="PROJ-1", tags=["auth", "jwt"]),
        _make_entry(issue_key="PROJ-2", id=str(uuid.uuid4()), tags=["auth"]),
        _make_entry(issue_key="PROJ-3", id=str(uuid.uuid4()), tags=["auth"]),
        _make_entry(issue_key="PROJ-4", id=str(uuid.uuid4()), tags=["payments"]),
        _make_entry(issue_key="PROJ-5", id=str(uuid.uuid4()), tags=[]),
    ]
    patterns = detect_patterns(entries)
    dt = [p for p in patterns if p["pattern_type"] == "dominant_tag"]
    assert any(p["evidence"]["tag"] == "auth" for p in dt)


def test_detect_top_work_item_type():
    from icx_engine.memory.patterns import detect_patterns

    entries = [
        _make_entry(issue_key=f"PROJ-{i}", id=str(uuid.uuid4()), work_item_type="bug")
        for i in range(4)
    ] + [_make_entry(issue_key="PROJ-99", id=str(uuid.uuid4()), work_item_type="story")]
    patterns = detect_patterns(entries)
    tt = [p for p in patterns if p["pattern_type"] == "top_work_item_type"]
    assert len(tt) == 1
    assert tt[0]["evidence"]["type"] == "bug"
    assert tt[0]["evidence"]["count"] == 4


def test_detect_top_type_surfaced_above_threshold():
    from icx_engine.memory.patterns import detect_patterns

    # 3 bug, 2 story - bug is 60% - above 50% threshold
    entries = [
        _make_entry(issue_key="PROJ-1", work_item_type="bug"),
        _make_entry(issue_key="PROJ-2", id=str(uuid.uuid4()), work_item_type="bug"),
        _make_entry(issue_key="PROJ-3", id=str(uuid.uuid4()), work_item_type="bug"),
        _make_entry(issue_key="PROJ-4", id=str(uuid.uuid4()), work_item_type="story"),
        _make_entry(issue_key="PROJ-5", id=str(uuid.uuid4()), work_item_type="story"),
    ]
    patterns = detect_patterns(entries)
    tt = [p for p in patterns if p["pattern_type"] == "top_work_item_type"]
    # 60% > 50% threshold, so should appear
    assert len(tt) == 1
    assert tt[0]["evidence"]["type"] == "bug"


def test_detect_equal_split_no_dominant_type():
    from icx_engine.memory.patterns import detect_patterns

    entries = [
        _make_entry(issue_key="PROJ-1", work_item_type="bug"),
        _make_entry(issue_key="PROJ-2", id=str(uuid.uuid4()), work_item_type="bug"),
        _make_entry(issue_key="PROJ-3", id=str(uuid.uuid4()), work_item_type="story"),
        _make_entry(issue_key="PROJ-4", id=str(uuid.uuid4()), work_item_type="story"),
    ]
    patterns = detect_patterns(entries)
    assert not any(p["pattern_type"] == "top_work_item_type" for p in patterns)


# -- PatternManager integration tests -----------------------------------------

def test_pattern_manager_refresh_and_get(tmp_path):
    from icx_engine.memory.patterns import PatternManager

    hot = "src/auth/token.py"
    entries = [
        _make_entry(issue_key=f"PROJ-{i}", id=str(uuid.uuid4()), files_changed=[hot])
        for i in range(4)
    ] + [_make_entry(issue_key="PROJ-99", id=str(uuid.uuid4()), files_changed=["src/other.py"])]

    pm = PatternManager(db_path=tmp_path)
    pm.refresh(entries, "PROJ")
    patterns = pm.get_patterns("PROJ")

    assert len(patterns) > 0
    assert any(p["pattern_type"] == "frequent_file" for p in patterns)
    assert all(p["project_key"] == "PROJ" for p in patterns)


def test_get_patterns_filtered_uses_where_not_full_table_scan(tmp_path):
    # Perf fix: get_patterns(project_key=...) must push the filter down via
    # .search().where(...) instead of pulling the whole table into Python.
    from icx_engine.memory.patterns import PatternManager

    hot = "src/auth/token.py"
    entries = [
        _make_entry(issue_key=f"PROJ-{i}", id=str(uuid.uuid4()), files_changed=[hot])
        for i in range(4)
    ]
    pm = PatternManager(db_path=tmp_path)
    pm.refresh(entries, "PROJ")

    real_table = pm._get_table()
    with patch.object(type(real_table), "to_arrow", side_effect=AssertionError(
        "get_patterns(project_key=...) must not call to_arrow() - it should filter via .where() instead"
    )), patch.object(pm, "_get_table", return_value=real_table):
        patterns = pm.get_patterns("PROJ")

    assert len(patterns) > 0
    assert all(p["project_key"] == "PROJ" for p in patterns)


def test_pattern_manager_refresh_logs_delete_failure(tmp_path, caplog):
    # Regression: a failed pre-refresh delete must not vanish silently - it's still
    # best-effort (never blocks inserting fresh patterns), but now traceable via debug log.
    import logging
    from icx_engine.memory.patterns import PatternManager

    hot = "src/auth/token.py"
    entries = [
        _make_entry(issue_key=f"PROJ-{i}", id=str(uuid.uuid4()), files_changed=[hot])
        for i in range(4)
    ]
    pm = PatternManager(db_path=tmp_path)
    real_table = pm._get_table()
    with patch.object(type(real_table), "delete", side_effect=RuntimeError("boom")), \
         patch.object(pm, "_get_table", return_value=real_table), \
         caplog.at_level(logging.DEBUG, logger="icx_engine.memory.patterns"):
        pm.refresh(entries, "PROJ")

    assert any("pattern delete failed" in r.message for r in caplog.records)
    # Fresh patterns still get inserted despite the failed pre-refresh delete.
    assert len(pm.get_patterns("PROJ")) > 0


def test_pattern_manager_refresh_replaces_old(tmp_path):
    from icx_engine.memory.patterns import PatternManager

    hot = "src/auth/token.py"
    entries_v1 = [
        _make_entry(issue_key=f"PROJ-{i}", id=str(uuid.uuid4()), files_changed=[hot])
        for i in range(4)
    ] + [_make_entry(issue_key="PROJ-99", id=str(uuid.uuid4()), files_changed=["src/other.py"])]

    pm = PatternManager(db_path=tmp_path)
    pm.refresh(entries_v1, "PROJ")
    first_count = len(pm.get_patterns("PROJ"))

    # Refresh with new entries - different pattern profile
    entries_v2 = [
        _make_entry(issue_key=f"PROJ-{i}", id=str(uuid.uuid4()), files_changed=["src/payments.py"])
        for i in range(4)
    ] + [_make_entry(issue_key="PROJ-99", id=str(uuid.uuid4()), files_changed=["src/other.py"])]

    pm.refresh(entries_v2, "PROJ")
    patterns = pm.get_patterns("PROJ")

    # Old pattern for token.py should be gone
    assert not any(
        p.get("evidence", {}).get("file", "").endswith("token.py")
        for p in patterns
    )


def test_pattern_manager_get_returns_empty_before_refresh(tmp_path):
    from icx_engine.memory.patterns import PatternManager

    pm = PatternManager(db_path=tmp_path)
    assert pm.get_patterns("PROJ") == []


def test_pattern_manager_reset_clears_cached_state(tmp_path):
    from icx_engine.memory.patterns import PatternManager

    hot = "src/auth/token.py"
    entries = [
        _make_entry(issue_key=f"PROJ-{i}", id=str(uuid.uuid4()), files_changed=[hot])
        for i in range(4)
    ] + [_make_entry(issue_key="PROJ-99", id=str(uuid.uuid4()), files_changed=["src/other.py"])]

    pm = PatternManager(db_path=tmp_path)
    pm.refresh(entries, "PROJ")
    assert pm._table is not None

    pm.reset()
    assert pm._table is None
    assert pm._db is None

    # Reconnects on next access
    patterns = pm.get_patterns("PROJ")
    assert len(patterns) > 0


# -- Integration: pattern trigger via MemoryManager ----------------------------

def test_pattern_trigger_on_tenth_save(tmp_path):
    from icx_engine.memory.manager import MemoryManager
    from icx_engine.memory.patterns import PatternManager

    hot = "src/auth/token.py"
    with patch("icx_engine.memory.manager.EmbeddingsManager", return_value=_mock_embeddings()):
        mgr = MemoryManager(db_path=tmp_path)
        for i in range(10):
            mgr.save(_make_entry(
                issue_key=f"PROJ-{i}",
                id=str(uuid.uuid4()),
                files_changed=[hot],
                project_key="PROJ",
            ))

    pm = PatternManager(db_path=tmp_path)
    patterns = pm.get_patterns("PROJ")
    assert len(patterns) > 0


def test_get_table_raises_on_stale_lock(tmp_path, monkeypatch):
    import time
    import lancedb
    import pytest
    from icx_engine.exceptions import MemoryError
    from icx_engine.memory.patterns import PatternManager

    monkeypatch.setattr(lancedb, "connect", lambda *a, **k: time.sleep(10))

    pm = PatternManager(db_path=tmp_path)
    with pytest.raises(MemoryError, match="timed out"):
        pm._get_table()
