"""Tests for Phase 3: temporal decay, _recompute_decay, _cosine_distance."""
from __future__ import annotations
import math
import pytest
from unittest.mock import MagicMock
from pathlib import Path
from datetime import datetime, timedelta, timezone


def _make_manager(tmp_path: Path):
    from icx_engine.memory.manager import MemoryManager
    mgr = MemoryManager(db_path=tmp_path / "mem")
    mgr._embeddings = MagicMock()
    mgr._embeddings.check_ready.return_value = None
    mgr._embeddings.embed.return_value = [0.1] * 768
    return mgr


def _entry_with_age(pattern: str, days: int, usage: int = 0):
    from icx_engine.memory.schema import MemoryEntry
    import uuid
    ts = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    return MemoryEntry(
        id=str(uuid.uuid4()),
        issue_key="DECAY-1",
        project_key="TEST",
        source_type="jira",
        issue_type="Bug",
        summary="Bug",
        problem_description="Desc",
        resolution_note="Fix",
        files_changed=[],
        resolution_confirmed=True,
        saved_at=ts,
        root_cause_pattern=pattern,
        usage_count=usage,
    )


class TestCosineDistance:
    def test_identical_vectors_distance_zero(self):
        from icx_engine.memory.manager import _cosine_distance
        v = [1.0, 0.0, 0.0]
        assert _cosine_distance(v, v) == pytest.approx(0.0, abs=1e-5)

    def test_orthogonal_vectors_distance_one(self):
        from icx_engine.memory.manager import _cosine_distance
        a = [1.0, 0.0]
        b = [0.0, 1.0]
        assert _cosine_distance(a, b) == pytest.approx(1.0, abs=1e-5)

    def test_zero_vector_returns_one(self):
        from icx_engine.memory.manager import _cosine_distance
        z = [0.0, 0.0, 0.0]
        v = [1.0, 0.0, 0.0]
        assert _cosine_distance(z, v) == 1.0


class TestDecayRateMap:
    def test_all_21_patterns_in_map(self):
        from icx_engine.memory.manager import _DECAY_RATE_MAP
        from icx_engine.memory.schema import ROOT_CAUSE_PATTERNS
        for pattern in ROOT_CAUSE_PATTERNS:
            assert pattern in _DECAY_RATE_MAP, f"Pattern {pattern!r} missing from _DECAY_RATE_MAP"

    def test_fast_patterns_have_higher_rate_than_slow(self):
        from icx_engine.memory.manager import _DECAY_RATE_MAP
        assert _DECAY_RATE_MAP["config_env_mismatch"] > _DECAY_RATE_MAP["missing_null_check"]


class TestRecomputeDecay:
    def test_fast_pattern_365_days_decay_le_04(self, tmp_path):
        mgr = _make_manager(tmp_path)
        entry = _entry_with_age("config_env_mismatch", days=365)
        decay = mgr._recompute_decay(entry)
        assert decay <= 0.4

    def test_slow_pattern_365_days_decay_ge_08(self, tmp_path):
        mgr = _make_manager(tmp_path)
        entry = _entry_with_age("missing_null_check", days=365)
        decay = mgr._recompute_decay(entry)
        assert decay >= 0.8

    def test_high_usage_decays_slower(self, tmp_path):
        mgr = _make_manager(tmp_path)
        e_low = _entry_with_age("stale_cache_reference", days=200, usage=0)
        e_high = _entry_with_age("stale_cache_reference", days=200, usage=10)
        d_low = mgr._recompute_decay(e_low)
        d_high = mgr._recompute_decay(e_high)
        assert d_high >= d_low

    def test_decay_never_below_min_decay(self, tmp_path):
        from icx_engine.memory.manager import _MIN_DECAY
        mgr = _make_manager(tmp_path)
        entry = _entry_with_age("config_env_mismatch", days=10000)
        decay = mgr._recompute_decay(entry)
        assert decay >= _MIN_DECAY

    def test_orthogonal_query_vector_adds_drift_penalty(self, tmp_path):
        mgr = _make_manager(tmp_path)
        entry = _entry_with_age("missing_null_check", days=0)
        # Orthogonal save vector
        entry.save_context_vector = [1.0] + [0.0] * 767
        query_vector = [0.0, 1.0] + [0.0] * 766
        decay_with_drift = mgr._recompute_decay(entry, query_vector=query_vector)
        assert entry.semantic_drift_score > 0.4

    def test_empty_save_vector_no_drift_penalty(self, tmp_path):
        mgr = _make_manager(tmp_path)
        entry = _entry_with_age("missing_null_check", days=0)
        entry.save_context_vector = []
        decay = mgr._recompute_decay(entry, query_vector=[0.5] * 768)
        assert entry.semantic_drift_score == 0.0
        assert decay >= 0.99
