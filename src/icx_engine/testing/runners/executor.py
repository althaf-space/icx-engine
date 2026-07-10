"""Test executor - runs a RunSpec (async subprocess, resolved runtime env) and normalizes the JUnit
XML result into a TestReport. Runs independent specs CONCURRENTLY via asyncio (the DAG's leaves).

Fully async: subprocess execution uses asyncio.create_subprocess_exec and never blocks the event
loop, so one test run cannot stall another. Guarded: any failure to run or parse yields an empty
(not-ok) TestReport rather than raising.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

from icx_engine.testing.runners.base import RunSpec, TestReport
from icx_engine.testing.runners.junit import parse_junit_xml


def _merge(reports: list[TestReport]) -> TestReport:
    out = TestReport()
    for r in reports:
        out.cases.extend(r.cases)
    out.total = len(out.cases)
    out.passed = sum(1 for c in out.cases if c.status == "passed")
    out.failures = sum(1 for c in out.cases if c.status == "failed")
    out.errors = sum(1 for c in out.cases if c.status == "error")
    out.skipped = sum(1 for c in out.cases if c.status == "skipped")
    out.time = round(sum(c.time for c in out.cases), 3)
    return out


def _parse_report_path(report_path: str) -> TestReport:
    p = Path(report_path)
    if p.is_dir():
        reports = [parse_junit_xml(str(x)) for x in sorted(p.glob("*.xml"))]
        merged = _merge(reports) if reports else TestReport()
        merged.raw_path = report_path
        return merged
    if p.exists():
        r = parse_junit_xml(str(p))
        r.raw_path = report_path
        return r
    return TestReport(raw_path=report_path)


async def run_spec(spec: RunSpec, timeout: float = 600.0) -> TestReport:
    """Execute a RunSpec asynchronously and return the normalized TestReport. Guarded - never raises.

    Uses an async subprocess so it never blocks the event loop. The exit code is not trusted for
    pass/fail; the JUnit XML the runner emits is the source of truth (a runner can exit non-zero yet
    still have written a full report, and vice versa). On timeout the process is killed.
    """
    env = {**os.environ, **(spec.env or {})}
    try:
        proc = await asyncio.create_subprocess_exec(
            *spec.command, cwd=spec.cwd, env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
    except (OSError, ValueError):
        # Runner missing / bad command -> parse whatever exists (likely nothing).
        return _parse_report_path(spec.report_path)
    try:
        await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()
    except (OSError, asyncio.CancelledError):
        pass
    return _parse_report_path(spec.report_path)


async def run_plan(specs: list[RunSpec], parallel: bool = True,
                   timeout: float = 600.0) -> list[tuple[RunSpec, TestReport]]:
    """Run several RunSpecs (independent DAG leaves) CONCURRENTLY via asyncio. Parallel by default;
    sequential when parallel=False. Returns [(spec, report)] in input order."""
    if not specs:
        return []
    if parallel and len(specs) > 1:
        reports = await asyncio.gather(*[run_spec(s, timeout) for s in specs])
    else:
        reports = [await run_spec(s, timeout) for s in specs]
    return list(zip(specs, reports))
