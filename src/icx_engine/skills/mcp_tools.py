"""MCP tool surface for ICX methodology + custom skill authoring/lookup. Owns its own
Tool() definitions and dispatch function - mcp_server.py's _list_tools()/_call_tool()
get a few additive lines only, no restructuring."""

from __future__ import annotations

import asyncio
import json

from mcp.types import TextContent, Tool

from icx_engine.skills.storage import SkillStorage
from icx_engine.mcp_server import _get_memory_executor, _show_entry_sync

_GET_METHODOLOGY_TOOL = "icx_get_methodology"
_SKILL_GET_TOOL = "icx_skill_get"
_SKILLS_INDEX_TOOL = "icx_skills_index"
_DRAFT_SKILL_TOOL = "icx_draft_skill"
_CREATE_SKILL_TOOL = "icx_create_skill"

SKILLS_TOOLS: list[Tool] = [
    Tool(
        name=_GET_METHODOLOGY_TOOL,
        description=(
            "Return the full ICX problem-solving methodology (intake, context, classify, decompose, "
            "plan, execute, self-check, confidence, fail-well, verify) with archetypes, decision "
            "rules, and pitfalls. jira_analyze_issue already injects the mandatory one-pager into its "
            "response.methodology - call this when you want the complete framework. Following the "
            "methodology on every ticket is MANDATORY. No input."
        ),
        inputSchema={"type": "object", "properties": {}, "required": []},
            annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    ),
    Tool(
        name=_DRAFT_SKILL_TOOL,
        description=(
            "MANDATORY immediately after every save_memory call where outcome_verified=true - no "
            "exceptions, even when the honest judgment is skill_worthy=false. YOU decide: is this "
            "fix non-obvious and likely to recur? If save_memory's response included "
            "related_skills, check whether one of those names already covers this - reuse that "
            "skill_name to refine it (your fresh text replaces the stale text) rather than create "
            "a near-duplicate. Write description in third person, stating both what the skill does "
            "and when to use it (e.g. 'Fixes N+1 query patterns in SQLAlchemy. Use when a list "
            "endpoint is slow and profiling shows repeated single-row queries.'). Generalize - do "
            "not paraphrase the raw ticket. Input: {issue_key, skill_worthy, skill_name?, "
            "description?, when_to_use?, procedure?, verification?, pitfalls?, tags?} - the five "
            "content fields are required when skill_worthy=true. Returns {status: skipped} or "
            "{status: created|updated, name} or {error}. Requires a prior, verified save_memory "
            "entry - for a general-purpose skill the user asks for directly, with no ticket or "
            "memory entry behind it, use icx_create_skill instead."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "issue_key": {"type": "string", "description": "The issue_key from the save_memory call this follows."},
                "skill_worthy": {"type": "boolean", "description": "Your own judgment - false is a valid, expected answer."},
                "skill_name": {"type": "string", "description": "Required when skill_worthy=true. Reuse an existing name (from related_skills) to refine it, or pick a new one to create."},
                "description": {"type": "string", "description": "Required when skill_worthy=true. Third person - states what it does AND when to use it."},
                "when_to_use": {"type": "string", "description": "Required when skill_worthy=true. The trigger condition."},
                "procedure": {"type": "string", "description": "Required when skill_worthy=true. The generalized step-by-step fix."},
                "verification": {"type": "string", "description": "Required when skill_worthy=true. How to confirm this class of fix worked."},
                "pitfalls": {"type": "string", "description": "Optional. Gotchas or wrong turns."},
                "tags": {"type": "array", "items": {"type": "string"}, "description": "Optional. Merged with the originating entry's own tags."},
            },
            "required": ["issue_key", "skill_worthy"],
        },
            annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False},
    ),
    Tool(
        name=_CREATE_SKILL_TOOL,
        description=(
            "USE WHEN the user directly asks you to create a general-purpose skill - not a "
            "follow-up to a verified fix (that path is icx_draft_skill). Has NO issue_key/memory "
            "dependency and works even when memory is completely unavailable or not ready. Builds "
            "a skill directly from the fields you supply, mirroring `icx skills create`'s CLI "
            "behavior exactly. If project_key is given, the skill is tied to that project "
            "(scope_hint='repo-specific'); omit it for a general-purpose skill "
            "(scope_hint='generic'). Calling again with the same name merges into the existing "
            "skill via the usual hash-guarded write_or_update rules (skipped if hand-edited since). "
            "Input: {name, description, when_to_use, procedure, verification, pitfalls?, tags?, "
            "project_key?}. Returns {status: created|updated|skipped_user_edited, name} or {error}."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Skill name - slugified for storage."},
                "description": {"type": "string", "description": "Third person - states what it does AND when to use it."},
                "when_to_use": {"type": "string", "description": "The trigger condition."},
                "procedure": {"type": "string", "description": "The generalized step-by-step approach."},
                "verification": {"type": "string", "description": "How to confirm this class of fix/approach worked."},
                "pitfalls": {"type": "string", "description": "Optional. Gotchas or wrong turns.", "default": ""},
                "tags": {"type": "array", "items": {"type": "string"}, "description": "Optional lowercase tags.", "default": []},
                "project_key": {"type": "string", "description": "Optional. Ties the skill to this project (scope_hint='repo-specific'); omit for a generic skill."},
            },
            "required": ["name", "description", "when_to_use", "procedure", "verification"],
        },
            annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False},
    ),
    Tool(
        name=_SKILL_GET_TOOL,
        description=(
            "USE WHEN icx_boost's brief includes a skills.index entry you want full context on - "
            "fetches one learned skill's complete markdown body (When to Use/Procedure/Pitfalls/"
            "Verification). Never bulk-fetch every candidate - call this only for the name(s) you "
            "actually want. Input: {name}. Returns {body} or {error}."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Skill name, from skills.index in the icx_boost brief."},
            },
            "required": ["name"],
        },
            annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    ),
    Tool(
        name=_SKILLS_INDEX_TOOL,
        description=(
            "USE WHEN you suspect icx_boost's skills.index or save_memory's related_skills missed "
            "something relevant - both are ranked/capped hints, not the full picture. Returns EVERY "
            "learned skill's name and description, unranked, uncapped. Scan it yourself and decide "
            "what's actually relevant; then call icx_skill_get for full content on the one(s) you "
            "want. No input."
        ),
        inputSchema={"type": "object", "properties": {}, "required": []},
            annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    ),
]


