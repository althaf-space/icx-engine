"""Test executor - runs a RunSpec (subprocess, resolved runtime env) and normalizes the JUnit XML
result into a TestReport. Runs independent specs in parallel (the DAG's leaves).

This is the engine that actually executes what the adapters describe. It is guarded: any failure to
run or parse yields an empty (not-ok) TestReport rather than raising. The LangGraph node swap that
calls this in the live flow lands with Phase 8 (Magik removal).
"""
from __future__ import annotations

import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
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


def run_spec(spec: RunSpec, timeout: float = 600.0) -> TestReport:
    """Execute a RunSpec and return the normalized TestReport. Guarded - never raises.

    The subprocess exit code is not trusted for pass/fail; the JUnit XML the runner emits is the
    source of truth (a runner can exit non-zero yet still have written a full report, and vice versa).
    """
    env = {**os.environ, **(spec.env or {})}
    try:
        subprocess.run(
            spec.command, cwd=spec.cwd, env=env,
            capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        # Runner missing or crashed before writing a report -> parse whatever exists (likely none).
        pass
    return _parse_report_path(spec.report_path)


def run_plan(specs: list[RunSpec], parallel: bool = True, max_workers: int = 4,
             timeout: float = 600.0) -> list[tuple[RunSpec, TestReport]]:
    """Run several RunSpecs (independent DAG leaves). Parallel by default; falls back to sequential.
    Returns [(spec, report)] in input order."""
    if not specs:
        return []
    if parallel and len(specs) > 1:
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            reports = list(ex.map(lambda s: run_spec(s, timeout), specs))
    else:
        reports = [run_spec(s, timeout) for s in specs]
    return list(zip(specs, reports))
