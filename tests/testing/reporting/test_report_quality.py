"""The report renders the Test-quality section: regression / perf / mutation, real data or not-run,
all escaped, never raises on missing/malformed data."""
from __future__ import annotations

from icx_engine.testing.reporting.session_report import render_session_report

_META = {"app": "demo", "url": "http://x/#/users", "test_type": "agent", "ts": 1784580500}


def _res(quality):
    return {"summary": {"total": 1, "passed": 1, "failures": 0, "skipped": 0},
            "cases": [("CREATE: save", "passed", 1.0)], "quality": quality}


def test_renders_all_three_ran():
    q = {"regression": {"status": "ran", "changed_files": 2, "candidate_tests": 10,
                        "relevant_tests": ["tests/test_auth.py"], "relevant_count": 1},
         "perf": {"status": "ran", "passed": False,
                  "findings": [{"metric": "latency", "before": 100, "after": 400,
                                "pct_change": 300.0, "threshold_pct": 10.0, "passed": False}],
                  "regressed": 1},
         "mutation": {"status": "ran", "tool": "mutmut", "total": 10, "killed": 8, "survived": 2,
                      "score": 0.8, "passed": True, "reason": "mutation score 0.8"}}
    html = render_session_report(_res(q), _META)
    assert "Test quality" in html
    assert "Regression selection" in html and "tests/test_auth.py" in html
    assert "Performance regression" in html and "REGRESSED" in html
    assert "Mutation testing" in html and "0.8" in html


def test_renders_not_run_states_honestly():
    q = {"regression": {"status": "skipped", "reason": "not a git repository"},
         "perf": {"status": "skipped", "reason": "no before/after metrics provided"},
         "mutation": {"status": "skipped", "reason": "opt-in - run mutation testing separately"}}
    html = render_session_report(_res(q), _META)
    assert html.count("Not run -") == 3
    assert "not a git repository" in html


def test_quality_escaped():
    q = {"regression": {"status": "skipped", "reason": "<script>x</script>"},
         "perf": {"status": "skipped", "reason": "ok"},
         "mutation": {"status": "skipped", "reason": "ok"}}
    html = render_session_report(_res(q), _META)
    assert "<script>x</script>" not in html
    assert "&lt;script&gt;x&lt;/script&gt;" in html
    assert all(ord(c) < 128 for c in html)


def test_missing_quality_no_section():
    res = {"summary": {"total": 1, "passed": 1, "failures": 0, "skipped": 0},
           "cases": [("CREATE: save", "passed", 1.0)]}
    html = render_session_report(res, _META)
    assert "Test quality" not in html
    assert html.startswith("<!doctype html")


def test_malformed_quality_no_raise():
    html = render_session_report(_res("not a dict"), _META)
    assert html.startswith("<!doctype html")
