"""
ICX MCP server - stdio transport.
Spawned by: icx mcp run
Communicates over: stdin/stdout (MCP JSON-RPC protocol)
"""
from __future__ import annotations
import asyncio
import json

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

from icx_engine.config_manager import ConfigManager
from icx_engine import engine
from icx_engine.exceptions import ICXError

server = Server("icx")

_FAST_TOOL_NAME = "analyze_issue_fast"
_FULL_TOOL_NAME = "analyze_issue"
_SEARCH_TOOL_NAME = "search_memory"
_SAVE_TOOL_NAME = "save_memory"

_FAST_DESCRIPTION = (
    "ALWAYS call this tool first before working on any development issue. "
    "Use it whenever the user mentions an issue key (e.g. PROJ-123, ABC-456) or a tracker URL, "
    "or asks you to implement, fix, or investigate a feature, bug, task, or story. "
    "Do NOT attempt to fetch the URL directly - the tracker requires authentication that this tool handles automatically.\n\n"
    "Text-only analysis - skips image processing for speed. Returns an IssueContext JSON object with these fields:\n"
    "  - problem_summary - plain-English description of what needs to be done\n"
    "  - detailed_description - full context and background\n"
    "  - reproduction_steps - ordered steps to reproduce (bugs only; empty list otherwise)\n"
    "  - expected_behavior - what should happen (null for tasks/stories)\n"
    "  - actual_behavior - what currently happens (null for tasks/stories)\n"
    "  - acceptance_criteria - explicit list of conditions the implementation must satisfy\n"
    "  - impact - who is affected and how severely\n"
    "  - priority - priority from the tracker\n"
    "  - issue_type - Bug / Story / Task / Sub-task etc.\n"
    "  - confidence_score - 0.0-1.0 confidence in the analysis\n"
    "  - completeness_score - 0.0-1.0 reflecting how well-specified the issue is\n"
    "  - missing_information - list of gaps that may block implementation\n"
    "  - pending_images - filenames of image attachments that were NOT processed (empty when no images exist)\n\n"
    "After calling: check pending_images. If it is non-empty AND you judge the images are relevant to "
    "understanding the issue, call analyze_issue for full vision analysis. This is your decision - "
    "do not auto-escalate for every issue that has images.\n\n"
    "Read every field before writing any code. "
    "Use acceptance_criteria as your implementation checklist. "
    "CRITICAL - BEFORE WRITING ANY CODE: Check missing_information and completeness_score. "
    "If missing_information is non-empty OR completeness_score is below 0.8, you MUST NOT start "
    "coding. Report every gap to the developer and ask for clarification first."
)

_FULL_DESCRIPTION = (
    "Full analysis with vision and OCR. Call this ONLY when analyze_issue_fast returned non-empty "
    "pending_images AND you judge the images are relevant to understanding the issue.\n\n"
    "Do NOT call this by default. Only call it when images are genuinely needed - for example, "
    "when the issue is a UI bug with a screenshot or an error with a diagram. "
    "If the issue is purely textual, skip this tool.\n\n"
    "Returns an IssueContext JSON object with the same fields as analyze_issue_fast, plus:\n"
    "  - images - dict of filename to Base64-encoded image data (populated when images exist)\n"
    "  - pending_images - always empty in full-vision mode\n\n"
    "CRITICAL - same rule applies: if missing_information is non-empty OR completeness_score is "
    "below 0.8, you MUST NOT start coding. Report every gap to the developer and ask for clarification first."
)

_SEARCH_DESCRIPTION = (
    "Search local memory for past resolutions to similar issues. "
    "Call this AFTER analyze_issue_fast (or analyze_issue) once you understand the problem.\n\n"
    "Write a semantically specific query using technical terms from the analysis - "
    "for example, \"OAuth token expiry JWT 401 auth middleware\" rather than just the issue key. "
    "The more specific your query, the more relevant the results.\n\n"
    "Do NOT pass the raw issue key as the query. The query should describe the technical problem.\n\n"
    "Returns: {\"results\": [...], \"count\": N}\n"
    "Each result includes: issue_key, summary, resolution_note, files_changed, similarity_score, saved_at."
)

