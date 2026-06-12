"""Tests for Phase 6: semantic drift detection, save_context_vector, cosine distance."""
from __future__ import annotations
import pytest
from unittest.mock import MagicMock, patch
from pathlib import Path


def _make_manager(tmp_path: Path, embed_fn=None):
    from icx_engine.memory.manager import MemoryManager
    mgr = MemoryManager(db_path=tmp_path / "mem")
    mgr._embeddings = MagicMock()
    mgr._embeddings.check_ready.return_value = None
    if embed_fn:
        mgr._embeddings.embed.side_effect = embed_fn
    else:
        mgr._embeddings.embed.return_value = [0.1] * 768
    return mgr


def _save_and_get(mgr, issue_key: str, files=None):
    from icx_engine.memory.schema import MemoryEntry
    import uuid
    e = MemoryEntry(
        id=str(uuid.uuid4()),
        issue_key=issue_key,
        project_key="TEST",
        source_type="jira",
        issue_type="Bug",
        summary="Bug in service",
        problem_description="Null pointer in auth flow",
        resolution_note="Added null check",
        files_changed=files or ["src/auth.py"],
        resolution_confirmed=True,
        saved_at="2026-01-01T00:00:00",
        root_cause_pattern="missing_null_check",
    )
    mgr.save(e)
    return mgr._find_by_key(issue_key)


class TestSaveContextVector:
    def test_save_stores_context_vector(self, tmp_path):
        mgr = _make_manager(tmp_path)
        entry = _save_and_get(mgr, "PROJ-1")
        assert len(entry.save_context_vector) == 768

    def test_save_vector_is_float_list(self, tmp_path):
        mgr = _make_manager(tmp_path)
        entry = _save_and_get(mgr, "PROJ-1")
        assert all(isinstance(v, float) for v in entry.save_context_vector)


class TestDriftDetection:
    def test_matching_query_vector_low_drift(self, tmp_path):
        mgr = _make_manager(tmp_path)
        entry = _save_and_get(mgr, "PROJ-1")
        # Same vector = distance 0
        entry.save_context_vector = [1.0 / (768 ** 0.5)] * 768
        query_vector = [1.0 / (768 ** 0.5)] * 768
        mgr._recompute_decay(entry, query_vector=query_vector)
        assert entry.semantic_drift_score < 0.1

    def test_orthogonal_query_vector_high_drift(self, tmp_path):
        mgr = _make_manager(tmp_path)
        entry = _save_and_get(mgr, "PROJ-1")
        entry.save_context_vector = [1.0] + [0.0] * 767
        query_vector = [0.0, 1.0] + [0.0] * 766
        mgr._recompute_decay(entry, query_vector=query_vector)
        assert entry.semantic_drift_score > 0.5

    def test_empty_save_vector_no_penalty(self, tmp_path):
        mgr = _make_manager(tmp_path)
        entry = _save_and_get(mgr, "PROJ-1")
        entry.save_context_vector = []
        initial_drift = entry.semantic_drift_score
        mgr._recompute_decay(entry, query_vector=[0.5] * 768)
        assert entry.semantic_drift_score == 0.0

    def test_combined_decay_never_below_min_decay(self, tmp_path):
        from icx_engine.memory.manager import _MIN_DECAY
        mgr = _make_manager(tmp_path)
        entry = _save_and_get(mgr, "PROJ-1")
        entry.save_context_vector = [1.0] + [0.0] * 767
        entry.saved_at = "2020-01-01T00:00:00"
        entry.root_cause_pattern = "config_env_mismatch"
        query_vector = [0.0, 1.0] + [0.0] * 766
        decay = mgr._recompute_decay(entry, query_vector=query_vector)
        assert decay >= _MIN_DECAY

    def test_cosine_zero_vector_safe(self):
        from icx_engine.memory.manager import _cosine_distance
        z = [0.0] * 768
        v = [0.1] * 768
        result = _cosine_distance(z, v)
        assert result == 1.0
