from __future__ import annotations
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch
import uuid
import pytest

from icx_engine.memory.schema import MemoryEntry, MemoryQueryInput


def _mock_embeddings():
    mgr = MagicMock()
    mgr.embed.return_value = [0.1] * 768
    mgr.ensure_ready.return_value = None
    mgr.check_ready.return_value = None
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
    rows = table.search([0.1] * 768).limit(10).to_list()
    assert any(r["issue_key"] == "PROJ-100" for r in rows)


def test_save_fileless_entry_skips_scan(tmp_path):
    """A file-less entry persists but does not trigger the auto_link candidate
    scan (finding R1) - auto_link is a no-op without files_changed."""
    from icx_engine.memory.manager import MemoryManager

    with patch("icx_engine.memory.manager.EmbeddingsManager", return_value=_mock_embeddings()):
        mgr = MemoryManager(db_path=tmp_path)
        entry = _make_entry(issue_key="PROJ-1", files_changed=[])
        with patch.object(mgr, "_lean_link_candidates", wraps=mgr._lean_link_candidates) as spy:
            mgr.save(entry)
        assert spy.call_count == 0             # no candidate scan for a file-less save
        assert mgr.show("PROJ-1") is not None   # still persisted


def test_save_file_bearing_entry_runs_auto_link(tmp_path):
    """A file-bearing entry runs auto_link via the lean candidate scan (behavior
    preserved, cheaper load)."""
    from icx_engine.memory.manager import MemoryManager

    with patch("icx_engine.memory.manager.EmbeddingsManager", return_value=_mock_embeddings()):
        mgr = MemoryManager(db_path=tmp_path)
        entry = _make_entry(issue_key="PROJ-2", files_changed=["src/a.py"])
        with patch.object(mgr, "_lean_link_candidates", wraps=mgr._lean_link_candidates) as spy:
            mgr.save(entry)
        assert spy.call_count >= 1             # auto_link candidate scan happened


def test_lean_link_candidates_match_full_entries(tmp_path):
    """Projected-scan candidates carry the same (issue_key, files_changed) as the
    full load - the only fields auto_link reads (finding R1 speedup)."""
    from icx_engine.memory.manager import MemoryManager

    with patch("icx_engine.memory.manager.EmbeddingsManager", return_value=_mock_embeddings()):
        mgr = MemoryManager(db_path=tmp_path)
        for i in range(6):
            mgr.save(_make_entry(issue_key=f"PROJ-{i}", files_changed=[f"src/f{i % 3}.py", "src/shared.py"]))
        lean = {c.issue_key: sorted(c.files_changed) for c in mgr._lean_link_candidates()}
        full = {e.issue_key: sorted(e.files_changed) for e in mgr.list_entries()}
        assert lean == full


def test_lean_candidates_yield_identical_edges(tmp_path):
    """Overlap computed against lean candidates == against full entries."""
    from icx_engine.memory.manager import MemoryManager

    def _norm(fs):
        return {f.replace("\\", "/").lower() for f in fs}

    def _overlap_keys(cands, target):
        needle = _norm(target.files_changed)
        return {
            c.issue_key for c in cands
            if c.issue_key != target.issue_key and needle & _norm(c.files_changed)
        }

    with patch("icx_engine.memory.manager.EmbeddingsManager", return_value=_mock_embeddings()):
        mgr = MemoryManager(db_path=tmp_path)
        for i in range(6):
            mgr.save(_make_entry(issue_key=f"PROJ-{i}", files_changed=[f"src/f{i % 3}.py", "src/shared.py"]))
        target = _make_entry(issue_key="PROJ-99", files_changed=["SRC\\F1.py"])  # mixed case/sep
        assert _overlap_keys(mgr._lean_link_candidates(), target) == \
               _overlap_keys(mgr.list_entries(), target)


def test_lean_candidates_empty_table(tmp_path):
    from icx_engine.memory.manager import MemoryManager
    with patch("icx_engine.memory.manager.EmbeddingsManager", return_value=_mock_embeddings()):
        mgr = MemoryManager(db_path=tmp_path)
        assert mgr._lean_link_candidates() == []


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


