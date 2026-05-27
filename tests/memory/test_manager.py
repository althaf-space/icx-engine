from __future__ import annotations
from pathlib import Path
from unittest.mock import MagicMock, patch
import uuid
import pytest

from icx_engine.memory.schema import MemoryEntry, MemoryQueryInput


def _mock_embeddings():
    mgr = MagicMock()
    mgr.embed.return_value = [0.1] * 384
    mgr.ensure_ready.return_value = None
    return mgr


def _make_entry(**kwargs) -> MemoryEntry:
    defaults = dict(
        id=str(uuid.uuid4()),
        issue_key="PROJ-100",
        project_key="PROJ",
        source_type="jira",
        issue_type="Bug",
        summary="Auth fails",
        problem_description="JWT expired",
        impact="All users",
        resolution_note="Updated TTL",
        files_changed=["src/auth/token.py"],
        resolution_confirmed=True,
        saved_at="2026-05-12T10:00:00Z",
        tags=["jwt"],
    )
    defaults.update(kwargs)
    return MemoryEntry(**defaults)


def test_save_creates_entry(tmp_path):
    import lancedb
    from icx_engine.memory.manager import MemoryManager

    with patch("icx_engine.memory.manager.EmbeddingsManager", return_value=_mock_embeddings()):
        mgr = MemoryManager(db_path=tmp_path)
        entry = _make_entry()
        mgr.save(entry)

    db = lancedb.connect(str(tmp_path))
    table = db.open_table("memory_entries")
    rows = table.search([0.1] * 384).limit(10).to_list()
    assert any(r["issue_key"] == "PROJ-100" for r in rows)


def test_save_upserts_on_duplicate_issue_key(tmp_path):
    from icx_engine.memory.manager import MemoryManager

    with patch("icx_engine.memory.manager.EmbeddingsManager", return_value=_mock_embeddings()):
        mgr = MemoryManager(db_path=tmp_path)
        entry_v1 = _make_entry(issue_key="PROJ-100", resolution_note="First fix")
        entry_v2 = _make_entry(issue_key="PROJ-100", resolution_note="Better fix")
        mgr.save(entry_v1)
        mgr.save(entry_v2)
        entries = mgr.list_entries()

    keys = [e.issue_key for e in entries]
    assert keys.count("PROJ-100") == 1
    notes = [e.resolution_note for e in entries]
    assert "Better fix" in notes
    assert "First fix" not in notes


def test_query_returns_hits_above_threshold(tmp_path):
    from icx_engine.memory.manager import MemoryManager

    with patch("icx_engine.memory.manager.EmbeddingsManager", return_value=_mock_embeddings()):
        mgr = MemoryManager(db_path=tmp_path)
        entry = _make_entry(issue_key="PROJ-100", summary="JWT auth token expires too quickly")
        mgr.save(entry)

        q = MemoryQueryInput(
            issue_key="PROJ-200",
            project_key="PROJ",
            source_type="jira",
            summary="OAuth token invalid after 1 hour",
            description="Users get 401 after one hour session",
            issue_type="Bug",
        )
        insights = mgr.query(q, top_k=3, min_score=0.0)

    assert len(insights) >= 1
    assert insights[0].issue_key == "PROJ-100"
    assert 0.0 <= insights[0].similarity_score <= 1.0


def test_query_empty_db_returns_empty_list(tmp_path):
    from icx_engine.memory.manager import MemoryManager

    with patch("icx_engine.memory.manager.EmbeddingsManager", return_value=_mock_embeddings()):
        mgr = MemoryManager(db_path=tmp_path)
        q = MemoryQueryInput(
            issue_key="PROJ-1",
            project_key="PROJ",
            source_type="jira",
            summary="something",
            description="desc",
            issue_type="Bug",
        )
        insights = mgr.query(q)

    assert insights == []


def test_query_respects_top_k(tmp_path):
    from icx_engine.memory.manager import MemoryManager

    with patch("icx_engine.memory.manager.EmbeddingsManager", return_value=_mock_embeddings()):
        mgr = MemoryManager(db_path=tmp_path)
        for i in range(5):
            mgr.save(_make_entry(issue_key=f"PROJ-{i}", id=str(uuid.uuid4())))

        q = MemoryQueryInput(
            issue_key="PROJ-99",
            project_key="PROJ",
            source_type="jira",
            summary="auth fails",
            description="desc",
            issue_type="Bug",
        )
        insights = mgr.query(q, top_k=2, min_score=0.0)

    assert len(insights) <= 2


def test_show_returns_entry(tmp_path):
    from icx_engine.memory.manager import MemoryManager

    with patch("icx_engine.memory.manager.EmbeddingsManager", return_value=_mock_embeddings()):
        mgr = MemoryManager(db_path=tmp_path)
        mgr.save(_make_entry(issue_key="PROJ-100"))
        entry = mgr.show("PROJ-100")

    assert entry is not None
    assert entry.issue_key == "PROJ-100"


def test_show_returns_none_for_missing(tmp_path):
    from icx_engine.memory.manager import MemoryManager

    with patch("icx_engine.memory.manager.EmbeddingsManager", return_value=_mock_embeddings()):
        mgr = MemoryManager(db_path=tmp_path)
        entry = mgr.show("PROJ-999")

    assert entry is None


def test_delete_removes_entry(tmp_path):
    from icx_engine.memory.manager import MemoryManager

    with patch("icx_engine.memory.manager.EmbeddingsManager", return_value=_mock_embeddings()):
        mgr = MemoryManager(db_path=tmp_path)
        mgr.save(_make_entry(issue_key="PROJ-100", id="id-1"))
        mgr.save(_make_entry(issue_key="PROJ-200", id="id-2"))
        mgr.delete("PROJ-100")
        entries = mgr.list_entries()

    assert not any(e.issue_key == "PROJ-100" for e in entries)
    assert any(e.issue_key == "PROJ-200" for e in entries)


def test_delete_nonexistent_is_noop(tmp_path):
    from icx_engine.memory.manager import MemoryManager

    with patch("icx_engine.memory.manager.EmbeddingsManager", return_value=_mock_embeddings()):
        mgr = MemoryManager(db_path=tmp_path)
        mgr.delete("PROJ-999")  # must not raise


def test_clear_removes_all(tmp_path):
    from icx_engine.memory.manager import MemoryManager

    with patch("icx_engine.memory.manager.EmbeddingsManager", return_value=_mock_embeddings()):
        mgr = MemoryManager(db_path=tmp_path)
        mgr.save(_make_entry(issue_key="PROJ-100", id="id-1"))
        mgr.save(_make_entry(issue_key="PROJ-200", id="id-2"))
        mgr.clear()
        entries = mgr.list_entries()

    assert entries == []


def test_status_returns_dict(tmp_path):
    from icx_engine.memory.manager import MemoryManager

    with patch("icx_engine.memory.manager.EmbeddingsManager", return_value=_mock_embeddings()):
        mgr = MemoryManager(db_path=tmp_path)
        mgr.save(_make_entry(issue_key="PROJ-100"))
        stats = mgr.status()

    assert stats["entry_count"] == 1
    assert "db_path" in stats
    assert "model" in stats
