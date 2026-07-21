"""Test a11y table rendering in the benchmark scorecard."""
from icx_engine.testing.benchmark.metrics import RunMetrics, CoverageScore
from icx_engine.testing.benchmark.report import scorecard_html


def _m(a):
    return RunMetrics(app="magik_ui", url="u", coverage=CoverageScore(1, 1, 1, 1, 1),
                      misfire_rate=0.0, flakiness=0.0, speed_seconds=1.0, authoring_actions=0,
                      total_tests=5, real_findings=0, a11y=a)


def test_scorecard_shows_a11y_when_present():
    html = scorecard_html([_m({"violations": 5, "critical": 3, "serious": 1})])
    assert "Accessibility" in html and "magik_ui" in html
    assert all(ord(c) < 128 for c in html)


def test_scorecard_omits_a11y_when_empty():
    html = scorecard_html([_m({})])
    assert "Accessibility (measured)" not in html
