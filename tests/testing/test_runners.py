"""Tests for the polyglot unit-runner layer (registry, JUnit parse, adapters, ephemeral repro)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

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


def test_parse_report_with_ansi_control_chars_in_failure():
    # REGRESSION: Playwright (and pytest/jest with colour) embed ANSI escape codes (ESC = 0x1b) and
    # other C0 controls in failure text. Those bytes are INVALID in XML 1.0 - left raw the whole
    # document is "not well-formed" and the parse yields ZERO cases: the exact "0 tests ran" bug
    # seen whenever a UI run had any failure. The parser must strip them and still read every case.
    esc = "\x1b"
    xml = ('<testsuite name="icx-ui" tests="2" failures="1">'
           '<testcase classname="ui-flow" name="opens" time="1.0"/>'
           f'<testcase classname="ui-flow" name="clicks"><failure message="Timeout.'
           f'{esc}[2m  - waiting for locator{esc}[22m\x00\x0b bad"/></testcase>'
           '</testsuite>')
    r = parse_junit_xml(xml)
    assert r.total == 2 and r.passed == 1 and r.failures == 1
    assert "waiting for locator" in {c.name: c.message for c in r.cases}["clicks"]


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


def test_ui_runner_is_repo_agnostic(tmp_path):
    # UI testing is URL-driven; ICX brings its own Playwright/Stagehand. The runner must be available
    # regardless of repo contents - a frontend repo, a backend repo, or an empty dir all qualify.
    r = get_runner("stagehand")
    assert r.detect(tmp_path) is True                                   # empty dir
    (tmp_path / "package.json").write_text(
        json.dumps({"dependencies": {"react": "^18"}}), encoding="utf-8")
    assert r.detect(tmp_path) is True                                   # frontend repo
    spec = r.build_command(tmp_path, runtime_path=None)
    assert "--mode" in spec.command and "replay" in spec.command
    assert spec.report_path.endswith(".icx-ui-junit.xml")


def test_ui_detect_true_for_backend_only_repo(tmp_path):
    # A Spring Boot / Express backend serving a UI must still get UI testing (hits the URL).
    (tmp_path / "package.json").write_text(json.dumps({"dependencies": {"express": "^4"}}), encoding="utf-8")
    assert get_runner("stagehand").detect(tmp_path) is True
    (tmp_path / "pom.xml").write_text("<project/>", encoding="utf-8")   # pure java backend
    assert get_runner("stagehand").detect(tmp_path) is True


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


def test_flow_cache_roundtrip_select_and_waitfor(tmp_path, monkeypatch):
    # Real-world login: text fields + a <select> dropdown (e.g. tenant) + a non-submit
    # button, then wait for the post-login redirect before asserting. select/waitfor must
    # survive the cache so the agent-authored flow replays deterministically.
    monkeypatch.setattr(_ui_mod, "_ui_cache_dir", lambda: tmp_path)
    flow = UiFlow(name="login", url="http://x/login", authored=True, steps=[
        UiStep(action="goto", target="http://x/login"),
        UiStep(action="fill", target="#loginUsername", value="admin"),
        UiStep(action="fill", target="#loginPassword", value="admin"),
        UiStep(action="select", target="#tenant", value="SMART", description="pick tenant"),
        UiStep(action="click", target="#loginButton"),
        UiStep(action="waitfor", target="body", description="post-login redirect"),
        UiStep(action="assert", target="body", value="app"),
    ])
    save_flow("PROJ-SEL", flow)
    loaded = load_flow("PROJ-SEL")
    assert loaded is not None
    assert [s.action for s in loaded.steps] == \
        ["goto", "fill", "fill", "select", "click", "waitfor", "assert"]
    sel = next(s for s in loaded.steps if s.action == "select")
    assert sel.target == "#tenant" and sel.value == "SMART"


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


async def test_executor_runs_real_pytest_pass(tmp_path):
    (tmp_path / "test_ok.py").write_text("def test_ok():\n    assert 1 + 1 == 2\n", encoding="utf-8")
    report = str(tmp_path / "out.xml")
    spec = _RunSpec(command=[_sys.executable, "-m", "pytest", f"--junitxml={report}", "-q"],
                    cwd=str(tmp_path), report_path=report)
    r = await run_spec(spec, timeout=120)
    assert r.total >= 1 and r.ok is True


async def test_executor_runs_real_pytest_fail(tmp_path):
    (tmp_path / "test_bad.py").write_text("def test_bad():\n    assert 1 == 2\n", encoding="utf-8")
    report = str(tmp_path / "out.xml")
    spec = _RunSpec(command=[_sys.executable, "-m", "pytest", f"--junitxml={report}", "-q"],
                    cwd=str(tmp_path), report_path=report)
    r = await run_spec(spec, timeout=120)
    assert r.failures >= 1 and r.ok is False


async def test_executor_missing_runner_returns_not_ok(tmp_path):
    spec = _RunSpec(command=["this-binary-does-not-exist-xyz"], cwd=str(tmp_path),
                    report_path=str(tmp_path / "none.xml"))
    r = await run_spec(spec, timeout=10)
    assert r.ok is False and r.total == 0


def _writer_cmd(report: str, content: str) -> list:
    # a subprocess that writes `content` to `report` - stands in for a harness that produces an artifact.
    code = ("import io,sys;"
            "open(r%r,'w',encoding='utf-8').write(%r)" % (report, content))
    return [_sys.executable, "-c", code]


async def test_run_spec_deletes_own_report_by_default(tmp_path):
    # an ICX-owned report (.icx- prefix) the runner wrote is removed after parsing by default - no litter.
    from pathlib import Path as _P
    report = str(tmp_path / ".icx-own.xml")
    spec = _RunSpec(command=_writer_cmd(report, "<testsuite><testcase name='t'/></testsuite>"),
                    cwd=str(tmp_path), report_path=report)
    await run_spec(spec, timeout=30)
    assert not _P(report).exists()


async def test_run_spec_keep_report_preserves_own_artifact(tmp_path):
    # keep_report=True leaves the ICX-owned output on disk so a caller (UI discover/verify/replay) can
    # read the harness's own census/JSON artifact from that path AFTER run_spec returns. This guards the
    # bug where run_spec deleted the discover census before the caller read it (-> silent None).
    from pathlib import Path as _P
    report = str(tmp_path / ".icx-census.json")
    spec = _RunSpec(command=_writer_cmd(report, '{"functionalities": [{"functionality": "Create"}]}'),
                    cwd=str(tmp_path), report_path=report)
    await run_spec(spec, timeout=30, keep_report=True)
    assert _P(report).exists()
    import json as _json
    assert _json.loads(_P(report).read_text(encoding="utf-8"))["functionalities"][0]["functionality"] == "Create"


async def test_run_plan_parallel_concurrent(monkeypatch, tmp_path):
    from icx_engine.testing.runners import base as _base
    async def _fake(spec, timeout=600.0):
        rep = _base.TestReport(total=1, passed=1)
        rep.cases = [_base.TestCase(name="x", status="passed")]
        return rep
    import icx_engine.testing.runners.executor as _ex
    monkeypatch.setattr(_ex, "run_spec", _fake)
    specs = [_RunSpec(command=["a"], cwd=str(tmp_path), report_path=f"r{i}") for i in range(3)]
    out = await _ex.run_plan(specs, parallel=True)
    assert len(out) == 3
    assert all(rep.ok for _s, rep in out)


# -- Review fixes: process-tree kill, cancel, report freshness, bounded parallel ----

import asyncio as _asyncio
import icx_engine.testing.runners.executor as _ex_mod


async def test_run_spec_kills_process_tree_on_timeout(monkeypatch, tmp_path):
    # A real long-running child must be tree-killed (not left orphaned) when it overruns.
    killed = {}
    real_kill = _ex_mod._kill_tree
    def _spy(pid):
        killed["pid"] = pid
        real_kill(pid)      # actually terminate so the test leaves no orphan
    monkeypatch.setattr(_ex_mod, "_kill_tree", _spy)
    spec = _RunSpec(command=[_sys.executable, "-c", "import time; time.sleep(30)"],
                    cwd=str(tmp_path), report_path=str(tmp_path / ".icx-junit.xml"))
    r = await run_spec(spec, timeout=0.5)
    assert "pid" in killed              # tree kill fired
    assert r.total == 0 and r.ok is False


async def test_run_spec_reraises_cancel_and_kills_tree(monkeypatch, tmp_path):
    killed = {}
    monkeypatch.setattr(_ex_mod, "_kill_tree", lambda pid: killed.setdefault("pid", pid))

    class _FakeProc:
        pid = 4242
        def __init__(self): self.calls = 0
        async def wait(self):
            self.calls += 1
            if self.calls == 1:
                raise _asyncio.CancelledError()
            return 0

    async def _fake_exec(*a, **k):
        return _FakeProc()
    monkeypatch.setattr(_ex_mod.asyncio, "create_subprocess_exec", _fake_exec)

    spec = _RunSpec(command=["x"], cwd=str(tmp_path), report_path=str(tmp_path / ".icx-junit.xml"))
    import pytest
    with pytest.raises(_asyncio.CancelledError):
        await run_spec(spec, timeout=5)
    assert killed.get("pid") == 4242


async def test_run_spec_deletes_stale_own_report_before_run(tmp_path):
    # A prior run's .icx- report must never be scored as this run's result.
    stale = tmp_path / ".icx-junit.xml"
    stale.write_text('<testsuite tests="5" failures="0"><testcase name="old"/></testsuite>',
                     encoding="utf-8")
    spec = _RunSpec(command=["this-binary-does-not-exist-xyz"], cwd=str(tmp_path),
                    report_path=str(stale))
    r = await run_spec(spec, timeout=5)
    assert r.total == 0                 # stale 5 not counted
    assert not stale.exists()           # cleaned, not left to litter


async def test_run_spec_cleans_own_report_after_parse(tmp_path):
    (tmp_path / "test_ok.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    report = tmp_path / ".icx-junit.xml"
    spec = _RunSpec(command=[_sys.executable, "-m", "pytest", f"--junitxml={report}", "-q"],
                    cwd=str(tmp_path), report_path=str(report))
    r = await run_spec(spec, timeout=120)
    assert r.total >= 1 and r.ok is True
    assert not report.exists()          # our file removed from the user's repo after parse


async def test_run_spec_leaves_non_icx_report(tmp_path):
    # Surefire/Gradle-style paths (not ".icx-*") must never be deleted by us.
    (tmp_path / "test_ok.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    report = tmp_path / "out.xml"       # user/tool-owned name
    spec = _RunSpec(command=[_sys.executable, "-m", "pytest", f"--junitxml={report}", "-q"],
                    cwd=str(tmp_path), report_path=str(report))
    r = await run_spec(spec, timeout=120)
    assert r.ok is True and report.exists()


def test_ui_build_command_verify_mode(tmp_path):
    # verify mode targets a caller-supplied report path and passes --mode verify to the harness.
    from icx_engine.testing.runners.ui import _StagehandUi
    out = str(tmp_path / "verify.json")
    spec = _StagehandUi().build_command(tmp_path, None, mode="verify", report=out)
    assert "--mode" in spec.command and spec.command[spec.command.index("--mode") + 1] == "verify"
    assert spec.report_path == out and out in spec.command


def test_ui_build_command_defaults_to_replay(tmp_path):
    from icx_engine.testing.runners.ui import _StagehandUi
    spec = _StagehandUi().build_command(tmp_path, None)
    assert spec.command[spec.command.index("--mode") + 1] == "replay"
    assert spec.report_path.endswith(".icx-ui-junit.xml")


async def test_run_ui_verify_none_when_no_ui_runner(monkeypatch, tmp_path):
    # best-effort: no stagehand runner detected -> returns None, never raises.
    import icx_engine.testing.local_executor as le
    monkeypatch.setattr("icx_engine.testing.runners.detect_runners",
                        lambda repo, category=None: [])
    r = await le.run_ui_verify(tmp_path, flow_path="/f.json", target_url="http://x")
    assert r is None


def test_parse_report_path_dir_rejects_stale_xml(tmp_path):
    # A report DIRECTORY (Surefire/Gradle) may hold XML from a PRIOR build. When the runner fails to
    # regenerate it, min_mtime must reject the stale file so it is not scored as this run's green.
    import os
    from icx_engine.testing.runners.executor import _parse_report_path
    d = tmp_path / "surefire"
    d.mkdir()
    old = d / "TEST-old.xml"
    old.write_text('<testsuite name="s" tests="3"><testcase name="a"/><testcase name="b"/>'
                   '<testcase name="c"/></testsuite>', encoding="utf-8")
    old_time = 1_000_000.0                       # far in the past
    os.utime(old, (old_time, old_time))
    # no min_mtime -> counts the (stale) file
    assert _parse_report_path(str(d)).total == 3
    # min_mtime after the file's mtime -> rejected -> empty
    assert _parse_report_path(str(d), min_mtime=old_time + 100).total == 0


def test_spawn_kwargs_no_rlimit_by_default(monkeypatch):
    monkeypatch.delenv("ICX_TEST_RLIMIT_MEM_MB", raising=False)
    monkeypatch.delenv("ICX_TEST_RLIMIT_CPU_S", raising=False)
    kw = _ex_mod._spawn_kwargs()
    assert "preexec_fn" not in kw            # opt-in only; default = no resource cap


@pytest.mark.skipif(_sys.platform == "win32", reason="preexec_fn / rlimit are POSIX only")
def test_spawn_kwargs_adds_preexec_when_env_set(monkeypatch):
    monkeypatch.setenv("ICX_TEST_RLIMIT_MEM_MB", "512")
    kw = _ex_mod._spawn_kwargs()
    assert callable(kw.get("preexec_fn"))


async def test_executor_logs_runner_done(caplog, tmp_path):
    import logging
    (tmp_path / "test_ok.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    report = tmp_path / ".icx-junit.xml"
    spec = _RunSpec(command=[_sys.executable, "-m", "pytest", f"--junitxml={report}", "-q"],
                    cwd=str(tmp_path), report_path=str(report))
    with caplog.at_level(logging.INFO, logger="icx.testing.executor"):
        await run_spec(spec, timeout=120)
    assert any("runner done" in r.getMessage() for r in caplog.records)


# -- JUnit parser is XXE / billion-laughs hardened (report XML is untrusted) --------

def _parse(x):
    from icx_engine.testing.runners import parse_junit_xml
    return parse_junit_xml(x)


def test_junit_billion_laughs_returns_empty():
    payload = (
        '<?xml version="1.0"?>'
        '<!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;">]>'
        '<testsuite><testcase name="&lol2;"/></testsuite>'
    )
    r = _parse(payload)
    assert r.total == 0            # entity expansion refused, not exploded


def test_junit_external_entity_blocked():
    payload = (
        '<?xml version="1.0"?>'
        '<!DOCTYPE t [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
        '<testsuite><testcase name="&x;"/></testsuite>'
    )
    r = _parse(payload)
    assert r.total == 0            # external reference blocked, no file read


def test_junit_normal_xml_still_parses():
    payload = ('<testsuite><testcase name="a"/>'
               '<testcase name="b"><failure message="boom"/></testcase></testsuite>')
    r = _parse(payload)
    assert r.total == 2 and r.failures == 1


async def test_run_plan_bounds_parallelism(monkeypatch, tmp_path):
    cur = {"n": 0, "peak": 0}
    async def _fake(spec, timeout=600.0):
        cur["n"] += 1
        cur["peak"] = max(cur["peak"], cur["n"])
        await _asyncio.sleep(0.02)
        cur["n"] -= 1
        return TestReport(total=1, passed=1, cases=[TestCase(name="x")])
    monkeypatch.setattr(_ex_mod, "run_spec", _fake)
    specs = [_RunSpec(command=["a"], cwd=str(tmp_path), report_path=f"r{i}") for i in range(6)]
    out = await _ex_mod.run_plan(specs, parallel=True, max_parallel=2)
    assert len(out) == 6
    assert cur["peak"] <= 2             # never more than the cap in flight


def test_gradle_wrapper_windows_uses_bat(tmp_path, monkeypatch):
    import os
    (tmp_path / "build.gradle").write_text("", encoding="utf-8")
    (tmp_path / "gradlew.bat").write_text("", encoding="utf-8")
    monkeypatch.setattr(os, "name", "nt")
    spec = get_runner("junit-gradle").build_command(tmp_path, None)
    assert spec.command[0].endswith("gradlew.bat")


def test_gradle_wrapper_posix_uses_gradlew(tmp_path, monkeypatch):
    import os
    (tmp_path / "build.gradle").write_text("", encoding="utf-8")
    (tmp_path / "gradlew").write_text("", encoding="utf-8")
    monkeypatch.setattr(os, "name", "posix")
    spec = get_runner("junit-gradle").build_command(tmp_path, None)
    assert spec.command[0] == "./gradlew"