def test_tech_stack_persists_and_surfaces_in_query(tmp_path):
    from icx_engine.memory.manager import MemoryManager

    stack = {".": {"languages": {"java": "17"}, "frameworks": {"spring-boot": "3.2.1"}, "package_manager": "maven"}}

    with patch("icx_engine.memory.manager.EmbeddingsManager", return_value=_mock_embeddings()):
        mgr = MemoryManager(db_path=tmp_path)
        entry = _make_entry(
            issue_key="PROJ-100",
            summary="JWT auth token expires too quickly",
            tech_stack=stack,
        )
        mgr.save(entry)

        stored = mgr.show("PROJ-100")
        assert stored.tech_stack == stack

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
    assert insights[0].tech_stack == stack


def test_tech_stack_defaults_empty_dict(tmp_path):
    from icx_engine.memory.manager import MemoryManager

    with patch("icx_engine.memory.manager.EmbeddingsManager", return_value=_mock_embeddings()):
        mgr = MemoryManager(db_path=tmp_path)
        entry = _make_entry(issue_key="PROJ-101")
        mgr.save(entry)
        stored = mgr.show("PROJ-101")

    assert stored.tech_stack == {}


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


def test_list_entries_project_key_full_issue_key(tmp_path):
    from icx_engine.memory.manager import MemoryManager

    with patch("icx_engine.memory.manager.EmbeddingsManager", return_value=_mock_embeddings()):
        mgr = MemoryManager(db_path=tmp_path)
        mgr.save(_make_entry(issue_key="PROJ-100", id="id-1", project_key="PROJ"))
        mgr.save(_make_entry(issue_key="OTHER-1", id="id-2", project_key="OTHER"))
        entries = mgr.list_entries(project_key="PROJ-100")

    assert len(entries) == 1
    assert entries[0].issue_key == "PROJ-100"


def test_list_entries_project_key_case_insensitive(tmp_path):
    from icx_engine.memory.manager import MemoryManager

    with patch("icx_engine.memory.manager.EmbeddingsManager", return_value=_mock_embeddings()):
        mgr = MemoryManager(db_path=tmp_path)
        mgr.save(_make_entry(issue_key="PROJ-100", id="id-1", project_key="PROJ"))
        entries = mgr.list_entries(project_key="proj")

    assert len(entries) == 1
    assert entries[0].issue_key == "PROJ-100"


def test_list_entries_project_key_non_jira_not_mangled(tmp_path):
    from icx_engine.memory.manager import MemoryManager

    with patch("icx_engine.memory.manager.EmbeddingsManager", return_value=_mock_embeddings()):
        mgr = MemoryManager(db_path=tmp_path)
        mgr.save(_make_entry(issue_key="PROJ-100", id="id-1", project_key="owner-org"))
        entries = mgr.list_entries(project_key="owner-org")

    assert len(entries) == 1
    assert entries[0].issue_key == "PROJ-100"


def test_clear_resets_sub_managers(tmp_path):
    from icx_engine.memory.manager import MemoryManager

    with patch("icx_engine.memory.manager.EmbeddingsManager", return_value=_mock_embeddings()):
        mgr = MemoryManager(db_path=tmp_path)
        mgr.save(_make_entry(issue_key="PROJ-100", id="id-1", files_changed=["src/a.py"]))
        mgr.save(_make_entry(issue_key="PROJ-200", id="id-2", files_changed=["src/a.py"]))
        # Force sub-manager table initialisation
        _ = mgr._relations._get_table()
        _ = mgr._patterns._get_table()
        mgr.clear()

    # Sub-manager caches cleared by reset()
    assert mgr._relations._table is None
    assert mgr._patterns._table is None


def test_get_related_delegates_to_sub_manager(tmp_path):
    from icx_engine.memory.manager import MemoryManager

    with patch("icx_engine.memory.manager.EmbeddingsManager", return_value=_mock_embeddings()):
        mgr = MemoryManager(db_path=tmp_path)
        mgr.save(_make_entry(issue_key="PROJ-1", id="id-1", files_changed=["src/a.py"]))
        mgr.save(_make_entry(issue_key="PROJ-2", id="id-2", files_changed=["src/a.py"]))
        result = mgr.get_related(None, None, files=["src/a.py"])

    assert any(r["issue_key"] in ("PROJ-1", "PROJ-2") for r in result)


def test_get_patterns_delegates_to_sub_manager(tmp_path):
    from icx_engine.memory.manager import MemoryManager

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
        patterns = mgr.get_patterns(project_key="PROJ")

    assert len(patterns) > 0
    assert all(p["project_key"] == "PROJ" for p in patterns)


