"""Local verification backend - the in-process test execution engine.

Given a repo, a test type (unit/api/ui), and (for api/ui) a user-confirmed target URL, this detects
the right runner plugins, builds their commands with the repo-correct runtime (from the Runtime
Manager), runs them via the async DAG executor, and returns one normalized suite result. The
LangGraph `local_run` node calls this; there is no external test service.

Fully local and async: no HTTP to any external tester. Guarded - returns a structured result,
never raises.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path

# How each third-party runner CLI receives the user-confirmed target URL as an argv flag.
# ICX's own harness (stagehand) reads ICX_TARGET_URL from the env instead, so it is absent here.
_URL_FLAG = {
    "schemathesis": lambda url: [f"--base-url={url}"],
    "hurl": lambda url: ["--variable", f"base={url}"],
}


def _prepend_tool_env(spec, install_path: str) -> None:
    """Prepend an ICX-installed tool's dir (and its bin/Scripts) to the spec's PATH so a bare command
    resolves to the pinned binary; expose it on PYTHONPATH too for pip --target packages."""
    import os
    parts = [install_path, os.path.join(install_path, "bin")]
    if os.name == "nt":
        parts.append(os.path.join(install_path, "Scripts"))
    env = dict(spec.env or {})
    cur_path = env.get("PATH", os.environ.get("PATH", ""))
    env["PATH"] = os.pathsep.join(parts) + (os.pathsep + cur_path if cur_path else "")
    cur_pp = env.get("PYTHONPATH", os.environ.get("PYTHONPATH", ""))
    env["PYTHONPATH"] = install_path + (os.pathsep + cur_pp if cur_pp else "")
    spec.env = env


def _inject_target_url(spec, runner_name: str, target_url: str) -> None:
    """Append the target-URL argv flag a third-party CLI needs. Env alone does not reach
    schemathesis/hurl (they read flags, not ICX_TARGET_URL) - the env stays set for the ICX harness."""
    make = _URL_FLAG.get(runner_name)
    if make:
        spec.command = list(spec.command) + make(target_url)


_CATEGORY_FOR_TYPE = {
    "unit": "unit",
    "api": "api",
    "ui": "ui",
    "agent": "ui",   # AI-driven UI runs are the UI category (Stagehand director + Playwright judge)
}


def _default_runtime_resolver(lang: str) -> str | None:
    """Resolve the repo's runtime path for a language via the Runtime Manager, best-effort."""
    return None


