"""Test-quality advisory: regression selection, perf regression, mutation scoring - real data or an
honest not-run reason. Never raises."""
from __future__ import annotations

import json

import icx_engine.testing.quality_advisory as qa


def test_regression_skipped_outside_git(tmp_path):
    b = qa._regression_block(tmp_path)
    assert b["status"] == "skipped"
    assert "git" in b["reason"]


def test_regression_selects_relevant_tests(tmp_path, monkeypatch):
    (tmp_path / "auth.py").write_text("x=1\n", encoding="utf-8")
    tdir = tmp_path / "tests"
    tdir.mkdir()
    (tdir / "test_auth.py").write_text("def test(): pass\n", encoding="utf-8")
    (tdir / "test_other.py").write_text("def test(): pass\n", encoding="utf-8")
    monkeypatch.setattr(qa, "_git_changed_files", lambda repo: ["auth.py"])
    b = qa._regression_block(tmp_path)
    assert b["status"] == "ran"
    assert b["changed_files"] == 1
    assert any("test_auth.py" in t for t in b["relevant_tests"])
    assert not any("test_other.py" in t for t in b["relevant_tests"])


def test_regression_no_changes_skips(tmp_path, monkeypatch):
    monkeypatch.setattr(qa, "_git_changed_files", lambda repo: [])
    assert qa._regression_block(tmp_path)["status"] == "skipped"


def test_perf_skipped_without_metrics(monkeypatch):
    monkeypatch.delenv("ICX_PERF_BEFORE", raising=False)
    monkeypatch.delenv("ICX_PERF_AFTER", raising=False)
    b = qa._perf_block()
    assert b["status"] == "skipped" and "metrics" in b["reason"]


def test_perf_runs_and_flags_regression(monkeypatch):
    monkeypatch.setenv("ICX_PERF_BEFORE", json.dumps({"latency": 100, "sql_query_count": 3}))
    monkeypatch.setenv("ICX_PERF_AFTER", json.dumps({"latency": 400, "sql_query_count": 5}))
    b = qa._perf_block()
    assert b["status"] == "ran"
    assert b["passed"] is False           # sql_query_count increased (0% threshold) + latency spiked
    assert b["regressed"] >= 1


def test_perf_from_file(tmp_path, monkeypatch):
    bf = tmp_path / "before.json"
    af = tmp_path / "after.json"
    bf.write_text(json.dumps({"latency": 100}), encoding="utf-8")
    af.write_text(json.dumps({"latency": 101}), encoding="utf-8")
    monkeypatch.setenv("ICX_PERF_BEFORE", str(bf))
    monkeypatch.setenv("ICX_PERF_AFTER", str(af))
    b = qa._perf_block()
    assert b["status"] == "ran" and b["passed"] is True   # 1% within default latency threshold


def test_mutation_skipped_without_report(monkeypatch):
    monkeypatch.delenv("ICX_MUTATION_REPORT", raising=False)
    b = qa._mutation_block()
    assert b["status"] == "skipped" and "opt-in" in b["reason"]


def test_mutation_scores_mutmut_report(tmp_path, monkeypatch):
    rep = tmp_path / "mutmut.txt"
    rep.write_text("killed: 8, survived: 2\n", encoding="utf-8")
    monkeypatch.setenv("ICX_MUTATION_REPORT", str(rep))
    monkeypatch.setenv("ICX_MUTATION_LANG", "python")
    b = qa._mutation_block()
    assert b["status"] == "ran"
    assert b["killed"] == 8 and b["total"] == 10 and b["score"] == 0.8
    assert b["passed"] is True


def test_mutation_missing_report_file(monkeypatch):
    monkeypatch.setenv("ICX_MUTATION_REPORT", "/no/such/report.json")
    assert qa._mutation_block()["status"] == "skipped"


def test_fold_quality_attaches_all_three(tmp_path):
    res = {"ok": True}
    qa.fold_quality(res, tmp_path)
    assert set(res["quality"]) == {"regression", "perf", "mutation"}
    json.dumps(res["quality"])            # serializable


def test_fold_quality_never_raises_on_bad_res(tmp_path):
    assert qa.fold_quality(None, tmp_path) is None