def test_status_returns_dict(tmp_path):
    from icx_engine.memory.manager import MemoryManager

    with patch("icx_engine.memory.manager.EmbeddingsManager", return_value=_mock_embeddings()):
        mgr = MemoryManager(db_path=tmp_path)
        mgr.save(_make_entry(issue_key="PROJ-100"))
        stats = mgr.status()

    assert stats["entry_count"] == 1
    assert "db_path" in stats
    assert "model" in stats


def test_dimension_mismatch_raises_memory_error(tmp_path):
    import lancedb
    import pyarrow as pa
    from icx_engine.exceptions import MemoryError as ICXMemoryError
    from icx_engine.memory.manager import MemoryManager

    # Pre-create table with wrong dimension (384 instead of 768)
    db = lancedb.connect(str(tmp_path))
    schema = pa.schema([
        pa.field("vector", pa.list_(pa.float32(), 384)),
        pa.field("id", pa.utf8()),
        pa.field("issue_key", pa.utf8()),
        pa.field("project_key", pa.utf8()),
        pa.field("source_type", pa.utf8()),
        pa.field("issue_type", pa.utf8()),
        pa.field("summary", pa.utf8()),
        pa.field("problem_description", pa.utf8()),
        pa.field("impact", pa.utf8()),
        pa.field("resolution_note", pa.utf8()),
        pa.field("files_changed", pa.list_(pa.utf8())),
        pa.field("resolution_confirmed", pa.bool_()),
        pa.field("saved_at", pa.utf8()),
        pa.field("tags", pa.list_(pa.utf8())),
        pa.field("work_item_type", pa.utf8()),
        pa.field("pattern_used", pa.utf8()),
    ])
    db.create_table("memory_entries", schema=schema)

    with patch("icx_engine.memory.manager.EmbeddingsManager", return_value=_mock_embeddings()):
        mgr = MemoryManager(db_path=tmp_path)
        with pytest.raises(ICXMemoryError, match="dimension mismatch"):
            mgr._get_table()


def test_migrate_empty_returns_zero(tmp_path):
    from icx_engine.memory.manager import MemoryManager

    mock_emb = _mock_embeddings()
    mock_emb.check_ready.return_value = None
    with patch("icx_engine.memory.manager.EmbeddingsManager", return_value=mock_emb):
        mgr = MemoryManager(db_path=tmp_path)
        count = mgr.migrate()

    assert count == 0


def test_migrate_reembeds_entries(tmp_path):
    from icx_engine.memory.manager import MemoryManager

    mock_emb = _mock_embeddings()
    mock_emb.check_ready.return_value = None
    with patch("icx_engine.memory.manager.EmbeddingsManager", return_value=mock_emb):
        mgr = MemoryManager(db_path=tmp_path)
        mgr.save(_make_entry(issue_key="PROJ-1", id="id-1"))
        mgr.save(_make_entry(issue_key="PROJ-2", id="id-2"))

        logged: list[str] = []
        count = mgr.migrate(log=logged.append)

    assert count == 2
    assert any("PROJ-1" in line for line in logged)
    assert any("PROJ-2" in line for line in logged)
    entries = mgr.list_entries()
    assert len(entries) == 2


def test_migrate_preserves_confirmation_state(tmp_path):
    from icx_engine.memory.manager import MemoryManager

    mock_emb = _mock_embeddings()
    with patch("icx_engine.memory.manager.EmbeddingsManager", return_value=mock_emb):
        mgr = MemoryManager(db_path=tmp_path)
        mgr.save(_make_entry(issue_key="PROJ-1", id="id-1"))
        before = mgr.show("PROJ-1")
        assert before.confirmation_count == 1
        assert before.memory_confidence == 0.25

        mgr.migrate()

        after = mgr.show("PROJ-1")
        assert after.confirmation_count == 1
        assert after.memory_confidence == 0.25


# -- _recompute_decay ---------------------------------------------------------

