from __future__ import annotations
import uuid
from unittest.mock import patch, MagicMock

import pytest

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


# ── RelationManager unit tests ────────────────────────────────────────────────

def test_shares_file_edge_created(tmp_path):
    from icx_engine.memory.relations import RelationManager

    rel = RelationManager(db_path=tmp_path)
    e1 = _make_entry(issue_key="PROJ-1", files_changed=["src/auth/token.py"])
    e2 = _make_entry(issue_key="PROJ-2", id=str(uuid.uuid4()), files_changed=["src/auth/token.py"])
    rel.auto_link(e1, [e2])

    related = rel.get_related("PROJ-1")
    assert len(related) == 1
    assert related[0]["issue_key"] == "PROJ-2"
    assert related[0]["relation_type"] == "shares_file"


def test_no_edge_when_no_shared_files(tmp_path):
    from icx_engine.memory.relations import RelationManager

    rel = RelationManager(db_path=tmp_path)
    e1 = _make_entry(issue_key="PROJ-1", files_changed=["src/auth/token.py"])
    e2 = _make_entry(issue_key="PROJ-2", id=str(uuid.uuid4()), files_changed=["src/payments/invoice.py"])
    rel.auto_link(e1, [e2])

    assert rel.get_related("PROJ-1") == []


def test_bidirectional_edges(tmp_path):
    from icx_engine.memory.relations import RelationManager

    rel = RelationManager(db_path=tmp_path)
    e1 = _make_entry(issue_key="PROJ-1", files_changed=["src/auth/token.py"])
    e2 = _make_entry(issue_key="PROJ-2", id=str(uuid.uuid4()), files_changed=["src/auth/token.py"])
    rel.auto_link(e1, [e2])

    assert any(r["issue_key"] == "PROJ-2" for r in rel.get_related("PROJ-1"))
    assert any(r["issue_key"] == "PROJ-1" for r in rel.get_related("PROJ-2"))


def test_strength_proportional_to_overlap(tmp_path):
    from icx_engine.memory.relations import RelationManager

    rel = RelationManager(db_path=tmp_path)
    # e1 has 4 files, e2 shares 2 -> strength = 2/4 = 0.5
    e1 = _make_entry(
        issue_key="PROJ-1",
        files_changed=["a.py", "b.py", "c.py", "d.py"],
    )
    e2 = _make_entry(
        issue_key="PROJ-2",
        id=str(uuid.uuid4()),
        files_changed=["a.py", "b.py"],
    )
    rel.auto_link(e1, [e2])

    related = rel.get_related("PROJ-1")
    assert len(related) == 1
    assert related[0]["strength"] == pytest.approx(0.5, abs=0.01)


def test_delete_for_removes_edges(tmp_path):
    from icx_engine.memory.relations import RelationManager

    rel = RelationManager(db_path=tmp_path)
    e1 = _make_entry(issue_key="PROJ-1", files_changed=["src/auth/token.py"])
    e2 = _make_entry(issue_key="PROJ-2", id=str(uuid.uuid4()), files_changed=["src/auth/token.py"])
    rel.auto_link(e1, [e2])

    rel.delete_for("PROJ-1")

    assert rel.get_related("PROJ-1") == []
    assert rel.get_related("PROJ-2") == []


def test_no_self_link(tmp_path):
    from icx_engine.memory.relations import RelationManager

    rel = RelationManager(db_path=tmp_path)
    e = _make_entry(issue_key="PROJ-1", files_changed=["src/auth/token.py"])
    rel.auto_link(e, [e])  # pass entry as its own neighbour

    assert rel.get_related("PROJ-1") == []


def test_no_edge_when_files_changed_empty(tmp_path):
    from icx_engine.memory.relations import RelationManager

    rel = RelationManager(db_path=tmp_path)
    e1 = _make_entry(issue_key="PROJ-1", files_changed=[])
    e2 = _make_entry(issue_key="PROJ-2", id=str(uuid.uuid4()), files_changed=["src/auth/token.py"])
    rel.auto_link(e1, [e2])

    assert rel.get_related("PROJ-1") == []


# ── add_relation direct tests ─────────────────────────────────────────────────

def test_add_relation_self_link_is_noop(tmp_path):
    from icx_engine.memory.relations import RelationManager

    rel = RelationManager(db_path=tmp_path)
    rel.add_relation("PROJ-1", "PROJ-1", "shares_file", 1.0)
    assert rel.get_related("PROJ-1") == []


def test_add_relation_explicit_type_and_strength(tmp_path):
    from icx_engine.memory.relations import RelationManager

    rel = RelationManager(db_path=tmp_path)
    rel.add_relation("PROJ-1", "PROJ-2", "co_occurrence", 0.75)
    related = rel.get_related("PROJ-1")
    assert len(related) == 1
    assert related[0]["issue_key"] == "PROJ-2"
    assert related[0]["relation_type"] == "co_occurrence"
    assert related[0]["strength"] == pytest.approx(0.75, abs=0.001)


def test_add_relation_update_replaces_existing_strength(tmp_path):
    from icx_engine.memory.relations import RelationManager

    rel = RelationManager(db_path=tmp_path)
    rel.add_relation("PROJ-1", "PROJ-2", "shares_file", 0.3)
    rel.add_relation("PROJ-1", "PROJ-2", "shares_file", 0.9)
    related = rel.get_related("PROJ-1")
    assert len(related) == 1
    assert related[0]["strength"] == pytest.approx(0.9, abs=0.001)


# ── Integration: auto_link called from MemoryManager.save ────────────────────

