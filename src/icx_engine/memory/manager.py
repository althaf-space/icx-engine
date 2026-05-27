from __future__ import annotations
import logging
import re
import sys
import threading
from pathlib import Path

_log = logging.getLogger(__name__)

from icx_engine.exceptions import MemoryError

_SAFE_KEY_RE = re.compile(r'^[A-Z][A-Z0-9]*-[0-9]+$')
_BARE_KEY_RE = re.compile(r'[A-Z][A-Z0-9]*-[0-9]+')
from icx_engine.memory.embeddings import EmbeddingsManager, VECTOR_DIM
from icx_engine.memory.schema import MemoryEntry, MemoryQueryInput
from icx_engine.models.output import PastInsight

_TABLE_NAME = "memory_entries"
_FTS_FIELDS = ["summary", "problem_description", "resolution_note"]
_DEFAULT_TOP_K = 3
_DEFAULT_MIN_SCORE = 0.65
_RRF_K = 60


def _extract_bare_key(s: str) -> str | None:
    """Extract PROJ-123 style key from a bare key or full URL."""
    m = _BARE_KEY_RE.search(s.upper())
    return m.group(0) if m else None


def _build_embed_text(entry: MemoryEntry) -> str:
    # Embed only problem description + tags. Resolution note is data we return,
    # not data we match on - including it skews embeddings away from problem space
    # and hurts cross-project similarity matching.
    parts = [entry.summary, entry.problem_description]
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
        work_item_type=row.get("work_item_type", "bug"),
        pattern_used=row.get("pattern_used", ""),
    )


class MemoryManager:
    """Primary interface for all local memory operations."""

    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = db_path or (Path.home() / ".icx" / "memory")
        self._db_path.mkdir(parents=True, exist_ok=True, **({"mode": 0o700} if sys.platform != "win32" else {}))
        self._embeddings = EmbeddingsManager()
        self._db = None
        self._table = None
        self._fts_ready = False  # deferred until first save - avoids hang on empty table

    def _get_table(self):
        if self._table is not None:
            return self._table
        import lancedb  # lazy import
        import pyarrow as pa

        _result: list = [None]
        _exc: list = [None]

        def _connect() -> None:
            try:
                _result[0] = lancedb.connect(str(self._db_path))
            except Exception as e:
                _exc[0] = e

        _t = threading.Thread(target=_connect, daemon=True)
        _t.start()
        _t.join(3.0)
        if _t.is_alive():
            raise MemoryError(
                f"LanceDB connection timed out after 3 s at {self._db_path}. "
                "A stale file lock from a previous server process may be blocking access. "
                "Restart your system or kill orphan icx processes to release the lock."
            )
        if _exc[0] is not None:
            raise MemoryError(f"LanceDB connection failed: {_exc[0]}") from _exc[0]
        self._db = _result[0]
        tables_response = self._db.list_tables()
        existing = (
            tables_response.tables
            if hasattr(tables_response, "tables")
            else list(tables_response)
        )
        if _TABLE_NAME in existing:
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
            pa.field("work_item_type", pa.utf8()),
            pa.field("pattern_used", pa.utf8()),
        ])
        self._table = self._db.create_table(_TABLE_NAME, schema=schema)
        # FTS indexes deferred to first save() - create_fts_index hangs on empty tables
        return self._table

    def save(self, entry: MemoryEntry) -> None:
        """Save or update a memory entry. One canonical record per issue_key."""
        self._embeddings.check_ready()
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
            "work_item_type": entry.work_item_type,
            "pattern_used": entry.pattern_used,
        }
        try:
            table = self._get_table()
            (
                table.merge_insert("issue_key")
                .when_matched_update_all()
                .when_not_matched_insert_all()
                .execute([row])
            )
            # Create FTS indexes after data exists - safe now, deferred from _get_table()
            if not self._fts_ready:
                for _fts_field in _FTS_FIELDS:
                    try:
                        table.create_fts_index(_fts_field, replace=True)
                    except Exception as exc:
                        _log.warning("FTS index on '%s' failed: %s; field excluded from keyword search", _fts_field, exc)
                self._fts_ready = True
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
            self._embeddings.check_ready()
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

        # Vector search with cosine metric - _distance = cosine distance (1 - similarity)
        try:
            vec_rows = table.search(vector).metric("cosine").limit(fetch_n).to_list()
        except Exception:
            return []

        # Build per-entry cosine similarity from actual distance. Filter below threshold here
        # so irrelevant entries never make it into ranking regardless of how many entries exist.
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
            return []

        # FTS search (best-effort) - only over already-qualified candidates
        fts_rows: list[dict] = []
        try:
            fts_rows = (
                table.search(query_text, query_type="fts")
                .limit(fetch_n)
                .to_list()
            )
        except Exception:
            pass

        # RRF over qualified candidates only - used purely for ranking, not filtering
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

        ranked = sorted(cosine_sim.keys(), key=lambda rid: rrf_scores.get(rid, 0.0), reverse=True)

        results: list[PastInsight] = []
        for rid in ranked[:top_k]:
            entry = _row_to_entry(id_to_row[rid])
            results.append(PastInsight(
                issue_key=entry.issue_key,
                source_type=entry.source_type,
                summary=entry.summary,
                resolution_note=entry.resolution_note,
                files_changed=entry.files_changed,
                similarity_score=cosine_sim[rid],
                saved_at=entry.saved_at,
                work_item_type=entry.work_item_type,
                pattern_used=entry.pattern_used,
            ))

        # Exact key match: always surface the saved entry for this specific ticket
        # regardless of similarity score. Handles the case where the same ticket
        # is queried again after being saved - bypasses embedding comparison entirely.
        exact_match: PastInsight | None = None
        if input.issue_key:
            bare_key = _extract_bare_key(input.issue_key)
            if bare_key:
                try:
                    exact_entry = self.show(bare_key)
                    if exact_entry:
                        exact_match = PastInsight(
                            issue_key=exact_entry.issue_key,
                            source_type=exact_entry.source_type,
                            summary=exact_entry.summary,
                            resolution_note=exact_entry.resolution_note,
                            files_changed=exact_entry.files_changed,
                            similarity_score=1.0,
                            saved_at=exact_entry.saved_at,
                            work_item_type=exact_entry.work_item_type,
                            pattern_used=exact_entry.pattern_used,
                        )
                except Exception:
                    pass

        if exact_match:
            # Prepend exact match, remove duplicate if semantic search also found it
            results = [exact_match] + [r for r in results if r.issue_key != exact_match.issue_key]

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
            tables_response = self._db.list_tables()
            existing = (
                tables_response.tables
                if hasattr(tables_response, "tables")
                else list(tables_response)
            )
            if _TABLE_NAME in existing:
                self._db.drop_table(_TABLE_NAME)
            self._table = None
            self._fts_ready = False
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

        db_size = 0
        for _f in self._db_path.rglob("*"):
            try:
                if _f.is_file():
                    db_size += _f.stat().st_size
            except OSError:
                pass

        return {
            "entry_count": entry_count,
            "db_path": str(self._db_path),
            "db_size_bytes": db_size,
            "model": "BAAI/bge-small-en-v1.5",
        }

    def prewarm(self) -> None:
        """Ensure ONNX model files are downloaded and eagerly load the ONNX session.

        Eager loading avoids a 3-10 s lazy-load stall on the first tool call,
        which would block the single-worker memory executor and cause a timeout.
        """
        self._embeddings.check_ready()
        try:
            self._embeddings._load_model()
        except Exception:
            raise
