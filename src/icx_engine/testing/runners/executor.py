"""Test executor - runs a RunSpec (async subprocess, resolved runtime env) and normalizes the JUnit
XML result into a TestReport. Runs independent specs CONCURRENTLY via asyncio (the DAG's leaves).

Fully async: subprocess execution uses asyncio.create_subprocess_exec and never blocks the event
loop, so one test run cannot stall another. Guarded: any failure to run or parse yields an empty
(not-ok) TestReport rather than raising.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys
import time
from pathlib import Path

from icx_engine._proc import kill_tree, win_argv
from icx_engine.testing.runners.base import RunSpec, TestReport
from icx_engine.testing.runners.junit import parse_junit_xml

_log = logging.getLogger("icx.testing.executor")

# Bound how many runner subprocesses run at once so a polyglot repo cannot spawn an
# unbounded fleet of processes on a low-memory laptop.
_DEFAULT_MAX_PARALLEL = 4

# POSIX-only: spawn each runner in its own session so the whole tree can be group-killed.
# Accepted-and-ignored on Windows, where kill_tree falls back to psutil / taskkill.
_SPAWN_KW = {"start_new_session": True} if sys.platform != "win32" else {}

# Opt-in POSIX resource caps for the runner subprocess. Default OFF (no behavior change) - a hard
# memory/CPU ceiling can break legitimate heavy suites (JVM/node reserve huge virtual memory), so
# ICX applies it only when the operator explicitly sets these env vars.
_RLIMIT_MEM_ENV = "ICX_TEST_RLIMIT_MEM_MB"
_RLIMIT_CPU_ENV = "ICX_TEST_RLIMIT_CPU_S"


def _rlimit_preexec() -> None:  # pragma: no cover - runs in the forked child, POSIX only
    """Apply RLIMIT_AS / RLIMIT_CPU in the child before exec. Best-effort; a bad value is ignored."""
    import resource
    mem_mb = os.environ.get(_RLIMIT_MEM_ENV)
    cpu_s = os.environ.get(_RLIMIT_CPU_ENV)
    if mem_mb:
        try:
            b = int(mem_mb) * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (b, b))
        except (ValueError, OSError):
            pass
    if cpu_s:
        try:
            s = int(cpu_s)
            resource.setrlimit(resource.RLIMIT_CPU, (s, s))
        except (ValueError, OSError):
            pass


def _spawn_kwargs() -> dict:
    """Per-call spawn kwargs: session isolation (POSIX) + optional resource caps when opted in."""
    kw = dict(_SPAWN_KW)
    if sys.platform != "win32" and (os.environ.get(_RLIMIT_MEM_ENV) or os.environ.get(_RLIMIT_CPU_ENV)):
        kw["preexec_fn"] = _rlimit_preexec
    return kw


def _kill_tree(pid: int) -> None:
    """Kill the runner's whole process tree. Thin, monkeypatch-friendly wrapper over the shared
    process-group-aware ``_proc.kill_tree`` (single implementation for new code)."""
    kill_tree(pid, process_group=True)


def _own_report(report_path: str) -> bool:
    """True only for an ICX-owned report FILE (basename starts with '.icx-'). Never a directory
    (Surefire/Gradle write into the user's build dir - we read those read-only, never delete)."""
    p = Path(report_path)
    return p.name.startswith(".icx-") and not p.is_dir()


def _clean_own_report(report_path: str) -> None:
    if _own_report(report_path):
        with contextlib.suppress(OSError):
            Path(report_path).unlink()


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
    """Execute a RunSpec asynchronously and return the normalized TestReport. Guarded - never raises
    (except a genuine cancellation, which is re-raised after killing the process tree).

    Uses an async subprocess so it never blocks the event loop. The exit code is not trusted for
    pass/fail; the JUnit XML the runner emits is the source of truth (a runner can exit non-zero yet
    still have written a full report, and vice versa). Our own stale report is deleted BEFORE the run
    so a crashed/absent runner can never be scored against a previous run's XML. On timeout OR
    cancellation the FULL process tree is killed (no orphan browsers/JVMs/workers). stdout/stderr go
    to DEVNULL - the JUnit file, not console output, is the result, so nothing is buffered in memory.
    """
    # Freshness: never let a prior run's file masquerade as this run's result.
    _clean_own_report(spec.report_path)

    cmd0 = spec.command[0] if spec.command else "?"
    env = {**os.environ, **(spec.env or {})}
    t0 = time.monotonic()
    _log.debug("runner start: cmd=%s cwd=%s timeout=%.0fs", cmd0, spec.cwd, timeout)
    try:
        proc = await asyncio.create_subprocess_exec(
            *win_argv(spec.command), cwd=spec.cwd, env=env,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            **_spawn_kwargs(),
        )
    except (OSError, ValueError) as exc:
        # Runner missing / bad command -> parse whatever exists (own file was just cleaned -> none).
        _log.warning("runner not launchable: cmd=%s err=%s", cmd0, exc)
        return _parse_report_path(spec.report_path)
    try:
        await asyncio.wait_for(proc.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        _log.warning("runner timeout after %.0fs, killing tree: cmd=%s pid=%s", timeout, cmd0, proc.pid)
        _kill_tree(proc.pid)
        with contextlib.suppress(Exception):
            await proc.wait()
    except asyncio.CancelledError:
        # Cooperative shutdown (Ctrl+C / session cancel): kill the tree, then propagate.
        _log.info("runner cancelled, killing tree: cmd=%s pid=%s", cmd0, proc.pid)
        _kill_tree(proc.pid)
        with contextlib.suppress(Exception):
            await proc.wait()
        raise
    except OSError as exc:
        _log.warning("runner wait failed: cmd=%s err=%s", cmd0, exc)
        with contextlib.suppress(Exception):
            await proc.wait()
    report = _parse_report_path(spec.report_path)
    # Do not litter the user's repo: remove our own report once parsed (data is in `report`).
    _clean_own_report(spec.report_path)
    _log.info("runner done: cmd=%s dur=%.2fs total=%d pass=%d fail=%d err=%d",
              cmd0, time.monotonic() - t0, report.total, report.passed, report.failures, report.errors)
    return report


async def run_plan(specs: list[RunSpec], parallel: bool = True, timeout: float = 600.0,
                   max_parallel: int = _DEFAULT_MAX_PARALLEL) -> list[tuple[RunSpec, TestReport]]:
    """Run several RunSpecs (independent DAG leaves) CONCURRENTLY via asyncio, bounded by
    max_parallel so process count never runs away. Sequential when parallel=False. Returns
    [(spec, report)] in input order."""
    if not specs:
        return []
    if parallel and len(specs) > 1:
        sem = asyncio.Semaphore(max(1, max_parallel))

        async def _bounded(s: RunSpec) -> TestReport:
            async with sem:
                return await run_spec(s, timeout)

        reports = await asyncio.gather(*[_bounded(s) for s in specs])
    else:
        reports = [await run_spec(s, timeout) for s in specs]
    return list(zip(specs, reports))
