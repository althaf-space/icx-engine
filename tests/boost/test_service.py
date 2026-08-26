"""Shared boost orchestrator with injected fake providers."""
from __future__ import annotations

from icx_engine.boost.service import build_boost_brief


def _env(has_repo=True, has_graph=True):
    return lambda repo, cont: {"has_repo": has_repo, "has_graph": has_graph, "is_continuation": cont}


def _signals(files):
    def make(repo, seeds, keywords):
        graph = lambda: [(f, "graph dependent") for f in files]
        empty = lambda: []
        return graph, empty, empty, empty
    return make


def _connected(**kw):
    return lambda: {"jira": kw.get("jira", False), "sonarqube": kw.get("sonarqube", False)}


def test_doubt_gathers_no_context():
    brief = build_boost_brief("what is a closure?", repo_path="/x",
                              env_fn=_env(), signals_fn=_signals(["a.py"]), connected_fn=_connected())
    assert brief["archetype"] == "doubt"
    assert brief["context"]["files"] == []


def test_code_task_gathers_graph_context():
    brief = build_boost_brief("fix the auth crash", repo_path="/x", current_file="auth.py",
                              env_fn=_env(has_repo=True, has_graph=True),
                              signals_fn=_signals(["dep.py"]), connected_fn=_connected())
    assert brief["archetype"] == "debugging"
    assert any(f["path"] == "dep.py" for f in brief["context"]["files"])


def test_no_repo_skips_context():
    brief = build_boost_brief("add a form", repo_path=None,
                              env_fn=_env(has_repo=False, has_graph=False),
                              signals_fn=_signals(["x.py"]), connected_fn=_connected())
    assert brief["context"]["files"] == []
    assert brief["context"]["skipped"]


def test_links_tiered_via_connected_fn():
    brief = build_boost_brief("see https://acme.atlassian.net/browse/AB-1", repo_path=None,
                              env_fn=_env(has_repo=False, has_graph=False),
                              signals_fn=_signals([]), connected_fn=_connected(jira=True))
    jira = [l for l in brief["links"] if l["target"] == "jira"][0]
    assert jira["status"] == "icx_tool"


def test_returns_full_brief_shape():
    brief = build_boost_brief("optimize the query", repo_path="/x",
                              env_fn=_env(), signals_fn=_signals([]), connected_fn=_connected())
    for k in ("intent", "archetype", "methodology", "context", "links", "gates",
              "boosted_prompt", "mandatory_directive"):
        assert k in brief


def test_boost_methodology_is_compact_not_full_framework():
    """Token guard: the boost brief carries the operative spine + archetype guidance, NOT the full
    framework (that is one icx_get_methodology call away). Prevents silent re-bloat of every-prompt tokens."""
    brief = build_boost_brief("add a login feature", repo_path=None,
                              env_fn=_env(has_repo=False, has_graph=False),
                              signals_fn=_signals([]), connected_fn=_connected())
    m = brief["methodology"]
    # kept (the value): the mandatory spine + this task's discipline
    assert m.get("one_pager") and m.get("archetype_discipline")
    # dropped from the every-prompt payload (lives in icx_get_methodology): the long lists
    for heavy in ("intake_checklist", "verification_battery", "failure_modes_to_avoid",
                  "hallucination_high_risk_zones"):
        assert heavy not in m, f"{heavy} bloats every boost call - keep it in icx_get_methodology only"
    # gate_sequence is surfaced once, at top level, not duplicated inside methodology
    assert "gate_sequence" not in m
    assert brief["gates"]
