"""Test-quality advisory - folds three deterministic quality layers onto the run result for the report:
regression selection (which tests are relevant to the change), performance regression (before/after), and
mutation-test scoring (are AI-drafted unit tests meaningful). Each layer reports REAL data when its
inputs exist, else an honest 'not run: <reason>' - never a faked number. Never raises.

Honest gating (why each is or is not always-on):
- regression: always runs on a git repo (git diff is read-only) - no external tool.
- perf: needs before/after metrics; runs only when ICX_PERF_BEFORE/ICX_PERF_AFTER are provided.
- mutation: a real mutation run needs the tool installed and takes minutes-hours, so it is opt-in - the
  advisory only parses + gates a report path given via ICX_MUTATION_REPORT (with ICX_MUTATION_LANG).
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

_TEST_NAME_HINTS = ("test_", "_test", ".test.", ".spec.", "test.", "spec.")
_TEST_DIR_HINTS = ("test", "tests", "spec", "__tests__")
_SKIP_DIRS = {".git", "node_modules", "venv", ".venv", "__pycache__", "dist", "build", "target",
              "site-packages", ".tox", "vendor"}


def _skipped(reason: str, **extra) -> dict:
    return {"status": "skipped", "reason": reason, **extra}


# ---- regression selection ---------------------------------------------------

def _git_changed_files(repo: Path) -> list[str] | None:
    """Working-tree + committed-vs-merge-base changed files. None if not a git repo. Read-only."""
    try:
        base = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=str(repo),
                              capture_output=True, text=True, timeout=15)
        if base.returncode != 0:
            return None
        out = subprocess.run(["git", "diff", "--name-only", "HEAD"], cwd=str(repo),
                             capture_output=True, text=True, timeout=15)
        files = [ln.strip() for ln in out.stdout.splitlines() if ln.strip()]
        return files
    except (OSError, subprocess.SubprocessError):
        return None


def _discover_test_files(repo: Path, limit: int = 4000) -> list[str]:
    out: list[str] = []
    count = 0
    for p in Path(repo).rglob("*"):
        if count >= limit:
            break
        try:
            if not p.is_file():
                continue
            rel_parts = p.relative_to(repo).parts
        except (OSError, ValueError):
            continue
        if any(part in _SKIP_DIRS for part in rel_parts):
            continue
        name = p.name.lower()
        in_test_dir = any(part.lower() in _TEST_DIR_HINTS for part in rel_parts[:-1])
        if any(h in name for h in _TEST_NAME_HINTS) or in_test_dir:
            count += 1
            out.append(str(p.relative_to(repo)).replace("\\", "/"))
    return out


def _regression_block(repo: Path) -> dict:
    changed = _git_changed_files(repo)
    if changed is None:
        return _skipped("not a git repository - cannot detect changed files")
    if not changed:
        return _skipped("no changed files in the working tree")
    from icx_engine.testing.regression import select_regression_targets
    candidates = _discover_test_files(repo)
    relevant = select_regression_targets(changed, candidates)
    return {"status": "ran", "changed_files": len(changed),
            "candidate_tests": len(candidates), "relevant_tests": relevant,
            "relevant_count": len(relevant)}


# ---- performance regression -------------------------------------------------

def _load_metrics(val: str | None) -> dict | None:
    if not val:
        return None
    # inline JSON, or a path to a JSON file
    try:
        if val.strip().startswith("{"):
            data = json.loads(val)
        else:
            p = Path(val)
            data = json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _perf_block() -> dict:
    before = _load_metrics(os.environ.get("ICX_PERF_BEFORE"))
    after = _load_metrics(os.environ.get("ICX_PERF_AFTER"))
    if before is None or after is None:
        return _skipped("no before/after metrics provided "
                        "(set ICX_PERF_BEFORE and ICX_PERF_AFTER to JSON or a file path)")
    from icx_engine.testing.perf import compare_performance
    findings = compare_performance(before, after)
    if not findings:
        return _skipped("no metrics present in both before and after")
    fdicts = [{"metric": f.metric, "before": f.before, "after": f.after,
               "pct_change": f.pct_change, "threshold_pct": f.threshold_pct, "passed": f.passed}
              for f in findings]
    regressed = [f for f in fdicts if not f["passed"]]
    return {"status": "ran", "findings": fdicts, "regressed": len(regressed),
            "passed": len(regressed) == 0}


# ---- mutation scoring -------------------------------------------------------

def _mutation_block() -> dict:
    report = os.environ.get("ICX_MUTATION_REPORT")
    if not report:
        return _skipped("opt-in - run mutation testing separately, then set ICX_MUTATION_REPORT "
                        "(with ICX_MUTATION_LANG) to score the drafts")
    p = Path(report)
    if not p.is_file():
        return _skipped(f"mutation report not found: {report}")
    lang = str(os.environ.get("ICX_MUTATION_LANG", "python")).lower()
    from icx_engine.testing.mutation import (
        parse_mutmut, parse_stryker, parse_pit, evaluate_mutation, select_mutation_tool)
    tool = select_mutation_tool(lang)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return _skipped(f"mutation report unreadable: {report}")
    parser = {"mutmut": parse_mutmut, "stryker": parse_stryker, "pit": parse_pit}.get(tool)
    if parser is None:
        return _skipped(f"no mutation tool for language '{lang}'")
    result = parser(text)
    passed, reason = evaluate_mutation(result)
    return {"status": "ran", "tool": result.tool, "total": result.total, "killed": result.killed,
            "survived": result.survived, "score": result.score, "passed": passed, "reason": reason}


# ---- aggregate --------------------------------------------------------------

def run_quality_advisory(repo: Path) -> dict:
    """Return {"regression": {...}, "perf": {...}, "mutation": {...}}. Each guarded; never raises."""
    out = {}
    for key, fn in (("regression", lambda: _regression_block(repo)),
                    ("perf", _perf_block), ("mutation", _mutation_block)):
        try:
            out[key] = fn()
        except Exception:
            out[key] = _skipped("advisory failed to run")
    return out


def fold_quality(res: dict, repo: Path) -> dict:
    """Attach the quality advisory to res['quality']. Never raises."""
    if not isinstance(res, dict):
        return res
    try:
        res["quality"] = run_quality_advisory(repo)
    except Exception:
        res["quality"] = {}
    return res
