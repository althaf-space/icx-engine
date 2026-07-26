"""Deterministic boosted-brief assembly."""
from __future__ import annotations

from icx_engine.boost.brief import build_brief, compose_boosted_prompt
from icx_engine.boost.router import ActivationPlan
from icx_engine.methodology import build_checklist_for


def _ctx():
    return {"activated_signals": ["grep"], "files": [{"path": "auth.py", "tier": "high",
            "signals": ["grep"], "reasons": ["references the change"]}], "skipped": ""}


def test_compose_is_deterministic_and_structured():
    meth = build_checklist_for("fix the auth crash", "debugging")
    a = compose_boosted_prompt("fix the auth crash", "debugging", meth, _ctx())
    b = compose_boosted_prompt("fix the auth crash", "debugging", meth, _ctx())
    assert a == b
    assert "fix the auth crash" in a
    assert "debugging" in a.lower()
    assert "auth.py" in a


def test_build_brief_shape():
    meth = build_checklist_for("add a form", "coding")
    plan = ActivationPlan(signals={"grep"}, reasons={"grep": "refs"}, skipped="")
    brief = build_brief("add a form", "coding", meth, _ctx(), plan, [], [])
    for k in ("intent", "archetype", "methodology", "context", "links", "clarifications",
              "gates", "boosted_prompt", "boost_meta", "mandatory_directive"):
        assert k in brief
    assert brief["archetype"] == "coding"
    assert brief["boost_meta"]["deterministic"] is True
    assert brief["boost_meta"]["llm_used"] is False


def test_empty_prompt_still_valid_brief():
    meth = build_checklist_for("", None)
    plan = ActivationPlan()
    brief = build_brief("", meth["archetype"], meth, {"activated_signals": [], "files": [],
                        "skipped": "no repository connected"}, plan, [], [])
    assert isinstance(brief["boosted_prompt"], str)
    assert brief["mandatory_directive"]


def test_gates_present():
    meth = build_checklist_for("optimize the query", "performance")
    brief = build_brief("optimize the query", "performance", meth, _ctx(),
                        ActivationPlan(), [], [])
    assert any("lock_plan" in g for g in brief["gates"])
    assert any("record_verification" in g for g in brief["gates"])


def test_links_appear_in_boosted_prompt():
    from icx_engine.boost.brief import build_brief
    from icx_engine.boost.router import ActivationPlan
    meth = build_checklist_for("look at this", "coding")
    links = [{"url": "https://figma.com/f/1", "target": "figma", "status": "agent_fetch",
              "action": "use your own tool"}]
    brief = build_brief("look at this", "coding", meth, _ctx(), ActivationPlan(), [], links)
    assert "https://figma.com/f/1" in brief["boosted_prompt"]
    assert "figma" in brief["boosted_prompt"]
