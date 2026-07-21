"""Tests for dataflow (DB + network) rendering in the benchmark scorecard."""
from icx_engine.testing.benchmark.metrics import RunMetrics, CoverageScore
from icx_engine.testing.benchmark.report import scorecard_html


def _m(d):
    return RunMetrics(app="magik_ui", url="u", coverage=CoverageScore(1, 1, 1, 1, 1),
                      misfire_rate=0.0, flakiness=0.0, speed_seconds=1.0, authoring_actions=0,
                      total_tests=5, real_findings=0, dataflow=d)


def test_scorecard_shows_dataflow_when_present():
    html = scorecard_html([_m({"db_checked": 1, "db_confirmed": 1, "net_checked": 1})])
    assert ("DB and network" in html or "DB/network" in html) and "magik_ui" in html
    assert all(ord(c) < 128 for c in html)


def test_scorecard_omits_dataflow_when_empty():
    html = scorecard_html([_m({})])
    assert "DB and network" not in html and "DB/network" not in html
