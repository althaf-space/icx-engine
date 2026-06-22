from __future__ import annotations
import json
import logging
import math
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

_log = logging.getLogger(__name__)

from icx_engine.exceptions import MemoryError

_SAFE_KEY_RE = re.compile(r'^[A-Z][A-Z0-9]*-[0-9]+$')
_BARE_KEY_RE = re.compile(r'[A-Z][A-Z0-9]*-[0-9]+')
from icx_engine.memory.embeddings import EmbeddingsManager, VECTOR_DIM, EMBEDDING_MODEL
from icx_engine.memory.patterns import PatternManager
from icx_engine.memory.relations import RelationManager
from icx_engine.memory.schema import MemoryEntry, MemoryQueryInput, MemoryAuditEvent, _sq, connect_with_timeout
from icx_engine.models.output import PastInsight

_TABLE_NAME = "memory_entries"
_AUDIT_TABLE_NAME = "memory_audit"
_FTS_FIELDS = ["summary", "problem_description"]
_DEFAULT_TOP_K = 3
_DEFAULT_MIN_SCORE = 0.65
_RRF_K = 60

# Temporal decay constants (Phase 3)
_MIN_DECAY: float = 0.2

_DECAY_CLASSES: dict[str, tuple[float, list[str]]] = {
    "fast":   (0.005,  ["config_env_mismatch", "timeout_misconfiguration",
                        "schema_drift", "feature_flag_state_leak"]),
    "medium": (0.002,  ["stale_cache_reference", "missing_index", "n_plus_one_query",
                        "retry_storm", "pagination_boundary_error"]),
    "slow":   (0.0005, ["missing_null_check", "incorrect_transaction_boundary",
                        "async_context_leak", "missing_idempotency", "cascade_delete_missing",
                        "auth_scope_mismatch", "tenant_isolation_breach",
                        "event_race_condition", "memory_leak", "type_coercion_error",
                        "deserialization_contract_break", "uncategorized"]),
}

_DECAY_RATE_MAP: dict[str, float] = {
    pattern: rate
    for rate, patterns in ((v[0], v[1]) for v in _DECAY_CLASSES.values())
    for pattern in patterns
}


def _cosine_distance(a: list[float], b: list[float]) -> float:
    try:
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(y * y for y in b))
        if norm_a == 0 or norm_b == 0:
            return 1.0
        return round(1.0 - dot / (norm_a * norm_b), 6)
    except Exception as exc:
        _log.debug("[memory] cosine distance computation failed: %s", exc)
        return 1.0


def _extract_bare_key(s: str) -> str | None:
    """Extract PROJ-123 style key from a bare key or full URL."""
    m = _BARE_KEY_RE.search(s.upper())
    return m.group(0) if m else None


def _log_debug(msg: str) -> None:
    _log.debug(msg)


def _build_embed_text(entry: MemoryEntry) -> str:
    parts: list[str] = []
    if entry.full_ticket_text:
        parts.append(entry.full_ticket_text[:1000])
    parts.append(entry.summary)
    if entry.problem_description:
        parts.append(entry.problem_description)
    if entry.root_cause_pattern and entry.root_cause_pattern != "uncategorized":
        parts.append(f"root cause: {entry.root_cause_pattern}")
    if entry.attachment_summary:
        parts.append(f"attachments showed: {entry.attachment_summary}")
    if entry.tags:
        parts.extend(entry.tags)
    return " ".join(p for p in parts if p)


def _row_to_entry(row: dict) -> MemoryEntry:
    # Deserialise JSON-encoded columns safely
    causal_chain: dict = {}
    _cc_raw = row.get("causal_chain_json") or ""
    if _cc_raw:
        try:
            causal_chain = json.loads(_cc_raw)
        except Exception as exc:
            _log.debug("[memory] _row_to_entry: causal_chain_json parse failed: %s", exc)

    save_context_vector: list[float] = []
    _sv_raw = row.get("save_context_vector_json") or ""
    if _sv_raw and _sv_raw != "[]":
        try:
            save_context_vector = json.loads(_sv_raw)
        except Exception as exc:
            _log.debug("[memory] _row_to_entry: save_context_vector_json parse failed: %s", exc)

    tech_stack: dict = {}
    _ts_raw = row.get("tech_stack_json") or ""
    if _ts_raw and _ts_raw != "{}":
        try:
            tech_stack = json.loads(_ts_raw)
        except Exception as exc:
            _log.debug("[memory] _row_to_entry: tech_stack_json parse failed: %s", exc)

    return MemoryEntry(
        id=row["id"],
        issue_key=row["issue_key"],
        project_key=row["project_key"],
        source_type=row["source_type"],
        issue_type=row["issue_type"],
        summary=row["summary"],
        problem_description=row["problem_description"],
        impact=row.get("impact", ""),
        resolution_note=row.get("resolution_note", ""),
        files_changed=list(row.get("files_changed") or []),
        resolution_confirmed=bool(row.get("resolution_confirmed", False)),
        saved_at=row["saved_at"],
        tags=list(row.get("tags") or []),
        work_item_type=row.get("work_item_type", "bug"),
        pattern_used=row.get("pattern_used", ""),
        confirmation_count=int(row.get("confirmation_count") or 0),
        memory_confidence=float(row.get("memory_confidence") or 0.0),
        root_cause_pattern=row.get("root_cause_pattern") or "uncategorized",
        pattern_confidence=float(row.get("pattern_confidence") or 0.0),
        outcome_verified=bool(row.get("outcome_verified", False)),
        outcome_feedback_note=row.get("outcome_feedback_note") or "",
        negated=bool(row.get("negated", False)),
        negation_reason=row.get("negation_reason") or "",
        used_by_tickets=list(row.get("used_by_tickets") or []),
        usage_count=int(row.get("usage_count") or 0),
        cross_reference_boost=float(row.get("cross_reference_boost") or 0.0),
        temporal_decay_factor=float(row.get("temporal_decay_factor") or 1.0),
        save_context_vector=save_context_vector,
        semantic_drift_score=float(row.get("semantic_drift_score") or 0.0),
        pattern_cluster_id=row.get("pattern_cluster_id") or "",
        attachment_fingerprints=list(row.get("attachment_fingerprints") or []),
        causal_chain=causal_chain,
        full_ticket_text=row.get("full_ticket_text") or "",
        attachment_summary=row.get("attachment_summary") or "",
        tech_stack=tech_stack,
    )


