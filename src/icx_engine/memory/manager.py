from __future__ import annotations
import logging
import re
import sys
import threading
from pathlib import Path
from typing import Callable

_log = logging.getLogger(__name__)

from icx_engine.exceptions import MemoryError

_SAFE_KEY_RE = re.compile(r'^[A-Z][A-Z0-9]*-[0-9]+$')
_BARE_KEY_RE = re.compile(r'[A-Z][A-Z0-9]*-[0-9]+')
from icx_engine.memory.embeddings import EmbeddingsManager, VECTOR_DIM, EMBEDDING_MODEL
from icx_engine.memory.patterns import PatternManager
from icx_engine.memory.relations import RelationManager
from icx_engine.memory.schema import MemoryEntry, MemoryQueryInput, _sq
from icx_engine.models.output import PastInsight

_TABLE_NAME = "memory_entries"
_FTS_FIELDS = ["summary", "problem_description"]
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
        resolution_note=row.get("resolution_note", ""),
        files_changed=list(row.get("files_changed") or []),
        resolution_confirmed=bool(row.get("resolution_confirmed", False)),
        saved_at=row["saved_at"],
        tags=list(row.get("tags") or []),
        work_item_type=row.get("work_item_type", "bug"),
        pattern_used=row.get("pattern_used", ""),
        confirmation_count=int(row.get("confirmation_count") or 0),
        memory_confidence=float(row.get("memory_confidence") or 0.0),
    )


class MemoryManager:
    """Primary interface for all local memory operations."""

    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = db_path or (Path.home() / ".icx" / "memory")
        self._db_path.mkdir(parents=True, exist_ok=True, **({"mode": 0o700} if sys.platform != "win32" else {}))
        self._embeddings = EmbeddingsManager()
        self._relations = RelationManager(db_path=self._db_path)
        self._patterns = PatternManager(db_path=self._db_path)
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
            if "confirmation_count" not in existing_cols:
                to_add["confirmation_count"] = "cast(0 as int)"
            if "memory_confidence" not in existing_cols:
                to_add["memory_confidence"] = "cast(0.0 as double)"
            if to_add:
                try:
                    self._table.add_columns(to_add)
                except Exception as exc:
                    _log.warning("Could not add Phase-3 columns: %s", exc)
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
        ])
        self._table = self._db.create_table(_TABLE_NAME, schema=schema)
        # FTS indexes deferred to first save() - create_fts_index hangs on empty tables
        return self._table

    def save(self, entry: MemoryEntry, restore: bool = False) -> None:
        """Save or update a memory entry. One canonical record per issue_key.

        restore=True preserves confirmation_count and memory_confidence as-is (used by import).
        """
        self._embeddings.check_ready()
        vector = self._embeddings.embed(_build_embed_text(entry))

        # Track how many times a confirmed resolution has been saved for this entry.
        # Each confirmed save increments the count; confidence grows by 0.25 per confirmation,
        # capped at 1.0. Unconfirmed saves preserve existing counts.
        # restore=True skips recalculation - used by `icx memory import` to preserve exported values.
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
            "confirmation_count": confirmation_count,
            "memory_confidence": memory_confidence,
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

        # Auto-detect file-sharing relations - best-effort, never raises
        try:
            self._relations.auto_link(entry, self.list_entries())
        except Exception as exc:
            _log.warning("auto_link failed for %s: %s", entry.issue_key, exc)

        # Refresh patterns every 10th unique entry - best-effort, never raises
        try:
            count = self._get_table().count_rows()
            if count > 0 and count % 10 == 0:
                project_entries = self.list_entries(project_key=entry.project_key)
                self._patterns.refresh(project_entries, entry.project_key)
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
        query_input: MemoryQueryInput,
        top_k: int = _DEFAULT_TOP_K,
        min_score: float = _DEFAULT_MIN_SCORE,
    ) -> list[PastInsight]:
        """Hybrid semantic + keyword search. Returns ranked PastInsight list.
        Falls back to vector-only if FTS index unavailable."""
        try:
            self._embeddings.check_ready()
        except MemoryError:
            return []

        query_text = f"{query_input.summary} {query_input.description}"
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

        # Tag pre-filter: narrow candidates to those sharing at least one query tag.
        # Falls back to full candidate set when no entry matches any tag.
        if query_input.tags:
            tag_set = {t.lower() for t in query_input.tags}
            tag_matched = {
                rid: row for rid, row in id_to_row.items()
                if tag_set & {t.lower() for t in (row.get("tags") or [])}
            }
            if tag_matched:
                id_to_row = tag_matched
                cosine_sim = {k: v for k, v in cosine_sim.items() if k in tag_matched}

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

        # Blend memory_confidence into RRF score: higher-confidence entries rank above
        # equally-scored peers. Factor of 0.5 keeps confidence as a tie-breaker,
        # not a dominant signal - prevents low-similarity high-confidence entries
        # from outranking genuinely relevant ones.
        ranked = sorted(
            cosine_sim.keys(),
            key=lambda rid: rrf_scores.get(rid, 0.0) * (
                1.0 + 0.5 * float(id_to_row[rid].get("memory_confidence") or 0.0)
            ),
            reverse=True,
        )

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
        if query_input.issue_key:
            bare_key = _extract_bare_key(query_input.issue_key)
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
            table.delete(f"issue_key = '{_sq(normalised)}'")
        except Exception as exc:
            raise MemoryError(f"Failed to delete memory entry for {normalised}: {exc}") from exc
        self._relations.delete_for(normalised)

    def clear(self) -> None:
        """Delete all memory entries, relations, and patterns. Recreates empty tables."""
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
            for tbl in (_TABLE_NAME, "memory_edges", "memory_patterns"):
                if tbl in existing:
                    self._db.drop_table(tbl)
            self._table = None
            self._fts_ready = False
            self._db = None  # force fresh connection on next access
            # Reset cached state in sub-managers so they reconnect cleanly.
            self._relations.reset()
            self._patterns.reset()
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
            "model": EMBEDDING_MODEL,
        }

    def migrate(self, log: Callable[[str], None] | None = None) -> int:
        """Re-embed all entries with the current EMBEDDING_MODEL and VECTOR_DIM.

        Call this after upgrading the embedding model. Dumps all entries, drops
        the table, recreates it with the new schema, then re-embeds and saves
        each entry. Returns the count of migrated entries.
        """
        self._embeddings.check_ready()
        entries = self.list_entries()
        count = len(entries)
        if count == 0:
            return 0
        if log:
            log(f"  migrating {count} work items to {VECTOR_DIM}-dim vectors...")
        self.clear()
        for i, entry in enumerate(entries, 1):
            self.save(entry)
            if log:
                log(f"  [{i}/{count}] {entry.issue_key}")
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
        """Ensure ONNX model files are downloaded and eagerly load the ONNX session.

        Eager loading avoids a 3-10 s lazy-load stall on the first tool call,
        which would block the single-worker memory executor and cause a timeout.
        """
        self._embeddings.check_ready()
        self._embeddings._load_model()
