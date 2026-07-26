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
    async def _run_plan(specs, parallel=True, timeout=600.0, **_kw):
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
    async def _run_plan(specs, parallel=True, timeout=600.0, **_kw):
        captured["env"] = specs[0].env
        return [(specs[0], _report(passed=1))]
    monkeypatch.setattr("icx_engine.testing.runners.run_plan", _run_plan)
    res = await le.run_local_verification(tmp_path, "api", target_url="http://svc",
                                    runtime_resolver=lambda lang: "/opt/py")
    assert seen["rt"] == "/opt/py"
    assert captured["env"]["ICX_TARGET_URL"] == "http://svc"
    assert res["ok"] is True


async def test_agent_type_is_not_a_run_local_verification_category(tmp_path):
    # agent-type is NOT executed by run_local_verification at all anymore - the agent runs its own
    # Playwright test (node_author_flow) and node_local_run reads that report directly
    # (_agent_report_result). Calling run_local_verification with "agent" has no category mapping.
    res = await le.run_local_verification(tmp_path, "agent")
    assert res["ok"] is False and "unknown test type" in res["reason"]


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


# -- Step 2: agent-authored Playwright test gate -------------------------------

from icx_engine.testing.nodes import node_author_flow, route_after_auth


def test_route_after_auth_agent_authors_first():
    assert route_after_auth({"test_type": "agent"}) == "author_flow"
    assert route_after_auth({"test_type": "api"}) == "local_run"
    assert route_after_auth({"test_type": "unit"}) == "local_run"
    assert route_after_auth({}) == "local_run"


async def test_node_author_flow_records_agent_report_fields(monkeypatch, tmp_path):
    monkeypatch.setattr("icx_engine.testing.nodes.interrupt", lambda p: {
        "report_path": str(tmp_path / "r.xml"),
        "test_file": str(tmp_path / "screen.spec.ts"),
        "covered": ["Create", "Edit"],
        "findings": ["delete confirm dialog never closes"],
        "read_receipts": [],
    })
    out = await node_author_flow({"test_type": "agent", "url": "http://x/login",
                                  "file_paths": ["src/Login.jsx"], "project": "P1"})
    assert out["agent_report_path"] == str(tmp_path / "r.xml")
    assert out["agent_test_file"] == str(tmp_path / "screen.spec.ts")
    assert out["agent_covered"] == ["Create", "Edit"]
    assert out["agent_findings"] == ["delete confirm dialog never closes"]
    assert "read_receipts" in out


async def test_node_author_flow_gate_carries_checklist_rules(monkeypatch, tmp_path):
    captured = {}
    monkeypatch.setattr("icx_engine.testing.nodes.interrupt", lambda p: captured.update(p) or {
        "report_path": str(tmp_path / "r.xml")})
    await node_author_flow({"test_type": "agent", "url": "http://x", "file_paths": ["a.jsx"],
                            "project": "P"})
    assert captured["gate"] == "author_flow"
    assert captured["rules"]           # rules_defaults/author_flow.md checklist text
    assert "playwright" in captured    # pinned node/env info for the agent to use
    assert captured["report_path"].endswith(".icx-agent-junit.xml")


async def test_node_author_flow_no_tool_branding_in_data_guidance(monkeypatch, tmp_path):
    captured = {}
    monkeypatch.setattr("icx_engine.testing.nodes.interrupt", lambda p: captured.update(p) or {
        "report_path": str(tmp_path / "r.xml")})
    await node_author_flow({"test_type": "agent", "url": "http://x", "file_paths": ["a.jsx"],
                            "project": "P", "test_writes": True})
    msg = captured["message"]
    assert "ICX_TEST" not in msg          # the old branded tag prefix must be gone
    assert "GENERIC" in msg and "tool/vendor name" in msg


async def test_node_author_flow_message_carries_iteration_cap(monkeypatch, tmp_path):
    captured = {}
    monkeypatch.setattr("icx_engine.testing.nodes.interrupt", lambda p: captured.update(p) or {
        "report_path": str(tmp_path / "r.xml")})
    await node_author_flow({"test_type": "agent", "url": "http://x", "file_paths": ["a.jsx"],
                            "project": "P", "max_iterations": 5})
    assert "5" in captured["message"] and "SELF-FIX BUDGET" in captured["message"]


async def test_node_author_flow_message_mandates_both_sources_and_no_create_step_coverage(monkeypatch, tmp_path):
    captured = {}
    monkeypatch.setattr("icx_engine.testing.nodes.interrupt", lambda p: captured.update(p) or {
        "report_path": str(tmp_path / "r.xml")})
    await node_author_flow({"test_type": "agent", "url": "http://x", "file_paths": ["a.jsx"],
                            "project": "P"})
    msg = captured["message"]
    assert "FLOOR" in msg and "discovered" in msg
    assert "export/upload/download" in msg or "no create-step" in msg.lower()


