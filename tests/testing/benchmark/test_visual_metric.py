"""Visual-regression metric: visual_summary() + RunMetrics.visual field (SP5 Task 3)."""
from __future__ import annotations

from icx_engine.testing.runners.junit import parse_junit_xml
from icx_engine.testing.benchmark.metrics import visual_summary, RunMetrics, CoverageScore


def test_visual_summary_counts_cases():
    xml = ('<testsuite>'
           '<testcase name="VISUAL: screen (baseline captured)"/>'
           '<testcase name="VISUAL: modal (0.10% diff)"/>'
           '<testcase name="VISUAL: header"><failure>visual regression: 500/1000 px changed</failure></testcase>'
           '</testsuite>')
    s = visual_summary(parse_junit_xml(xml))
    assert s["checked"] == 3 and s["baselines"] == 1 and s["regressions"] == 1


def test_visual_summary_counts_soft_flagged_diff_as_regression():
    # a woven (soft) screenshot step never hard-fails - it lands as status="skipped" with a
    # "VISUAL DIFF (review)" message. The metric must still count it as a regression.
    xml = ('<testsuite>'
           '<testcase name="VISUAL: screen (baseline captured)"/>'
           '<testcase name="VISUAL: header">'
           '<skipped message="VISUAL DIFF (review): 500/1000 px changed (0.50% &gt; 0.02%) - saved header.diff.png"/>'
           '</testcase>'
           '</testsuite>')
    s = visual_summary(parse_junit_xml(xml))
    assert s["checked"] == 2 and s["baselines"] == 1 and s["regressions"] == 1


def test_run_metrics_has_visual_field():
    m = RunMetrics(app="x", url="u", coverage=CoverageScore(1, 1, 1, 1, 1),
                   misfire_rate=0.0, flakiness=0.0, speed_seconds=1.0, authoring_actions=0,
                   total_tests=1, real_findings=0, visual={"checked": 2, "baselines": 2, "regressions": 0})
    assert m.visual["regressions"] == 0
