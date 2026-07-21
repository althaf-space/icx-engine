"""The report renders the static-security block: severity chips, per-scanner cards, escaping, and a
clean-state message. Never raises on missing/malformed security data."""
from __future__ import annotations

from icx_engine.testing.reporting.session_report import render_session_report

_META = {"app": "demo", "url": "http://x/#/users", "test_type": "ui", "ts": 1784580500}


def _res_with_security(findings, summary=None):
    counts = {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0}
    for f in findings:
        counts[f["severity"]] = counts.get(f["severity"], 0) + 1
    summ = summary or {"total": len(findings), **counts,
                       "clean": not findings, "gate_failed": counts["critical"] + counts["high"] > 0}
    return {"summary": {"total": 1, "passed": 1, "failures": 0, "skipped": 0},
            "cases": [("CREATE: save", "passed", 1.0)],
            "security": {"findings": findings, "summary": summ}}


def test_renders_security_findings_with_severity_and_location():
    findings = [
        {"scanner": "secrets", "rule": "aws-access-key", "severity": "critical",
         "title": "AWS access key id", "file": "app.py", "line": 3, "detail": "leak", "snippet": "AWS=..."},
        {"scanner": "sast", "rule": "py-eval", "severity": "high", "title": "Use of eval()",
         "file": "b.py", "line": 5, "detail": "eval executes code", "snippet": "eval(u)"},
    ]
    html = render_session_report(_res_with_security(findings), _META)
    assert "Security scan" in html
    assert "Leaked secrets" in html
    assert "Code security (SAST)" in html
    assert "sev critical" in html and "sev high" in html
    assert "app.py:3" in html
    assert "not a full taint" in html


def test_clean_security_shows_ok_message():
    html = render_session_report(_res_with_security([]), _META)
    assert "No security issues found" in html


def test_security_findings_are_escaped():
    findings = [{"scanner": "sast", "rule": "x", "severity": "high",
                 "title": "<script>t</script>", "file": "<b>f</b>", "line": 1,
                 "detail": "<i>d</i>", "snippet": "<img src=x>"}]
    html = render_session_report(_res_with_security(findings), _META)
    assert "<script>t</script>" not in html
    assert "&lt;script&gt;t&lt;/script&gt;" in html
    assert all(ord(c) < 128 for c in html)


def test_missing_security_key_no_section_no_raise():
    res = {"summary": {"total": 1, "passed": 1, "failures": 0, "skipped": 0},
           "cases": [("CREATE: save", "passed", 1.0)]}
    html = render_session_report(res, _META)
    assert "Security scan" not in html
    assert html.startswith("<!doctype html")


def test_malformed_security_block_no_raise():
    res = {"summary": {"total": 1, "passed": 1}, "cases": [], "security": "not a dict"}
    html = render_session_report(res, _META)
    assert html.startswith("<!doctype html")
