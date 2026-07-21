from icx_engine.testing.runners.junit import parse_junit_xml
from icx_engine.testing.benchmark.metrics import a11y_summary, RunMetrics, CoverageScore


def test_a11y_summary_parses_impacts():
    xml = ('<testsuite><testcase name="ACCESSIBILITY: WCAG audit of the screen">'
           '<failure>a11y violations (axe wcag2.1aa) 5 [critical:3 serious:1 moderate:1 minor:0]: '
           'critical:image-alt | critical:label</failure></testcase></testsuite>')
    s = a11y_summary(parse_junit_xml(xml))
    assert s["violations"] == 5 and s["critical"] == 3 and s["serious"] == 1


def test_a11y_summary_empty_when_no_a11y_case():
    xml = '<testsuite><testcase name="RENDER: chart"/></testsuite>'
    assert a11y_summary(parse_junit_xml(xml)) == {}


def test_a11y_summary_clean_pass():
    xml = '<testsuite><testcase name="ACCESSIBILITY: WCAG audit of the screen"/></testsuite>'
    s = a11y_summary(parse_junit_xml(xml))
    assert s == {"violations": 0, "critical": 0, "serious": 0}


def test_a11y_summary_builtin_format():
    xml = ('<testsuite><testcase name="ACCESSIBILITY: WCAG audit of the screen">'
           '<failure>a11y violations (5): html element has no lang attribute | img[0] has no alt/aria-label</failure></testcase></testsuite>')
    s = a11y_summary(parse_junit_xml(xml))
    assert s["violations"] == 5 and s["critical"] == 0 and s["serious"] == 0


def test_run_metrics_has_a11y_field():
    m = RunMetrics(app="x", url="u", coverage=CoverageScore(1, 1, 1, 1, 1),
                   misfire_rate=0.0, flakiness=0.0, speed_seconds=1.0, authoring_actions=0,
                   total_tests=1, real_findings=0, a11y={"violations": 5, "critical": 3, "serious": 1})
    assert m.a11y["critical"] == 3
