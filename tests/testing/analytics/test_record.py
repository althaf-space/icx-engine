from icx_engine.testing.analytics.record import record_from_result
from icx_engine.testing.analytics.store import AnalyticsStore


def _res():
    return {"ok": True, "summary": {"total": 3, "passed": 2, "failures": 1, "skipped": 0},
            "reports": [{"cases": [{"name": "t1", "status": "passed", "time": 1.0},
                                   {"name": "HEAL: #a -> #b (0.9)", "status": "passed", "time": 0.0},
                                   {"name": "t2", "status": "failed", "time": 2.0}]}]}


def test_record_from_result_stores_run_and_cases(tmp_path):
    store = AnalyticsStore(tmp_path / "a.db")
    ok = record_from_result(_res(), app="magik_ui", run_id="r1", ts=1000.0, store=store)
    assert ok is True
    runs = store.recent_runs()
    assert len(runs) == 1 and runs[0].heals == 1          # the HEAL: case counted
    hist = store.test_history()
    assert "t1" in hist and "t2" in hist
    store.close()


def test_record_from_result_never_raises_on_bad_input(tmp_path):
    store = AnalyticsStore(tmp_path / "a.db")
    assert record_from_result({}, app="x", run_id="r", ts=0.0, store=store) in (True, False)
    assert record_from_result(None, app="x", run_id="r", ts=0.0, store=store) is False
    store.close()


def _production_res():
    """Shape actually returned by run_local_verification: reports carry only runner/report_path/
    total/ok (no per-case cases dict) - the flattened case list is a top-level "cases" key."""
    return {"ok": True, "summary": {"total": 3, "passed": 2, "failures": 1, "skipped": 0},
            "reports": [{"runner": "pytest", "report_path": "junit.xml", "total": 3, "ok": False}],
            "cases": [("t1", "passed", 1.0),
                      ("HEAL: #a -> #b (0.9)", "passed", 0.0),
                      ("t2", "failed", 2.0)]}


def test_record_from_result_reads_production_top_level_cases(tmp_path):
    store = AnalyticsStore(tmp_path / "a.db")
    ok = record_from_result(_production_res(), app="magik_ui", run_id="r1", ts=1000.0, store=store)
    assert ok is True
    runs = store.recent_runs()
    assert len(runs) == 1 and runs[0].heals == 1
    hist = store.test_history()
    assert "t1" in hist and "t2" in hist
    store.close()
