"""Tests for the local verification backend (the Magik executor replacement)."""
from pathlib import Path

import icx_engine.testing.local_executor as le
from icx_engine.testing.runners.base import RunSpec, TestReport, TestCase


def _report(passed=1, failed=0):
    cases = [TestCase(name=f"p{i}", status="passed") for i in range(passed)]
    cases += [TestCase(name=f"f{i}", status="failed") for i in range(failed)]
    r = TestReport(total=len(cases), passed=passed, failures=failed, cases=cases)
    return r


def _aplan(pairs):
    """Return an async run_plan stub mapping spec-index -> report."""
    async def _run_plan(specs, parallel=True, timeout=600.0):
        return [(specs[i], rep) for (i, rep) in pairs]
    return _run_plan


class _FakeRunner:
    def __init__(self, name, lang, category):
        self.name, self.lang, self.category = name, lang, category
    def detect(self, repo): return True
    def build_command(self, repo, runtime_path):
        return RunSpec(command=["fake", self.name], cwd=str(repo),
                       report_path=f"{self.name}.xml", env={"RT": str(runtime_path)})


async def test_unknown_test_type():
    res = await le.run_local_verification("/x", "quantum")
    assert res["ok"] is False and "unknown test type" in res["reason"]


async def test_no_runner_detected(monkeypatch, tmp_path):
    monkeypatch.setattr("icx_engine.testing.runners.detect_runners", lambda repo, category=None: [])
    res = await le.run_local_verification(tmp_path, "unit")
    assert res["ok"] is False and "no unit runner" in res["reason"]


async def test_all_pass(monkeypatch, tmp_path):
    monkeypatch.setattr("icx_engine.testing.runners.detect_runners",
                        lambda repo, category=None: [_FakeRunner("pytest", "python", "unit")])
    monkeypatch.setattr("icx_engine.testing.runners.run_plan",
                        _aplan([(0, _report(passed=3))]))
    res = await le.run_local_verification(tmp_path, "unit")
    assert res["ok"] is True
    assert res["summary"]["total"] == 3 and res["summary"]["passed"] == 3
    assert res["runners"] == ["pytest"]


async def test_failure_makes_not_ok(monkeypatch, tmp_path):
    monkeypatch.setattr("icx_engine.testing.runners.detect_runners",
                        lambda repo, category=None: [_FakeRunner("pytest", "python", "unit")])
    monkeypatch.setattr("icx_engine.testing.runners.run_plan",
                        _aplan([(0, _report(passed=2, failed=1))]))
    res = await le.run_local_verification(tmp_path, "unit")
    assert res["ok"] is False and res["summary"]["failures"] == 1


async def test_runtime_resolver_and_target_url(monkeypatch, tmp_path):
    seen = {}
    fake = _FakeRunner("schemathesis", "http", "api")
    def _bc(repo, runtime_path):
        seen["rt"] = runtime_path
        return RunSpec(command=["schemathesis"], cwd=str(repo), report_path="r.xml")
    fake.build_command = _bc
    monkeypatch.setattr("icx_engine.testing.runners.detect_runners",
                        lambda repo, category=None: [fake])
    captured = {}
    async def _run_plan(specs, parallel=True, timeout=600.0):
        captured["env"] = specs[0].env
        return [(specs[0], _report(passed=1))]
    monkeypatch.setattr("icx_engine.testing.runners.run_plan", _run_plan)
    res = await le.run_local_verification(tmp_path, "api", target_url="http://svc",
                                    runtime_resolver=lambda lang: "/opt/py")
    assert seen["rt"] == "/opt/py"
    assert captured["env"]["ICX_TARGET_URL"] == "http://svc"
    assert res["ok"] is True


async def test_agent_type_maps_to_ui(monkeypatch, tmp_path):
    got = {}
    def _detect(repo, category=None):
        got["category"] = category
        return [_FakeRunner("stagehand", "ui", "ui")]
    monkeypatch.setattr("icx_engine.testing.runners.detect_runners", _detect)
    monkeypatch.setattr("icx_engine.testing.runners.run_plan",
                        _aplan([(0, _report(passed=1))]))
    await le.run_local_verification(tmp_path, "agent")
    assert got["category"] == "ui"


# -- Step 1b: local_run node + routing -----------------------------------------

from icx_engine.testing.nodes import node_local_run, route_before_submit


def test_route_before_submit_local_vs_magik():
    assert route_before_submit({"engine": "local"}) == "local_run"
    assert route_before_submit({"engine": "magik"}) == "submit"
    assert route_before_submit({}) == "submit"  # default = magik


async def test_node_local_run_pass(monkeypatch, tmp_path):
    import icx_engine.testing.local_executor as _le
    async def _fake(repo, test_type, target_url=None, **kw):
        return {"ok": True, "test_type": test_type, "summary": {"total": 3, "passed": 3},
                "runners": ["pytest"], "reports": []}
    monkeypatch.setattr(_le, "run_local_verification", _fake)
    out = await node_local_run({"engine": "local", "test_type": "unit",
                                "file_paths": [str(tmp_path / "a.py")]})
    assert out["status"] == "parsed"
    assert out["issues"] == []
    assert out["full_report"]["ok"] is True


async def test_node_local_run_fail_becomes_issue(monkeypatch, tmp_path):
    import icx_engine.testing.local_executor as _le
    async def _fake(repo, test_type, target_url=None, **kw):
        return {"ok": False, "test_type": test_type, "reason": "tests failed",
                "summary": {"total": 3, "passed": 2, "failures": 1}, "runners": ["pytest"], "reports": []}
    monkeypatch.setattr(_le, "run_local_verification", _fake)
    out = await node_local_run({"engine": "local", "test_type": "unit",
                                "file_paths": [str(tmp_path / "a.py")]})
    assert out["status"] == "parsed"
    assert len(out["issues"]) == 1
    assert out["issues"][0]["name"] == "verification_failed"


async def test_node_local_run_guarded_on_exception(monkeypatch, tmp_path):
    import icx_engine.testing.local_executor as _le
    async def _boom(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(_le, "run_local_verification", _boom)
    out = await node_local_run({"engine": "local", "test_type": "unit", "file_paths": []})
    assert out["status"] == "error" and "boom" in out["last_error"]
