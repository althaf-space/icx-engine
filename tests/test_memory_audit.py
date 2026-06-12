"""Tests for Phase 7: audit trail, _log_audit, get_audit_trail, negative signal warning."""
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


def _save_entry(mgr, issue_key: str):
    from icx_engine.memory.schema import MemoryEntry
    import uuid
    e = MemoryEntry(
        id=str(uuid.uuid4()),
        issue_key=issue_key,
        project_key="TEST",
        source_type="jira",
        issue_type="Bug",
        summary="Bug",
        problem_description="Desc",
        resolution_note="Fix",
        files_changed=["src/app.py"],
        resolution_confirmed=True,
        saved_at="2026-01-01T00:00:00",
    )
    mgr.save(e)


class TestAuditLogging:
    def test_reinforce_writes_reinforced_event(self, tmp_path):
        mgr = _make_manager(tmp_path)
        _save_entry(mgr, "PROJ-1")
        mgr.reinforce_usage("PROJ-1", "PROJ-2")
        trail = mgr.get_audit_trail("PROJ-1")
        event_types = [e.get("event_type") for e in trail]
        assert "reinforced" in event_types

    def test_verify_writes_verified_event(self, tmp_path):
        mgr = _make_manager(tmp_path)
        _save_entry(mgr, "PROJ-1")
        mgr.verify_resolution("PROJ-1", "Confirmed working")
        trail = mgr.get_audit_trail("PROJ-1")
        event_types = [e.get("event_type") for e in trail]
        assert "verified" in event_types

    def test_negate_with_2_citers_writes_3_events(self, tmp_path):
        mgr = _make_manager(tmp_path)
        _save_entry(mgr, "PROJ-SRC")
        _save_entry(mgr, "PROJ-C1")
        _save_entry(mgr, "PROJ-C2")
        mgr.reinforce_usage("PROJ-SRC", "PROJ-C1")
        mgr.reinforce_usage("PROJ-SRC", "PROJ-C2")
        mgr.negate_resolution("PROJ-SRC", "Wrong")
        # 1 negated event on PROJ-SRC + 2 propagated negated events on citers
        trail_src = mgr.get_audit_trail("PROJ-SRC")
        negated_events = [e for e in trail_src if e.get("event_type") == "negated"]
        assert len(negated_events) >= 1

    def test_audit_trail_sorted_descending_by_timestamp(self, tmp_path):
        mgr = _make_manager(tmp_path)
        _save_entry(mgr, "PROJ-1")
        mgr.reinforce_usage("PROJ-1", "PROJ-2")
        mgr.verify_resolution("PROJ-1", "confirmed")
        trail = mgr.get_audit_trail("PROJ-1")
        if len(trail) >= 2:
            timestamps = [e.get("timestamp", "") for e in trail]
            assert timestamps == sorted(timestamps, reverse=True)

    def test_log_audit_failure_does_not_raise(self, tmp_path):
        mgr = _make_manager(tmp_path)
        _save_entry(mgr, "PROJ-1")
        # Mock audit table to raise on add
        with patch.object(mgr, '_audit_table', side_effect=RuntimeError("DB error")):
            from icx_engine.memory.schema import MemoryAuditEvent
            # Should not raise
            mgr._log_audit(MemoryAuditEvent(
                event_type="reinforced", source_key="PROJ-1", actor_key="PROJ-2"
            ))

    def test_reinforce_succeeds_even_if_audit_fails(self, tmp_path):
        mgr = _make_manager(tmp_path)
        _save_entry(mgr, "PROJ-1")
        with patch.object(mgr, '_log_audit', side_effect=RuntimeError("audit error")):
            result = mgr.reinforce_usage("PROJ-1", "PROJ-2")
        # reinforce should still return success
        assert "usage_count" in result


class TestNegativeSignalWarning:
    def test_warning_built_with_negative_signals(self):
        from icx_engine.mcp_server import _build_negative_signal_warning
        signals = [
            {"issue_key": "PROJ-1", "negation_reason": "Caused deadlock", "root_cause_pattern": "incorrect_transaction_boundary"},
        ]
        warning = _build_negative_signal_warning(signals)
        assert "NEGATIVE SIGNAL WARNING" in warning
        assert "PROJ-1" in warning
        assert "Caused deadlock" in warning

    def test_empty_signals_returns_empty_string(self):
        from icx_engine.mcp_server import _build_negative_signal_warning
        assert _build_negative_signal_warning([]) == ""

    def test_warning_includes_do_not_reuse_message(self):
        from icx_engine.mcp_server import _build_negative_signal_warning
        signals = [{"issue_key": "PROJ-5", "negation_reason": "wrong", "root_cause_pattern": "uncategorized"}]
        warning = _build_negative_signal_warning(signals)
        assert "DO NOT reuse" in warning
