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


# -- Step 1b: local_run node ---------------------------------------------------

from icx_engine.testing.nodes import node_local_run


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


# -- Step 2: UI authoring gate + Stagehand harness -----------------------------

from icx_engine.testing.nodes import node_author_flow, route_after_auth


def test_route_after_auth_ui_authors_first():
    assert route_after_auth({"test_type": "ui"}) == "author_flow"
    assert route_after_auth({"test_type": "agent"}) == "author_flow"
    assert route_after_auth({"test_type": "api"}) == "local_run"
    assert route_after_auth({"test_type": "unit"}) == "local_run"
    assert route_after_auth({}) == "local_run"


async def test_node_author_flow_caches_flow(monkeypatch, tmp_path):
    from icx_engine.testing.runners import ui as _ui
    monkeypatch.setattr(_ui, "_ui_cache_dir", lambda: tmp_path)
    monkeypatch.setattr("icx_engine.testing.nodes.interrupt", lambda p: {
        "steps": [
            {"action": "goto", "target": "http://x/login"},
            {"action": "fill", "target": "#user", "value": "a", "description": "enter user"},
            {"action": "click", "target": "#submit"},
            {"action": "assert", "target": ".welcome", "value": "Welcome"},
        ],
        "read_receipts": [],
    })
    out = await node_author_flow({"test_type": "ui", "url": "http://x/login",
                                  "file_paths": ["src/Login.jsx"], "project": "P1"})
    from icx_engine.testing.runners.ui import load_flow
    flow = load_flow("P1")
    assert flow is not None and flow.authored is True
    assert [s.action for s in flow.steps] == ["goto", "fill", "click", "assert"]
    assert "read_receipts" in out


def test_stagehand_harness_asset_exists():
    from icx_engine.testing.runners.install import harness_path
    from pathlib import Path
    p = Path(harness_path())
    assert p.exists() and p.name == "icx-replay.mjs"
    txt = p.read_text(encoding="utf-8")
    assert "Stagehand" in txt and "writeJUnit" in txt
    assert all(ord(c) < 128 for c in txt)  # ASCII


def test_ui_build_command_points_at_packaged_harness(tmp_path):
    from icx_engine.testing.runners import get_runner
    spec = get_runner("stagehand").build_command(tmp_path, runtime_path=None)
    assert spec.command[1].endswith("icx-replay.mjs")
    from pathlib import Path
    assert Path(spec.command[1]).exists()


# -- Step 4: DoD confidence surfaced from local_run ----------------------------

async def test_node_local_run_emits_confidence(monkeypatch, tmp_path):
    import icx_engine.testing.local_executor as _le
    async def _fake(repo, test_type, target_url=None, **kw):
        return {"ok": True, "test_type": test_type, "summary": {"total": 2, "passed": 2},
                "runners": ["pytest"],
                "reports": [{"runner": "pytest", "report_path": "x.xml", "total": 2, "ok": True}]}
    monkeypatch.setattr(_le, "run_local_verification", _fake)
    out = await node_local_run({"engine": "local", "test_type": "unit",
                                "file_paths": [str(tmp_path / "a.py")],
                                "risk_tier": "medium", "selected_layers": ["unit"]})
    fr = out["full_report"]
    assert "confidence" in fr and "dod_items" in fr
    assert fr["confidence"]["confidence_score"] == 1.0
    assert fr["dod_items"][0]["passed"] is True
    assert fr["dod_items"][0]["command"] == "pytest"
