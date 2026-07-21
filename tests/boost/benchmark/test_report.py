"""Scorecard render - segmented by difficulty + archetype, per-prompt coverage."""
from __future__ import annotations

from icx_engine.boost.benchmark.runner import BenchReport
from icx_engine.boost.benchmark.report import render_scorecard


def _rep():
    return BenchReport(
        rows=[{"id": "p<1>", "archetype": "coding", "difficulty": "underspecified",
               "raw_frac": 0.25, "boosted_frac": 0.75, "delta": 0.5,
               "req_total": 4, "raw_covered": 1, "boosted_covered": 3}],
        raw_avg=0.25, boosted_avg=0.75, lift_pct=200.0, abs_gain_pts=50.0,
        by_difficulty={"underspecified": {"raw": 0.25, "boosted": 0.75, "abs_gain_pts": 50.0,
                                          "lift_pct": 200.0, "n": 1}},
        by_archetype={"coding": {"raw": 0.25, "boosted": 0.75, "abs_gain_pts": 50.0,
                                 "lift_pct": 200.0, "n": 1}})


def test_render_headline_is_underspecified_lift():
    html = render_scorecard(_rep())
    assert html.startswith("<!doctype html")
    assert "200.0%" in html                          # underspecified lift headline
    assert "underspecified" in html
    assert "By difficulty" in html and "By archetype" in html
    assert "1/4" in html and "3/4" in html            # req covered raw -> boosted
    assert "&lt;1&gt;" in html                        # id escaped
    assert all(ord(c) < 128 for c in html)


def test_render_empty_report_no_raise():
    html = render_scorecard(BenchReport())
    assert html.startswith("<!doctype html")