def test_auto_link_via_memory_manager_save(tmp_path):
    from icx_engine.memory.manager import MemoryManager
    from icx_engine.memory.relations import RelationManager

    with patch("icx_engine.memory.manager.EmbeddingsManager", return_value=_mock_embeddings()):
        mgr = MemoryManager(db_path=tmp_path)
        mgr.save(_make_entry(issue_key="PROJ-1", id="id-1", files_changed=["src/auth/token.py"]))
        mgr.save(_make_entry(issue_key="PROJ-2", id="id-2", files_changed=["src/auth/token.py"]))

    rel = RelationManager(db_path=tmp_path)
    assert any(r["issue_key"] == "PROJ-2" for r in rel.get_related("PROJ-1"))


def test_delete_cleans_up_relations(tmp_path):
    from icx_engine.memory.manager import MemoryManager
    from icx_engine.memory.relations import RelationManager

    with patch("icx_engine.memory.manager.EmbeddingsManager", return_value=_mock_embeddings()):
        mgr = MemoryManager(db_path=tmp_path)
        mgr.save(_make_entry(issue_key="PROJ-1", id="id-1", files_changed=["src/auth/token.py"]))
        mgr.save(_make_entry(issue_key="PROJ-2", id="id-2", files_changed=["src/auth/token.py"]))
        mgr.delete("PROJ-1")

    rel = RelationManager(db_path=tmp_path)
    assert rel.get_related("PROJ-1") == []
    assert rel.get_related("PROJ-2") == []


# ── get_related_by_files tests ────────────────────────────────────────────────

def test_get_related_by_files_returns_overlap(tmp_path):
    from icx_engine.memory.relations import RelationManager

    rel = RelationManager(db_path=tmp_path)
    entries = [
        _make_entry(issue_key="PROJ-1", files_changed=["auth/token.py", "auth/session.py"]),
        _make_entry(issue_key="PROJ-2", id=str(uuid.uuid4()), files_changed=["auth/token.py"]),
    ]
    result = rel.get_related_by_files(["auth/token.py"], entries)
    keys = [r["issue_key"] for r in result]
    assert "PROJ-1" in keys
    assert "PROJ-2" in keys
    assert all(r["relation_type"] == "shares_file" for r in result)


def test_get_related_by_files_no_overlap(tmp_path):
    from icx_engine.memory.relations import RelationManager

    rel = RelationManager(db_path=tmp_path)
    entries = [_make_entry(issue_key="PROJ-1", files_changed=["payments/invoice.py"])]
    result = rel.get_related_by_files(["auth/token.py"], entries)
    assert result == []


def test_get_related_by_files_empty_needle(tmp_path):
    from icx_engine.memory.relations import RelationManager

    rel = RelationManager(db_path=tmp_path)
    entries = [_make_entry(issue_key="PROJ-1", files_changed=["auth/token.py"])]
    result = rel.get_related_by_files([], entries)
    assert result == []


def test_get_related_by_files_excludes_key(tmp_path):
    from icx_engine.memory.relations import RelationManager

    rel = RelationManager(db_path=tmp_path)
    entries = [
        _make_entry(issue_key="PROJ-1", files_changed=["auth/token.py"]),
        _make_entry(issue_key="PROJ-2", id=str(uuid.uuid4()), files_changed=["auth/token.py"]),
    ]
    result = rel.get_related_by_files(["auth/token.py"], entries, exclude_key="PROJ-1")
    keys = [r["issue_key"] for r in result]
    assert "PROJ-1" not in keys
    assert "PROJ-2" in keys


def test_reset_clears_cached_state(tmp_path):
    from icx_engine.memory.relations import RelationManager

    rel = RelationManager(db_path=tmp_path)
    # Force table initialisation
    e1 = _make_entry(issue_key="PROJ-1", files_changed=["a.py"])
    e2 = _make_entry(issue_key="PROJ-2", id=str(uuid.uuid4()), files_changed=["a.py"])
    rel.auto_link(e1, [e2])
    assert rel._table is not None

    rel.reset()
    assert rel._table is None
    assert rel._db is None

    # Should reconnect on next access
    result = rel.get_related("PROJ-1")
    assert any(r["issue_key"] == "PROJ-2" for r in result)


def test_get_related_by_files_sorted_by_strength(tmp_path):
    from icx_engine.memory.relations import RelationManager

    rel = RelationManager(db_path=tmp_path)
    # needle = 3 files; PROJ-1 shares 1 (strength=1/3), PROJ-2 shares 2 (strength=2/3)
    entries = [
        _make_entry(issue_key="PROJ-1", files_changed=["a.py"]),
        _make_entry(issue_key="PROJ-2", id=str(uuid.uuid4()), files_changed=["a.py", "b.py"]),
    ]
    result = rel.get_related_by_files(["a.py", "b.py", "c.py"], entries)
    assert result[0]["issue_key"] == "PROJ-2"
    assert result[0]["strength"] > result[1]["strength"]


def test_get_related_by_files_multi_file_strength(tmp_path):
    from icx_engine.memory.relations import RelationManager

    rel = RelationManager(db_path=tmp_path)
    # needle = 5 files, entry shares 3 -> strength = 3/5 = 0.6
    entries = [
        _make_entry(
            issue_key="PROJ-1",
            files_changed=["a.py", "b.py", "c.py"],
        )
    ]
    result = rel.get_related_by_files(["a.py", "b.py", "c.py", "d.py", "e.py"], entries)
    assert len(result) == 1
    assert result[0]["strength"] == pytest.approx(0.6, abs=0.01)
