"""Intelligent regression selection - run only the tests relevant to a change, not the whole suite.

Maps changed source files to candidate test files by name/path relationship, optionally widened by
graph-impacted files (dependent modules). Pure + testable; the graph impact set is passed in by the
caller (from graph_impact / blast_radius) so this stays dependency-free.
"""
from __future__ import annotations

from pathlib import Path


def _stems(paths) -> set[str]:
    out: set[str] = set()
    for p in paths or []:
        stem = Path(p).stem
        if stem:
            out.add(stem.lower())
            # test_foo.py / foo_test.py -> foo
            for pre, suf in (("test_", ""), ("", "_test"), ("", ".test"), ("", ".spec")):
                s = stem.lower()
                if pre and s.startswith(pre):
                    out.add(s[len(pre):])
                if suf and s.endswith(suf):
                    out.add(s[: -len(suf)])
    return {s for s in out if s}


def select_regression_targets(changed_files, candidate_tests, graph_impacted=None) -> list[str]:
    """Return the subset of candidate_tests relevant to the changed (+ graph-impacted) files.

    A candidate test is selected when its file stem relates to a changed/impacted source stem
    (e.g. changed `auth.py` selects `test_auth.py`, `auth.test.ts`, `auth_test.go`). If nothing
    matches, returns [] - the caller decides whether to fall back to the full suite.
    """
    source = _stems(changed_files) | _stems(graph_impacted)
    if not source:
        return []
    selected: list[str] = []
    for t in candidate_tests or []:
        tstems = _stems([t])
        if tstems & source:
            selected.append(t)
    return selected
