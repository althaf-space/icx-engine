from icx_engine.testing.benchmark.metrics import RunMetrics, CoverageScore
from icx_engine.testing.benchmark.report import scorecard_html, render_scorecard


def _m():
    return RunMetrics(app="magik_ui", url="http://x",
                      coverage=CoverageScore(1.0, 0.9, 9, 10, 9),
                      misfire_rate=0.0, flakiness=0.0, speed_seconds=5.5,
                      authoring_actions=0, total_tests=25, real_findings=1)


def test_scorecard_html_has_icx_and_competitor_columns():
    html = scorecard_html([_m()])
    assert "magik_ui" in html
    assert "ICX (measured)" in html and "published" in html.lower()
    assert "BrowserStack" in html
    assert "100%" in html or "1.0" in html            # recall rendered
    assert all(ord(c) < 128 for c in html)            # ASCII


def test_render_scorecard_writes_file(tmp_path):
    out = render_scorecard([_m()], tmp_path / "score.html")
    assert out.exists() and out.read_text(encoding="utf-8").startswith("<!doctype html")
