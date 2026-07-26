"""Adaptive context router: methodology always; graph/memory only when the problem + env warrant."""
from __future__ import annotations

from icx_engine.boost.router import plan_activation


def test_doubt_activates_nothing():
    p = plan_activation("what is a closure?", "doubt", {"has_repo": True, "has_graph": True})
    assert p.signals == set()
    assert p.skipped


def test_no_repo_skips_repo_signals():
    p = plan_activation("add a login form", "coding", {"has_repo": False, "has_graph": False})
    assert "graph" not in p.signals and "grep" not in p.signals and "semantic" not in p.signals
    assert p.skipped


def test_code_task_with_graph_activates_graph_and_grep():
    p = plan_activation("fix the auth crash", "debugging",
                        {"has_repo": True, "has_graph": True, "is_continuation": False})
    assert "graph" in p.signals and "grep" in p.signals and "semantic" in p.signals
    assert "memory" not in p.signals
    assert all(k in p.reasons for k in p.signals)


def test_continuation_adds_memory():
    p = plan_activation("continue fixing the token refresh", "debugging",
                        {"has_repo": True, "has_graph": True, "is_continuation": True})
    assert "memory" in p.signals


def test_repo_without_graph_uses_grep_only():
    p = plan_activation("edit the parser", "coding",
                        {"has_repo": True, "has_graph": False, "is_continuation": False})
    assert "grep" in p.signals
    assert "graph" not in p.signals and "semantic" not in p.signals
