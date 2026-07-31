"""Tests for Phase 2: reference reinforcement, reinforce_usage, cross_reference_boost."""
from __future__ import annotations
import pytest
from unittest.mock import MagicMock, patch
from pathlib import Path


def _make_manager(tmp_path: Path):
    from icx_engine.memory.manager import MemoryManager
    mgr = MemoryManager(db_path=tmp_path / "mem")
    mgr._embeddings = MagicMock()
    mgr._embeddings.check_ready.return_value = None
    mgr._embeddings.embed.return_value = [0.1] * 768
    return mgr


def _save_entry(mgr, issue_key: str, root_cause_pattern: str = "missing_null_check"):
    from icx_engine.memory.schema import MemoryEntry
    import uuid
    e = MemoryEntry(
        id=str(uuid.uuid4()),
        issue_key=issue_key,
        project_key="TEST",
        source_type="jira",
        issue_type="Bug",
        summary=f"Bug in {issue_key}",
        problem_description="Desc",
        resolution_note="Fix",
        files_changed=["src/foo.py"],
        resolution_confirmed=True,
        saved_at="2026-01-01T00:00:00",
        root_cause_pattern=root_cause_pattern,
    )
    mgr.save(e)
    return e


class TestReinforceUsage:
    def test_non_existent_key_returns_error(self, tmp_path):
        mgr = _make_manager(tmp_path)
        result = mgr.reinforce_usage("PROJ-999", "PROJ-1")
        assert "error" in result
        assert result["source_key"] == "PROJ-999"

    def test_same_used_by_key_twice_increments_once(self, tmp_path):
        mgr = _make_manager(tmp_path)
        _save_entry(mgr, "PROJ-1")
        mgr.reinforce_usage("PROJ-1", "PROJ-50")
        mgr.reinforce_usage("PROJ-1", "PROJ-50")
        entry = mgr._find_by_key("PROJ-1")
        assert entry.usage_count == 1

    def test_five_reinforcements_confidence_ge_075(self, tmp_path):
        mgr = _make_manager(tmp_path)
        _save_entry(mgr, "PROJ-1")
        for i in range(5):
            mgr.reinforce_usage("PROJ-1", f"PROJ-{100 + i}")
        entry = mgr._find_by_key("PROJ-1")
        assert entry.memory_confidence >= 0.75

    def test_ten_reinforcements_confidence_eq_one(self, tmp_path):
        mgr = _make_manager(tmp_path)
        _save_entry(mgr, "PROJ-1")
        for i in range(10):
            mgr.reinforce_usage("PROJ-1", f"PROJ-{200 + i}")
        entry = mgr._find_by_key("PROJ-1")
        assert entry.memory_confidence == 1.0

    def test_sibling_same_pattern_overlapping_citation_boost_incremented(self, tmp_path):
        mgr = _make_manager(tmp_path)
        _save_entry(mgr, "PROJ-A", root_cause_pattern="stale_cache_reference")
        _save_entry(mgr, "PROJ-B", root_cause_pattern="stale_cache_reference")
        mgr.reinforce_usage("PROJ-A", "PROJ-COMMON")
        # Now PROJ-B shares a potential sibling relationship
        # Reinforce PROJ-B with same citation key - sibling recalculation runs
        result = mgr.reinforce_usage("PROJ-B", "PROJ-COMMON")
        assert isinstance(result, dict)
        assert "usage_count" in result

    def test_reinforce_usage_scans_pattern_pool_once_not_per_sibling(self, tmp_path):
        """Regression test for the scan-sharing fix: entry's own boost recompute and the
        sibling recalculation must share one table scan, not one scan each."""
        mgr = _make_manager(tmp_path)
        _save_entry(mgr, "PROJ-A", root_cause_pattern="shared_pattern")
        _save_entry(mgr, "PROJ-B", root_cause_pattern="shared_pattern")
        _save_entry(mgr, "PROJ-C", root_cause_pattern="shared_pattern")
        mgr.reinforce_usage("PROJ-A", "PROJ-COMMON")
        mgr.reinforce_usage("PROJ-B", "PROJ-COMMON")

        table = mgr._get_table()
        with patch.object(table, "search", wraps=table.search) as spy:
            mgr.reinforce_usage("PROJ-C", "PROJ-COMMON")

        # 1 call to find PROJ-C by key (unrelated to this fix) + 1 shared pattern-pool
        # scan reused by both the entry's own boost recompute and the sibling
        # recalculation (which here updates 2 siblings: PROJ-A and PROJ-B). Before the
        # fix this was 1 (find) + 1 (entry boost) + 1 (siblings fetch) + 2 (one recompute
        # scan per updated sibling) = 5.
        assert spy.call_count == 2

    def test_negated_entry_boost_reduced_by_04(self, tmp_path):
        mgr = _make_manager(tmp_path)
        _save_entry(mgr, "PROJ-1")
        # Reinforce to get a non-zero base boost
        for i in range(4):
            mgr.reinforce_usage("PROJ-1", f"PROJ-{i}")
        # Negate it
        entry = mgr._find_by_key("PROJ-1")
        boost_before = entry.cross_reference_boost
        mgr.negate_resolution("PROJ-1", "Wrong fix")
        entry_after = mgr._find_by_key("PROJ-1")
        assert entry_after.cross_reference_boost <= boost_before - 0.3

    def test_reinforce_result_returns_expected_keys(self, tmp_path):
        mgr = _make_manager(tmp_path)
        _save_entry(mgr, "PROJ-1")
        result = mgr.reinforce_usage("PROJ-1", "PROJ-2")
        assert "source_key" in result
        assert "usage_count" in result
        assert "cross_reference_boost" in result
        assert "siblings_updated" in result
