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
    def __init__(self, name, lang, category, requires=None):
        self.name, self.lang, self.category = name, lang, category
        if requires is not None:
            self.requires = requires
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


# -- Step 6: real end-to-end proof (full detect -> build -> async run -> parse) --
# These run a REAL pytest subprocess on a generated temp repo. pytest is always present
# in this repo's own test env, so the unit path is exercised end to end. The api path
# proves the guard: a detected-but-uninstalled runner yields a not-ok result, never a raise.

import sys as _sys
from icx_engine.testing import nodes as _nodes


def _write_pytest_repo(root, body):
    (root / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (root / "test_sample.py").write_text(body, encoding="utf-8")


async def test_e2e_real_pytest_all_pass(tmp_path):
    _write_pytest_repo(tmp_path, "def test_a():\n    assert 1 + 1 == 2\n\ndef test_b():\n    assert True\n")
    res = await le.run_local_verification(tmp_path, "unit",
                                          runtime_resolver=lambda lang: _sys.executable)
    assert res["ok"] is True
    assert res["runners"] == ["pytest"]
    assert res["summary"]["total"] == 2
    assert res["summary"]["passed"] == 2
    assert res["summary"]["failures"] == 0


async def test_e2e_real_pytest_reports_failure(tmp_path):
    _write_pytest_repo(tmp_path, "def test_pass():\n    assert True\n\ndef test_fail():\n    assert 1 == 2\n")
    res = await le.run_local_verification(tmp_path, "unit",
                                          runtime_resolver=lambda lang: _sys.executable)
    assert res["ok"] is False
    assert res["summary"]["total"] == 2
    assert res["summary"]["passed"] == 1
    assert res["summary"]["failures"] == 1


async def test_e2e_node_local_run_real_pytest_with_dod(tmp_path, monkeypatch):
    # Full LangGraph node path on a real pytest run, including the DoD confidence report.
    _write_pytest_repo(tmp_path, "def test_ok():\n    assert True\n")

    async def _rr(repo):
        async def _resolve(lang):
            return _sys.executable
        return _resolve
    monkeypatch.setattr(_nodes, "_runtime_resolver", _rr)

    out = await node_local_run({"engine": "local", "test_type": "unit",
                                "file_paths": [str(tmp_path / "test_sample.py")],
                                "risk_tier": "medium", "selected_layers": ["unit"]})
    assert out["status"] == "parsed"
    assert out["issues"] == []
    fr = out["full_report"]
    assert fr["ok"] is True
    assert fr["summary"]["passed"] == 1
    assert "confidence" in fr and "dod_items" in fr


async def test_e2e_uninstalled_api_runner_skips_gracefully(tmp_path):
    # A detected runner whose tool is not installed must yield a not-ok result, never raise.
    (tmp_path / "openapi.json").write_text('{"openapi": "3.0.0", "paths": {}}', encoding="utf-8")
    res = await le.run_local_verification(tmp_path, "api", target_url="http://localhost:1",
                                          runtime_resolver=lambda lang: None)
    assert res["ok"] is False
    assert "schemathesis" in res["runners"]


# -- ensure_runner gate: ICX-owned tooling must be present or install user-approved ----

async def test_unapproved_icx_runner_reported_unavailable(monkeypatch, tmp_path):
    # Runner needs an ICX-owned tool; ensure_runner returns None (uninstalled + unapproved).
    monkeypatch.setattr("icx_engine.testing.runners.detect_runners",
                        lambda repo, category=None: [_FakeRunner("schemathesis", "http", "api",
                                                                  requires="schemathesis")])
    monkeypatch.setattr("icx_engine.testing.runners.install.ensure_runner",
                        lambda name, approve=None: None)
    called = {"run_plan": False}
    async def _no_run(specs, parallel=True, timeout=600.0):
        called["run_plan"] = True
        return []
    monkeypatch.setattr("icx_engine.testing.runners.run_plan", _no_run)
    res = await le.run_local_verification(tmp_path, "api", target_url="http://svc")
    assert res["ok"] is False
    assert called["run_plan"] is False            # never even attempted
    assert res["unavailable"] and res["unavailable"][0]["runner"] == "schemathesis"
    assert "unapproved/uninstalled" in res["reason"]


async def test_approved_icx_runner_runs(monkeypatch, tmp_path):
    monkeypatch.setattr("icx_engine.testing.runners.detect_runners",
                        lambda repo, category=None: [_FakeRunner("schemathesis", "http", "api",
                                                                  requires="schemathesis")])
    monkeypatch.setattr("icx_engine.testing.runners.install.ensure_runner",
                        lambda name, approve=None: "/opt/icx/testing/schemathesis/3.36.0")
    monkeypatch.setattr("icx_engine.testing.runners.run_plan",
                        _aplan([(0, _report(passed=2))]))
    res = await le.run_local_verification(tmp_path, "api", target_url="http://svc",
                                          approve=lambda n: True)
    assert res["ok"] is True
    assert res["runners"] == ["schemathesis"]
    assert res["unavailable"] == []


async def test_schemathesis_gets_base_url_flag(monkeypatch, tmp_path):
    # The confirmed URL must reach the schemathesis CLI as --base-url (env alone is not read by it).
    fake = _FakeRunner("schemathesis", "http", "api", requires="schemathesis")
    def _bc(repo, runtime_path):
        return RunSpec(command=["schemathesis", "run", "openapi.json"], cwd=str(repo),
                       report_path="r.xml")
    fake.build_command = _bc
    monkeypatch.setattr("icx_engine.testing.runners.detect_runners",
                        lambda repo, category=None: [fake])
    monkeypatch.setattr("icx_engine.testing.runners.install.ensure_runner",
                        lambda name, approve=None: "/opt/schemathesis")
    captured = {}
    async def _run_plan(specs, parallel=True, timeout=600.0):
        captured["cmd"] = specs[0].command
        captured["env"] = specs[0].env
        return [(specs[0], _report(passed=1))]
    monkeypatch.setattr("icx_engine.testing.runners.run_plan", _run_plan)
    await le.run_local_verification(tmp_path, "api", target_url="http://svc", approve=lambda n: True)
    assert "--base-url=http://svc" in captured["cmd"]
    assert captured["env"]["ICX_TARGET_URL"] == "http://svc"   # env still set for the ICX harness


async def test_pytest_no_requires_not_gated(monkeypatch, tmp_path):
    # A user-SDK runner (no `requires`) must never be gated on ensure_runner.
    def _boom(name, approve=None):
        raise AssertionError("ensure_runner must not be consulted for user-SDK runners")
    monkeypatch.setattr("icx_engine.testing.runners.detect_runners",
                        lambda repo, category=None: [_FakeRunner("pytest", "python", "unit")])
    monkeypatch.setattr("icx_engine.testing.runners.install.ensure_runner", _boom)
    monkeypatch.setattr("icx_engine.testing.runners.run_plan",
                        _aplan([(0, _report(passed=1))]))
    res = await le.run_local_verification(tmp_path, "unit")
    assert res["ok"] is True


# -- authenticated UI replay: node_local_run threads the captured session through ---

async def test_node_local_run_passes_storage_state(monkeypatch, tmp_path):
    import icx_engine.testing.local_executor as _le
    from icx_engine.testing import auth as _auth
    state_file = tmp_path / "st.json"
    state_file.write_text("{}", encoding="utf-8")
    rec = type("R", (), {"storage_state": str(state_file)})()
    monkeypatch.setattr(_auth, "load_session", lambda p, h: rec)
    monkeypatch.setattr(_auth, "host_of", lambda u: "host")
    monkeypatch.setattr("icx_engine.testing.runners.ui.flow_path", lambda k: "/flow")
    seen = {}

    async def _fake(repo, tt, **kw):
        seen.update(kw)
        return {"ok": True, "test_type": tt, "summary": {}, "runners": [], "reports": []}
    monkeypatch.setattr(_le, "run_local_verification", _fake)

    await node_local_run({"engine": "local", "test_type": "ui", "url": "http://x",
                          "project": "proj", "file_paths": [str(tmp_path / "a.jsx")]})
    assert seen.get("storage_state") == str(state_file)


# -- installed ICX tool is put on the runner's PATH --------------------------------

def test_prepend_tool_env_puts_install_dir_first():
    from icx_engine.testing.local_executor import _prepend_tool_env
    spec = RunSpec(command=["hurl"], cwd=".", report_path="r")
    _prepend_tool_env(spec, "/opt/icx/hurl")
    import os
    assert spec.env["PATH"].split(os.pathsep)[0] == "/opt/icx/hurl"
    assert "/opt/icx/hurl" in spec.env["PYTHONPATH"]


async def test_installed_hurl_path_wired_onto_runner(monkeypatch, tmp_path):
    fake = _FakeRunner("hurl", "http", "api", requires="hurl")
    monkeypatch.setattr("icx_engine.testing.runners.detect_runners",
                        lambda repo, category=None: [fake])
    monkeypatch.setattr("icx_engine.testing.runners.install.ensure_runner",
                        lambda name, approve=None, node_dir=None: "/opt/icx/hurl/5.0.1")
    captured = {}
    async def _run_plan(specs, parallel=True, timeout=600.0):
        captured["env"] = specs[0].env
        return [(specs[0], _report(passed=1))]
    monkeypatch.setattr("icx_engine.testing.runners.run_plan", _run_plan)
    await le.run_local_verification(tmp_path, "api", target_url="http://svc", approve=lambda n: True)
    import os
    assert "/opt/icx/hurl/5.0.1" in captured["env"]["PATH"].split(os.pathsep)


async def test_ui_headed_env_injected(monkeypatch, tmp_path):
    fake = _FakeRunner("stagehand", "ui", "ui")
    monkeypatch.setattr("icx_engine.testing.runners.detect_runners",
                        lambda repo, category=None: [fake])
    captured = {}
    async def _run_plan(specs, parallel=True, timeout=600.0):
        captured["env"] = specs[0].env
        return [(specs[0], _report(passed=1))]
    monkeypatch.setattr("icx_engine.testing.runners.run_plan", _run_plan)
    await le.run_local_verification(tmp_path, "ui", target_url="http://x", ui_headed=True)
    assert captured["env"].get("ICX_UI_HEADED") == "1"
    # default (headless) -> not set
    captured.clear()
    await le.run_local_verification(tmp_path, "ui", target_url="http://x", ui_headed=False)
    assert "ICX_UI_HEADED" not in captured["env"]


async def test_author_flow_agent_is_exploratory_ui_is_scripted(monkeypatch, tmp_path):
    from icx_engine.testing.runners import ui as _ui
    monkeypatch.setattr(_ui, "_ui_cache_dir", lambda: tmp_path)
    seen = {}
    monkeypatch.setattr("icx_engine.testing.nodes.interrupt",
                        lambda p: seen.update(msg=p["message"], tt=p["test_type"]) or {"steps": [], "read_receipts": []})
    await node_author_flow({"test_type": "agent", "url": "http://x", "file_paths": ["a.jsx"], "project": "A"})
    assert seen["tt"] == "agent" and "EXPLORATORY" in seen["msg"] and "edge case" in seen["msg"].lower()
    await node_author_flow({"test_type": "ui", "url": "http://x", "file_paths": ["a.jsx"], "project": "B"})
    assert seen["tt"] == "ui" and "SCRIPTED" in seen["msg"]
