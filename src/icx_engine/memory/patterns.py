"""Auto pattern detection over saved work items.

Pure detection logic + PatternManager for persistence.
Triggered every 5th unique entry by MemoryManager.save().
No ML, no connector imports, no new dependencies.
"""
from __future__ import annotations

import json
import logging
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from icx_engine.memory.schema import MemoryEntry
    from icx_engine.memory.manager import MemoryManager

from icx_engine.memory.schema import _sq, connect_with_timeout

_log = logging.getLogger(__name__)
_PATTERNS_TABLE = "memory_patterns"
_MIN_ENTRIES = 3
_FREQUENT_FILE_THRESHOLD = 0.30
_DOMINANT_TAG_THRESHOLD = 0.20
_TOP_TYPE_THRESHOLD = 0.50
_MAX_PATTERNS_PER_TYPE = 5

_CITATION_HUB_MIN_GROUP = 3
_SEMANTIC_MIN_GROUP = 3
_SEMANTIC_WORD_FREQ_THRESHOLD = 0.60
_SEMANTIC_FILE_FREQ_THRESHOLD = 0.50
_SEMANTIC_MAX_SIGNAL_WORDS = 5

_STOP_WORDS: set[str] = {
    "the", "a", "an", "is", "it", "in", "to", "of", "and", "or",
    "was", "were", "be", "been", "has", "have", "had", "do", "did",
    "does", "not", "but", "for", "with", "this", "that", "they",
    "we", "our", "your", "i", "on", "at", "by", "from", "as", "are",
}


def _norm_file(path: str) -> str:
    return path.replace("\\", "/").lower()


