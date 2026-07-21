"""Boost A/B variants - all deterministic, lead with the task, include the checklist dims."""
from __future__ import annotations

from icx_engine.boost.variants import VARIANTS


def test_all_variants_lead_with_task_and_are_deterministic():
    for name, fn in VARIANTS.items():
        a = fn("add a login feature", "security")
        b = fn("add a login feature", "security")
        assert a == b, f"{name} not deterministic"
        assert "add a login feature" in a, f"{name} dropped the task"
        # security checklist dims present (reminders, generic)
        assert "authoriz" in a.lower() or "authentication" in a.lower(), f"{name} missing dims"


def test_variant_set_has_champion():
    assert "champion" in VARIANTS