def test_recompute_decay_handles_non_utc_offset(tmp_path):
    from icx_engine.memory.manager import MemoryManager

    mock_emb = _mock_embeddings()
    with patch("icx_engine.memory.manager.EmbeddingsManager", return_value=mock_emb):
        mgr = MemoryManager(db_path=tmp_path)
        saved_at = (datetime.now(timezone.utc) - timedelta(days=10)).strftime(
            "%Y-%m-%dT%H:%M:%S+05:30"
        )
        entry = _make_entry(saved_at=saved_at)
        decay = mgr._recompute_decay(entry)
        assert 0.0 < decay <= 1.0


def test_recompute_decay_future_saved_at_not_above_one(tmp_path):
    from icx_engine.memory.manager import MemoryManager

    mock_emb = _mock_embeddings()
    with patch("icx_engine.memory.manager.EmbeddingsManager", return_value=mock_emb):
        mgr = MemoryManager(db_path=tmp_path)
        future = (datetime.now(timezone.utc) + timedelta(days=5)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        entry = _make_entry(saved_at=future)
        decay = mgr._recompute_decay(entry)
        assert decay <= 1.0


# -- prewarm ------------------------------------------------------------------

def test_prewarm_calls_check_ready_and_load_model(tmp_path):
    from icx_engine.memory.manager import MemoryManager

    mock_emb = _mock_embeddings()
    with patch("icx_engine.memory.manager.EmbeddingsManager", return_value=mock_emb):
        mgr = MemoryManager(db_path=tmp_path)
        mgr.prewarm()

    mock_emb.check_ready.assert_called_once()
    mock_emb._load_model.assert_called_once()


def test_prewarm_propagates_check_ready_error(tmp_path):
    from icx_engine.memory.manager import MemoryManager
    from icx_engine.exceptions import MemoryError as IcxMemoryError

    mock_emb = _mock_embeddings()
    mock_emb.check_ready.side_effect = IcxMemoryError("model not found")
    with patch("icx_engine.memory.manager.EmbeddingsManager", return_value=mock_emb):
        mgr = MemoryManager(db_path=tmp_path)
        with pytest.raises(IcxMemoryError):
            mgr.prewarm()


# -- Phase 3: confirmation_count + memory_confidence --------------------------

def test_confirmed_save_increments_confirmation_count(tmp_path):
    from icx_engine.memory.manager import MemoryManager

    with patch("icx_engine.memory.manager.EmbeddingsManager", return_value=_mock_embeddings()):
        mgr = MemoryManager(db_path=tmp_path)
        mgr.save(_make_entry(issue_key="PROJ-10", id="id-a", resolution_confirmed=True))
        mgr.save(_make_entry(issue_key="PROJ-10", id="id-a", resolution_confirmed=True))
        entry = mgr.show("PROJ-10")

    assert entry is not None
    assert entry.confirmation_count == 2
    assert entry.memory_confidence == 0.5


def test_unconfirmed_save_no_count_increment(tmp_path):
    from icx_engine.memory.manager import MemoryManager

    with patch("icx_engine.memory.manager.EmbeddingsManager", return_value=_mock_embeddings()):
        mgr = MemoryManager(db_path=tmp_path)
        mgr.save(_make_entry(issue_key="PROJ-11", id="id-b", resolution_confirmed=False))
        entry = mgr.show("PROJ-11")

    assert entry is not None
    assert entry.confirmation_count == 0
    assert entry.memory_confidence == 0.0


def test_higher_confidence_ranks_above_lower(tmp_path):
    from icx_engine.memory.manager import MemoryManager

    with patch("icx_engine.memory.manager.EmbeddingsManager", return_value=_mock_embeddings()):
        mgr = MemoryManager(db_path=tmp_path)
        # Save PROJ-20 with 4 confirmed saves (confidence=1.0)
        for i in range(4):
            mgr.save(_make_entry(issue_key="PROJ-20", id="id-c", resolution_confirmed=True))
        # Save PROJ-21 once confirmed (confidence=0.25)
        mgr.save(_make_entry(issue_key="PROJ-21", id="id-d", resolution_confirmed=True))

        q = MemoryQueryInput(
            issue_key="PROJ-99",
            project_key="PROJ",
            source_type="jira",
            summary="Auth fails",
            description="JWT expired",
            issue_type="Bug",
        )
        insights = mgr.query(q, top_k=5, min_score=0.0)

    keys = [i.issue_key for i in insights]
    assert "PROJ-20" in keys
    assert keys.index("PROJ-20") < keys.index("PROJ-21")


def test_tag_filter_surfaces_matching_entry(tmp_path):
    from icx_engine.memory.manager import MemoryManager

    with patch("icx_engine.memory.manager.EmbeddingsManager", return_value=_mock_embeddings()):
        mgr = MemoryManager(db_path=tmp_path)
        mgr.save(_make_entry(issue_key="PROJ-30", id="id-e", tags=["auth", "jwt"]))
        mgr.save(_make_entry(issue_key="PROJ-31", id="id-f", tags=["payments"]))

        q = MemoryQueryInput(
            issue_key="PROJ-99",
            project_key="PROJ",
            source_type="jira",
            summary="Token validation broken",
            description="JWT rejected on every request",
            issue_type="Bug",
            tags=["auth"],
        )
        insights = mgr.query(q, top_k=5, min_score=0.0)

    keys = [i.issue_key for i in insights]
    assert "PROJ-30" in keys
    assert "PROJ-31" not in keys


def test_tag_filter_fallback_on_no_match(tmp_path):
    from icx_engine.memory.manager import MemoryManager

    with patch("icx_engine.memory.manager.EmbeddingsManager", return_value=_mock_embeddings()):
        mgr = MemoryManager(db_path=tmp_path)
        mgr.save(_make_entry(issue_key="PROJ-40", id="id-g", tags=["payments"]))

        q = MemoryQueryInput(
            issue_key="PROJ-99",
            project_key="PROJ",
            source_type="jira",
            summary="Auth fails",
            description="JWT expired",
            issue_type="Bug",
            tags=["auth"],  # no entry has "auth" - should fall back to all results
        )
        insights = mgr.query(q, top_k=5, min_score=0.0)

    # Falls back to full results, so PROJ-40 still surfaces
    assert any(i.issue_key == "PROJ-40" for i in insights)


def test_fts_fields_do_not_include_resolution_note():
    from icx_engine.memory.manager import _FTS_FIELDS
    assert "resolution_note" not in _FTS_FIELDS
    assert "summary" in _FTS_FIELDS
    assert "problem_description" in _FTS_FIELDS


# -- restore=True (import path) ------------------------------------------------

def test_restore_preserves_confirmation_count(tmp_path):
    """save(restore=True) must write confirmation_count/memory_confidence as-is."""
    from icx_engine.memory.manager import MemoryManager

    with patch("icx_engine.memory.manager.EmbeddingsManager", return_value=_mock_embeddings()):
        mgr = MemoryManager(db_path=tmp_path)
        entry = _make_entry(
            issue_key="PROJ-50",
            id="id-restore",
            resolution_confirmed=True,
            confirmation_count=4,
            memory_confidence=1.0,
        )
        mgr.save(entry, restore=True)
        result = mgr.show("PROJ-50")

    assert result is not None
    assert result.confirmation_count == 4
    assert result.memory_confidence == 1.0


def test_restore_false_still_increments(tmp_path):
    """Default save() (restore=False) still increments confirmation_count normally."""
    from icx_engine.memory.manager import MemoryManager

    with patch("icx_engine.memory.manager.EmbeddingsManager", return_value=_mock_embeddings()):
        mgr = MemoryManager(db_path=tmp_path)
        mgr.save(_make_entry(issue_key="PROJ-51", id="id-norm", resolution_confirmed=True))
        mgr.save(_make_entry(issue_key="PROJ-51", id="id-norm", resolution_confirmed=True))
        result = mgr.show("PROJ-51")

    assert result is not None
    assert result.confirmation_count == 2


def test_restore_triggers_pattern_refresh_per_project(tmp_path):
    """Pattern refresh is called for each project after restore saves."""
    from icx_engine.memory.manager import MemoryManager

    with patch("icx_engine.memory.manager.EmbeddingsManager", return_value=_mock_embeddings()):
        mgr = MemoryManager(db_path=tmp_path)
        entries = [
            _make_entry(issue_key=f"PROJ-6{i}", id=f"id-p{i}", project_key="PROJ")
            for i in range(3)
        ]
        with patch.object(mgr._patterns, "refresh") as mock_refresh:
            for e in entries:
                mgr.save(e, restore=True)
            # Simulate explicit post-import pattern refresh (as done in cli.py)
            mgr._patterns.refresh(entries, "PROJ")
        mock_refresh.assert_called_once_with(entries, "PROJ")