def detect_patterns(entries: list["MemoryEntry"]) -> list[dict]:
    """Derive statistical patterns from a list of MemoryEntry objects.

    Returns list of pattern dicts: {pattern_type, label, evidence, entry_count}.
    Returns [] when fewer than _MIN_ENTRIES entries exist.
    """
    if len(entries) < _MIN_ENTRIES:
        return []

    total = len(entries)
    patterns: list[dict] = []

    # Frequent files: files present in >= 30% of entries
    file_counts: Counter = Counter()
    for e in entries:
        seen: set[str] = set()
        for f in e.files_changed:
            key = _norm_file(f)
            if key not in seen:
                file_counts[key] += 1
                seen.add(key)

    for f, count in file_counts.most_common(_MAX_PATTERNS_PER_TYPE):
        pct = count / total
        if pct < _FREQUENT_FILE_THRESHOLD:
            break
        patterns.append({
            "pattern_type": "frequent_file",
            "label": f"{f} touched in {count}/{total} work items ({pct:.0%})",
            "evidence": {"file": f, "count": count, "percentage": round(pct, 4)},
            "entry_count": total,
        })

    # Dominant tags: tags present in >= 20% of entries
    tag_counts: Counter = Counter()
    for e in entries:
        for t in e.tags:
            tag_counts[t.lower()] += 1

    for tag, count in tag_counts.most_common(_MAX_PATTERNS_PER_TYPE):
        pct = count / total
        if pct < _DOMINANT_TAG_THRESHOLD:
            break
        patterns.append({
            "pattern_type": "dominant_tag",
            "label": f"Tag '{tag}' in {count}/{total} work items ({pct:.0%})",
            "evidence": {"tag": tag, "count": count, "percentage": round(pct, 4)},
            "entry_count": total,
        })

    # Top work item type: dominant type if > 50% share
    type_counts: Counter = Counter(e.work_item_type for e in entries)
    if type_counts:
        top_type, top_count = type_counts.most_common(1)[0]
        pct = top_count / total
        if pct > _TOP_TYPE_THRESHOLD:
            patterns.append({
                "pattern_type": "top_work_item_type",
                "label": f"'{top_type}' is {top_count}/{total} work items ({pct:.0%})",
                "evidence": {"type": top_type, "count": top_count, "percentage": round(pct, 4)},
                "entry_count": total,
            })

    # Citation hub detection (Phase 5)
    pattern_groups: dict[str, list] = defaultdict(list)
    for e in entries:
        rcp = getattr(e, "root_cause_pattern", "uncategorized") or "uncategorized"
        pattern_groups[rcp].append(e)

    citation_hub_patterns: list[dict] = []
    for pattern_name, group in pattern_groups.items():
        if len(group) < _CITATION_HUB_MIN_GROUP:
            continue
        citation_counter: Counter = Counter()
        for entry in group:
            for cited_key in getattr(entry, "used_by_tickets", []) or []:
                citation_counter[cited_key] += 1

        threshold = max(2, len(group) * 0.30)
        hubs = [(key, count) for key, count in citation_counter.items() if count >= threshold]
        for hub_key, hub_count in hubs:
            citation_hub_patterns.append({
                "pattern_type": "citation_hub",
                "label": f"{hub_key} cited by {hub_count}/{len(group)} {pattern_name} resolutions",
                "evidence": {
                    "hub_key": hub_key,
                    "root_cause_pattern": pattern_name,
                    "citation_count": hub_count,
                    "group_size": len(group),
                    "citation_rate": round(hub_count / len(group), 2),
                },
                "entry_count": len(group),
            })

    patterns.extend(citation_hub_patterns)

    # Semantic pattern detection (Phase 9)
    semantic_candidates: dict[str, list] = defaultdict(list)
    for e in entries:
        ft = getattr(e, "full_ticket_text", "") or ""
        rcp = getattr(e, "root_cause_pattern", "uncategorized") or "uncategorized"
        if ft and rcp != "uncategorized":
            semantic_candidates[rcp].append(e)

    semantic_patterns: list[dict] = []
    for root_cause, group in semantic_candidates.items():
        if len(group) < _SEMANTIC_MIN_GROUP:
            continue

        word_freq: dict[str, int] = defaultdict(int)
        for entry in group:
            seen_in_entry: set[str] = set()
            for w in (getattr(entry, "full_ticket_text", "") or "").lower().split():
                clean = w.strip(".,!?;:'\"()")
                if len(clean) > 3 and clean not in _STOP_WORDS:
                    seen_in_entry.add(clean)
            for clean in seen_in_entry:
                word_freq[clean] += 1

        threshold_words = max(2, len(group) * _SEMANTIC_WORD_FREQ_THRESHOLD)
        signal_words = [
            w for w, count in sorted(word_freq.items(), key=lambda x: -x[1])
            if count >= threshold_words
        ][:_SEMANTIC_MAX_SIGNAL_WORDS]

        if not signal_words:
            continue

        file_freq: dict[str, int] = defaultdict(int)
        for entry in group:
            for f in (getattr(entry, "files_changed", []) or []):
                file_freq[f] += 1

        if not file_freq:
            continue

        top_file = max(file_freq, key=lambda k: file_freq[k])
        top_file_count = file_freq[top_file]

        if top_file_count < len(group) * _SEMANTIC_FILE_FREQ_THRESHOLD:
            continue

        semantic_patterns.append({
            "pattern_type": "semantic_signal",
            "label": f"tickets mentioning {', '.join(signal_words[:3])} -> {root_cause}",
            "evidence": {
                "signal_words": signal_words,
                "root_cause_pattern": root_cause,
                "top_fix_file": top_file,
                "top_fix_file_rate": round(top_file_count / len(group), 2),
                "group_size": len(group),
                "sample_tickets": [getattr(e, "issue_key", "?") for e in group[:3]],
            },
            "entry_count": len(group),
        })

    patterns.extend(semantic_patterns)
    return patterns