async def run_local_verification(
    repo,
    test_type: str,
    target_url: str | None = None,
    runtime_resolver=None,
    parallel: bool = True,
    timeout: float = 600.0,
    ui_flow_path: str | None = None,
    storage_state: str | None = None,
    ui_headed: bool = False,
    ui_slowmo: int = 0,
    approve=None,
) -> dict:
    """Run the local test suite for one layer and return a normalized result.

    Returns {ok, test_type, reason, runners, summary, reports, unavailable} where summary aggregates
    total/passed/failures/errors/skipped across every runner in the layer. ok is True only when at
    least one test ran and none failed or errored.

    Runners that need ICX-owned tooling (a `requires` in RUNNER_SPECS) are gated on ensure_runner:
    missing + not user-approved -> that runner is skipped as unavailable (never a silent install,
    never a crash). `approve(name)->bool` authorizes an install; None uses the env default (off).
    """
    from icx_engine.testing.runners import detect_runners, run_plan
    from icx_engine.testing.runners.base import TestReport
    from icx_engine.testing.runners.install import RUNNER_SPECS, ensure_runner

    cat = _CATEGORY_FOR_TYPE.get(str(test_type).lower())
    if cat is None:
        return {"ok": False, "test_type": test_type, "reason": f"unknown test type '{test_type}'",
                "runners": [], "summary": {}, "reports": [], "unavailable": []}

    resolver = runtime_resolver or _default_runtime_resolver
    try:
        runners = detect_runners(Path(repo), category=cat)
    except Exception as exc:
        return {"ok": False, "test_type": test_type, "reason": f"runner detection failed: {exc}",
                "runners": [], "summary": {}, "reports": [], "unavailable": []}
    if not runners:
        return {"ok": False, "test_type": test_type,
                "reason": f"no {cat} runner detected for this repo", "runners": [],
                "summary": {}, "reports": [], "unavailable": []}

    import inspect
    specs = []
    ran_runners = []
    unavailable = []
    for r in runners:
        # ICX-owned tooling must be present (or install user-approved) before we can run it.
        # ensure_runner may perform a blocking install (subprocess), so it is offloaded to a
        # thread - it must never stall the event loop.
        req = getattr(r, "requires", None)
        tool_path = None
        if req and req in RUNNER_SPECS:
            try:
                tool_path = await asyncio.to_thread(ensure_runner, req, approve)
            except Exception:
                tool_path = None
            if tool_path is None:
                unavailable.append({
                    "runner": r.name, "requires": req,
                    "reason": (f"ICX-owned '{req}' tooling is not installed yet. Approve ICX to install "
                               f"it under ~/.icx/testing (this does NOT modify your repo, and you do "
                               f"NOT add any test dependency to your project). Re-run once approved."),
                })
                continue
        try:
            rt = resolver(getattr(r, "lang", ""))
            if inspect.isawaitable(rt):
                rt = await rt
        except Exception:
            rt = None
        spec = r.build_command(Path(repo), rt)
        if tool_path:
            # Make the pinned ICX tool (e.g. ~/.icx/testing/hurl/<ver>/hurl) discoverable so the
            # runner's bare command resolves to it instead of a global/absent one.
            _prepend_tool_env(spec, tool_path)
        if target_url:
            spec.env = {**(spec.env or {}), "ICX_TARGET_URL": target_url}
            _inject_target_url(spec, r.name, target_url)
        if ui_flow_path and cat == "ui":
            spec.env = {**(spec.env or {}), "ICX_UI_FLOW": ui_flow_path}
        if storage_state and cat == "ui":
            spec.env = {**(spec.env or {}), "ICX_STORAGE_STATE": storage_state}
        if ui_headed and cat == "ui":
            spec.env = {**(spec.env or {}), "ICX_UI_HEADED": "1"}
        if ui_slowmo and cat == "ui":
            spec.env = {**(spec.env or {}), "ICX_UI_SLOWMO": str(int(ui_slowmo))}
        specs.append(spec)
        ran_runners.append(r)

    if not specs:
        names = ", ".join(u["runner"] for u in unavailable) or "none"
        return {"ok": False, "test_type": test_type,
                "reason": f"no {cat} runner available (unapproved/uninstalled: {names})",
                "runners": [r.name for r in runners], "summary": {}, "reports": [],
                "unavailable": unavailable}
    runners = ran_runners

    try:
        results = await run_plan(specs, parallel=parallel, timeout=timeout)
    except Exception as exc:
        return {"ok": False, "test_type": test_type, "reason": f"execution failed: {exc}",
                "runners": [r.name for r in runners], "summary": {}, "reports": [],
                "unavailable": unavailable}

    all_cases = []
    for _spec, rep in results:
        all_cases.extend(rep.cases)
    merged = TestReport(
        total=len(all_cases),
        passed=sum(1 for c in all_cases if c.status == "passed"),
        failures=sum(1 for c in all_cases if c.status == "failed"),
        errors=sum(1 for c in all_cases if c.status == "error"),
        skipped=sum(1 for c in all_cases if c.status == "skipped"),
        cases=all_cases,
    )
    summary = {
        "total": merged.total, "passed": merged.passed, "failures": merged.failures,
        "errors": merged.errors, "skipped": merged.skipped,
    }
    return {
        "ok": merged.ok,
        "test_type": test_type,
        "reason": "" if merged.ok else "tests failed or none ran",
        "runners": [r.name for r in runners],
        "summary": summary,
        "reports": [{"runner": s.command[0] if s.command else "", "report_path": rep.raw_path,
                     "total": rep.total, "ok": rep.ok} for s, rep in results],
        "cases": [(c.name, c.status, c.time) for c in all_cases],
        "unavailable": unavailable,
    }