async def test_node_author_flow_records_agent_discovered(monkeypatch, tmp_path):
    monkeypatch.setattr("icx_engine.testing.nodes.interrupt", lambda p: {
        "report_path": str(tmp_path / "r.xml"),
        "covered": ["Create"],
        "discovered": ["Export CSV"],
    })
    out = await node_author_flow({"test_type": "agent", "url": "http://x", "file_paths": ["a.jsx"],
                                  "project": "P"})
    assert out["agent_discovered"] == ["Export CSV"]


def test_discover_harness_asset_exists():
    from icx_engine.testing.runners.install import discover_harness_path
    from pathlib import Path
    p = Path(discover_harness_path())
    assert p.exists() and p.name == "icx-discover.mjs"
    txt = p.read_text(encoding="utf-8")
    assert "discoverTopLevel" in txt and "functionalities" in txt
    assert all(ord(c) < 128 for c in txt)  # ASCII


# -- run_ui_discovery: runtime census auto-discovery (COMBINED census, live half) --

async def test_run_ui_discovery_empty_url_returns_none(tmp_path):
    from icx_engine.testing.local_executor import run_ui_discovery
    assert await run_ui_discovery(str(tmp_path), "") is None


async def test_run_ui_discovery_no_runner_returns_none(monkeypatch, tmp_path):
    from icx_engine.testing.local_executor import run_ui_discovery
    monkeypatch.setattr("icx_engine.testing.runners.install.ensure_runner",
                        lambda name, approve=None: None)
    assert await run_ui_discovery(str(tmp_path), "http://x/#/users") is None


async def test_run_ui_discovery_parses_census(monkeypatch, tmp_path):
    from icx_engine.testing.local_executor import run_ui_discovery
    monkeypatch.setattr("icx_engine.testing.runners.install.ensure_runner",
                        lambda name, approve=None: "/opt/icx/playwright/1.48.0")
    async def _fake_run_spec(spec, timeout=0, **kwargs):
        Path(spec.report_path).write_text(
            '{"functionalities":[{"functionality":"Create","modalDetails":{"triggerSelector":"#c"}}]}',
            encoding="utf-8")
    monkeypatch.setattr("icx_engine.testing.runners.executor.run_spec", _fake_run_spec)
    out = await run_ui_discovery(str(tmp_path), "http://x/#/users")
    assert isinstance(out, dict)
    assert out["functionalities"][0]["functionality"] == "Create"


async def test_run_ui_discovery_empty_crawl_returns_none(monkeypatch, tmp_path):
    from icx_engine.testing.local_executor import run_ui_discovery
    monkeypatch.setattr("icx_engine.testing.runners.install.ensure_runner",
                        lambda name, approve=None: "/opt/icx/playwright/1.48.0")
    async def _fake_run_spec(spec, timeout=0, **kwargs):
        Path(spec.report_path).write_text('{"functionalities":[]}', encoding="utf-8")
    monkeypatch.setattr("icx_engine.testing.runners.executor.run_spec", _fake_run_spec)
    assert await run_ui_discovery(str(tmp_path), "http://x/#/users") is None


# -- COMBINED census wiring: author_flow always fuses discovery + source ------

def _source_model():
    return {"functionalities": [{"functionality": "Create",
            "modalDetails": {"triggerSelector": "#c"}, "submitButton": {"selectors": ["#s"]},
            "fields": [{"label": "Name", "domSelectors": ["#n"], "type": "text"}]}]}


async def test_author_flow_merges_discovered_census(monkeypatch, tmp_path):
    async def _disc(repo, url, **kw):
        # discovery surfaces an Export the source census missed
        return {"functionalities": [{"functionality": "Export CSV",
                "modalDetails": {"triggerSelector": "#exp"}, "type": "Download"}]}
    monkeypatch.setattr(le, "run_ui_discovery", _disc)
    monkeypatch.setattr("icx_engine.testing.nodes.interrupt",
                        lambda p: {"report_path": str(tmp_path / "r.xml")})
    out = await node_author_flow({"test_type": "agent", "url": "http://x/#/u",
                                  "file_paths": ["src/U.jsx"], "project": "PC1",
                                  "screen_model": _source_model()})
    merged = out["screen_model"]
    kinds = [f["functionality"] for f in merged["functionalities"]]
    assert "Create" in kinds and any("Export" in k for k in kinds)   # both halves present


async def test_author_flow_degrades_to_source_when_no_discovery(monkeypatch, tmp_path):
    async def _disc(repo, url, **kw):
        return None                                    # app/session unavailable
    monkeypatch.setattr(le, "run_ui_discovery", _disc)
    monkeypatch.setattr("icx_engine.testing.nodes.interrupt",
                        lambda p: {"report_path": str(tmp_path / "r.xml")})
    out = await node_author_flow({"test_type": "agent", "url": "http://x/#/u",
                                  "file_paths": ["src/U.jsx"], "project": "PC2",
                                  "screen_model": _source_model()})
    kinds = [f["functionality"] for f in out["screen_model"]["functionalities"]]
    assert kinds == ["Create"]                          # unchanged source census


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
    async def _run_plan(specs, parallel=True, timeout=600.0, **_kw):
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


