"""Local verification backend - the in-process replacement for the Magik executor.

Given a repo, a test type (unit/api/ui), and (for api/ui) a user-confirmed target URL, this detects
the right runner plugins, builds their commands with the repo-correct runtime (from the Runtime
Manager), runs them via the async DAG executor, and returns one normalized suite result. The
LangGraph `local_run` node calls this; there is no external test service.

Fully local and async: no HTTP to any external tester. Guarded - returns a structured result,
never raises.
"""
from __future__ import annotations

from pathlib import Path

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
) -> dict:
    """Run the local test suite for one layer and return a normalized result.

    Returns {ok, test_type, reason, runners, summary, reports} where summary aggregates
    total/passed/failures/errors/skipped across every runner in the layer. ok is True only when at
    least one test ran and none failed or errored.
    """
    from icx_engine.testing.runners import detect_runners, run_plan
    from icx_engine.testing.runners.base import TestReport

    cat = _CATEGORY_FOR_TYPE.get(str(test_type).lower())
    if cat is None:
        return {"ok": False, "test_type": test_type, "reason": f"unknown test type '{test_type}'",
                "runners": [], "summary": {}, "reports": []}

    resolver = runtime_resolver or _default_runtime_resolver
    try:
        runners = detect_runners(Path(repo), category=cat)
    except Exception as exc:
        return {"ok": False, "test_type": test_type, "reason": f"runner detection failed: {exc}",
                "runners": [], "summary": {}, "reports": []}
    if not runners:
        return {"ok": False, "test_type": test_type,
                "reason": f"no {cat} runner detected for this repo", "runners": [],
                "summary": {}, "reports": []}

    import inspect
    specs = []
    for r in runners:
        try:
            rt = resolver(getattr(r, "lang", ""))
            if inspect.isawaitable(rt):
                rt = await rt
        except Exception:
            rt = None
        spec = r.build_command(Path(repo), rt)
        if target_url:
            spec.env = {**(spec.env or {}), "ICX_TARGET_URL": target_url}
        specs.append(spec)

    try:
        results = await run_plan(specs, parallel=parallel, timeout=timeout)
    except Exception as exc:
        return {"ok": False, "test_type": test_type, "reason": f"execution failed: {exc}",
                "runners": [r.name for r in runners], "summary": {}, "reports": []}

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
    }
