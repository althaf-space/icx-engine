"""Test self-heal rendering in the benchmark scorecard."""
from icx_engine.testing.benchmark.metrics import RunMetrics, CoverageScore
from icx_engine.testing.benchmark.report import scorecard_html


def _m(sh):
    return RunMetrics(app="magik_ui", url="u", coverage=CoverageScore(1, 1, 1, 1, 1),
                      misfire_rate=0.0, flakiness=0.0, speed_seconds=1.0, authoring_actions=0,
                      total_tests=5, real_findings=0, self_heal=sh)


def test_scorecard_shows_self_heal_when_present():
    html = scorecard_html([_m({"injected": 4, "recovered": 4, "rate": 1.0})])
    assert "Self-healing" in html
    assert "magik_ui" in html and ("100%" in html or "4" in html)
    assert all(ord(c) < 128 for c in html)


def test_scorecard_omits_self_heal_when_empty():
    html = scorecard_html([_m({})])
    assert "Self-healing" not in html
