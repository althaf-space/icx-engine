from pathlib import Path
from icx_engine.testing.analytics.store import AnalyticsStore, RunRecord


def _rec(run_id, app="magik_ui", ts=1000.0, total=3, passed=3, failed=0, skipped=0, dur=5.0, heals=0):
    return RunRecord(run_id=run_id, app=app, ts=ts, total=total, passed=passed,
                     failed=failed, skipped=skipped, duration=dur, heals=heals)


def test_record_and_read_back(tmp_path):
    s = AnalyticsStore(tmp_path / "a.db")
    s.record_run(_rec("r1"), [("t1", "passed", 1.0), ("t2", "passed", 2.0)])
    runs = s.recent_runs()
    assert len(runs) == 1 and runs[0].run_id == "r1" and runs[0].passed == 3
    s.close()


def test_test_history_orders_newest_first(tmp_path):
    s = AnalyticsStore(tmp_path / "a.db")
    s.record_run(_rec("r1", ts=1000.0), [("t1", "passed", 1.0)])
    s.record_run(_rec("r2", ts=2000.0, failed=1, passed=2), [("t1", "failed", 1.5)])
    hist = s.test_history()
    assert "t1" in hist
    assert hist["t1"][0][0] == "failed"      # newest run first
    assert hist["t1"][1][0] == "passed"
    s.close()


def test_persists_across_instances(tmp_path):
    db = tmp_path / "a.db"
    s1 = AnalyticsStore(db); s1.record_run(_rec("r1"), [("t1", "passed", 1.0)]); s1.close()
    s2 = AnalyticsStore(db)
    assert len(s2.recent_runs()) == 1        # real persistence, not mocked
    s2.close()


def test_recent_runs_limit_and_order(tmp_path):
    s = AnalyticsStore(tmp_path / "a.db")
    for i in range(5):
        s.record_run(_rec(f"r{i}", ts=1000.0 + i), [("t1", "passed", 1.0)])
    runs = s.recent_runs(limit=3)
    assert len(runs) == 3 and runs[0].run_id == "r4"   # newest first
    s.close()