async def run_ui_verify(
    repo,
    flow_path: str,
    target_url: str | None = None,
    storage_state: str | None = None,
    runtime_resolver=None,
    ui_headed: bool = False,
    approve=None,
    timeout: float = 300.0,
) -> dict | None:
    """Live-DOM selector heal-probe (harness --mode verify). Runs the authored flow WITHOUT scoring:
    resolves every selector against the real DOM and returns {broken, ambiguous, steps:[...]} so the
    caller can repair broken/ambiguous selectors BEFORE the scored run. This is the anti-misfire pass.

    Best-effort: returns None if the UI runner/tooling is unavailable, the app is unreachable, or the
    probe cannot complete - the caller then proceeds without healing (never blocks the session).
    """
    from icx_engine.testing.runners import detect_runners
    from icx_engine.testing.runners.install import RUNNER_SPECS, ensure_runner
    from icx_engine.testing.runners.executor import run_spec
    import inspect

    try:
        runners = [r for r in detect_runners(Path(repo), category="ui") if r.name == "stagehand"]
    except Exception:
        return None
    if not runners:
        return None
    r = runners[0]
    req = getattr(r, "requires", None)
    if req and req in RUNNER_SPECS:
        try:
            if await asyncio.to_thread(ensure_runner, req, approve) is None:
                return None
        except Exception:
            return None

    out = os.path.join(tempfile.gettempdir(), f".icx-verify-{os.getpid()}-{id(flow_path)}.json")
    try:
        rt = runtime_resolver("ui") if runtime_resolver else None
        if inspect.isawaitable(rt):
            rt = await rt
    except Exception:
        rt = None
    try:
        spec = r.build_command(Path(repo), rt, mode="verify", report=out)
    except Exception:
        return None
    env = dict(spec.env or {})
    env["ICX_UI_FLOW"] = flow_path
    if target_url:
        env["ICX_TARGET_URL"] = target_url
    if storage_state:
        env["ICX_STORAGE_STATE"] = storage_state
    if ui_headed:
        env["ICX_UI_HEADED"] = "1"
    spec.env = env
    try:
        await run_spec(spec, timeout=timeout, keep_report=True)   # writes JSON to `out`; JUnit parse result unused
        report = json.loads(Path(out).read_text(encoding="utf-8"))
        return report if isinstance(report, dict) else None
    except Exception:
        return None
    finally:
        try:
            os.remove(out)
        except OSError:
            pass


