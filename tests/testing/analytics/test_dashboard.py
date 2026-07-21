from icx_engine.testing.analytics.store import AnalyticsStore, RunRecord
from icx_engine.testing.analytics.dashboard import dashboard_html, render_dashboard


def _seed(tmp_path):
    s = AnalyticsStore(tmp_path / "a.db")
    s.record_run(RunRecord("r1", "magik_ui", 1000.0, 3, 3, 0, 0, 5.0, 0),
                 [("t1", "passed", 1.0), ("t2", "passed", 2.0)])
    s.record_run(RunRecord("r2", "magik_ui", 2000.0, 3, 2, 1, 0, 6.0, 1),
                 [("t1", "passed", 1.0), ("t2", "failed", 2.5)])
    return s


def test_dashboard_html_has_sections(tmp_path):
    s = _seed(tmp_path)
    html = dashboard_html(s)
    assert "Flakiness" in html and "Pass" in html and "Slowest" in html
    assert "magik_ui" in html or "t2" in html
    assert all(ord(c) < 128 for c in html)
    s.close()


def test_render_dashboard_writes_file(tmp_path):
    s = _seed(tmp_path)
    out = render_dashboard(s, tmp_path / "dash.html")
    assert out.exists() and out.read_text(encoding="utf-8").startswith("<!doctype html")
    s.close()


def test_dashboard_html_empty_store_does_not_raise(tmp_path):
    s = AnalyticsStore(tmp_path / "empty.db")
    html = dashboard_html(s)
    assert html.startswith("<!doctype html")
    s.close()
