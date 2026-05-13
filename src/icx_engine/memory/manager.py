from __future__ import annotations
import re
from pathlib import Path

from icx_engine.exceptions import MemoryError

_SAFE_KEY_RE = re.compile(r'^[A-Z][A-Z0-9]*-[0-9]+$')
from icx_engine.memory.embeddings import EmbeddingsManager, VECTOR_DIM
from icx_engine.memory.schema import MemoryEntry, MemoryQueryInput
from icx_engine.models.output import PastInsight

_TABLE_NAME = "memory_entries"
_FTS_FIELDS = ["summary", "problem_description", "resolution_note"]
_DEFAULT_TOP_K = 3
_DEFAULT_MIN_SCORE = 0.65
_RRF_K = 60


def _build_embed_text(entry: MemoryEntry) -> str:
    parts = [entry.summary, entry.problem_description, entry.resolution_note]
    if entry.tags:
        parts.append(" ".join(entry.tags))
    return " ".join(p for p in parts if p)


def _row_to_entry(row: dict) -> MemoryEntry:
    return MemoryEntry(
        id=row["id"],
        issue_key=row["issue_key"],
        project_key=row["project_key"],
        source_type=row["source_type"],
        issue_type=row["issue_type"],
        summary=row["summary"],
        problem_description=row["problem_description"],
        impact=row.get("impact", ""),
        resolution_note=row["resolution_note"],
        files_changed=list(row.get("files_changed") or []),
        resolution_confirmed=bool(row.get("resolution_confirmed", False)),
        saved_at=row["saved_at"],
        tags=list(row.get("tags") or []),
    )


