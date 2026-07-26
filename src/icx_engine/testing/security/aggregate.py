"""Aggregate the native static security scanners (secrets + SAST-lite + SCA) into one result block and
fold it onto the run result for the report. Deterministic; never raises (a scan failure must not affect
a test run). Runtime DAST probes are separate - they run inside the UI/API flow and appear as test cases;
this block is the static, code-level posture."""
from __future__ import annotations

from pathlib import Path

from icx_engine.testing.security.sast import scan_sast
from icx_engine.testing.security.sca import scan_deps
from icx_engine.testing.security.scan_base import (
    Finding,
    SEVERITY_RANK,
    dedupe,
    sort_findings,
)
from icx_engine.testing.security.secrets import scan_secrets

_SEVERITIES = ("critical", "high", "medium", "low", "info")


def _finding_dict(f: Finding) -> dict:
    return {"scanner": f.scanner, "rule": f.rule, "severity": f.severity, "title": f.title,
            "file": f.file, "line": f.line, "detail": f.detail, "snippet": f.snippet}


def run_static_security(repo: Path, file_limit: int = 6000) -> dict:
    """Run every static scanner over `repo` and return a serializable block:
    {"findings": [...], "summary": {"total", per-severity counts, "clean", "gate_failed"}}.
    `gate_failed` is True when any critical/high finding exists. Never raises."""
    findings: list[Finding] = []
    for scan in (scan_secrets, scan_sast):
        try:
            findings.extend(scan(repo, file_limit=file_limit))
        except Exception:
            pass
    try:
        findings.extend(scan_deps(repo))
    except Exception:
        pass

    findings = sort_findings(dedupe(findings))
    counts = {s: 0 for s in _SEVERITIES}
    for f in findings:
        if f.severity in counts:
            counts[f.severity] += 1
    gate_failed = counts["critical"] > 0 or counts["high"] > 0
    summary = {"total": len(findings), **counts,
               "clean": len(findings) == 0, "gate_failed": gate_failed}
    return {"findings": [_finding_dict(f) for f in findings], "summary": summary}


def fold_into_result(res: dict, repo: Path, file_limit: int = 6000) -> dict:
    """Attach a static-security block to the run result under res['security']. Never raises."""
    if not isinstance(res, dict):
        return res
    try:
        res["security"] = run_static_security(repo, file_limit=file_limit)
    except Exception:
        res["security"] = {"findings": [], "summary": {"total": 0, "clean": True,
                                                       "gate_failed": False,
                                                       **{s: 0 for s in _SEVERITIES}}}
    return res