class PatternManager:
    """Persists detected patterns in the memory_patterns LanceDB table."""

    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = db_path or (Path.home() / ".icx" / "memory")
        self._db = None
        self._table = None

    def _get_table(self):
        if self._table is not None:
            return self._table
        import pyarrow as pa

        if self._db is None:
            self._db = connect_with_timeout(self._db_path)

        tables_response = self._db.list_tables()
        existing = (
            tables_response.tables
            if hasattr(tables_response, "tables")
            else list(tables_response)
        )

        if _PATTERNS_TABLE in existing:
            self._table = self._db.open_table(_PATTERNS_TABLE)
            return self._table

        schema = pa.schema([
            pa.field("id", pa.utf8()),
            pa.field("project_key", pa.utf8()),
            pa.field("pattern_type", pa.utf8()),
            pa.field("label", pa.utf8()),
            pa.field("evidence", pa.utf8()),   # JSON-encoded dict
            pa.field("entry_count", pa.int32()),
            pa.field("detected_at", pa.utf8()),
        ])
        self._table = self._db.create_table(_PATTERNS_TABLE, schema=schema)
        return self._table

    def _apply_hub_boosts(self, hub_patterns: list[dict], manager) -> int:
        """Apply reinforce_usage for citation hub entries."""
        boosted = 0
        for hp in hub_patterns:
            try:
                evidence = hp.get("evidence", {})
                if isinstance(evidence, str):
                    evidence = json.loads(evidence)
            except (KeyError, ValueError):
                continue
            hub_key = evidence.get("hub_key", "")
            citation_count = evidence.get("citation_count", 0)
            if not hub_key:
                continue
            entry = manager._find_by_key(hub_key)
            if entry is None:
                continue
            if entry.usage_count >= citation_count:
                boosted += 1
                continue
            synthetic_key = f"__pattern_hub__{evidence.get('root_cause_pattern', 'unknown')}"
            manager.reinforce_usage(hub_key, synthetic_key)
            from icx_engine.memory.schema import MemoryAuditEvent
            manager._log_audit(MemoryAuditEvent(
                event_type="hub_detected",
                source_key=hub_key,
                actor_key=synthetic_key,
                note=f"citation_hub: {citation_count} citations in {evidence.get('root_cause_pattern')} group",
            ))
            boosted += 1
        return boosted

    def refresh(self, entries: list["MemoryEntry"], project_key: str, manager: "MemoryManager | None" = None) -> None:
        """Replace stored patterns for project_key with freshly detected ones."""
        try:
            table = self._get_table()
            try:
                table.delete(f"project_key = '{_sq(project_key)}'")
            except Exception as exc:
                # Clearing prior patterns is best-effort - a delete failure must not block
                # inserting fresh ones below, but stays traceable rather than vanishing silently.
                _log.debug("[memory] pattern delete failed for project %s: %s", project_key, exc)

            raw_patterns = detect_patterns(entries)
            if not raw_patterns:
                return

            now = datetime.now(timezone.utc).isoformat()
            rows = [
                {
                    "id": str(uuid.uuid4()),
                    "project_key": project_key,
                    "pattern_type": p["pattern_type"],
                    "label": p["label"],
                    "evidence": json.dumps(p["evidence"]),
                    "entry_count": p.get("entry_count", len(entries)),
                    "detected_at": now,
                }
                for p in raw_patterns
            ]
            table.add(rows)

            if manager:
                hub_patterns = [p for p in raw_patterns if p["pattern_type"] == "citation_hub"]
                if hub_patterns:
                    try:
                        self._apply_hub_boosts(hub_patterns, manager)
                    except Exception as exc:
                        _log.warning("Hub boost application failed: %s", exc)
        except Exception as exc:
            _log.warning("Pattern refresh failed for %s: %s", project_key, exc)

    def reset(self) -> None:
        """Disconnect from the LanceDB table so the next access reconnects cleanly."""
        self._table = None
        self._db = None

    def get_patterns(self, project_key: str | None = None) -> list[dict]:
        """Return stored patterns, optionally filtered to one project_key."""
        try:
            table = self._get_table()
            total = table.count_rows()
            if total == 0:
                return []
            if project_key:
                rows = table.search().where(
                    f"project_key = '{_sq(project_key)}'", prefilter=True
                ).limit(total).to_list()
            else:
                rows = table.search().limit(total).to_list()
        except Exception:
            return []

        result = []
        for r in rows:
            try:
                evidence = json.loads(r["evidence"])
            except Exception:
                evidence = {}
            result.append({
                "project_key": r["project_key"],
                "pattern_type": r["pattern_type"],
                "label": r["label"],
                "evidence": evidence,
                "entry_count": r["entry_count"],
                "detected_at": r["detected_at"],
            })
        return result
