from icx_engine.testing.runners.junit import parse_junit_xml
from icx_engine.testing.benchmark.metrics import (
    coverage, misfire_rate, flakiness, build_run_metrics,
)

_GT = {"elements": [
    {"kind": "create", "label": "Create User"},
    {"kind": "field", "label": "first Name"},
    {"kind": "search", "label": "Search"}],
    "known_bugs": [{"category": "a11y", "match": "no alt"}]}

_CENSUS = {"functionalities": [
    {"functionality": "Create User", "fields": [{"label": "first Name"}]},
    {"functionality": "Search"}]}  # missing nothing extra; covers create+field+search


def test_coverage_recall_and_precision():
    c = coverage(_CENSUS, _GT)
    # truth = 3 (Create User, first Name, Search); discovered matches all 3
    assert c.matched == 3 and c.true_total == 3
    assert c.recall == 1.0
    assert c.precision == 1.0


def test_misfire_separates_real_findings_from_false_failures():
    xml = (
        '<testsuite tests="4" failures="3">'
        '<testcase name="ACCESSIBILITY: WCAG audit"><failure>img has no alt</failure></testcase>'
        '<testcase name="SECURITY(XSS): reflected"><failure>xss</failure></testcase>'
        '<testcase name="RENDER: chart present"><failure>timeout</failure></testcase>'
        '<testcase name="CREATE: open"/></testsuite>')
    rate, real = misfire_rate(parse_junit_xml(xml), _GT)
    # a11y + security failures are REAL findings; the render timeout is a misfire
    assert real == 2
    assert abs(rate - 0.25) < 1e-6   # 1 misfire / 4 total


def test_flakiness_detects_status_variance():
    good = '<testsuite><testcase name="t1"/><testcase name="t2"/></testsuite>'
    mixed = '<testsuite><testcase name="t1"><failure>x</failure></testcase><testcase name="t2"/></testsuite>'
    f = flakiness([parse_junit_xml(good), parse_junit_xml(mixed)])
    assert abs(f - 0.5) < 1e-6      # t1 varied, t2 stable -> 1/2

    stable = flakiness([parse_junit_xml(good), parse_junit_xml(good)])
    assert stable == 0.0


def test_build_run_metrics_assembles_all():
    rep = parse_junit_xml('<testsuite><testcase name="RENDER: x"/></testsuite>')
    m = build_run_metrics("magik_ui", "http://x", _CENSUS, [rep], 5.5, 0, _GT)
    assert m.app == "magik_ui" and m.speed_seconds == 5.5
    assert m.authoring_actions == 0 and m.total_tests == 1
    assert m.coverage.recall == 1.0


def test_coverage_counts_fieldname_only_field():
    census = {"functionalities": [{"functionality": "Create", "fields": [{"fieldName": "emp Code"}]}]}
    gt = {"elements": [{"label": "emp Code"}, {"label": "Create"}]}
    c = coverage(census, gt)
    assert c.matched == 2
    assert c.recall == 1.0
    assert c.precision == 1.0


def test_misfire_classifies_by_message():
    xml = '<testsuite tests="1"><testcase name="CREATE: save"><failure>duplicate key violation</failure></testcase></testsuite>'
    gt = {"known_bugs": [{"category": "functional", "match": "duplicate key"}]}
    rate, real = misfire_rate(parse_junit_xml(xml), gt)
    assert real == 1
    assert rate == 0.0


def test_coverage_guard_branches():
    c = coverage({}, {})
    assert c.recall == 0.0
    assert c.precision == 0.0


def test_flakiness_guard_branches():
    assert flakiness([]) == 0.0
    rep = parse_junit_xml('<testsuite><testcase name="t1"/></testsuite>')
    assert flakiness([rep]) == 0.0


def test_misfire_rate_empty_suite():
    report = parse_junit_xml('<testsuite></testsuite>')
    rate, real = misfire_rate(report, {})
    assert rate == 0.0
    assert real == 0