_SAVE_DESCRIPTION = (
    "Save a resolution to local memory after the fix is tested and confirmed. "
    "Call this ONLY after the developer explicitly confirms the fix is working.\n\n"
    "Before calling this tool, ask the developer: "
    "\"The fix is complete. Please test it manually and let me know if you need any adjustments, "
    "or if we can save this resolution to memory.\"\n\n"
    "Wait for the developer to confirm the fix is verified and working before calling this tool.\n\n"
    "This tool re-fetches the issue from the tracker to capture current metadata. "
    "You provide the resolution details.\n\n"
    "Returns: {\"saved\": true, \"issue_key\": \"...\", \"summary\": \"...\"} on success."
)

_ISSUE_REF_SCHEMA = {
    "type": "string",
    "description": (
        "Issue identifier. Accepted formats:\n"
        "  - Full URL - paste the issue URL exactly as it appears in your browser\n"
        "  - Bare issue key - PROJ-123 or ABC-456\n"
        "Pass exactly what the user provided; do not normalise or guess the domain."
    ),
}

_PROFILE_SCHEMA = {
    "type": "string",
    "description": (
        "Optional AI profile name to use for this analysis. "
        "Must match a profile configured in ICX. "
        "Defaults to the active profile when omitted."
    ),
}


@server.list_tools()
async def _list_tools() -> list[Tool]:
    try:
        profile_names = sorted(ConfigManager.load().llm_profiles.keys())
    except Exception:
        profile_names = []

    if profile_names:
        profile_hint = f" Optional: 'profile' parameter. Available in your config: {profile_names!r}."
    else:
        profile_hint = ""

    analyze_schema = {
        "type": "object",
        "properties": {
            "issue_ref": _ISSUE_REF_SCHEMA,
            "profile": _PROFILE_SCHEMA,
        },
        "required": ["issue_ref"],
    }

    return [
        Tool(
            name=_FAST_TOOL_NAME,
            description=_FAST_DESCRIPTION + profile_hint,
            inputSchema=analyze_schema,
        ),
        Tool(
            name=_FULL_TOOL_NAME,
            description=_FULL_DESCRIPTION + profile_hint,
            inputSchema=analyze_schema,
        ),
        Tool(
            name=_SEARCH_TOOL_NAME,
            description=_SEARCH_DESCRIPTION,
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Free-form text describing the technical problem. Use specific technical terms.",
                    }
                },
                "required": ["query"],
            },
        ),
        Tool(
            name=_SAVE_TOOL_NAME,
            description=_SAVE_DESCRIPTION,
            inputSchema={
                "type": "object",
                "properties": {
                    "issue_key": {
                        "type": "string",
                        "description": "Issue key (e.g. PROJ-456) or full URL.",
                    },
                    "resolution_note": {
                        "type": "string",
                        "description": "Description of the fix - what was changed and why it works.",
                    },
                    "files_changed": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of file paths that were modified. Optional.",
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional tags for categorising the memory entry (e.g. ['auth', 'jwt']).",
                    },
                },
                "required": ["issue_key", "resolution_note"],
            },
        ),
    ]


@server.call_tool()
async def _call_tool(name: str, arguments: dict | None) -> list[TextContent]:
    args = arguments or {}

    if name in (_FAST_TOOL_NAME, _FULL_TOOL_NAME):
        issue_ref = args.get("issue_ref", "")
        if not isinstance(issue_ref, str) or not issue_ref.strip() or len(issue_ref) > 2048:
            return [TextContent(type="text", text=json.dumps(
                {"error": "issue_ref must be a non-empty string under 2048 characters."}
            ))]
        profile = args.get("profile")
        if profile is not None:
            if not isinstance(profile, str) or len(profile) > 256:
                return [TextContent(type="text", text=json.dumps(
                    {"error": "profile must be a string under 256 characters."}
                ))]
            profile = profile.strip() or None
        skip_vision = name == _FAST_TOOL_NAME
        text = await _handle_analyze_issue(issue_ref.strip(), profile=profile, skip_vision=skip_vision)
        return [TextContent(type="text", text=text)]

    if name == _SEARCH_TOOL_NAME:
        query = args.get("query", "")
        if not isinstance(query, str) or not query.strip() or len(query) > 2048:
            return [TextContent(type="text", text=json.dumps(
                {"error": "query must be a non-empty string under 2048 characters."}
            ))]
        text = await _handle_search_memory(query.strip())
        return [TextContent(type="text", text=text)]

    if name == _SAVE_TOOL_NAME:
        issue_key = args.get("issue_key", "")
        if not isinstance(issue_key, str) or not issue_key.strip() or len(issue_key) > 2048:
            return [TextContent(type="text", text=json.dumps(
                {"error": "issue_key must be a non-empty string under 2048 characters."}
            ))]
        resolution_note = args.get("resolution_note", "")
        if not isinstance(resolution_note, str) or not resolution_note.strip() or len(resolution_note) > 10000:
            return [TextContent(type="text", text=json.dumps(
                {"error": "resolution_note must be a non-empty string under 10000 characters."}
            ))]
        files_changed = args.get("files_changed") or []
        if not isinstance(files_changed, list):
            return [TextContent(type="text", text=json.dumps(
                {"error": "files_changed must be a list of strings."}
            ))]
        if len(files_changed) > 100:
            return [TextContent(type="text", text=json.dumps(
                {"error": "files_changed must not contain more than 100 entries."}
            ))]
        tags = args.get("tags") or []
        if not isinstance(tags, list):
            return [TextContent(type="text", text=json.dumps(
                {"error": "tags must be a list of strings."}
            ))]
        if len(tags) > 50:
            return [TextContent(type="text", text=json.dumps(
                {"error": "tags must not contain more than 50 entries."}
            ))]
        text = await _handle_save_memory(
            issue_key.strip(),
            resolution_note.strip(),
            [str(f) for f in files_changed],
            [str(t) for t in tags],
        )
        return [TextContent(type="text", text=text)]

    return [TextContent(type="text", text=json.dumps({"error": f"Unknown tool: {name}"}))]


