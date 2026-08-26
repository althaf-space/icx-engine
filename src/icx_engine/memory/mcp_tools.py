"""MCP tool surface for the local RAG memory store. Owns its own Tool()
definitions and dispatch function - mcp_server.py's _list_tools()/_call_tool()
get a few additive lines only, no restructuring."""

from __future__ import annotations

import asyncio
import functools
import json

from mcp.types import TextContent, Tool

from icx_engine.mcp_server import (
    _delete_entry_sync, _find_by_file_sync, _get_audit_trail_sync, _get_hotspots_sync,
    _get_memory_executor, _get_memory_state, _get_patterns_sync, _get_related_sync,
    _reinforce_usage_sync, _search_memory_sync, _update_entry_sync, _validate_issue_key_arg,
)

def _paginate(items: list, limit, offset) -> tuple[list, dict]:
    """limit omitted (None): returns items unchanged, no extra fields - exact legacy behavior.
    limit given: slices [offset:offset+limit] and returns total/has_more/next_offset alongside."""
    if limit is None:
        return items, {}
    offset = offset or 0
    total = len(items)
    sliced = items[offset:offset + limit]
    has_more = offset + len(sliced) < total
    return sliced, {"total": total, "has_more": has_more, "next_offset": (offset + len(sliced)) if has_more else None}


_MEM_SEARCH_TOOL = "memory_search"
_MEM_HOTSPOTS_TOOL = "memory_get_hotspots"
_MEM_BY_FILE_TOOL = "memory_find_by_file"
_MEM_RELATED_TOOL = "memory_get_related"
_MEM_PATTERNS_TOOL = "memory_get_patterns"
_MEM_DELETE_TOOL = "memory_delete"
_MEM_UPDATE_TOOL = "memory_update"
_REINFORCE_TOOL_NAME = "reinforce_memory_usage"
_AUDIT_TOOL_NAME = "get_memory_audit"

_REINFORCE_DESCRIPTION = """\
CALL BEFORE save_memory - every time a past memory_search result influenced your approach on a new ticket.
USE WHEN: memory_search returned results AND any result shaped your implementation plan, \
approach direction, or confirmed a diagnosis. Call even if you modified the approach slightly.
MANDATORY: skipping this call is a violation when memory_search was useful. \
This is how ICX memory becomes self-improving - cited resolutions surface first on future similar tickets.

RETURNS: {source_key, usage_count, cross_reference_boost, siblings_updated}
  usage_count >= 5 -> source entry auto-elevated to memory_confidence >= 0.75
  usage_count >= 10 -> source entry auto-elevated to memory_confidence = 1.0
VALUE: Entries cited repeatedly differentiate from untested guesses. Without this call, the best \
resolutions never gain credibility over ones that were never verified. One call = one evidence vote.

WHEN TO CALL:
  - After user confirms fix works AND before calling save_memory
  - source_key: the issue_key from the memory_search result you referenced
  - new_ticket_key: the issue_key of the ticket you are currently solving
SKIP ONLY IF: memory_search returned no results OR no result influenced your approach in any way.
RUNTIME: under 1 second.

EXAMPLE: memory_search returned PROJ-88 [JWT expiry fix] and you applied the same pattern ->
  reinforce_memory_usage(source_key="PROJ-88", new_ticket_key="PROJ-142")
  -> {source_key: "PROJ-88", usage_count: 4, cross_reference_boost: 0.60, siblings_updated: 1}
  -> PROJ-88 now ranks higher on all future similar tickets\
"""


