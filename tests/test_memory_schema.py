"""Tests for Phase 1: ROOT_CAUSE_PATTERNS, MemoryEntry new fields, MemoryAuditEvent."""
from __future__ import annotations
import pytest
from icx_engine.memory.schema import (
    MemoryEntry,
    MemoryAuditEvent,
    ROOT_CAUSE_PATTERNS,
)


def _base_entry(**kwargs) -> MemoryEntry:
    defaults = dict(
        id="test-id",
        issue_key="TEST-1",
        project_key="TEST",
        source_type="jira",
        issue_type="Bug",
        summary="Test bug",
        problem_description="Problem desc",
        resolution_note="Fix note",
        files_changed=["src/app.py"],
        resolution_confirmed=True,
        saved_at="2026-01-01T00:00:00",
    )
    defaults.update(kwargs)
    return MemoryEntry(**defaults)


class TestRootCausePatterns:
    def test_all_21_patterns_present(self):
        expected = {
            "stale_cache_reference", "missing_null_check", "incorrect_transaction_boundary",
            "event_race_condition", "schema_drift", "auth_scope_mismatch",
            "async_context_leak", "missing_index", "type_coercion_error",
            "config_env_mismatch", "missing_idempotency", "cascade_delete_missing",
            "n_plus_one_query", "memory_leak", "timeout_misconfiguration",
            "pagination_boundary_error", "deserialization_contract_break",
            "feature_flag_state_leak", "tenant_isolation_breach", "retry_storm",
            "uncategorized",
        }
        assert expected == ROOT_CAUSE_PATTERNS

    def test_uncategorized_in_patterns(self):
        assert "uncategorized" in ROOT_CAUSE_PATTERNS


class TestMemoryEntryDefaults:
    def test_no_new_fields_all_defaults_correct(self):
        e = _base_entry()
        assert e.root_cause_pattern == "uncategorized"
        assert e.pattern_confidence == 0.0
        assert e.outcome_verified is False
        assert e.outcome_feedback_note == ""
        assert e.negated is False
        assert e.negation_reason == ""
        assert e.used_by_tickets == []
        assert e.usage_count == 0
        assert e.cross_reference_boost == 0.0
        assert e.temporal_decay_factor == 1.0
        assert e.save_context_vector == []
        assert e.semantic_drift_score == 0.0
        assert e.pattern_cluster_id == ""
        assert e.attachment_fingerprints == []
        assert e.causal_chain == {}
        assert e.full_ticket_text == ""
        assert e.attachment_summary == ""

    def test_root_cause_pattern_settable(self):
        e = _base_entry(root_cause_pattern="stale_cache_reference")
        assert e.root_cause_pattern == "stale_cache_reference"

    def test_negated_entry_has_correct_fields(self):
        e = _base_entry(negated=True, negation_reason="Wrong approach")
        assert e.negated is True
        assert e.negation_reason == "Wrong approach"


class TestMemoryAuditEvent:
    def test_audit_event_defaults_correct(self):
        ev = MemoryAuditEvent(event_type="reinforced", source_key="PROJ-1", actor_key="PROJ-2")
        assert ev.event_type == "reinforced"
        assert ev.source_key == "PROJ-1"
        assert ev.actor_key == "PROJ-2"
        assert ev.before_boost == 0.0
        assert ev.after_boost == 0.0
        assert ev.before_confidence == 0.0
        assert ev.after_confidence == 0.0
        assert ev.note == ""
        assert ev.id  # auto-generated UUID
        assert ev.timestamp  # auto-generated

    def test_audit_event_all_types_constructable(self):
        for t in ("reinforced", "verified", "negated", "boost_applied", "hub_detected"):
            ev = MemoryAuditEvent(event_type=t, source_key="K-1", actor_key="K-2")
            assert ev.event_type == t
