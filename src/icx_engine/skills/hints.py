"""Attaches a named default skill's name+description to an MCP tool's own response dict, so a
matching best-practice skill reaches the calling agent through a tool-family entrypoint (testing,
Sonar, ticket analysis, git) even when it never calls icx_boost. Optionally also ranks the user's
own custom skills (via rank_skills) against a caller-supplied prompt and appends any additional
matches, so a relevant custom skill can surface here too, not only through icx_boost. A lookup or
ranking failure must never break the tool's own result - this is a hint, not a required part of
the response."""
from __future__ import annotations

from icx_engine.skills.router import rank_skills
from icx_engine.skills.storage import SkillStorage


def attach_skill_hint(
    response: dict,
    skill_name: str,
    storage: SkillStorage | None = None,
    rank_prompt: str | None = None,
    archetype: str = "coding",
) -> dict:
    storage = storage or SkillStorage()
    entries: list[dict] = []
    try:
        entry = storage.read(skill_name)
        if entry is not None:
            entries.append({"name": entry.name, "description": entry.description})
    except Exception:
        pass
    if rank_prompt is not None:
        try:
            seen = {e["name"] for e in entries}
            for ranked in rank_skills(rank_prompt, archetype, storage=storage):
                if ranked["name"] not in seen:
                    entries.append({"name": ranked["name"], "description": ranked["description"]})
                    seen.add(ranked["name"])
        except Exception:
            pass
    if entries:
        response["skills"] = {"index": entries}
    return response