_AUDIT_DESCRIPTION = """\
USE WHEN: Investigating why a memory entry has an unexpected confidence score, boost value, or ranking.
ANSWERS: "Why does PROJ-88 rank higher than PROJ-91?" / "Was PROJ-42 ever verified?" / "When was PROJ-55 negated?"

RETURNS: All audit events for issue_key sorted newest-first:
  {event_type, source_key, actor_key, timestamp, before_boost, after_boost, before_confidence, after_confidence, note}
  event_types: reinforced | verified | negated | hub_detected
VALUE: Full traceability of every mutation that changed an entry's credibility. Confirms whether \
reinforcements were actually recorded, verifications applied correctly, and why a negation propagated.

WHEN TO CALL:
  - When a memory_search result ranks unexpectedly high or low
  - Before deciding to negate an entry - verify it has not already been negated
  - When a developer reports ICX surfacing a wrong resolution repeatedly
SKIP: This is a diagnostic tool. Do not call it on every ticket. Call it when something seems wrong.
RUNTIME: under 1 second.

EXAMPLE: get_memory_audit(issue_key="PROJ-88", limit=5) ->
  [{event_type:"reinforced", actor_key:"PROJ-142", after_boost:0.60, note:""},
   {event_type:"reinforced", actor_key:"PROJ-91",  after_boost:0.45, note:""},
   {event_type:"verified",   actor_key:"developer", after_confidence:0.75, note:"Deployed, no errors"}]
  -> PROJ-88 was reinforced by 2 tickets and verified by a developer - high boost is correct\
"""


