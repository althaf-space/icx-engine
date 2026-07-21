"""SP7 dataflow metric: count DATAFLOW cases (DB checked/confirmed, network checks)."""
from __future__ import annotations

from icx_engine.testing.runners.junit import parse_junit_xml
from icx_engine.testing.benchmark.metrics import dataflow_summary, RunMetrics, CoverageScore


def test_dataflow_summary_counts():
    xml = ('<testsuite>'
           '<testcase name="DATAFLOW: DB verify record (db confirmed)"/>'
           '<testcase name="DATAFLOW: graceful under slow network"/>'
           '<testcase name="DATAFLOW: DB verify record"><failure>not found</failure></testcase>'
           '</testsuite>')
    s = dataflow_summary(parse_junit_xml(xml))
    assert s["db_checked"] == 2 and s["db_confirmed"] == 1 and s["net_checked"] == 1


def test_dataflow_summary_empty():
    assert dataflow_summary(parse_junit_xml('<testsuite><testcase name="x"/></testsuite>')) == {}


def test_run_metrics_has_dataflow_field():
    m = RunMetrics(app="x", url="u", coverage=CoverageScore(1, 1, 1, 1, 1),
                   misfire_rate=0.0, flakiness=0.0, speed_seconds=1.0, authoring_actions=0,
                   total_tests=1, real_findings=0, dataflow={"db_checked": 1, "db_confirmed": 1, "net_checked": 1})
    assert m.dataflow["db_confirmed"] == 1
