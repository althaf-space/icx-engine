"""Turn a node_local_run result into a stored RunRecord. Guarded - never raises; a recording failure
must never affect a test run."""
from __future__ import annotations

import os

from icx_engine.testing.analytics.store import AnalyticsStore, RunRecord


def analytics_enabled() -> bool:
    """Analytics recording is OFF by default; enabled by ICX_TEST_ANALYTICS=1."""
    return os.environ.get("ICX_TEST_ANALYTICS") == "1"


def _cases_of(res: dict):
    """Production shape: a top-level res["cases"] list of (name, status, time) tuples/lists (from
    run_local_verification). Fall back to the legacy per-report dict-cases shape for older callers."""
    top = res.get("cases")
    if isinstance(top, list):
        for c in top:
            if isinstance(c, dict):
                yield (str(c.get("name", "")), str(c.get("status", "")), c.get("time", 0.0))
            elif isinstance(c, (list, tuple)) and len(c) >= 3:
                yield (str(c[0]), str(c[1]), c[2])
        return
    for rep in (res.get("reports") or []):
        for c in (rep.get("cases") or []) if isinstance(rep, dict) else []:
            if isinstance(c, dict):
                yield (str(c.get("name", "")), str(c.get("status", "")), c.get("time", 0.0))


def record_from_result(res, app: str, run_id: str, ts: float, store=None) -> bool:
    """Append a RunRecord + per-test rows built from a node_local_run result. Returns True on success,
    False on any failure (never raises)."""
    if not isinstance(res, dict):
        return False
    try:
        cases = list(_cases_of(res))
        summ = res.get("summary") if isinstance(res.get("summary"), dict) else {}
        heals = sum(1 for (n, _s, _t) in cases if str(n).startswith("HEAL:"))
        total = int(summ.get("total", len(cases)) or 0)
        passed = int(summ.get("passed", 0) or 0)
        failed = int(summ.get("failures", 0) or 0)
        skipped = int(summ.get("skipped", 0) or 0)
        dur = round(sum(float(t or 0) for (_n, _s, t) in cases), 3)
        rec = RunRecord(run_id=str(run_id), app=str(app), ts=float(ts), total=total, passed=passed,
                        failed=failed, skipped=skipped, duration=dur, heals=heals)
        own = store is None
        st = store or AnalyticsStore()
        try:
            st.record_run(rec, cases)
        finally:
            if own:
                st.close()
        return True
    except Exception:
        return False
