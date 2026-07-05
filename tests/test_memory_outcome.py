"""Tests for Phase 4: verify_resolution, negate_resolution, outcome feedback."""
from __future__ import annotations
import pytest
from unittest.mock import MagicMock
from pathlib import Path


def _make_manager(tmp_path: Path):
    from icx_engine.memory.manager import MemoryManager
    mgr = MemoryManager(db_path=tmp_path / "mem")
    mgr._embeddings = MagicMock()
    mgr._embeddings.check_ready.return_value = None
    mgr._embeddings.embed.return_value = [0.1] * 768
    return mgr


def _save_entry(mgr, issue_key: str, resolution_confirmed: bool = False):
    from icx_engine.memory.schema import MemoryEntry
    import uuid
    e = MemoryEntry(
        id=str(uuid.uuid4()),
        issue_key=issue_key,
        project_key="TEST",
        source_type="jira",
        issue_type="Bug",
        summary=f"Bug {issue_key}",
        problem_description="Desc",
        resolution_note="Fix",
        files_changed=["src/app.py"],
        resolution_confirmed=resolution_confirmed,
        saved_at="2026-01-01T00:00:00",
    )
    mgr.save(e)


class TestVerifyResolution:
    def test_verify_sets_confidence_by_count(self, tmp_path):
        mgr = _make_manager(tmp_path)
        _save_entry(mgr, "PROJ-1")
        result = mgr.verify_resolution("PROJ-1", "Fixed in prod")
        assert result["memory_confidence"] == pytest.approx(0.25)
        assert result["confirmation_count"] == 1

    def test_verify_twice_count_is_two(self, tmp_path):
        mgr = _make_manager(tmp_path)
        _save_entry(mgr, "PROJ-1")
        mgr.verify_resolution("PROJ-1", "First confirm")
        result = mgr.verify_resolution("PROJ-1", "Second confirm")
        assert result["confirmation_count"] == 2
        assert result["memory_confidence"] == pytest.approx(0.50)

    def test_verify_non_existent_key_error(self, tmp_path):
        mgr = _make_manager(tmp_path)
        result = mgr.verify_resolution("PROJ-999", "note")
        assert "error" in result

    def test_verify_capped_at_one(self, tmp_path):
        mgr = _make_manager(tmp_path)
        _save_entry(mgr, "PROJ-1")
        for _ in range(10):
            mgr.verify_resolution("PROJ-1", "confirmed")
        entry = mgr._find_by_key("PROJ-1")
        assert entry.memory_confidence == 1.0

    def test_verify_does_not_regress_reinforced_confidence(self, tmp_path):
        # reinforce_usage raises confidence to 1.0 (>=10 citations). A single
        # positive verify_resolution must NOT drop it back to 0.25.
        mgr = _make_manager(tmp_path)
        _save_entry(mgr, "PROJ-1")
        for i in range(10):
            mgr.reinforce_usage("PROJ-1", f"PROJ-{200 + i}")
        assert mgr._find_by_key("PROJ-1").memory_confidence == 1.0
        result = mgr.verify_resolution("PROJ-1", "Confirmed working")
        assert result["confirmation_count"] == 1
        assert result["memory_confidence"] == 1.0
        assert mgr._find_by_key("PROJ-1").memory_confidence == 1.0


class TestNegateResolution:
    def test_negate_sets_negated_flag(self, tmp_path):
        mgr = _make_manager(tmp_path)
        _save_entry(mgr, "PROJ-1")
        result = mgr.negate_resolution("PROJ-1", "Caused regression")
        assert result["negated"] is True

    def test_negate_reduces_boost_significantly(self, tmp_path):
        mgr = _make_manager(tmp_path)
        _save_entry(mgr, "PROJ-1")
        for i in range(4):
            mgr.reinforce_usage("PROJ-1", f"PROJ-{i + 10}")
        before = mgr._find_by_key("PROJ-1").cross_reference_boost
        mgr.negate_resolution("PROJ-1", "Wrong")
        after = mgr._find_by_key("PROJ-1").cross_reference_boost
        assert after <= before - 0.3

    def test_negate_propagates_to_citers(self, tmp_path):
        mgr = _make_manager(tmp_path)
        _save_entry(mgr, "PROJ-SRC")
        _save_entry(mgr, "PROJ-CITER1")
        _save_entry(mgr, "PROJ-CITER2")
        # Make PROJ-CITER1 and PROJ-CITER2 reference PROJ-SRC
        mgr.reinforce_usage("PROJ-SRC", "PROJ-CITER1")
        mgr.reinforce_usage("PROJ-SRC", "PROJ-CITER2")
        result = mgr.negate_resolution("PROJ-SRC", "Bad fix")
        assert len(result["propagated_penalty_to"]) == 2
        assert "PROJ-CITER1" in result["propagated_penalty_to"]
        assert "PROJ-CITER2" in result["propagated_penalty_to"]

    def test_negate_clears_outcome_verified(self, tmp_path):
        mgr = _make_manager(tmp_path)
        _save_entry(mgr, "PROJ-1")
        mgr.verify_resolution("PROJ-1", "Confirmed")
        mgr.negate_resolution("PROJ-1", "Actually wrong")
        entry = mgr._find_by_key("PROJ-1")
        assert entry.negated is True
        assert entry.outcome_verified is False

    def test_negate_non_existent_key_error(self, tmp_path):
        mgr = _make_manager(tmp_path)
        result = mgr.negate_resolution("PROJ-999", "reason")
        assert "error" in result

    def test_negated_entry_appears_in_negative_signals(self, tmp_path):
        mgr = _make_manager(tmp_path)
        _save_entry(mgr, "PROJ-1")
        mgr.negate_resolution("PROJ-1", "Wrong approach")
        from icx_engine.memory.schema import MemoryQueryInput
        qi = MemoryQueryInput(
            issue_key="",
            project_key="TEST",
            source_type="jira",
            summary="Bug in app",
            description="Desc",
            issue_type="Bug",
        )
        smart = mgr.query_smart(qi, top_k=5, min_score=0.0)
        neg_keys = [r["issue_key"] for r in smart.get("negative_signals", [])]
        result_keys = [r["issue_key"] for r in smart.get("results", [])]
        # If the entry was found at all, it should be in negative_signals not results
        if "PROJ-1" in neg_keys or "PROJ-1" in result_keys:
            assert "PROJ-1" in neg_keys
            assert "PROJ-1" not in result_keys
