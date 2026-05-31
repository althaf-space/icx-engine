"""Auto pattern detection over saved work items.

Pure detection logic + PatternManager for persistence.
Triggered every 10th unique entry by MemoryManager.save().
No ML, no connector imports, no new dependencies.
"""
from __future__ import annotations

import json
import logging
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from icx_engine.memory.schema import MemoryEntry

_log = logging.getLogger(__name__)
_PATTERNS_TABLE = "memory_patterns"


def _sq(value: str) -> str:
    """Escape a string for use in a LanceDB SQL filter (single-quote escape)."""
    return value.replace("'", "''")
_MIN_ENTRIES = 3
_FREQUENT_FILE_THRESHOLD = 0.30
_DOMINANT_TAG_THRESHOLD = 0.20
_TOP_TYPE_THRESHOLD = 0.50
_MAX_PATTERNS_PER_TYPE = 5


def _norm_file(path: str) -> str:
    return path.replace("\\", "/").lower()


def detect_patterns(entries: list["MemoryEntry"]) -> list[dict]:
    """Derive statistical patterns from a list of MemoryEntry objects.

    Returns list of pattern dicts: {pattern_type, label, evidence (dict)}.
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
            })

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
        import lancedb
        import pyarrow as pa

        if self._db is None:
            self._db = lancedb.connect(str(self._db_path))

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

    def refresh(self, entries: list["MemoryEntry"], project_key: str) -> None:
        """Replace stored patterns for project_key with freshly detected ones."""
        try:
            table = self._get_table()
            try:
                table.delete(f"project_key = '{_sq(project_key)}'")
            except Exception:
                pass

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
                    "entry_count": len(entries),
                    "detected_at": now,
                }
                for p in raw_patterns
            ]
            table.add(rows)
        except Exception as exc:
            _log.warning("Pattern refresh failed for %s: %s", project_key, exc)

    def get_patterns(self, project_key: str | None = None) -> list[dict]:
        """Return stored patterns, optionally filtered to one project_key."""
        try:
            table = self._get_table()
            rows = table.to_arrow().to_pylist()
        except Exception:
            return []

        if project_key:
            rows = [r for r in rows if r["project_key"] == project_key]

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