async def _handle_analyze_issue(
    issue_ref: str,
    profile: str | None = None,
    skip_vision: bool = False,
) -> str:
    """Testable core: load config, run engine pipeline, return IssueContext as JSON."""
    try:
        config = ConfigManager.load()
        result = await engine.run(
            issue_ref, config, mcp_mode=True,
            profile_override=profile, skip_vision=skip_vision,
        )
        return result.model_dump_json(indent=2)
    except ICXError as exc:
        return json.dumps({"error": str(exc), "type": type(exc).__name__})
    except Exception as exc:
        return json.dumps({"error": f"Unexpected error: {exc}"})


async def _handle_search_memory(query: str) -> str:
    """Search local memory with a free-form query string."""
    try:
        from icx_engine.memory import MemoryManager, MemoryQueryInput
        _mem = MemoryManager()
        _qi = MemoryQueryInput(
            issue_key="",
            project_key="",
            source_type="",
            summary=query,
            description=query,
            issue_type="",
        )
        results = _mem.query(_qi)
        return json.dumps({
            "results": [r.model_dump() for r in results],
            "count": len(results),
        })
    except Exception as exc:
        return json.dumps({"error": f"Memory search failed: {exc}"})


async def _handle_save_memory(
    issue_key: str,
    resolution_note: str,
    files_changed: list[str],
    tags: list[str],
) -> str:
    """Re-fetch issue metadata from the tracker and save resolution to local memory."""
    import uuid
    from datetime import datetime, timezone
    try:
        config = ConfigManager.load()
        from icx_engine.engine import extract_domain, resolve_connection, _extract_project_key
        domain = extract_domain(issue_key)
        conn = resolve_connection(domain, config, raw_input=issue_key)
        if conn is None:
            return json.dumps({"error": "Multiple connections configured. Include the full URL."})
        from icx_engine.connectors.base import get_connector
        connector = get_connector(conn)
        parsed = connector.parse_input(issue_key)
        raw = await connector.fetch(parsed.issue_key, config)
        from icx_engine.memory.manager import MemoryManager
        from icx_engine.memory.schema import MemoryEntry
        entry = MemoryEntry(
            id=str(uuid.uuid4()),
            issue_key=parsed.issue_key,
            project_key=_extract_project_key(parsed.issue_key),
            source_type=conn.connector_type,
            issue_type=raw.issue_type,
            summary=raw.summary,
            problem_description=(raw.description or "")[:2000],
            impact="",
            resolution_note=resolution_note,
            files_changed=files_changed,
            resolution_confirmed=True,
            saved_at=datetime.now(timezone.utc).isoformat(),
            tags=tags,
        )
        MemoryManager().save(entry)
        return json.dumps({
            "saved": True,
            "issue_key": parsed.issue_key,
            "summary": raw.summary[:80],
        })
    except ICXError as exc:
        return json.dumps({"error": str(exc), "type": type(exc).__name__})
    except Exception as exc:
        return json.dumps({"error": f"Unexpected error: {exc}"})


def run_mcp_server() -> None:
    """Entry point called by `icx mcp run`. Blocks until the MCP host closes stdio."""
    async def _serve() -> None:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options(),
            )

    asyncio.run(_serve())
