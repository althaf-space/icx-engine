"""Self-validation for the memory factories.

Ties the factory confidence math to the real MemoryManager transitions by
running the manager and comparing - so the factories can't drift from the code
they model (the drift that hid BUG-1).
"""
from __future__ import annotations

from unittest.mock import MagicMock

from memory.factories import (
    make_entry,
    make_reinforced_entry,
    make_verified_entry,
    reinforced_confidence,
    verified_confidence,
)


def _make_manager(tmp_path):
    from icx_engine.memory.manager import MemoryManager
    mgr = MemoryManager(db_path=tmp_path / "mem")
    mgr._embeddings = MagicMock()
    mgr._embeddings.check_ready.return_value = None
    mgr._embeddings.embed.return_value = [0.1] * 768
    return mgr


class TestFactoryProducesValidEntries:
    def test_make_entry_is_schema_valid(self):
        e = make_entry("ABC-9")
        assert e.issue_key == "ABC-9"
        assert e.project_key == "ABC"
        assert e.memory_confidence == 0.0

    def test_overrides_apply(self):
        e = make_entry("ABC-1", summary="custom", files_changed=["x.py", "y.py"])
        assert e.summary == "custom"
        assert e.files_changed == ["x.py", "y.py"]


class TestReinforcedFactoryMatchesManager:
    def test_reinforced_confidence_matches_real_reinforce(self, tmp_path):
        # Run real reinforce_usage N times on a fresh entry, compare to the
        # factory's predicted memory_confidence.
        for n in (3, 5, 10):
            mgr = _make_manager(tmp_path / f"n{n}")
            mgr.save(make_entry("PROJ-1"))
            for i in range(n):
                mgr.reinforce_usage("PROJ-1", f"CITE-{i}")
            real = mgr._find_by_key("PROJ-1")
            assert real.usage_count == n
            assert real.memory_confidence == reinforced_confidence(n), (
                f"factory reinforced_confidence diverged from manager at n={n}"
            )

    def test_make_reinforced_entry_shape(self):
        e = make_reinforced_entry("PROJ-1", usage_count=10)
        assert e.usage_count == 10
        assert len(e.used_by_tickets) == 10
        assert e.memory_confidence == 1.0


class TestVerifiedFactoryMatchesManager:
    def test_verified_confidence_matches_real_verify(self, tmp_path):
        for n in (1, 2, 4):
            mgr = _make_manager(tmp_path / f"v{n}")
            mgr.save(make_entry("PROJ-1"))
            for _ in range(n):
                mgr.verify_resolution("PROJ-1", "confirmed")
            real = mgr._find_by_key("PROJ-1")
            assert real.confirmation_count == n
            assert real.memory_confidence == verified_confidence(n), (
                f"factory verified_confidence diverged from manager at n={n}"
            )

    def test_make_verified_entry_shape(self):
        e = make_verified_entry("PROJ-1", confirmation_count=2)
        assert e.outcome_verified is True
        assert e.confirmation_count == 2
        assert e.memory_confidence == 0.5


class TestBug1RegressionOnRealisticState:
    def test_verify_does_not_regress_reinforced_confidence(self, tmp_path):
        # The exact BUG-1 scenario, seeded from the realistic reinforced state
        # the factory provides instead of a fresh 0.0 entry.
        mgr = _make_manager(tmp_path)
        # restore=True persists the reinforced state as-is; plain save() would
        # recompute memory_confidence for a resolution_confirmed entry.
        mgr.save(make_reinforced_entry("PROJ-1", usage_count=10), restore=True)
        assert mgr._find_by_key("PROJ-1").memory_confidence == 1.0
        result = mgr.verify_resolution("PROJ-1", "Confirmed working")
        assert result["memory_confidence"] == 1.0  # must NOT drop to 0.25
