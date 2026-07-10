"""Tests for perf-regression comparison + intelligent regression selection."""
from icx_engine.testing.perf import compare_performance, PerfFinding
from icx_engine.testing.regression import select_regression_targets


def test_perf_regression_over_threshold_fails():
    findings = compare_performance({"latency": 100}, {"latency": 130})  # +30% > 20% default
    lat = next(f for f in findings if f.metric == "latency")
    assert lat.passed is False and lat.pct_change == 30.0


def test_perf_within_threshold_passes():
    findings = compare_performance({"latency": 100}, {"latency": 110})  # +10% < 20%
    assert next(f for f in findings if f.metric == "latency").passed is True


def test_perf_decrease_passes():
    findings = compare_performance({"memory": 200}, {"memory": 150})
    assert next(f for f in findings if f.metric == "memory").passed is True


def test_perf_sql_query_any_increase_flagged():
    findings = compare_performance({"sql_query_count": 3}, {"sql_query_count": 4})  # threshold 0%
    assert next(f for f in findings if f.metric == "sql_query_count").passed is False


def test_perf_override_thresholds():
    findings = compare_performance({"latency": 100}, {"latency": 130}, {"latency_pct": 50.0})
    assert next(f for f in findings if f.metric == "latency").passed is True


def test_perf_only_common_metrics():
    findings = compare_performance({"latency": 100}, {"cpu": 50})
    assert findings == []  # no metric present in both


def test_regression_selects_matching_tests():
    changed = ["src/auth.py", "src/user_service.py"]
    tests = ["tests/test_auth.py", "tests/test_user_service.py", "tests/test_billing.py"]
    picked = select_regression_targets(changed, tests)
    assert "tests/test_auth.py" in picked
    assert "tests/test_user_service.py" in picked
    assert "tests/test_billing.py" not in picked


def test_regression_widens_with_graph_impact():
    changed = ["src/auth.py"]
    tests = ["tests/test_auth.py", "tests/test_session.py"]
    picked = select_regression_targets(changed, tests, graph_impacted=["src/session.py"])
    assert set(picked) == {"tests/test_auth.py", "tests/test_session.py"}


def test_regression_empty_when_no_match():
    assert select_regression_targets(["src/foo.py"], ["tests/test_bar.py"]) == []


def test_regression_ts_naming():
    picked = select_regression_targets(["src/cart.ts"], ["cart.test.ts", "checkout.spec.ts"])
    assert picked == ["cart.test.ts"]
