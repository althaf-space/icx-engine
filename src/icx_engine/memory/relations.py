"""Issue relationship graph.

Stores cross-references between saved work items in a LanceDB edges table.
Auto-detects shares_file relations (pure set intersection, no ML required).
No imports from connectors/.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from icx_engine.memory.schema import MemoryEntry

_log = logging.getLogger(__name__)
_EDGES_TABLE = "memory_edges"


def _sq(value: str) -> str:
    """Escape a string for use in a LanceDB SQL filter (single-quote escape)."""
    return value.replace("'", "''")


def _norm_file(path: str) -> str:
    return path.replace("\\", "/").lower()


class RelationManager:
    """CRUD interface for the memory_edges LanceDB table."""

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

        if _EDGES_TABLE in existing:
            self._table = self._db.open_table(_EDGES_TABLE)
            return self._table

        schema = pa.schema([
            pa.field("source_key", pa.utf8()),
            pa.field("target_key", pa.utf8()),
            pa.field("relation_type", pa.utf8()),
            pa.field("strength", pa.float64()),
            pa.field("created_at", pa.utf8()),
        ])
        self._table = self._db.create_table(_EDGES_TABLE, schema=schema)
        return self._table

    def add_relation(
        self,
        source_key: str,
        target_key: str,
        relation_type: str,
        strength: float,
    ) -> None:
        """Upsert a bidirectional edge. No-op when source_key == target_key."""
        if source_key == target_key:
            return
        now = datetime.now(timezone.utc).isoformat()
        try:
            table = self._get_table()
            # Remove stale edges before inserting updated strength
            for a, b in [(source_key, target_key), (target_key, source_key)]:
                try:
                    table.delete(
                        f"source_key = '{_sq(a)}' AND target_key = '{_sq(b)}'"
                        f" AND relation_type = '{_sq(relation_type)}'"
                    )
                except Exception:
                    pass
            table.add([
                {
                    "source_key": source_key,
                    "target_key": target_key,
                    "relation_type": relation_type,
                    "strength": strength,
                    "created_at": now,
                },
                {
                    "source_key": target_key,
                    "target_key": source_key,
                    "relation_type": relation_type,
                    "strength": strength,
                    "created_at": now,
                },
            ])
        except Exception as exc:
            _log.warning("Could not save relation %s -> %s: %s", source_key, target_key, exc)

    def get_related(self, issue_key: str) -> list[dict]:
        """Return [{issue_key, relation_type, strength}] sorted by strength desc."""
        try:
            table = self._get_table()
            rows = table.to_arrow().to_pylist()
        except Exception:
            return []
        result = [
            {
                "issue_key": r["target_key"],
                "relation_type": r["relation_type"],
                "strength": r["strength"],
            }
            for r in rows
            if r["source_key"] == issue_key
        ]
        result.sort(key=lambda x: -x["strength"])
        return result

    def get_related_by_files(
        self,
        needle_files: list[str],
        all_entries: list["MemoryEntry"],
        exclude_key: str | None = None,
    ) -> list[dict]:
        """Compute file overlap against all_entries without pre-stored edges.

        Used when no stored edges exist (new ticket). Returns same format as
        get_related(): [{issue_key, relation_type, strength}] sorted by strength desc.
        Strength = shared / max(len(needle_files), len(entry.files_changed)).
        """
        needle = {_norm_file(f) for f in needle_files}
        if not needle:
            return []
        result: list[dict] = []
        seen: set[str] = set()
        for entry in all_entries:
            if exclude_key and entry.issue_key == exclude_key:
                continue
            if entry.issue_key in seen:
                continue
            other = {_norm_file(f) for f in entry.files_changed}
            if not other:
                continue
            shared = needle & other
            if not shared:
                continue
            denominator = max(len(needle), len(other))
            strength = round(len(shared) / denominator, 4)
            result.append({
                "issue_key": entry.issue_key,
                "relation_type": "shares_file",
                "strength": strength,
            })
            seen.add(entry.issue_key)
        result.sort(key=lambda x: -x["strength"])
        return result

    def delete_for(self, issue_key: str) -> None:
        """Remove all edges where issue_key is source or target."""
        try:
            table = self._get_table()
            table.delete(f"source_key = '{_sq(issue_key)}' OR target_key = '{_sq(issue_key)}'")
        except Exception as exc:
            _log.warning("Could not delete edges for %s: %s", issue_key, exc)

    def auto_link(self, entry: "MemoryEntry", all_entries: list["MemoryEntry"]) -> None:
        """Detect shares_file relations between entry and existing entries.

        Strength = shared file count / max(len(entry.files_changed), len(other.files_changed)).
        Skips entries with no files_changed on either side.
        """
        needle_files = {_norm_file(f) for f in entry.files_changed}
        if not needle_files:
            return
        for other in all_entries:
            if other.issue_key == entry.issue_key:
                continue
            other_files = {_norm_file(f) for f in other.files_changed}
            if not other_files:
                continue
            shared = needle_files & other_files
            if not shared:
                continue
            denominator = max(len(needle_files), len(other_files))
            strength = round(len(shared) / denominator, 4)
            self.add_relation(entry.issue_key, other.issue_key, "shares_file", strength)
