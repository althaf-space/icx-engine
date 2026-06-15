from __future__ import annotations
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from pydantic import BaseModel, Field

from icx_engine.exceptions import MemoryError


def _sq(value: str) -> str:
    """Escape a string for use in a LanceDB SQL filter (single-quote escape)."""
    return value.replace("'", "''")


def connect_with_timeout(db_path: Path, timeout: float = 3.0):
    """Connect to LanceDB on a daemon thread, raising MemoryError if it hangs.

    Guards against a stale file lock left by a previous server process.
    """
    import lancedb  # lazy import

    _result: list = [None]
    _exc: list = [None]

    def _connect() -> None:
        try:
            _result[0] = lancedb.connect(str(db_path))
        except Exception as e:
            _exc[0] = e

    _t = threading.Thread(target=_connect, daemon=True)
    _t.start()
    _t.join(timeout)
    if _t.is_alive():
        raise MemoryError(
            f"LanceDB connection timed out after {timeout:g} s at {db_path}. "
            "A stale file lock from a previous server process may be blocking access. "
            "Restart your system or kill orphan icx processes to release the lock."
        )
    if _exc[0] is not None:
        raise MemoryError(f"LanceDB connection failed: {_exc[0]}") from _exc[0]
    return _result[0]


ROOT_CAUSE_PATTERNS: set[str] = {
    "stale_cache_reference",
    "missing_null_check",
    "incorrect_transaction_boundary",
    "event_race_condition",
    "schema_drift",
    "auth_scope_mismatch",
    "async_context_leak",
    "missing_index",
    "type_coercion_error",
    "config_env_mismatch",
    "missing_idempotency",
    "cascade_delete_missing",
    "n_plus_one_query",
    "memory_leak",
    "timeout_misconfiguration",
    "pagination_boundary_error",
    "deserialization_contract_break",
    "feature_flag_state_leak",
    "tenant_isolation_breach",
    "retry_storm",
    "uncategorized",
}


class MemoryEntry(BaseModel):
    """User-facing memory record. Never contains secrets or raw API data."""

    id: str
    issue_key: str
    project_key: str
    source_type: str
    issue_type: str
    summary: str
    problem_description: str
    impact: str = ""
    resolution_note: str
    files_changed: list[str]
    resolution_confirmed: bool
    saved_at: str
    tags: list[str] = Field(default_factory=list)
    work_item_type: str = "bug"
    pattern_used: str = ""
    confirmation_count: int = 0
    memory_confidence: float = 0.0

    # Phase 1: root cause classification
    root_cause_pattern: str = "uncategorized"
    pattern_confidence: float = 0.0

    # Phase 1: outcome tracking
    outcome_verified: bool = False
    outcome_feedback_note: str = ""
    negated: bool = False
    negation_reason: str = ""

    # Phase 2: reference reinforcement
    used_by_tickets: list[str] = Field(default_factory=list)
    usage_count: int = 0
    cross_reference_boost: float = 0.0

    # Phase 3: temporal decay
    temporal_decay_factor: float = 1.0

    # Phase 6: semantic drift
    save_context_vector: list[float] = Field(default_factory=list)
    semantic_drift_score: float = 0.0

    # Phase 5: pattern clustering
    pattern_cluster_id: str = ""

    # Phase 1/4: attachment analysis
    attachment_fingerprints: list[str] = Field(default_factory=list)

    # Phase 8: causal chain + ticket text
    causal_chain: dict = Field(default_factory=dict)
    full_ticket_text: str = ""
    attachment_summary: str = ""

    # Tech-stack fingerprint: {dir: {"languages": {...}, "frameworks": {...}, "package_manager": "..."}}
    tech_stack: dict = Field(default_factory=dict)


class MemoryAuditEvent(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    event_type: str  # "reinforced"|"verified"|"negated"|"boost_applied"|"hub_detected"
    source_key: str
    actor_key: str
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    before_boost: float = 0.0
    after_boost: float = 0.0
    before_confidence: float = 0.0
    after_confidence: float = 0.0
    note: str = ""


@dataclass
class MemoryQueryInput:
    """Connector-agnostic query contract. Built from RawIssueData by engine.run()."""

    issue_key: str
    project_key: str
    source_type: str
    summary: str
    description: str
    issue_type: str
    tags: list[str] = field(default_factory=list)
