"""Deterministic rubric grader."""
from __future__ import annotations

from icx_engine.boost.benchmark.corpus import RubricItem
from icx_engine.boost.benchmark.grader import grade


def _rubric():
    return [RubricItem("validation", ["validat", "required"]),
            RubricItem("status", ["201", "created"], weight=2)]


def test_full_marks():
    out = "Validate the input; return 201 Created."
    g = grade(out, _rubric())
    assert g.score == g.max_score == 3
    assert g.fraction == 1.0
    assert not g.misses


def test_partial():
    g = grade("just validate it", _rubric())
    assert g.score == 1 and g.max_score == 3
    assert "status" in " ".join(g.misses).lower()


def test_case_insensitive():
    assert grade("VALIDATION REQUIRED, 201", _rubric()).score == 3


def test_empty_rubric_is_full():
    g = grade("anything", [])
    assert g.fraction == 1.0


def test_empty_output_scores_zero():
    assert grade("", _rubric()).score == 0
