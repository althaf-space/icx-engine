"""Tests for the polyglot unit-runner layer (registry, JUnit parse, adapters, ephemeral repro)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from icx_engine.testing.runners import (
    TestReport, TestCase, RunSpec,
    register_runner, get_runner, detect_runners, list_runners,
    parse_junit_xml, run_ephemeral_repro,
)


# -- Task 1: registry + model --------------------------------------------------

def test_report_ok_logic():
    assert TestReport(total=3, passed=3).ok is True
    assert TestReport(total=3, passed=2, failures=1).ok is False
    assert TestReport(total=0).ok is False  # nothing ran -> not a pass


def test_registry_register_get_detect(tmp_path):
    class _Fake:
        lang = "fake"
        name = "fake-runner"
        def detect(self, repo): return (repo / "FAKEMARK").exists()
        def build_command(self, repo, runtime_path):
            return RunSpec(command=["x"], cwd=str(repo), report_path="r")
    register_runner(_Fake())
    assert get_runner("fake-runner") is not None
    (tmp_path / "FAKEMARK").write_text("", encoding="utf-8")
    assert any(r.name == "fake-runner" for r in detect_runners(tmp_path))


def test_builtin_runners_registered():
    names = {r.name for r in list_runners()}
    assert {"pytest", "vitest", "jest", "junit-maven", "junit-gradle", "go", "cargo"} <= names


# -- Task 2: JUnit XML parsing -------------------------------------------------

def test_parse_all_pass():
    xml = ('<testsuite name="s" tests="2">'
           '<testcase name="a" time="0.1"/>'
           '<testcase name="b" time="0.2"/></testsuite>')
    r = parse_junit_xml(xml)
    assert r.total == 2 and r.passed == 2 and r.ok is True
    assert round(r.time, 2) == 0.3


def test_parse_with_failure_and_error_and_skip():
    xml = ('<testsuites><testsuite name="s">'
           '<testcase name="a"/>'
           '<testcase name="b"><failure message="boom"/></testcase>'
           '<testcase name="c"><error message="crash"/></testcase>'
           '<testcase name="d"><skipped/></testcase>'
           '</testsuite></testsuites>')
    r = parse_junit_xml(xml)
    assert r.total == 4
    assert r.passed == 1 and r.failures == 1 and r.errors == 1 and r.skipped == 1
    assert r.ok is False
    msgs = {c.name: c.message for c in r.cases}
    assert msgs["b"] == "boom" and msgs["c"] == "crash"


def test_parse_malformed_returns_empty():
    r = parse_junit_xml("<not-xml")
    assert r.total == 0 and r.ok is False


# -- Task 3: per-language detection + command shape ----------------------------

def test_pytest_detect_and_command(tmp_path):
    (tmp_path / "conftest.py").write_text("", encoding="utf-8")
    r = get_runner("pytest")
    assert r.detect(tmp_path)
    spec = r.build_command(tmp_path, runtime_path=None)
    assert any(a.startswith("--junitxml=") for a in spec.command)


def test_vitest_detect_from_package_json(tmp_path):
    (tmp_path / "package.json").write_text(
        json.dumps({"devDependencies": {"vitest": "^1.0.0"}}), encoding="utf-8")
    assert get_runner("vitest").detect(tmp_path)
    spec = get_runner("vitest").build_command(tmp_path, None)
    assert "--reporter=junit" in spec.command


def test_gradle_detect_covers_java_kotlin(tmp_path):
    (tmp_path / "build.gradle.kts").write_text("", encoding="utf-8")
    r = get_runner("junit-gradle")
    assert r.detect(tmp_path)
    spec = r.build_command(tmp_path, runtime_path="/opt/jdk17")
    assert spec.env.get("JAVA_HOME") == "/opt/jdk17"
    assert "test-results" in spec.report_path


def test_go_and_cargo_detect(tmp_path):
    (tmp_path / "go.mod").write_text("module x\ngo 1.22\n", encoding="utf-8")
    assert get_runner("go").detect(tmp_path)
    (tmp_path / "Cargo.toml").write_text("[package]\nname='x'\n", encoding="utf-8")
    assert get_runner("cargo").detect(tmp_path)


# -- Task 4: ephemeral repro (real python run) ---------------------------------

def test_ephemeral_python_pass():
    passed, out = run_ephemeral_repro("python", "assert 2 + 2 == 4\nprint('ok')", sys.executable)
    assert passed is True
    assert "ok" in out


def test_ephemeral_python_fail():
    passed, out = run_ephemeral_repro("python", "assert 2 + 2 == 5", sys.executable)
    assert passed is False
    assert "AssertionError" in out or "assert" in out.lower()


def test_ephemeral_unsupported_language():
    passed, out = run_ephemeral_repro("cobol", "x", None)
    assert passed is False
    assert "not supported" in out.lower()


# -- Phase 4: API runners + security -------------------------------------------

from icx_engine.testing.runners import (
    build_security_plan, check_security_headers, REQUIRED_SECURITY_HEADERS,
)


def test_api_runners_registered_with_category():
    assert get_runner("schemathesis").category == "api"
    assert get_runner("hurl").category == "api"


def test_schemathesis_detect_from_openapi(tmp_path):
    (tmp_path / "openapi.yaml").write_text("openapi: 3.0.0\n", encoding="utf-8")
    r = get_runner("schemathesis")
    assert r.detect(tmp_path)
    spec = r.build_command(tmp_path, None)
    assert any(a.startswith("--junit-xml=") for a in spec.command)


def test_schemathesis_detect_nested(tmp_path):
    (tmp_path / "api").mkdir()
    (tmp_path / "api" / "swagger.json").write_text("{}", encoding="utf-8")
    assert get_runner("schemathesis").detect(tmp_path)


def test_hurl_detect_from_hurl_files(tmp_path):
    (tmp_path / "login.hurl").write_text("GET http://x\nHTTP 200\n", encoding="utf-8")
    r = get_runner("hurl")
    assert r.detect(tmp_path)
    spec = r.build_command(tmp_path, None)
    assert "--report-junit" in spec.command


def test_detect_runners_category_filter(tmp_path):
    (tmp_path / "openapi.json").write_text("{}", encoding="utf-8")
    (tmp_path / "conftest.py").write_text("", encoding="utf-8")
    api = {r.name for r in detect_runners(tmp_path, category="api")}
    unit = {r.name for r in detect_runners(tmp_path, category="unit")}
    assert "schemathesis" in api and "pytest" not in api
    assert "pytest" in unit and "schemathesis" not in unit


def test_build_security_plan_auth_ticket():
    a = {"problem_summary": "jwt auth token bypass in login", "detailed_description": "", "impact": ""}
    plan = build_security_plan(a)
    assert "authentication" in plan


def test_build_security_plan_empty_when_no_surface():
    a = {"problem_summary": "rename a css color", "detailed_description": "", "impact": ""}
    plan = build_security_plan(a)
    assert "sql_injection" not in plan and "authentication" not in plan


def test_check_security_headers_flags_missing():
    findings = check_security_headers({"Content-Type": "text/html"})
    assert all(not f.passed for f in findings)  # none of the required headers present
    assert any(f.check == "content-security-policy" and f.severity == "high" for f in findings)


def test_check_security_headers_passes_when_present():
    headers = {h: "x" for h in REQUIRED_SECURITY_HEADERS}
    headers["x-content-type-options"] = "nosniff"
    findings = check_security_headers(headers)
    assert all(f.passed for f in findings)


def test_check_security_headers_nosniff_enforced():
    headers = {h: "x" for h in REQUIRED_SECURITY_HEADERS}
    headers["x-content-type-options"] = "wrong"
    findings = check_security_headers(headers)
    xcto = next(f for f in findings if f.check == "x-content-type-options")
    assert xcto.passed is False


# -- Phase 5: UI runner (Stagehand author -> deterministic Playwright replay) ---

from icx_engine.testing.runners import UiStep, UiFlow, save_flow, load_flow, plan_ui_run
from icx_engine.testing.runners import ui as _ui_mod


def test_ui_runner_registered_with_category():
    assert get_runner("stagehand").category == "ui"


def test_ui_detect_from_frontend_framework(tmp_path):
    (tmp_path / "package.json").write_text(
        json.dumps({"dependencies": {"react": "^18", "react-dom": "^18"}}), encoding="utf-8")
    r = get_runner("stagehand")
    assert r.detect(tmp_path)
    spec = r.build_command(tmp_path, runtime_path=None)
    assert "--mode" in spec.command and "replay" in spec.command
    assert spec.report_path.endswith(".icx-ui-junit.xml")


def test_ui_no_detect_without_frontend(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"dependencies": {"express": "^4"}}), encoding="utf-8")
    assert get_runner("stagehand").detect(tmp_path) is False


def test_flow_cache_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(_ui_mod, "_ui_cache_dir", lambda: tmp_path)
    flow = UiFlow(name="login", url="http://x/login", authored=True, steps=[
        UiStep(action="goto", target="http://x/login"),
        UiStep(action="fill", target="#user", value="a", description="enter user"),
        UiStep(action="click", target="#submit"),
        UiStep(action="assert", target=".welcome", value="Welcome"),
    ])
    save_flow("PROJ-1", flow)
    loaded = load_flow("PROJ-1")
    assert loaded is not None
    assert loaded.authored is True
    assert [s.action for s in loaded.steps] == ["goto", "fill", "click", "assert"]
    assert loaded.steps[1].value == "a"


def test_flow_cache_deterministic_reload(tmp_path, monkeypatch):
    monkeypatch.setattr(_ui_mod, "_ui_cache_dir", lambda: tmp_path)
    flow = UiFlow(name="f", authored=True, steps=[UiStep(action="click", target="#x")])
    save_flow("K", flow)
    a = load_flow("K").to_dict()
    b = load_flow("K").to_dict()
    assert a == b  # identical replay input every time -> deterministic


def test_plan_ui_run_replay_vs_author():
    authored = UiFlow(name="f", authored=True, steps=[UiStep(action="click", target="#x")])
    assert plan_ui_run(authored) == "replay"
    assert plan_ui_run(UiFlow(name="f")) == "author"          # no steps
    assert plan_ui_run(None) == "author"
    not_authored = UiFlow(name="f", authored=False, steps=[UiStep(action="click", target="#x")])
    assert plan_ui_run(not_authored) == "author"


def test_ui_included_in_category_filter(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"dependencies": {"vue": "^3"}}), encoding="utf-8")
    ui = {r.name for r in detect_runners(tmp_path, category="ui")}
    assert "stagehand" in ui


# -- Phase 6: executor (real subprocess run) -----------------------------------

import sys as _sys
from icx_engine.testing.runners import run_spec, run_plan
from icx_engine.testing.runners.base import RunSpec as _RunSpec


def test_executor_runs_real_pytest_pass(tmp_path):
    (tmp_path / "test_ok.py").write_text("def test_ok():\n    assert 1 + 1 == 2\n", encoding="utf-8")
    report = str(tmp_path / "out.xml")
    spec = _RunSpec(command=[_sys.executable, "-m", "pytest", f"--junitxml={report}", "-q"],
                    cwd=str(tmp_path), report_path=report)
    r = run_spec(spec, timeout=120)
    assert r.total >= 1 and r.ok is True


def test_executor_runs_real_pytest_fail(tmp_path):
    (tmp_path / "test_bad.py").write_text("def test_bad():\n    assert 1 == 2\n", encoding="utf-8")
    report = str(tmp_path / "out.xml")
    spec = _RunSpec(command=[_sys.executable, "-m", "pytest", f"--junitxml={report}", "-q"],
                    cwd=str(tmp_path), report_path=report)
    r = run_spec(spec, timeout=120)
    assert r.failures >= 1 and r.ok is False


def test_executor_missing_runner_returns_not_ok(tmp_path):
    spec = _RunSpec(command=["this-binary-does-not-exist-xyz"], cwd=str(tmp_path),
                    report_path=str(tmp_path / "none.xml"))
    r = run_spec(spec, timeout=10)
    assert r.ok is False and r.total == 0


def test_run_plan_parallel(monkeypatch, tmp_path):
    from icx_engine.testing.runners import base as _base
    calls = []
    def _fake(spec, timeout=600.0):
        calls.append(spec.report_path)
        rep = _base.TestReport(total=1, passed=1)
        rep.cases = [_base.TestCase(name="x", status="passed")]
        return rep
    import icx_engine.testing.runners.executor as _ex
    monkeypatch.setattr(_ex, "run_spec", _fake)
    specs = [_RunSpec(command=["a"], cwd=str(tmp_path), report_path=f"r{i}") for i in range(3)]
    out = _ex.run_plan(specs, parallel=True)
    assert len(out) == 3
    assert all(rep.ok for _s, rep in out)