# -- agent-type completion: node_local_run reads the agent's own JUnit report ------

_JUNIT_ONE_PASS = ('<?xml version="1.0"?><testsuite tests="1" failures="0" errors="0">'
                  '<testcase name="create user" time="0.1"/></testsuite>')


async def test_node_local_run_agent_type_never_calls_run_local_verification(monkeypatch, tmp_path):
    report = tmp_path / "r.xml"
    report.write_text(_JUNIT_ONE_PASS, encoding="utf-8")

    async def _boom(*a, **k):
        raise AssertionError("run_local_verification must not be called for agent-type")
    monkeypatch.setattr("icx_engine.testing.local_executor.run_local_verification", _boom)

    out = await node_local_run({"engine": "local", "test_type": "agent",
                                "agent_report_path": str(report)})
    assert out["status"] == "parsed"
    assert out["full_report"]["ok"] is True
    assert out["full_report"]["summary"]["total"] == 1


async def test_node_local_run_agent_type_missing_report_is_not_ok(tmp_path):
    out = await node_local_run({"engine": "local", "test_type": "agent",
                                "agent_report_path": str(tmp_path / "never-written.xml")})
    assert out["status"] == "parsed"
    assert out["full_report"]["ok"] is False
    assert "no JUnit report found" in out["full_report"]["reason"]
    assert len(out["issues"]) == 1


async def test_node_local_run_agent_type_flags_census_coverage_gaps(tmp_path):
    report = tmp_path / "r.xml"
    report.write_text(_JUNIT_ONE_PASS, encoding="utf-8")
    model = {"functionalities": [{"functionality": "Create"}, {"functionality": "Delete"}]}
    out = await node_local_run({"engine": "local", "test_type": "agent",
                                "agent_report_path": str(report), "screen_model": model,
                                "agent_covered": ["Create"]})   # Delete never mentioned
    fr = out["full_report"]
    assert fr["ok"] is False                     # passing tests, but coverage is incomplete
    assert fr["coverage_gaps"] == ["Delete"]


async def test_node_local_run_agent_type_discovered_closes_coverage_gap(tmp_path):
    report = tmp_path / "r.xml"
    report.write_text(_JUNIT_ONE_PASS, encoding="utf-8")
    model = {"functionalities": [{"functionality": "Create"}, {"functionality": "Export CSV"}]}
    out = await node_local_run({"engine": "local", "test_type": "agent",
                                "agent_report_path": str(report), "screen_model": model,
                                "agent_covered": ["Create"],
                                "agent_discovered": ["Export CSV"]})  # agent found it itself
    fr = out["full_report"]
    assert fr["ok"] is True
    assert fr["coverage_gaps"] == []
    assert fr["discovered"] == ["Export CSV"]


async def test_node_local_run_agent_type_no_gaps_when_fully_covered(tmp_path):
    report = tmp_path / "r.xml"
    report.write_text(_JUNIT_ONE_PASS, encoding="utf-8")
    model = {"functionalities": [{"functionality": "Create"}]}
    out = await node_local_run({"engine": "local", "test_type": "agent",
                                "agent_report_path": str(report), "screen_model": model,
                                "agent_covered": ["Create"]})
    assert out["full_report"]["ok"] is True
    assert out["full_report"]["coverage_gaps"] == []


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
    async def _run_plan(specs, parallel=True, timeout=600.0, **_kw):
        captured["env"] = specs[0].env
        return [(specs[0], _report(passed=1))]
    monkeypatch.setattr("icx_engine.testing.runners.run_plan", _run_plan)
    await le.run_local_verification(tmp_path, "api", target_url="http://svc", approve=lambda n: True)
    import os
    assert "/opt/icx/hurl/5.0.1" in captured["env"]["PATH"].split(os.pathsep)


async def test_author_flow_gate_tells_agent_to_launch_headed_with_slowmo(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr("icx_engine.testing.nodes.interrupt",
                        lambda p: seen.update(msg=p["message"], headless=p["headless"], slowmo=p["slowmo"])
                        or {"report_path": str(tmp_path / "r.xml")})
    await node_author_flow({"test_type": "agent", "url": "http://x", "file_paths": ["a.jsx"],
                            "project": "A", "headless": False, "slowmo": 750})
    assert seen["headless"] is False and seen["slowmo"] == 750
    assert "HEADED" in seen["msg"] and "750" in seen["msg"]


async def test_author_flow_gate_default_headless(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr("icx_engine.testing.nodes.interrupt",
                        lambda p: seen.update(msg=p["message"], headless=p["headless"])
                        or {"report_path": str(tmp_path / "r.xml")})
    await node_author_flow({"test_type": "agent", "url": "http://x", "file_paths": ["a.jsx"], "project": "B"})
    assert seen["headless"] is True and "HEADLESS" in seen["msg"]