MEMORY_TOOLS: list[Tool] = [
    Tool(
        name=_MEM_SEARCH_TOOL,
        description=(
            "CALL IMMEDIATELY AFTER jira_analyze_issue - before any graph or file exploration.\n"
            "Search memory using tags YOU generate from work_item.analysis. "
            "The analysis result is what teaches you the problem domain - use it now.\n\n"
            "HOW TO GENERATE TAGS: From work_item.analysis extract 3-6 specific lowercase tags covering:\n"
            "  - Problem domain (e.g. 'jwt-expiry', 'db-connection-pool', 'websocket-reconnect')\n"
            "  - Affected system layer (e.g. 'auth-middleware', 'data-access-layer', 'event-handler')\n"
            "  - Failure type (e.g. 'race-condition', 'null-reference', 'timeout', 'off-by-one')\n"
            "  Rules: no generic words (bug/fix/error/issue/backend), hyphenate multi-word concepts.\n"
            "  GOOD: ['jwt-expiry', 'auth-middleware', 'clock-skew']\n"
            "  BAD: ['auth', 'bug', 'backend']\n\n"
            "SKIP ONLY IF: memory.status != 'ready' in the jira_analyze_issue response.\n\n"
            "RETURNS: Ranked list of past work items with resolution_note, files_changed, similarity_score.\n"
            "Use results as a pattern reference when forming your implementation approach - "
            "if a past resolution matches this problem, derive your approach from it.\n"
            "RUNTIME: under 1 second."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Problem description to search - combine summary and key symptoms from work_item.analysis.",
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "3-6 agent-generated tags derived from work_item.analysis. Hyphenate multi-word. GOOD: ['jwt-expiry', 'auth-middleware']. BAD: ['auth', 'bug'].",
                },
                "top_k": {
                    "type": "integer",
                    "description": "Max results to return. Default 5.",
                    "default": 5,
                },
            },
            "required": ["query", "tags"],
        },
            annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    ),
    Tool(
        name=_MEM_HOTSPOTS_TOOL,
        description=(
            "ANSWERS: 'Which FILES have the most bugs?' - ranks files by historical work item count.\n"
            "USE AT THE START OF BUG INVESTIGATION OR REFACTORING PLANNING - shows which files "
            "carry the most historical bug density.\n"
            "RETURNS: Top N files ranked by saved work item count: "
            "[{file, count, work_items: [issue_keys]}] sorted by count descending.\n"
            "RUNTIME: ~1 second.\n"
            "VALUE: Files with high counts are fragile. They need extra test coverage, extra caution "
            "before editing, and are the strongest candidates for refactoring. "
            "A file with 8 past bugs is a pattern, not a coincidence.\n"
            "WHEN TO CALL:\n"
            "  - At the start of a bug investigation to identify fragile areas before searching\n"
            "  - When planning a refactor to know which files carry the highest risk\n"
            "  - When deciding where to add tests first\n"
            "EXAMPLE: memory_get_hotspots() ->\n"
            "  1. src/auth/token.py [count=8]\n"
            "  2. src/billing/invoice.py [count=6]\n"
            "  3. src/api/middleware.py [count=5]\n"
            "  -> Before touching any of these files, call memory_find_by_file on each one."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project_key": {
                    "type": "string",
                    "description": "Optional project key to restrict to one project.",
                },
                "top_n": {
                    "type": "integer",
                    "description": "Number of top files to return (default 20, max 100).",
                    "default": 20,
                },
            },
            "required": [],
        },
            annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    ),
    Tool(
        name=_MEM_BY_FILE_TOOL,
        description=(
            "CALL BEFORE EDITING ANY FILE - surfaces past bugs, fixes, and regressions for that exact file.\n"
            "Uses substring matching (case-insensitive, cross-platform path separators).\n"
            "RETURNS: All saved work items whose files_changed list contains the given path. "
            "Each result includes: issue_key, resolution_note (what fix was applied), tags, files_changed.\n"
            "VALUE: Tells you what broke here before and how it was fixed. Prevents repeating past mistakes. "
            "Without this call, you may re-introduce a regression that was already solved.\n"
            "RUNTIME: under 1 second per file.\n"
            "WHEN TO CALL: For each core file identified in STEP 1 and STEP 1b that you intend to modify. "
            "SKIP ONLY IF the file is brand new with zero edit history.\n"
            "EXAMPLE: memory_find_by_file('src/auth/token.py') ->\n"
            "  PROJ-88: 'JWT not expiring' fixed by adding clock_skew tolerance in validate_token()\n"
            "  PROJ-112: 'Token refresh race' fixed by adding lock in refresh_token()\n"
            "  -> token.py has two past concurrency/timing bugs - check both before changing it."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "File path to look up (relative or absolute; substring match).",
                },
                "project_key": {
                    "type": "string",
                    "description": "Optional project key to restrict search (e.g. PROJ).",
                },
                "limit": {"type": "integer", "description": "Optional - page results instead of returning all."},
                "offset": {"type": "integer"},
            },
            "required": ["file_path"],
        },
            annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    ),
    Tool(
        name=_MEM_RELATED_TOOL,
        description=(
            "CALL AFTER IDENTIFYING THE BUG LOCATION - discovers historically coupled issues "
            "via shared change history.\n"
            "How it works: tickets that touched the same files are related. "
            "Strength = shared file count / max(your files, their files) (0.0-1.0).\n"
            "RETURNS: [{issue_key, relation_type, strength}] sorted by strength descending.\n"
            "RUNTIME: under 1 second.\n"
            "TWO MODES - pass one or both:\n"
            "  files (primary - use for all tickets): pass file paths from graph_find_context. "
            "Computes overlap on-the-fly against all saved entries. Works for new tickets.\n"
            "  issue_key (secondary - reopened tickets only): looks up pre-stored edges. "
            "Only returns results if this ticket was previously saved via save_memory.\n"
            "When both provided: edges checked first; falls back to file overlap if no edges found.\n"
            "VALUE: Finds hidden dependencies you would miss by reading code alone. "
            "A high-strength result means 'when you fix X, Y tends to also need attention.' "
            "Prevents fixing one place while silently breaking another.\n"
            "WHEN TO CALL:\n"
            "  - After graph_find_context - pass its file results directly\n"
            "  - When the ticket touches multiple features or services\n"
            "  - Before writing your test plan (related issues reveal regression-test areas)\n"
            "EXAMPLE: memory_get_related(files=['auth/token.py', 'auth/session.py']) ->\n"
            "  PROJ-88 [strength=0.75] - 3 shared files including auth/token.py\n"
            "  PROJ-112 [strength=0.50] - 2 shared files including auth/session.py\n"
            "  -> This fix area has been broken twice before. "
            "Read those resolution_notes before writing your approach."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "File paths for the current ticket, from graph_find_context results. "
                        "Primary input - works for new tickets with no prior history."
                    ),
                },
                "issue_key": {
                    "type": "string",
                    "description": (
                        "Issue key to look up pre-stored edges (e.g. PROJ-123). "
                        "Use for reopened tickets only - requires prior save_memory call."
                    ),
                },
                "project_key": {
                    "type": "string",
                    "description": "Optional project key to restrict results to one project.",
                },
                "limit": {"type": "integer", "description": "Optional - page results instead of returning all."},
                "offset": {"type": "integer"},
            },
            "required": [],
        },
            annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    ),
    Tool(
        name=_MEM_PATTERNS_TOOL,
        description=(
            "ANSWERS: 'Which BUG CATEGORIES keep recurring?' - detects systemic failure patterns across all saved work items.\n"
            "USE WHEN THE SAME BUG CATEGORY KEEPS RECURRING OR BEFORE MAJOR REFACTORING - "
            "reveals systemic weaknesses in the codebase.\n"
            "Patterns are recomputed every 5 saves.\n"
            "RUNTIME: under 1 second.\n"
            "RETURNS: [{project_key, pattern_type, label, evidence, entry_count, detected_at}]\n"
            "Pattern types and what they mean:\n"
            "  frequent_file: This file appears in >= 30% of all saved work items. "
            "Architectural smell - likely needs a rewrite.\n"
            "  dominant_tag: This category (e.g. 'auth', 'date-handling', 'validation') "
            "appears in >= 20% of bugs. A systemic failure category.\n"
            "  top_work_item_type: One work item type (bug/story/task) makes up > 50% of all saved items.\n"
            "VALUE: Reveals root causes at the architectural level, not just per-file. "
            "If 'auth' is a dominant_tag, fixing individual auth bugs is not enough - "
            "the auth system itself needs redesign.\n"
            "WHEN TO CALL:\n"
            "  - Before major refactoring to understand what keeps breaking and why\n"
            "  - When the same bug category recurs (date parsing, null handling, auth)\n"
            "  - When planning technical debt work to prioritize highest-impact areas\n"
            "EXAMPLE: memory_get_patterns() ->\n"
            "  hotspot_file: src/auth/token.py [appears in 40% of bugs, entry_count=12]\n"
            "  dominant_tag: 'date-handling' [present in 25% of bugs]\n"
            "  -> token.py needs a rewrite; date handling needs a shared utility layer."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project_key": {
                    "type": "string",
                    "description": "Optional project key to restrict to one project.",
                },
                "limit": {"type": "integer", "description": "Optional - page results instead of returning all."},
                "offset": {"type": "integer"},
            },
            "required": [],
        },
            annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    ),
    Tool(
        name=_MEM_DELETE_TOOL,
        description=(
            "USE WHEN the human explicitly wants a saved memory entry permanently removed - MUST "
            "delete exactly that issue_key's entry (relations cleaned up automatically). This is a "
            "CONFIRMATION-GATED tool: the first call (no confirm_token) returns pending_confirmation "
            "plus a one-time token after showing the human the issue_key - you MUST show that to the "
            "human and get an explicit yes before calling again with confirm_token set. Calling with "
            "a wrong or reused token fails. Returns {ok: true, issue_key} on success or "
            "{ok: false, error} on failure."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "issue_key": {"type": "string", "description": "Issue key to delete, e.g. PROJ-456."},
                "confirm_token": {"type": "string"},
            },
            "required": ["issue_key"],
        },
            annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False},
    ),
    Tool(
        name=_MEM_UPDATE_TOOL,
        description=(
            "USE WHEN a saved memory entry's own text or file list needs correcting - not for "
            "recording a new fix (use save_memory) or a verification outcome (use save_memory's "
            "outcome_verified/negate flags). Pass issue_key plus only the field(s) you want to "
            "change; unlisted fields are left untouched. files_changed and tags are REPLACED "
            "entirely, not merged/appended. UNGATED - no confirm_token. Returns "
            "{ok: true, issue_key, updated_fields} on success or {ok: false, error} on failure "
            "(unknown issue_key, or a field outside the allowed set)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "issue_key": {"type": "string", "description": "Issue key to update, e.g. PROJ-456."},
                "summary": {"type": "string"},
                "problem_description": {"type": "string"},
                "impact": {"type": "string"},
                "resolution_note": {"type": "string"},
                "files_changed": {"type": "array", "items": {"type": "string"}, "description": "Replaces the entry's files_changed entirely."},
                "tags": {"type": "array", "items": {"type": "string"}, "description": "Replaces the entry's tags entirely."},
            },
            "required": ["issue_key"],
        },
            annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
    ),
    Tool(
        name=_REINFORCE_TOOL_NAME,
        description=_REINFORCE_DESCRIPTION,
        inputSchema={
            "type": "object",
            "properties": {
                "source_key": {
                    "type": "string",
                    "description": "Issue key of the past resolution you referenced (from memory_search results).",
                },
                "new_ticket_key": {
                    "type": "string",
                    "description": "Issue key of the ticket you are currently solving.",
                },
            },
            "required": ["source_key", "new_ticket_key"],
        },
            annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False},
    ),
    Tool(
        name=_AUDIT_TOOL_NAME,
        description=_AUDIT_DESCRIPTION,
        inputSchema={
            "type": "object",
            "properties": {
                "issue_key": {
                    "type": "string",
                    "description": "Issue key of the memory entry to audit.",
                },
                "limit": {
                    "type": "integer",
                    "default": 20,
                    "description": "Maximum events to return.",
                },
            },
            "required": ["issue_key"],
        },
            annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    ),
]