async def dispatch_skills_tool(name: str, arguments: dict) -> list[TextContent] | None:
    args = arguments or {}

    if name == _GET_METHODOLOGY_TOOL:
        from icx_engine.methodology import full_text, METHODOLOGY_VERSION
        return [TextContent(type="text", text=json.dumps(
            {"version": METHODOLOGY_VERSION, "methodology": full_text()}))]

    if name == _DRAFT_SKILL_TOOL:
        issue_key = args.get("issue_key")
        if not isinstance(issue_key, str) or not issue_key.strip():
            return [TextContent(type="text", text=json.dumps(
                {"error": "issue_key must be a non-empty string."}))]
        skill_worthy = bool(args.get("skill_worthy", False))
        if not skill_worthy:
            return [TextContent(type="text", text=json.dumps({"status": "skipped"}))]

        skill_name = args.get("skill_name")
        description = args.get("description")
        when_to_use = args.get("when_to_use")
        procedure = args.get("procedure")
        verification = args.get("verification")
        missing = [
            field_name for field_name, value in (
                ("skill_name", skill_name), ("description", description),
                ("when_to_use", when_to_use), ("procedure", procedure),
                ("verification", verification),
            ) if not isinstance(value, str) or not value.strip()
        ]
        if missing:
            return [TextContent(type="text", text=json.dumps(
                {"error": f"skill_worthy=true requires: {', '.join(missing)}."}))]

        try:
            loop = asyncio.get_running_loop()
            entry = await loop.run_in_executor(_get_memory_executor(), _show_entry_sync, issue_key.strip())
        except Exception as exc:
            return [TextContent(type="text", text=json.dumps(
                {"error": f"Failed to look up memory entry for issue_key '{issue_key}': {exc}"}))]
        if entry is None:
            return [TextContent(type="text", text=json.dumps(
                {"error": f"No memory entry found for issue_key '{issue_key}'. Call save_memory first."}))]
        if not entry.outcome_verified:
            return [TextContent(type="text", text=json.dumps(
                {"error": "The entry for this issue_key is not outcome_verified. A skill can only be drafted from a verified fix."}))]

        pitfalls = args.get("pitfalls") if isinstance(args.get("pitfalls"), str) else ""
        tags = [str(t) for t in (args.get("tags") or [])] if isinstance(args.get("tags"), list) else []

        try:
            from icx_engine.skills.writer import draft_skill_entry, write_or_update
            draft = draft_skill_entry(
                entry, skill_name.strip(), description.strip(), when_to_use.strip(),
                procedure.strip(), verification.strip(), pitfalls=pitfalls, tags=tags,
            )
            status = write_or_update(SkillStorage(), draft)
        except Exception as exc:
            return [TextContent(type="text", text=json.dumps(
                {"error": f"Failed to draft skill: {exc}"}))]
        return [TextContent(type="text", text=json.dumps({"status": status, "name": draft.name}))]

    if name == _CREATE_SKILL_TOOL:
        name_arg = args.get("name")
        description = args.get("description")
        when_to_use = args.get("when_to_use")
        procedure = args.get("procedure")
        verification = args.get("verification")
        missing = [
            field_name for field_name, value in (
                ("name", name_arg), ("description", description),
                ("when_to_use", when_to_use), ("procedure", procedure),
                ("verification", verification),
            ) if not isinstance(value, str) or not value.strip()
        ]
        if missing:
            return [TextContent(type="text", text=json.dumps(
                {"error": f"Missing required field(s): {', '.join(missing)}."}))]

        pitfalls = args.get("pitfalls") if isinstance(args.get("pitfalls"), str) else ""
        tags = [str(t) for t in (args.get("tags") or [])] if isinstance(args.get("tags"), list) else []
        project_key = args.get("project_key")
        project_key = project_key.strip() if isinstance(project_key, str) and project_key.strip() else None
        origin_projects = [project_key] if project_key else []

        try:
            from icx_engine.skills.schema import SkillEntry
            from icx_engine.skills.writer import _slugify, write_or_update
            slug = _slugify(name_arg)
            draft = SkillEntry(
                name=slug, description=description.strip(), tags=tags, origin_projects=origin_projects,
                origin_issue_keys=[], scope_hint="repo-specific" if origin_projects else "generic",
                title=name_arg.strip(), when_to_use=when_to_use.strip(), procedure=procedure.strip(),
                pitfalls=pitfalls.strip(), verification=verification.strip(),
            )
            draft.icx_hash = draft.compute_hash()
            status = write_or_update(SkillStorage(), draft)
        except Exception as exc:
            return [TextContent(type="text", text=json.dumps(
                {"error": f"Failed to create skill: {exc}"}))]
        return [TextContent(type="text", text=json.dumps({"status": status, "name": slug}))]

    if name == _SKILL_GET_TOOL:
        skill_name = args.get("name")
        if not isinstance(skill_name, str) or not skill_name.strip():
            return [TextContent(type="text", text=json.dumps(
                {"error": "name must be a non-empty string."}))]
        entry = SkillStorage().read(skill_name.strip())
        if entry is None:
            return [TextContent(type="text", text=json.dumps(
                {"error": f"No skill named '{skill_name}' found."}))]
        return [TextContent(type="text", text=json.dumps({"body": entry.to_markdown()}))]

    if name == _SKILLS_INDEX_TOOL:
        skills = SkillStorage().list_all()
        return [TextContent(type="text", text=json.dumps(
            {"skills": [{"name": s.name, "description": s.description} for s in skills]}))]

    return None