class MemoryManager:
    """Primary interface for all local memory operations."""

    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = db_path or (Path.home() / ".icx" / "memory")
        self._db_path.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._embeddings = EmbeddingsManager()
        self._db = None
        self._table = None

    def _get_table(self):
        if self._table is not None:
            return self._table
        import lancedb  # lazy import
        import pyarrow as pa

        self._db = lancedb.connect(str(self._db_path))
        if _TABLE_NAME in self._db.list_tables():
            self._table = self._db.open_table(_TABLE_NAME)
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
        ])
        self._table = self._db.create_table(_TABLE_NAME, schema=schema)
        for _fts_field in _FTS_FIELDS:
            try:
                self._table.create_fts_index(_fts_field, replace=True)
            except Exception as exc:
                import sys
                print(
                    f"[memory] FTS index on '{_fts_field}' failed ({exc}); "
                    "that field will be excluded from keyword search.",
                    file=sys.stderr,
                )
        return self._table

    def save(self, entry: MemoryEntry) -> None:
        """Save or update a memory entry. One canonical record per issue_key."""
        self._embeddings.ensure_ready()
        vector = self._embeddings.embed(_build_embed_text(entry))

        row = {
            "vector": vector,
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
        }
        table = self._get_table()
        try:
            (
                table.merge_insert("issue_key")
                .when_matched_update_all()
                .when_not_matched_insert_all()
                .execute([row])
            )
        except Exception as exc:
            raise MemoryError(f"Failed to save memory entry for {entry.issue_key}: {exc}") from exc

    def list_entries(
        self,
        project_key: str | None = None,
        source_type: str | None = None,
    ) -> list[MemoryEntry]:
        """Return all entries, newest first. Optionally filter by project_key or source_type."""
        try:
            table = self._get_table()
            rows = table.to_arrow().to_pylist()
        except Exception:
            return []
        entries = [_row_to_entry(r) for r in rows]
        if project_key:
            entries = [e for e in entries if e.project_key == project_key]
        if source_type:
            entries = [e for e in entries if e.source_type == source_type]
        entries.sort(key=lambda e: e.saved_at, reverse=True)
        return entries

    def query(
        self,
        input: MemoryQueryInput,
        top_k: int = _DEFAULT_TOP_K,
        min_score: float = _DEFAULT_MIN_SCORE,
    ) -> list[PastInsight]:
        """Hybrid semantic + keyword search. Returns ranked PastInsight list.
        Falls back to vector-only if FTS index unavailable."""
        try:
            self._embeddings.ensure_ready()
        except MemoryError:
            return []

        query_text = f"{input.summary} {input.description}"
        try:
            vector = self._embeddings.embed(query_text)
        except MemoryError:
            return []

        try:
            table = self._get_table()
            row_count = table.count_rows()
        except Exception:
            return []

        if row_count == 0:
            return []

        fetch_n = min(row_count, top_k * 4)

        # Vector search
        try:
            vec_rows = table.search(vector).limit(fetch_n).to_list()
        except Exception:
            return []

        # FTS search (best-effort - may not be available)
        fts_rows: list[dict] = []
        try:
            fts_rows = (
                table.search(query_text, query_type="fts")
                .limit(fetch_n)
                .to_list()
            )
        except Exception:
            pass

        # RRF merge
        scores: dict[str, float] = {}
        id_to_row: dict[str, dict] = {}

        for rank, row in enumerate(vec_rows):
            rid = row["id"]
            scores[rid] = scores.get(rid, 0.0) + 1.0 / (_RRF_K + rank + 1)
            id_to_row[rid] = row

        for rank, row in enumerate(fts_rows):
            rid = row["id"]
            scores[rid] = scores.get(rid, 0.0) + 1.0 / (_RRF_K + rank + 1)
            id_to_row[rid] = row

        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)

        # Normalize scores to 0-1 range
        max_score = ranked[0][1] if ranked else 1.0

        results: list[PastInsight] = []
        for rid, raw_score in ranked:
            norm_score = round(raw_score / max_score, 4) if max_score > 0 else 0.0
            if norm_score < min_score:
                continue
            row = id_to_row[rid]
            entry = _row_to_entry(row)
            results.append(PastInsight(
                issue_key=entry.issue_key,
                source_type=entry.source_type,
                summary=entry.summary,
                resolution_note=entry.resolution_note,
                files_changed=entry.files_changed,
                similarity_score=norm_score,
                saved_at=entry.saved_at,
            ))

        return results[:top_k]

    def show(self, issue_key: str) -> MemoryEntry | None:
        """Return the full MemoryEntry for one issue_key, or None if not found."""
        try:
            table = self._get_table()
            rows = [r for r in table.to_arrow().to_pylist() if r["issue_key"] == issue_key]
        except Exception:
            rows = []
        if not rows:
            return None
        return _row_to_entry(rows[0])

    def delete(self, issue_key: str) -> None:
        """Remove the entry for issue_key. No-op if not found."""
        normalised = issue_key.strip().upper()
        if not _SAFE_KEY_RE.match(normalised):
            raise MemoryError(
                f"Invalid issue key format: {issue_key!r}. Expected format: PROJ-123"
            )
        try:
            table = self._get_table()
            table.delete(f"issue_key = '{normalised}'")
        except Exception as exc:
            raise MemoryError(f"Failed to delete memory entry for {normalised}: {exc}") from exc

    def clear(self) -> None:
        """Delete all memory entries. Recreates an empty table."""
        try:
            import lancedb

            if self._db is None:
                self._db = lancedb.connect(str(self._db_path))
            if _TABLE_NAME in self._db.list_tables():
                self._db.drop_table(_TABLE_NAME)
            self._table = None
            self._db = None  # force fresh connection on next access
        except Exception as exc:
            raise MemoryError(f"Failed to clear memory: {exc}") from exc

    def status(self) -> dict:
        """Return a stats dict: entry_count, db_path, db_size_bytes, model."""
        try:
            table = self._get_table()
            entry_count = table.count_rows()
        except Exception:
            entry_count = 0

        db_size = sum(f.stat().st_size for f in self._db_path.rglob("*") if f.is_file())

        return {
            "entry_count": entry_count,
            "db_path": str(self._db_path),
            "db_size_bytes": db_size,
            "model": "BAAI/bge-small-en-v1.5",
        }
