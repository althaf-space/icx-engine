"""Aggregate: runs all scanners, severity counts + gate, folds onto res, never raises."""
from __future__ import annotations

import json

from icx_engine.testing.security.aggregate import fold_into_result, run_static_security


def _vuln_repo(tmp_path):
    (tmp_path / "app.py").write_text(
        'password = "S3cr3tP@ssw0rd123XZ"\n'
        "def f(u):\n    return eval(u)\n", encoding="utf-8")
    (tmp_path / "requirements.txt").write_text("flask\n", encoding="utf-8")
    return tmp_path


def test_run_static_security_counts_and_gate(tmp_path):
    r = run_static_security(_vuln_repo(tmp_path))
    s = r["summary"]
    assert s["total"] >= 3
    assert s["critical"] >= 1          # py-eval
    assert s["high"] >= 1              # hardcoded-credential
    assert s["gate_failed"] is True    # any critical/high fails the gate
    assert s["clean"] is False


def test_findings_sorted_most_severe_first(tmp_path):
    r = run_static_security(_vuln_repo(tmp_path))
    ranks = {"critical": 4, "high": 3, "medium": 2, "low": 1, "info": 0}
    sevs = [ranks[f["severity"]] for f in r["findings"]]
    assert sevs == sorted(sevs, reverse=True)


def test_clean_repo_gate_passes(tmp_path):
    (tmp_path / "ok.py").write_text("x = 1\n", encoding="utf-8")
    r = run_static_security(tmp_path)
    assert r["summary"]["clean"] is True
    assert r["summary"]["gate_failed"] is False
    assert r["findings"] == []


def test_fold_into_result_attaches_block(tmp_path):
    res = {"ok": True, "summary": {"total": 1, "passed": 1}}
    fold_into_result(res, _vuln_repo(tmp_path))
    assert "security" in res
    assert res["security"]["summary"]["gate_failed"] is True
    # findings are JSON-serializable dicts
    json.dumps(res["security"])


def test_fold_never_raises_on_bad_input(tmp_path):
    # non-dict res returns unchanged; bad repo path yields a clean block
    assert fold_into_result(None, tmp_path) is None
    res = {}
    fold_into_result(res, tmp_path / "does_not_exist")
    assert res["security"]["summary"]["total"] == 0


def test_advisory_folds_in(tmp_path):
    (tmp_path / "requirements.txt").write_text("requests==2.5.0\n", encoding="utf-8")
    (tmp_path / ".icx-advisories.json").write_text(
        json.dumps({"pypi": {"requests": [{"lt": "2.20.0", "severity": "critical", "id": "CVE-X"}]}}),
        encoding="utf-8")
    r = run_static_security(tmp_path)
    assert any(f["rule"] == "known-vulnerable-dependency" for f in r["findings"])