class MemoryManager:
    """Primary interface for all local memory operations."""

    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = db_path or (Path.home() / ".icx" / "memory")
        self._db_path.mkdir(parents=True, exist_ok=True)
        if sys.platform != "win32":
            self._db_path.chmod(0o700)
        self._embeddings = EmbeddingsManager()
        self._relations = RelationManager(db_path=self._db_path)
        self._patterns = PatternManager(db_path=self._db_path)
        self._db = None
        self._table = None
        self._audit_tbl = None
        self._fts_ready = False  # deferred until first save - avoids hang on empty table
        self._in_migrate: bool = False

    def _get_table(self):
        if self._table is not None:
            return self._table
        import pyarrow as pa

        self._db = connect_with_timeout(self._db_path)
        tables_response = self._db.list_tables()
        existing = (
            tables_response.tables
            if hasattr(tables_response, "tables")
            else list(tables_response)
        )
        if _TABLE_NAME in existing:
            self._table = self._db.open_table(_TABLE_NAME)
            try:
                existing_dim = self._table.schema.field("vector").type.list_size
                if existing_dim != VECTOR_DIM:
                    raise MemoryError(
                        f"Memory vector dimension mismatch: stored={existing_dim}, "
                        f"current={VECTOR_DIM}. "
                        "Run `icx memory migrate` to re-embed all saved work items "
                        "with the new model."
                    )
            except (KeyError, AttributeError):
                pass
            existing_cols = {f.name for f in self._table.schema}
            to_add: dict[str, str] = {}
            # Phase 0 (existing) columns
            if "confirmation_count" not in existing_cols:
                to_add["confirmation_count"] = "cast(0 as int)"
            if "memory_confidence" not in existing_cols:
                to_add["memory_confidence"] = "cast(0.0 as double)"
            # Phase 1 columns
            if "root_cause_pattern" not in existing_cols:
                to_add["root_cause_pattern"] = "'uncategorized'"
            if "pattern_confidence" not in existing_cols:
                to_add["pattern_confidence"] = "cast(0.0 as double)"
            if "outcome_verified" not in existing_cols:
                to_add["outcome_verified"] = "cast(false as bool)"
            if "outcome_feedback_note" not in existing_cols:
                to_add["outcome_feedback_note"] = "''"
            if "negated" not in existing_cols:
                to_add["negated"] = "cast(false as bool)"
            if "negation_reason" not in existing_cols:
                to_add["negation_reason"] = "''"
            # Phase 2 columns
            if "usage_count" not in existing_cols:
                to_add["usage_count"] = "cast(0 as int)"
            if "cross_reference_boost" not in existing_cols:
                to_add["cross_reference_boost"] = "cast(0.0 as double)"
            # Phase 3 columns
            if "temporal_decay_factor" not in existing_cols:
                to_add["temporal_decay_factor"] = "cast(1.0 as double)"
            # Phase 5 columns
            if "pattern_cluster_id" not in existing_cols:
                to_add["pattern_cluster_id"] = "''"
            # Phase 6 columns
            if "save_context_vector_json" not in existing_cols:
                to_add["save_context_vector_json"] = "'[]'"
            if "semantic_drift_score" not in existing_cols:
                to_add["semantic_drift_score"] = "cast(0.0 as double)"
            # Phase 8 columns
            if "causal_chain_json" not in existing_cols:
                to_add["causal_chain_json"] = "'{}'"
            if "full_ticket_text" not in existing_cols:
                to_add["full_ticket_text"] = "''"
            if "attachment_summary" not in existing_cols:
                to_add["attachment_summary"] = "''"
            # Tech-stack fingerprint
            if "tech_stack_json" not in existing_cols:
                to_add["tech_stack_json"] = "'{}'"
            if to_add:
                try:
                    self._table.add_columns(to_add)
                except Exception as exc:
                    _log.warning("Could not add new memory columns: %s", exc)
            return self._table

        schema = pa.schema([
            pa.field("vector", pa.list_(pa.float32(), VECTOR_DIM)),
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
            pa.field("confirmation_count", pa.int32()),
            pa.field("memory_confidence", pa.float64()),
            # Phase 1
            pa.field("root_cause_pattern", pa.utf8()),
            pa.field("pattern_confidence", pa.float64()),
            pa.field("outcome_verified", pa.bool_()),
            pa.field("outcome_feedback_note", pa.utf8()),
            pa.field("negated", pa.bool_()),
            pa.field("negation_reason", pa.utf8()),
            # Phase 2
            pa.field("used_by_tickets", pa.list_(pa.utf8())),
            pa.field("usage_count", pa.int32()),
            pa.field("cross_reference_boost", pa.float64()),
            # Phase 3
            pa.field("temporal_decay_factor", pa.float64()),
            # Phase 5
            pa.field("pattern_cluster_id", pa.utf8()),
            pa.field("attachment_fingerprints", pa.list_(pa.utf8())),
            # Phase 6
            pa.field("save_context_vector_json", pa.utf8()),
            pa.field("semantic_drift_score", pa.float64()),
            # Phase 8
            pa.field("causal_chain_json", pa.utf8()),
            pa.field("full_ticket_text", pa.utf8()),
            pa.field("attachment_summary", pa.utf8()),
            pa.field("tech_stack_json", pa.utf8()),
        ])
        self._table = self._db.create_table(_TABLE_NAME, schema=schema)
        return self._table

    def _audit_table(self):
        """Return the memory_audit LanceDB table, creating it if absent."""
        if self._audit_tbl is not None:
            return self._audit_tbl
        if self._db is None:
            self._get_table()  # ensure DB is connected
        import pyarrow as pa
        tables_response = self._db.list_tables()
        existing = (
            tables_response.tables
            if hasattr(tables_response, "tables")
            else list(tables_response)
        )
        if _AUDIT_TABLE_NAME in existing:
            self._audit_tbl = self._db.open_table(_AUDIT_TABLE_NAME)
            return self._audit_tbl
        schema = pa.schema([
            pa.field("id", pa.utf8()),
            pa.field("event_type", pa.utf8()),
            pa.field("source_key", pa.utf8()),
            pa.field("actor_key", pa.utf8()),
            pa.field("timestamp", pa.utf8()),
            pa.field("before_boost", pa.float64()),
            pa.field("after_boost", pa.float64()),
            pa.field("before_confidence", pa.float64()),
            pa.field("after_confidence", pa.float64()),
            pa.field("note", pa.utf8()),
        ])
        self._audit_tbl = self._db.create_table(_AUDIT_TABLE_NAME, schema=schema)
        return self._audit_tbl

    def _log_audit(self, event: MemoryAuditEvent) -> None:
        """Write one audit row. Best-effort - never raises."""
        try:
            self._audit_table().add([event.model_dump()])
        except Exception as e:
            _log_debug(f"[memory_audit] write failed: {e}")

    def _find_by_key(self, issue_key: str) -> MemoryEntry | None:
        try:
            table = self._get_table()
            rows = table.search().where(
                f"issue_key = '{_sq(issue_key.upper())}'", prefilter=True
            ).limit(1).to_list()
            return _row_to_entry(rows[0]) if rows else None
        except Exception as exc:
            _log.debug("[memory] _find_by_key failed for %s: %s", issue_key, exc)
            return None

    def _upsert_entry(self, entry: MemoryEntry) -> None:
        """Update an existing MemoryEntry by issue_key (merge_insert pattern)."""
        try:
            row = self._entry_to_row(entry, vector=None)
            table = self._get_table()
            (
                table.merge_insert("issue_key")
                .when_matched_update_all()
                .execute([row])
            )
        except Exception as e:
            _log_debug(f"[memory] upsert failed for {entry.issue_key}: {e}")

    def _entry_to_row(self, entry: MemoryEntry, vector: list | None) -> dict:
        """Convert MemoryEntry to a LanceDB row dict."""
        row: dict = {
            "id": entry.id,
            "issue_key": entry.issue_key,
            "project_key": entry.project_key,
            "source_type": entry.source_type,
            "issue_type": entry.issue_type,
            "summary": entry.summary,
            "problem_description": entry.problem_description,
            "impact": entry.impact,
            "resolution_note": entry.resolution_note,
            "files_changed": entry.files_changed,
            "resolution_confirmed": entry.resolution_confirmed,
            "saved_at": entry.saved_at,
            "tags": entry.tags,
            "work_item_type": entry.work_item_type,
            "pattern_used": entry.pattern_used,
            "confirmation_count": entry.confirmation_count,
            "memory_confidence": entry.memory_confidence,
            "root_cause_pattern": entry.root_cause_pattern or "uncategorized",
            "pattern_confidence": entry.pattern_confidence,
            "outcome_verified": entry.outcome_verified,
            "outcome_feedback_note": entry.outcome_feedback_note or "",
            "negated": entry.negated,
            "negation_reason": entry.negation_reason or "",
            "used_by_tickets": list(entry.used_by_tickets or []),
            "usage_count": entry.usage_count,
            "cross_reference_boost": entry.cross_reference_boost,
            "temporal_decay_factor": entry.temporal_decay_factor,
            "pattern_cluster_id": entry.pattern_cluster_id or "",
            "attachment_fingerprints": list(entry.attachment_fingerprints or []),
            "save_context_vector_json": json.dumps(entry.save_context_vector) if entry.save_context_vector else "[]",
            "semantic_drift_score": entry.semantic_drift_score,
            "causal_chain_json": json.dumps(entry.causal_chain) if entry.causal_chain else "{}",
            "full_ticket_text": entry.full_ticket_text or "",
            "attachment_summary": entry.attachment_summary or "",
            "tech_stack_json": json.dumps(entry.tech_stack) if entry.tech_stack else "{}",
        }
        if vector is not None:
            row["vector"] = vector
        return row

    def _recompute_boost(self, entry: MemoryEntry) -> float:
        """Recalculate cross_reference_boost for an entry."""
        base = min(1.0, entry.usage_count * 0.15)
        pattern_cluster_bonus = 0.0
        try:
            table = self._get_table()
            safe_pattern = _sq(entry.root_cause_pattern or "uncategorized")
            safe_id = _sq(entry.id)
            siblings = table.search().where(
                f"root_cause_pattern = '{safe_pattern}' AND id != '{safe_id}'",
                prefilter=True,
            ).to_list()
            entry_citations = set(entry.used_by_tickets)
            if entry_citations:
                pattern_cluster_bonus = sum(
                    0.05 for s in siblings
                    if entry_citations & set(s.get("used_by_tickets") or [])
                )
                pattern_cluster_bonus = min(0.30, pattern_cluster_bonus)
        except Exception:
            pass
        negation_penalty = -0.4 if entry.negated else 0.0
        return max(0.0, min(1.0, base + pattern_cluster_bonus + negation_penalty))

    def _recompute_decay(
        self,
        entry: MemoryEntry,
        query_vector: list[float] | None = None,
    ) -> float:
        """Compute temporal_decay_factor combining time-based decay and semantic drift."""
        try:
            saved_at = datetime.fromisoformat(entry.saved_at.replace("Z", "+00:00"))
            if saved_at.tzinfo is None:
                saved_at = saved_at.replace(tzinfo=timezone.utc)
            days = max(0, (datetime.now(timezone.utc) - saved_at.astimezone(timezone.utc)).days)
        except Exception:
            days = 0

        base_rate = _DECAY_RATE_MAP.get(entry.root_cause_pattern, 0.002)
        resistance = 1.0 - min(0.8, (entry.usage_count // 5) * 0.20)
        effective_rate = base_rate * resistance
        time_decay = max(_MIN_DECAY, 1.0 - effective_rate * days)

        drift_penalty = 0.0
        if query_vector and len(entry.save_context_vector) == VECTOR_DIM:
            drift = _cosine_distance(entry.save_context_vector, query_vector)
            entry.semantic_drift_score = round(drift, 4)
            if drift > 0.4:
                drift_penalty = min(0.3, (drift - 0.4) / 0.4 * 0.3)

        combined = max(_MIN_DECAY, time_decay - drift_penalty)
        entry.temporal_decay_factor = round(combined, 4)
        return entry.temporal_decay_factor

    def _recalculate_sibling_boosts(self, entry: MemoryEntry, used_by_key: str) -> int:
        """Recalculate cross_reference_boost for siblings sharing pattern + citation."""
        try:
            table = self._get_table()
            safe_pattern = _sq(entry.root_cause_pattern or "uncategorized")
            safe_id = _sq(entry.id)
            siblings = table.search().where(
                f"root_cause_pattern = '{safe_pattern}' AND id != '{safe_id}'",
                prefilter=True,
            ).to_list()
        except Exception:
            return 0

        updated = 0
        for s_dict in siblings:
            if used_by_key in (s_dict.get("used_by_tickets") or []):
                s_entry = _row_to_entry(s_dict)
                new_boost = self._recompute_boost(s_entry)
                if abs(new_boost - s_entry.cross_reference_boost) > 0.001:
                    s_entry.cross_reference_boost = new_boost
                    self._upsert_entry(s_entry)
                    updated += 1
        return updated

    def reinforce_usage(self, source_key: str, used_by_key: str) -> dict:
        """Record that source_key was used when solving used_by_key."""
        entry = self._find_by_key(source_key)
        if entry is None:
            return {"error": "entry not found", "source_key": source_key}

        if used_by_key not in entry.used_by_tickets:
            entry.used_by_tickets.append(used_by_key)
            entry.usage_count = len(entry.used_by_tickets)

        if entry.usage_count >= 10:
            entry.memory_confidence = 1.0
        elif entry.usage_count >= 5:
            entry.memory_confidence = max(entry.memory_confidence, 0.75)

        before_boost = entry.cross_reference_boost
        entry.cross_reference_boost = self._recompute_boost(entry)
        self._upsert_entry(entry)
        try:
            self._log_audit(MemoryAuditEvent(
                event_type="reinforced",
                source_key=source_key,
                actor_key=used_by_key,
                before_boost=before_boost,
                after_boost=entry.cross_reference_boost,
            ))
        except Exception:
            pass

        siblings_updated = self._recalculate_sibling_boosts(entry, used_by_key)
        return {
            "source_key": source_key,
            "usage_count": entry.usage_count,
            "cross_reference_boost": entry.cross_reference_boost,
            "siblings_updated": siblings_updated,
        }

    def verify_resolution(self, issue_key: str, feedback_note: str) -> dict:
        """Record that a resolution was confirmed working by the developer."""
        entry = self._find_by_key(issue_key)
        if entry is None:
            return {"error": "entry not found", "issue_key": issue_key}

        before_conf = entry.memory_confidence
        entry.outcome_verified = True
        entry.outcome_feedback_note = feedback_note[:500]
        entry.confirmation_count = entry.confirmation_count + 1
        entry.memory_confidence = min(1.0, entry.confirmation_count * 0.25)

        self._upsert_entry(entry)
        try:
            self._log_audit(MemoryAuditEvent(
                event_type="verified",
                source_key=issue_key,
                actor_key="developer",
                before_confidence=before_conf,
                after_confidence=entry.memory_confidence,
                note=feedback_note[:200],
            ))
        except Exception:
            pass
        return {
            "issue_key": issue_key,
            "memory_confidence": entry.memory_confidence,
            "confirmation_count": entry.confirmation_count,
        }

    def negate_resolution(self, issue_key: str, reason: str) -> dict:
        """Record that a resolution was confirmed wrong by the developer."""
        entry = self._find_by_key(issue_key)
        if entry is None:
            return {"error": "entry not found", "issue_key": issue_key}

        before_boost = entry.cross_reference_boost
        entry.negated = True
        entry.negation_reason = reason[:500]
        entry.outcome_verified = False
        entry.cross_reference_boost = self._recompute_boost(entry)

        self._upsert_entry(entry)
        try:
            self._log_audit(MemoryAuditEvent(
                event_type="negated",
                source_key=issue_key,
                actor_key="developer",
                before_boost=before_boost,
                after_boost=entry.cross_reference_boost,
                note=reason[:200],
            ))
        except Exception:
            pass

        propagated_to: list[str] = []
        for citing_key in list(entry.used_by_tickets):
            citing = self._find_by_key(citing_key)
            if citing is None:
                continue
            old_boost = citing.cross_reference_boost
            citing.cross_reference_boost = max(0.0, citing.cross_reference_boost - 0.05)
            self._upsert_entry(citing)
            try:
                self._log_audit(MemoryAuditEvent(
                    event_type="negated",
                    source_key=citing_key,
                    actor_key=issue_key,
                    before_boost=old_boost,
                    after_boost=citing.cross_reference_boost,
                    note=f"propagated from negation of {issue_key}",
                ))
            except Exception:
                pass
            propagated_to.append(citing_key)

        return {
            "issue_key": issue_key,
            "negated": True,
            "cross_reference_boost": entry.cross_reference_boost,
            "propagated_penalty_to": propagated_to,
        }

    def get_audit_trail(self, issue_key: str, limit: int = 20) -> list[dict]:
        """Return audit events for issue_key, sorted descending by timestamp."""
        try:
            table = self._audit_table()
            rows = table.search().where(
                f"source_key = '{_sq(issue_key.upper())}'", prefilter=True
            ).limit(limit).to_list()
            rows.sort(key=lambda r: r.get("timestamp", ""), reverse=True)
            return rows
        except Exception as e:
            return [{"error": str(e)}]

    def save(self, entry: MemoryEntry, restore: bool = False) -> None:
        """Save or update a memory entry. One canonical record per issue_key.

        restore=True preserves confirmation_count and memory_confidence as-is (used by import).
        """
        self._embeddings.check_ready()
        vector = self._embeddings.embed(_build_embed_text(entry))

        confirmation_count = entry.confirmation_count
        memory_confidence = entry.memory_confidence
        if entry.resolution_confirmed and not restore:
            try:
                existing = self.show(entry.issue_key)
                base = existing.confirmation_count if existing else 0
                confirmation_count = base + 1
            except Exception:
                confirmation_count = 1
            memory_confidence = min(1.0, round(confirmation_count * 0.25, 4))

        # Build save-time context vector for drift detection (Phase 6)
        context_snapshot = (
            f"{entry.summary} "
            f"{entry.root_cause_pattern} "
            f"{' '.join(entry.files_changed)}"
        )
        try:
            save_context_vector = self._embeddings.embed(context_snapshot)
        except Exception:
            save_context_vector = []

        # Build the full entry with computed values
        entry_to_save = MemoryEntry(
            **{
                **entry.model_dump(),
                "confirmation_count": confirmation_count,
                "memory_confidence": memory_confidence,
                "save_context_vector": save_context_vector,
            }
        )

        row = self._entry_to_row(entry_to_save, vector=vector)
        try:
            table = self._get_table()
            (
                table.merge_insert("issue_key")
                .when_matched_update_all()
                .when_not_matched_insert_all()
                .execute([row])
            )
            if not self._fts_ready:
                for _fts_field in _FTS_FIELDS:
                    try:
                        table.create_fts_index(_fts_field, replace=True)
                    except Exception as exc:
                        _log.warning("FTS index on '%s' failed: %s; field excluded from keyword search", _fts_field, exc)
                self._fts_ready = True
        except Exception as exc:
            raise MemoryError(f"Failed to save memory entry for {entry.issue_key}: {exc}") from exc

        if not self._in_migrate:
            try:
                self._relations.auto_link(entry, self.list_entries())
            except Exception as exc:
                _log.warning("auto_link failed for %s: %s", entry.issue_key, exc)

        # Refresh patterns every 5th unique entry (Phase 5 lowered from 10)
        try:
            count = self._get_table().count_rows()
            if count >= 5 and count % 5 == 0:
                project_entries = self.list_entries(project_key=entry.project_key)
                self._patterns.refresh(project_entries, entry.project_key, manager=self)
        except Exception as exc:
            _log.warning("Pattern refresh failed after save of %s: %s", entry.issue_key, exc)

    def list_entries(
        self,
        project_key: str | None = None,
        source_type: str | None = None,
    ) -> list[MemoryEntry]:
        """Return all entries, newest first. Optionally filter by project_key or source_type."""
        try:
            table = self._get_table()
            rows = table.to_arrow().to_pylist()
        except Exception as exc:
            _log.debug("[memory] list_entries failed: %s", exc)
            return []
        entries = [_row_to_entry(r) for r in rows]
        if project_key:
            _pk = project_key.upper()
            if _SAFE_KEY_RE.match(_pk):
                _pk = _pk.split("-", 1)[0]
            entries = [e for e in entries if e.project_key.upper() == _pk]
        if source_type:
            entries = [e for e in entries if e.source_type == source_type]
        entries.sort(key=lambda e: e.saved_at, reverse=True)
        return entries

    def query(
        self,
        query_input: MemoryQueryInput,
        top_k: int = _DEFAULT_TOP_K,
        min_score: float = _DEFAULT_MIN_SCORE,
    ) -> list[PastInsight]:
        """Hybrid semantic + keyword search. Returns ranked PastInsight list.
        Falls back to vector-only if FTS index unavailable."""
        smart = self.query_smart(query_input, top_k=top_k, min_score=min_score)
        results = smart.get("results", [])
        insights = []
        for r in results:
            insights.append(PastInsight(
                issue_key=r["issue_key"],
                source_type=r.get("source_type", ""),
                summary=r.get("summary", ""),
                resolution_note=r.get("resolution_note", ""),
                files_changed=r.get("files_changed", []),
                similarity_score=r.get("similarity_score", 0.0),
                saved_at=r.get("saved_at", ""),
                work_item_type=r.get("work_item_type", "bug"),
                pattern_used=r.get("pattern_used", ""),
                tech_stack=r.get("tech_stack", {}),
            ))
        return insights

    def query_smart(
        self,
        query_input: MemoryQueryInput,
        top_k: int = _DEFAULT_TOP_K,
        min_score: float = _DEFAULT_MIN_SCORE,
    ) -> dict:
        """Hybrid search with decay, boost, and negative signal separation.

        Returns: {results: list[dict], negative_signals: list[dict], decay_applied: bool}
        """
        try:
            self._embeddings.check_ready()
        except MemoryError:
            return {"results": [], "negative_signals": [], "decay_applied": False}

        query_text = f"{query_input.summary} {query_input.description}"
        try:
            query_vector = self._embeddings.embed(query_text)
        except MemoryError:
            return {"results": [], "negative_signals": [], "decay_applied": False}

        try:
            table = self._get_table()
            row_count = table.count_rows()
        except Exception:
            return {"results": [], "negative_signals": [], "decay_applied": False}

        if row_count == 0:
            return {"results": [], "negative_signals": [], "decay_applied": False}

        fetch_n = min(row_count, top_k * 4)

        try:
            vec_rows = table.search(query_vector).metric("cosine").limit(fetch_n).to_list()
        except Exception:
            return {"results": [], "negative_signals": [], "decay_applied": False}

        id_to_row: dict[str, dict] = {}
        cosine_sim: dict[str, float] = {}
        for row in vec_rows:
            rid = row["id"]
            dist = row.get("_distance", 1.0)
            sim = round(1.0 - dist, 4)
            if sim < min_score:
                continue
            cosine_sim[rid] = sim
            id_to_row[rid] = row

        if not cosine_sim:
            return {"results": [], "negative_signals": [], "decay_applied": False}

        if query_input.tags:
            tag_set = {t.lower() for t in query_input.tags}
            tag_matched = {
                rid: row for rid, row in id_to_row.items()
                if tag_set & {t.lower() for t in (row.get("tags") or [])}
            }
            if tag_matched:
                id_to_row = tag_matched
                cosine_sim = {k: v for k, v in cosine_sim.items() if k in tag_matched}

        fts_rows: list[dict] = []
        try:
            fts_rows = (
                table.search(query_text, query_type="fts")
                .limit(fetch_n)
                .to_list()
            )
        except Exception:
            pass

        rrf_scores: dict[str, float] = {}
        for rank, row in enumerate(vec_rows):
            rid = row["id"]
            if rid in cosine_sim:
                rrf_scores[rid] = rrf_scores.get(rid, 0.0) + 1.0 / (_RRF_K + rank + 1)

        for rank, row in enumerate(fts_rows):
            rid = row["id"]
            if rid in cosine_sim:
                id_to_row[rid] = row
                rrf_scores[rid] = rrf_scores.get(rid, 0.0) + 1.0 / (_RRF_K + rank + 1)

        # Apply decay and adjusted scoring
        entries_with_scores: list[tuple[MemoryEntry, float, float]] = []
        entries_to_update: list[MemoryEntry] = []

        for rid, sim in cosine_sim.items():
            entry = _row_to_entry(id_to_row[rid])
            decay = self._recompute_decay(entry, query_vector=query_vector)
            rrf = rrf_scores.get(rid, 0.0)
            adjusted = (
                rrf
                * decay
                * (1.0 + 0.5 * entry.memory_confidence)
                * (1.0 + entry.cross_reference_boost)
            )
            entries_with_scores.append((entry, sim, round(adjusted, 6)))
            entries_to_update.append(entry)

        # Write back decay + drift scores best-effort
        for entry in entries_to_update:
            try:
                self._upsert_entry(entry)
            except Exception:
                pass

        # Sort by adjusted score
        entries_with_scores.sort(key=lambda t: t[2], reverse=True)

        primary_results: list[dict] = []
        negative_signals: list[dict] = []

        for entry, sim, adj in entries_with_scores[:top_k * 2]:
            d = {
                "issue_key": entry.issue_key,
                "source_type": entry.source_type,
                "summary": entry.summary,
                "resolution_note": entry.resolution_note,
                "files_changed": entry.files_changed,
                "similarity_score": sim,
                "adjusted_score": adj,
                "saved_at": entry.saved_at,
                "work_item_type": entry.work_item_type,
                "pattern_used": entry.pattern_used,
                "root_cause_pattern": entry.root_cause_pattern,
                "pattern_confidence": entry.pattern_confidence,
                "usage_count": entry.usage_count,
                "cross_reference_boost": entry.cross_reference_boost,
                "temporal_decay_factor": entry.temporal_decay_factor,
                "semantic_drift_score": entry.semantic_drift_score,
                "outcome_verified": entry.outcome_verified,
                "negated": entry.negated,
                "negation_reason": entry.negation_reason,
                "memory_confidence": entry.memory_confidence,
                "tech_stack": entry.tech_stack,
            }
            if entry.negated:
                negative_signals.append(d)
            else:
                primary_results.append(d)

        # Exact key match override
        if query_input.issue_key:
            bare_key = _extract_bare_key(query_input.issue_key)
            if bare_key:
                already = {r["issue_key"] for r in primary_results}
                if bare_key not in already:
                    try:
                        exact = self.show(bare_key)
                        if exact and not exact.negated:
                            primary_results.insert(0, {
                                "issue_key": exact.issue_key,
                                "source_type": exact.source_type,
                                "summary": exact.summary,
                                "resolution_note": exact.resolution_note,
                                "files_changed": exact.files_changed,
                                "similarity_score": 1.0,
                                "adjusted_score": 1.0,
                                "saved_at": exact.saved_at,
                                "work_item_type": exact.work_item_type,
                                "pattern_used": exact.pattern_used,
                                "root_cause_pattern": exact.root_cause_pattern,
                                "pattern_confidence": exact.pattern_confidence,
                                "usage_count": exact.usage_count,
                                "cross_reference_boost": exact.cross_reference_boost,
                                "temporal_decay_factor": exact.temporal_decay_factor,
                                "semantic_drift_score": exact.semantic_drift_score,
                                "outcome_verified": exact.outcome_verified,
                                "negated": exact.negated,
                                "negation_reason": exact.negation_reason,
                                "memory_confidence": exact.memory_confidence,
                                "tech_stack": exact.tech_stack,
                            })
                    except Exception:
                        pass

        return {
            "results": primary_results[:top_k],
            "negative_signals": negative_signals,
            "decay_applied": True,
        }

    def show(self, issue_key: str) -> MemoryEntry | None:
        """Return the full MemoryEntry for one issue_key, or None if not found."""
        return self._find_by_key(issue_key)

    def delete(self, issue_key: str) -> None:
        """Remove the entry for issue_key. No-op if not found."""
        normalised = issue_key.strip().upper()
        if not _SAFE_KEY_RE.match(normalised):
            raise MemoryError(
                f"Invalid issue key format: {issue_key!r}. Expected format: PROJ-123"
            )
        try:
            table = self._get_table()
            table.delete(f"issue_key = '{_sq(normalised)}'")
        except Exception as exc:
            raise MemoryError(f"Failed to delete memory entry for {normalised}: {exc}") from exc
        self._relations.delete_for(normalised)

    def clear(self) -> None:
        """Delete all memory entries, relations, and patterns. Recreates empty tables."""
        try:
            if self._db is None:
                self._db = connect_with_timeout(self._db_path)
            tables_response = self._db.list_tables()
            existing = (
                tables_response.tables
                if hasattr(tables_response, "tables")
                else list(tables_response)
            )
            for tbl in (_TABLE_NAME, "memory_edges", "memory_patterns", _AUDIT_TABLE_NAME):
                if tbl in existing:
                    self._db.drop_table(tbl)
            self._table = None
            self._audit_tbl = None
            self._fts_ready = False
            self._db = None
            self._relations.reset()
            self._patterns.reset()
        except Exception as exc:
            raise MemoryError(f"Failed to clear memory: {exc}") from exc

    def status(self) -> dict:
        """Return a stats dict including new Phase 1-7 metrics."""
        try:
            table = self._get_table()
            rows = table.to_arrow().to_pylist()
            entry_count = len(rows)
        except Exception:
            rows = []
            entry_count = 0

        db_size = 0
        for _f in self._db_path.rglob("*"):
            try:
                if _f.is_file():
                    db_size += _f.stat().st_size
            except OSError:
                pass

        outcome_verified = sum(1 for r in rows if r.get("outcome_verified"))
        negated = sum(1 for r in rows if r.get("negated"))
        boosts = [float(r.get("cross_reference_boost") or 0.0) for r in rows]
        avg_boost = round(sum(boosts) / len(boosts), 4) if boosts else 0.0
        decays = [float(r.get("temporal_decay_factor") or 1.0) for r in rows]
        avg_decay = round(sum(decays) / len(decays), 4) if decays else 1.0

        patterns = self.get_patterns()
        citation_hubs = sum(1 for p in patterns if p.get("pattern_type") == "citation_hub")
        semantic_signals = sum(1 for p in patterns if p.get("pattern_type") == "semantic_signal")

        return {
            "entry_count": entry_count,
            "outcome_verified": outcome_verified,
            "negated": negated,
            "avg_cross_reference_boost": avg_boost,
            "avg_temporal_decay_factor": avg_decay,
            "citation_hubs_detected": citation_hubs,
            "semantic_patterns_detected": semantic_signals,
            "db_path": str(self._db_path),
            "db_size_bytes": db_size,
            "model": EMBEDDING_MODEL,
        }

    def migrate(self, log: Callable[[str], None] | None = None) -> int:
        """Re-embed all entries with the current EMBEDDING_MODEL and VECTOR_DIM."""
        self._embeddings.check_ready()
        entries = self.list_entries()
        count = len(entries)
        if count == 0:
            return 0
        if log:
            log(f"  migrating {count} work items to {VECTOR_DIM}-dim vectors...")
        self.clear()
        self._in_migrate = True
        try:
            for i, entry in enumerate(entries, 1):
                self.save(entry, restore=True)
                if log:
                    log(f"  [{i}/{count}] {entry.issue_key}")
        finally:
            self._in_migrate = False
        try:
            all_entries = self.list_entries()
            for entry in all_entries:
                self._relations.auto_link(entry, all_entries)
        except Exception as exc:
            _log.warning("auto_link after migrate failed: %s", exc)
        return count

    def get_related(
        self,
        issue_key: str | None,
        project_key: str | None,
        files: list[str] | None,
    ) -> list[dict]:
        """Return related work items via stored edges or file-overlap fallback."""
        related: list[dict] = []
        if issue_key:
            related = self._relations.get_related(issue_key)
            if project_key:
                in_project = {e.issue_key for e in self.list_entries(project_key=project_key)}
                related = [r for r in related if r["issue_key"] in in_project]
        if not related and files:
            all_entries = self.list_entries(project_key=project_key)
            related = self._relations.get_related_by_files(files, all_entries, exclude_key=issue_key)
        return related

    def get_patterns(self, project_key: str | None = None) -> list[dict]:
        """Return stored patterns, optionally filtered by project_key."""
        return self._patterns.get_patterns(project_key=project_key)

    def prewarm(self) -> None:
        """Ensure ONNX model files are downloaded and eagerly load the ONNX session."""
        self._embeddings.check_ready()
        self._embeddings._load_model()
