from icx_engine.testing.analytics.compute import (
    flakiness, suite_flakiness, pass_trend, slowest, heal_trend,
)
from icx_engine.testing.analytics.store import RunRecord


def test_flakiness_flags_varying_tests():
    hist = {"stable": [("passed", 1.0), ("passed", 1.0)],
            "flaky": [("passed", 1.0), ("failed", 1.0), ("passed", 1.0)]}
    fl = flakiness(hist)
    assert fl["stable"] == 0.0 and fl["flaky"] > 0.0
    assert 0.0 < suite_flakiness(hist) <= 1.0        # 1 of 2 tests flaky -> 0.5


def test_pass_trend_over_runs():
    runs = [RunRecord("r2", "a", 2000.0, 4, 2, 2, 0, 5.0, 0),
            RunRecord("r1", "a", 1000.0, 4, 4, 0, 0, 5.0, 0)]
    tr = pass_trend(runs)
    # returned oldest-first for a chart; r1 = 1.0, r2 = 0.5
    assert tr[0] == (1000.0, 1.0) and tr[1] == (2000.0, 0.5)


def test_slowest_by_mean_time():
    hist = {"fast": [("passed", 0.5), ("passed", 0.5)],
            "slow": [("passed", 3.0), ("passed", 5.0)]}
    top = slowest(hist, top=1)
    assert top[0][0] == "slow" and abs(top[0][1] - 4.0) < 1e-6


def test_heal_trend():
    runs = [RunRecord("r2", "a", 2000.0, 4, 4, 0, 0, 5.0, 3),
            RunRecord("r1", "a", 1000.0, 4, 4, 0, 0, 5.0, 1)]
    ht = heal_trend(runs)
    assert ht == [(1000.0, 1), (2000.0, 3)]          # oldest-first
