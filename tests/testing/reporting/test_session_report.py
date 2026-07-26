import json
from pathlib import Path
from icx_engine.testing.reporting.session_report import (
    categorize, render_session_report, write_session_report,
)
from icx_engine.testing.reporting.index import update_index


def _res():
    return {"ok": False, "test_type": "agent",
            "summary": {"total": 6, "passed": 4, "failures": 1, "errors": 0, "skipped": 1},
            "census_coverage": 1.0,
            "cases": [
                ("CREATE: SAVE the new record", "passed", 1.2),
                ("SECURITY(XSS): reflected payload", "passed", 0.4),
                ("ACCESSIBILITY: WCAG audit of the screen", "failed", 0.3),
                ("VISUAL: screen baseline (baseline captured)", "passed", 0.5),
                ("DATAFLOW: DB verify record (db confirmed)", "passed", 0.2),
                ("CONSTRAINT: first Name maxLength", "skipped", 0.0),
            ]}


def _meta():
    return {"app": "magik_ui", "url": "http://x/#/users", "test_type": "agent", "ts": 1000.0, "run_id": "r1"}


def test_categorize_by_prefix():
    assert categorize("SECURITY(XSS): reflected") == "security"
    assert categorize("ACCESSIBILITY: audit") == "accessibility"
    assert categorize("VISUAL: screen") == "visual"
    assert categorize("DATAFLOW: DB verify") == "dataflow"
    assert categorize("HEAL: #a -> #b") == "heal"
    assert categorize("CONSTRAINT: maxLength") == "constraint"
    assert categorize("CREATE: SAVE") == "functional"


def test_render_has_summary_and_cases_and_escapes():
    res = _res()
    res["cases"].append(("assert <b>markup</b> & \"q\"", "failed", 0.1))  # must be escaped
    html = render_session_report(res, _meta())
    assert html.startswith("<!doctype html")
    assert "magik_ui" in html
    assert "4" in html and "pass" in html.lower()          # passed count present
    assert "ACCESSIBILITY: WCAG audit" in html             # a failed case listed
    assert "&lt;b&gt;markup&lt;/b&gt;" in html             # escaped, not raw markup
    assert "<b>markup</b>" not in html
    assert all(ord(c) < 128 for c in html)                 # ASCII


def test_write_report_and_index(tmp_path):
    p = write_session_report(_res(), _meta(), reports_dir=tmp_path)
    assert p.exists() and p.suffix == ".html"
    ledger = tmp_path / "reports.jsonl"
    assert ledger.exists()
    row = json.loads(ledger.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert row["app"] == "magik_ui" and row["passed"] == 4 and row["failed"] == 1
    idx = tmp_path / "index.html"
    assert idx.exists() and "magik_ui" in idx.read_text(encoding="utf-8")


def test_write_never_raises_on_bad_dir(tmp_path):
    # a res missing summary/cases must not crash the writer
    p = write_session_report({}, {"app": "x", "url": "", "test_type": "agent", "ts": 0.0, "run_id": "r"},
                             reports_dir=tmp_path)
    assert isinstance(p, Path)


def test_render_never_raises_on_non_numeric_summary_field():
    res = {"summary": {"total": "many", "passed": 1}}
    html = render_session_report(res, _meta())
    assert html.startswith("<!doctype html")


def test_write_never_raises_on_non_numeric_ts(tmp_path):
    meta = {"app": "x", "url": "", "test_type": "agent", "ts": "not-a-number", "run_id": "r"}
    p = write_session_report(_res(), meta, reports_dir=tmp_path)
    assert isinstance(p, Path)


def test_categorize_strict_prefix_not_substring():
    assert categorize("CREATE: constraint on name field") == "functional"


def test_update_index_tolerant_of_bad_ts_row(tmp_path):
    good = {"run_id": "r1", "app": "a1", "url": "", "test_type": "agent", "ts": 1000,
            "total": 2, "passed": 2, "failed": 0, "skipped": 0, "pass_rate": 100, "file": "a1-1000.html"}
    bad = {"run_id": "r2", "app": "a2", "url": "", "test_type": "agent", "ts": "bad",
           "total": 1, "passed": 0, "failed": 1, "skipped": 0, "pass_rate": 0, "file": "a2-bad.html"}
    ledger = tmp_path / "reports.jsonl"
    ledger.write_text(json.dumps(good) + "\n" + json.dumps(bad) + "\n", encoding="utf-8")
    idx = update_index(tmp_path)
    assert isinstance(idx, Path)
    assert idx.exists()
