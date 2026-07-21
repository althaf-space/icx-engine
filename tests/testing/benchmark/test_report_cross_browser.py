from icx_engine.testing.benchmark.metrics import RunMetrics, CoverageScore
from icx_engine.testing.benchmark.report import scorecard_html


def _m(xb):
    return RunMetrics(app="magik_ui", url="u", coverage=CoverageScore(1, 1, 1, 1, 1),
                      misfire_rate=0.0, flakiness=0.0, speed_seconds=1.0, authoring_actions=0,
                      total_tests=5, real_findings=0, cross_browser=xb)


def test_scorecard_shows_cross_browser_when_present():
    html = scorecard_html([_m({"chromium": 1.0, "firefox": 1.0, "webkit:Pixel 7": 0.8})])
    assert "chromium" in html and "firefox" in html and "Pixel 7" in html
    assert "Cross-browser" in html or "cross-browser" in html
    assert all(ord(c) < 128 for c in html)


def test_scorecard_omits_cross_browser_when_empty():
    html = scorecard_html([_m({})])
    # no cross-browser section header when there is no cross-browser data
    assert "Cross-browser" not in html