async def dispatch_memory_tool(name: str, arguments: dict) -> list[TextContent] | None:
    args = arguments or {}

    if name == _MEM_SEARCH_TOOL:
        query = args.get("query", "")
        if not isinstance(query, str) or not query.strip() or len(query) > 2000:
            return [TextContent(type="text", text=json.dumps(
                {"error": "query must be a non-empty string under 2000 characters."}
            ))]
        tags = args.get("tags") or []
        if not isinstance(tags, list):
            return [TextContent(type="text", text=json.dumps(
                {"error": "tags must be a list of strings."}
            ))]
        if len(tags) > 20:
            return [TextContent(type="text", text=json.dumps(
                {"error": "tags must not contain more than 20 entries."}
            ))]
        for _tag in tags:
            if not isinstance(_tag, str) or len(_tag) > 256:
                return [TextContent(type="text", text=json.dumps(
                    {"error": "Each tag must be a string under 256 characters."}
                ))]
        top_k_raw = args.get("top_k", 5)
        try:
            top_k = max(1, min(20, int(top_k_raw)))
        except (TypeError, ValueError):
            return [TextContent(type="text", text=json.dumps(
                {"error": "top_k must be an integer between 1 and 20."}
            ))]
        mem_state = _get_memory_state()
        if mem_state != "ready":
            return [TextContent(type="text", text=json.dumps(
                {"results": [], "count": 0, "status": mem_state,
                 "note": f"Memory not ready (status={mem_state!r}) - skipping search."}
            ))]
        from icx_engine.memory.schema import MemoryQueryInput
        import functools
        qi = MemoryQueryInput(
            issue_key="search",
            project_key="",
            source_type="",
            summary=query.strip(),
            description=query.strip(),
            issue_type="",
            tags=[str(t) for t in tags],
        )
        try:
            loop = asyncio.get_running_loop()
            smart = await loop.run_in_executor(
                _get_memory_executor(),
                functools.partial(_search_memory_sync, qi, top_k),
            )
            results = smart.get("results", [])
            return [TextContent(type="text", text=json.dumps({
                "results": results,
                "negative_signals": smart.get("negative_signals", []),
                "count": len(results),
                "status": "ok",
                "decay_applied": smart.get("decay_applied", False),
            }))]
        except Exception as exc:
            return [TextContent(type="text", text=json.dumps({"error": str(exc)}))]

    if name == _MEM_HOTSPOTS_TOOL:
        project_key = args.get("project_key") or None
        if project_key is not None and (not isinstance(project_key, str) or len(project_key) > 64):
            return [TextContent(type="text", text=json.dumps(
                {"error": "project_key must be a string under 64 characters."}
            ))]
        top_n_raw = args.get("top_n", 20)
        try:
            top_n = max(1, min(100, int(top_n_raw)))
        except (TypeError, ValueError):
            return [TextContent(type="text", text=json.dumps(
                {"error": "top_n must be an integer between 1 and 100."}
            ))]
        try:
            loop = asyncio.get_running_loop()
            hotspots = await loop.run_in_executor(
                _get_memory_executor(), _get_hotspots_sync, project_key, top_n
            )
            return [TextContent(type="text", text=json.dumps({"results": hotspots, "count": len(hotspots)}))]
        except Exception as exc:
            return [TextContent(type="text", text=json.dumps({"error": str(exc)}))]

    if name == _MEM_BY_FILE_TOOL:
        file_path = args.get("file_path", "")
        if not isinstance(file_path, str) or not file_path.strip():
            return [TextContent(type="text", text=json.dumps(
                {"error": "file_path must be a non-empty string."}
            ))]
        project_key = args.get("project_key") or None
        if project_key is not None and (not isinstance(project_key, str) or len(project_key) > 64):
            return [TextContent(type="text", text=json.dumps(
                {"error": "project_key must be a string under 64 characters."}
            ))]
        try:
            loop = asyncio.get_running_loop()
            entries = await loop.run_in_executor(
                _get_memory_executor(), _find_by_file_sync, file_path.strip(), project_key
            )
            entries, extra = _paginate(entries, args.get("limit"), args.get("offset"))
            return [TextContent(type="text", text=json.dumps({"results": entries, "count": len(entries), **extra}))]
        except Exception as exc:
            return [TextContent(type="text", text=json.dumps({"error": str(exc)}))]

    if name == _MEM_RELATED_TOOL:
        issue_key = args.get("issue_key") or None
        if issue_key is not None:
            if not isinstance(issue_key, str) or not issue_key.strip():
                return [TextContent(type="text", text=json.dumps(
                    {"error": "issue_key must be a non-empty string."}
                ))]
            issue_key = issue_key.strip().upper()
        files = args.get("files") or None
        if files is not None:
            if not isinstance(files, list):
                return [TextContent(type="text", text=json.dumps(
                    {"error": "files must be a list of strings."}
                ))]
            if not all(isinstance(f, str) and f.strip() for f in files):
                return [TextContent(type="text", text=json.dumps(
                    {"error": "each entry in files must be a non-empty string."}
                ))]
            files = [f.strip() for f in files]
        if not issue_key and not files:
            return [TextContent(type="text", text=json.dumps(
                {"error": "provide files (new ticket) or issue_key (reopened ticket)."}
            ))]
        project_key = args.get("project_key") or None
        if project_key is not None and (not isinstance(project_key, str) or len(project_key) > 64):
            return [TextContent(type="text", text=json.dumps(
                {"error": "project_key must be a string under 64 characters."}
            ))]
        try:
            loop = asyncio.get_running_loop()
            related = await loop.run_in_executor(
                _get_memory_executor(), _get_related_sync, issue_key, project_key, files
            )
            related, extra = _paginate(related, args.get("limit"), args.get("offset"))
            return [TextContent(type="text", text=json.dumps({"results": related, "count": len(related), **extra}))]
        except Exception as exc:
            return [TextContent(type="text", text=json.dumps({"error": str(exc)}))]

    if name == _MEM_PATTERNS_TOOL:
        project_key = args.get("project_key") or None
        if project_key is not None and (not isinstance(project_key, str) or len(project_key) > 64):
            return [TextContent(type="text", text=json.dumps(
                {"error": "project_key must be a string under 64 characters."}
            ))]
        try:
            loop = asyncio.get_running_loop()
            patterns = await loop.run_in_executor(
                _get_memory_executor(), _get_patterns_sync, project_key
            )
            patterns, extra = _paginate(patterns, args.get("limit"), args.get("offset"))
            return [TextContent(type="text", text=json.dumps({"results": patterns, "count": len(patterns), **extra}))]
        except Exception as exc:
            return [TextContent(type="text", text=json.dumps({"error": str(exc)}))]

    if name == _MEM_DELETE_TOOL:
        from icx_engine.confirm import issue_token, verify_token
        confirm_token = args.get("confirm_token")
        if not confirm_token:
            issue_key, err = _validate_issue_key_arg(args, "issue_key")
            if err:
                return err
            token = issue_token("memory_delete", {"issue_key": issue_key})
            return [TextContent(type="text", text=json.dumps({
                "status": "pending_confirmation",
                "token": token,
                "issue_key": issue_key,
                "instruction": "Show the human which memory entry (issue_key) is about to be "
                               "permanently deleted. Only call this tool again with confirm_token "
                               "set once they explicitly agree.",
            }))]
        payload = verify_token(confirm_token, "memory_delete")
        if payload is None:
            return [TextContent(type="text", text=json.dumps(
                {"ok": False, "error": "Invalid or already-used confirm_token. Call again without a token to get a fresh one."}
            ))]
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(_get_memory_executor(), _delete_entry_sync, payload["issue_key"])
            return [TextContent(type="text", text=json.dumps({"ok": True, "issue_key": payload["issue_key"]}))]
        except Exception as exc:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": str(exc)}))]

    if name == _MEM_UPDATE_TOOL:
        issue_key, err = _validate_issue_key_arg(args, "issue_key")
        if err:
            return err
        from icx_engine.memory.manager import _UPDATE_ALLOWED_FIELDS
        fields: dict = {}
        for field_name in _UPDATE_ALLOWED_FIELDS:
            if field_name not in args:
                continue
            value = args[field_name]
            if field_name in ("files_changed", "tags"):
                if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                    return [TextContent(type="text", text=json.dumps(
                        {"ok": False, "error": f"{field_name} must be a list of strings."}
                    ))]
                fields[field_name] = [str(v) for v in value]
            else:
                if not isinstance(value, str):
                    return [TextContent(type="text", text=json.dumps(
                        {"ok": False, "error": f"{field_name} must be a string."}
                    ))]
                fields[field_name] = value
        if not fields:
            return [TextContent(type="text", text=json.dumps(
                {"ok": False, "error": f"No updatable field given. Allowed: {sorted(_UPDATE_ALLOWED_FIELDS)}."}
            ))]
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                _get_memory_executor(), _update_entry_sync, issue_key, fields,
            )
            return [TextContent(type="text", text=json.dumps({
                "ok": True, "issue_key": issue_key, "updated_fields": sorted(fields),
            }))]
        except Exception as exc:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": str(exc)}))]

    if name == _REINFORCE_TOOL_NAME:
        source_key, err = _validate_issue_key_arg(args, "source_key")
        if err:
            return err
        # External MCP schema arg is "new_ticket_key" (unchanged - callers already
        # use this name) - the local variable is named used_by_key from here on to
        # match _reinforce_usage_sync/MemoryManager.reinforce_usage/used_by_tickets,
        # which already use this name consistently internally.
        used_by_key, err = _validate_issue_key_arg(args, "new_ticket_key")
        if err:
            return err
        mem_state = _get_memory_state()
        if mem_state != "ready":
            return [TextContent(type="text", text=json.dumps({"error": f"Memory not ready (status={mem_state!r})."}
            ))]
        import functools
        try:
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(
                _get_memory_executor(),
                functools.partial(_reinforce_usage_sync, source_key.strip(), used_by_key.strip()),
            )
            return [TextContent(type="text", text=json.dumps(result))]
        except Exception as exc:
            return [TextContent(type="text", text=json.dumps({"error": str(exc)}))]

    if name == _AUDIT_TOOL_NAME:
        issue_key_audit, err = _validate_issue_key_arg(args, "issue_key")
        if err:
            return err
        limit_raw = args.get("limit", 20)
        try:
            audit_limit = max(1, min(100, int(limit_raw)))
        except (TypeError, ValueError):
            audit_limit = 20
        mem_state = _get_memory_state()
        if mem_state != "ready":
            return [TextContent(type="text", text=json.dumps({"error": f"Memory not ready (status={mem_state!r})."}
            ))]
        import functools
        try:
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(
                _get_memory_executor(),
                functools.partial(_get_audit_trail_sync, issue_key_audit.strip(), audit_limit),
            )
            return [TextContent(type="text", text=json.dumps(result))]
        except Exception as exc:
            return [TextContent(type="text", text=json.dumps({"error": str(exc)}))]

    return None
