from icx_engine.testing.benchmark.metrics import RunMetrics, CoverageScore
from icx_engine.testing.benchmark.report import scorecard_html


def _m(v):
    return RunMetrics(app="magik_ui", url="u", coverage=CoverageScore(1, 1, 1, 1, 1),
                      misfire_rate=0.0, flakiness=0.0, speed_seconds=1.0, authoring_actions=0,
                      total_tests=5, real_findings=0, visual=v)


def test_scorecard_shows_visual_when_present():
    html = scorecard_html([_m({"checked": 3, "baselines": 1, "regressions": 0})])
    assert "Visual regression" in html and "magik_ui" in html
    assert all(ord(c) < 128 for c in html)


def test_scorecard_omits_visual_when_empty():
    html = scorecard_html([_m({})])
    assert "Visual regression" not in html