async def run_ui_discovery(
    repo,
    target_url: str,
    storage_state: str | None = None,
    runtime_resolver=None,
    ui_headed: bool = False,
    approve=None,
    timeout: float = 90.0,
) -> dict | None:
    """Runtime census AUTO-DISCOVERY: open the LIVE screen and inspect the rendered DOM to build the
    census itself (search box, toolbar create/export, per-row view/edit/delete, form fields with real
    control kinds + maxLength, wizard steps). Returns the census dict (the shape census_to_flow /
    merge_census consume) or None.

    This is the live-verified half of the COMBINED census - it can never name a selector that does not
    exist. Best-effort: returns None if the UI tooling is unavailable, the app is unreachable, or the
    crawl produced nothing, so the caller falls back to the source census (never a user-facing mode)."""
    if not target_url:
        return None
    from icx_engine.testing.runners import detect_runners
    from icx_engine.testing.runners.install import (
        RUNNER_SPECS, ensure_runner, installed_path, browsers_dir,
        harness_path, runtime_harness_path, discover_harness_path,
    )
    from icx_engine.testing.runners.executor import run_spec
    from icx_engine.testing.runners.base import RunSpec
    import inspect

    try:
        runners = [r for r in detect_runners(Path(repo), category="ui") if r.name == "stagehand"]
    except Exception:
        return None
    if not runners:
        return None
    r = runners[0]
    req = getattr(r, "requires", None)
    if req and req in RUNNER_SPECS:
        try:
            if await asyncio.to_thread(ensure_runner, req, approve) is None:
                return None
        except Exception:
            return None

    try:
        rt = runtime_resolver("ui") if runtime_resolver else None
        if inspect.isawaitable(rt):
            rt = await rt
    except Exception:
        rt = None
    node = rt or "node"
    # Run the discover harness FROM the install dir (next to node_modules) so ESM `import "playwright"`
    # resolves - identical constraint to the replay harness.
    harness = runtime_harness_path("icx-discover.mjs", discover_harness_path())
    env = {}
    try:
        sh = installed_path("stagehand")
        if sh:
            env["NODE_PATH"] = str(Path(sh) / "node_modules")
            env["PLAYWRIGHT_BROWSERS_PATH"] = str(browsers_dir(Path(sh)))
    except Exception:
        pass
    if ui_headed:
        env["ICX_UI_HEADED"] = "1"

    out = os.path.join(tempfile.gettempdir(), f".icx-discover-{os.getpid()}-{id(target_url)}.json")
    cmd = [node, harness, "--url", target_url, "--out", out, "--timeout", str(int(timeout * 1000))]
    if storage_state:
        cmd += ["--state", storage_state]
    spec = RunSpec(command=cmd, cwd=str(repo), report_path=out, env=env)
    try:
        await run_spec(spec, timeout=timeout, keep_report=True)
        census = json.loads(Path(out).read_text(encoding="utf-8"))
        if isinstance(census, dict) and census.get("functionalities"):
            return census
        return None
    except Exception:
        return None
    finally:
        try:
            os.remove(out)
        except OSError:
            pass


async def run_ui_replay(
    repo,
    flow_path: str,
    target_url: str | None = None,
    storage_state: str | None = None,
    runtime_resolver=None,
    timeout: float = 300.0,
):
    """Run the authored flow in SCORED replay mode and return the parsed JUnit TestReport (or None if
    the UI tooling/app is unavailable). Additive sibling of run_ui_verify - used by the benchmark
    harness; the production scored run stays in node_local_run and is untouched."""
    from icx_engine.testing.runners import detect_runners
    from icx_engine.testing.runners.install import RUNNER_SPECS, ensure_runner
    from icx_engine.testing.runners.executor import run_spec
    from icx_engine.testing.runners.junit import parse_junit_xml
    import inspect

    try:
        runners = [r for r in detect_runners(Path(repo), category="ui") if r.name == "stagehand"]
    except Exception:
        return None
    if not runners:
        return None
    r = runners[0]
    req = getattr(r, "requires", None)
    if req and req in RUNNER_SPECS:
        try:
            if await asyncio.to_thread(ensure_runner, req, None) is None:
                return None
        except Exception:
            return None
    out = os.path.join(tempfile.gettempdir(), f".icx-bench-{os.getpid()}-{id(flow_path)}.xml")
    try:
        rt = runtime_resolver("ui") if runtime_resolver else None
        if inspect.isawaitable(rt):
            rt = await rt
    except Exception:
        rt = None
    try:
        spec = r.build_command(Path(repo), rt, mode="replay", report=out)
    except Exception:
        return None
    env = dict(spec.env or {})
    env["ICX_UI_FLOW"] = flow_path
    if target_url:
        env["ICX_TARGET_URL"] = target_url
    if storage_state:
        env["ICX_STORAGE_STATE"] = storage_state
    spec.env = env
    try:
        await run_spec(spec, timeout=timeout, keep_report=True)
        return parse_junit_xml(out)
    except Exception:
        return None
    finally:
        try:
            os.remove(out)
        except OSError:
            pass
