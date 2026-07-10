"""Tests for the local verification backend (the Magik executor replacement)."""
from pathlib import Path

import icx_engine.testing.local_executor as le
from icx_engine.testing.runners.base import RunSpec, TestReport, TestCase


def _report(passed=1, failed=0):
    cases = [TestCase(name=f"p{i}", status="passed") for i in range(passed)]
    cases += [TestCase(name=f"f{i}", status="failed") for i in range(failed)]
    r = TestReport(total=len(cases), passed=passed, failures=failed, cases=cases)
    return r


class _FakeRunner:
    def __init__(self, name, lang, category):
        self.name, self.lang, self.category = name, lang, category
    def detect(self, repo): return True
    def build_command(self, repo, runtime_path):
        return RunSpec(command=["fake", self.name], cwd=str(repo),
                       report_path=f"{self.name}.xml", env={"RT": str(runtime_path)})


def test_unknown_test_type():
    res = le.run_local_verification("/x", "quantum")
    assert res["ok"] is False and "unknown test type" in res["reason"]


def test_no_runner_detected(monkeypatch, tmp_path):
    monkeypatch.setattr("icx_engine.testing.runners.detect_runners", lambda repo, category=None: [])
    res = le.run_local_verification(tmp_path, "unit")
    assert res["ok"] is False and "no unit runner" in res["reason"]


def test_all_pass(monkeypatch, tmp_path):
    monkeypatch.setattr("icx_engine.testing.runners.detect_runners",
                        lambda repo, category=None: [_FakeRunner("pytest", "python", "unit")])
    monkeypatch.setattr("icx_engine.testing.runners.run_plan",
                        lambda specs, parallel=True, timeout=600.0: [(specs[0], _report(passed=3))])
    res = le.run_local_verification(tmp_path, "unit")
    assert res["ok"] is True
    assert res["summary"]["total"] == 3 and res["summary"]["passed"] == 3
    assert res["runners"] == ["pytest"]


def test_failure_makes_not_ok(monkeypatch, tmp_path):
    monkeypatch.setattr("icx_engine.testing.runners.detect_runners",
                        lambda repo, category=None: [_FakeRunner("pytest", "python", "unit")])
    monkeypatch.setattr("icx_engine.testing.runners.run_plan",
                        lambda specs, parallel=True, timeout=600.0: [(specs[0], _report(passed=2, failed=1))])
    res = le.run_local_verification(tmp_path, "unit")
    assert res["ok"] is False and res["summary"]["failures"] == 1


def test_runtime_resolver_and_target_url(monkeypatch, tmp_path):
    seen = {}
    fake = _FakeRunner("schemathesis", "http", "api")
    def _bc(repo, runtime_path):
        seen["rt"] = runtime_path
        return RunSpec(command=["schemathesis"], cwd=str(repo), report_path="r.xml")
    fake.build_command = _bc
    monkeypatch.setattr("icx_engine.testing.runners.detect_runners",
                        lambda repo, category=None: [fake])
    captured = {}
    def _run_plan(specs, parallel=True, timeout=600.0):
        captured["env"] = specs[0].env
        return [(specs[0], _report(passed=1))]
    monkeypatch.setattr("icx_engine.testing.runners.run_plan", _run_plan)
    res = le.run_local_verification(tmp_path, "api", target_url="http://svc",
                                    runtime_resolver=lambda lang: "/opt/py")
    assert seen["rt"] == "/opt/py"
    assert captured["env"]["ICX_TARGET_URL"] == "http://svc"
    assert res["ok"] is True


def test_agent_type_maps_to_ui(monkeypatch, tmp_path):
    got = {}
    def _detect(repo, category=None):
        got["category"] = category
        return [_FakeRunner("stagehand", "ui", "ui")]
    monkeypatch.setattr("icx_engine.testing.runners.detect_runners", _detect)
    monkeypatch.setattr("icx_engine.testing.runners.run_plan",
                        lambda specs, parallel=True, timeout=600.0: [(specs[0], _report(passed=1))])
    le.run_local_verification(tmp_path, "agent")
    assert got["category"] == "ui"
