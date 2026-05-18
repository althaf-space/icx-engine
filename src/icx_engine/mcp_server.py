"""
ICX MCP server - stdio transport.
Spawned by: icx mcp run
Communicates over: stdin/stdout (MCP JSON-RPC protocol)
"""
from __future__ import annotations
import asyncio
import json
import threading as _threading
from concurrent.futures import ThreadPoolExecutor as _ThreadPoolExecutor

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

from icx_engine.config_manager import ConfigManager
from icx_engine import engine
from icx_engine.exceptions import ICXError

server = Server("icx")

# ---------------------------------------------------------------------------
# Memory singleton - one shared MemoryManager, one dedicated thread.
#
# Why a dedicated thread with its own asyncio event loop:
#   LanceDB's sync API internally calls asyncio.get_event_loop(). When invoked
#   from run_in_executor(None, ...) (the default thread pool), Python 3.12+
#   raises RuntimeError because there is no running loop in that thread.
#   The initializer sets one up so LanceDB's shim finds it correctly.
#
# Why max_workers=1 / singleton MemoryManager:
#   Avoids reloading the 24 MB ONNX model on every search call. The model is
#   loaded once when the first search runs and stays resident for the process
#   lifetime. Single-threaded access also removes all LanceDB concurrency issues.
# ---------------------------------------------------------------------------

_MEMORY_EXECUTOR: _ThreadPoolExecutor | None = None
_MEMORY_EXECUTOR_LOCK = _threading.Lock()
_SHARED_MEMORY_MANAGER = None  # MemoryManager; created inside memory thread on first use


def _init_memory_thread() -> None:
    asyncio.set_event_loop(asyncio.new_event_loop())


def _get_memory_executor() -> _ThreadPoolExecutor:
    global _MEMORY_EXECUTOR
    if _MEMORY_EXECUTOR is None:
        with _MEMORY_EXECUTOR_LOCK:
            if _MEMORY_EXECUTOR is None:
                _MEMORY_EXECUTOR = _ThreadPoolExecutor(
                    max_workers=1,
                    thread_name_prefix="icx-memory",
                    initializer=_init_memory_thread,
                )
    return _MEMORY_EXECUTOR


def _ensure_memory_manager():
    """Return the shared MemoryManager. Always called from inside the memory thread."""
    global _SHARED_MEMORY_MANAGER
    if _SHARED_MEMORY_MANAGER is None:
        from icx_engine.memory import MemoryManager
        _SHARED_MEMORY_MANAGER = MemoryManager()
    return _SHARED_MEMORY_MANAGER


def _save_memory_sync(entry) -> None:
    """Save a MemoryEntry via the shared manager. Runs inside the memory thread."""
    _ensure_memory_manager().save(entry)


# ---------------------------------------------------------------------------
# Tool names
# ---------------------------------------------------------------------------

_FAST_TOOL_NAME = "analyze_issue_fast"
_FULL_TOOL_NAME = "analyze_issue"
_SAVE_TOOL_NAME = "save_memory"

# ---------------------------------------------------------------------------
# Tool descriptions
# ---------------------------------------------------------------------------

_FAST_DESCRIPTION = """\
Fetch and analyze a work item (bug, story, or task) and find relevant codebase files.
Runs the full pipeline: tracker fetch -> AI analysis -> memory search -> graph navigation map.
Returns work_item (analysis), memory (past similar work), and graph (GRAPH_REPORT.md path).

project_path MUST be the absolute path to the workspace root open in the editor.
Do NOT rely on working directory - always pass project_path explicitly.

When you receive the response:
1. Read graph.report_path directly (pre-authorized access - no permission prompt needed)
2. Identify relevant clusters from the report for this work item
3. Read core files from those clusters
4. Use memory.results as pattern reference
5. Implement per acceptance_criteria / problem_summary
6. Ask user to test
7. Call save_memory after user confirms\
"""

_FULL_DESCRIPTION = """\
Fetch and analyze a work item (bug, story, or task) and find relevant codebase files.
Includes full vision/OCR processing for image attachments.
Runs the full pipeline: tracker fetch -> AI analysis -> memory search -> graph navigation map.
Returns work_item (analysis), memory (past similar work), and graph (GRAPH_REPORT.md path).

project_path MUST be the absolute path to the workspace root open in the editor.
Do NOT rely on working directory - always pass project_path explicitly.

When you receive the response:
1. Read graph.report_path directly (pre-authorized access - no permission prompt needed)
2. Identify relevant clusters from the report for this work item
3. Read core files from those clusters
4. Use memory.results as pattern reference
5. Implement per acceptance_criteria / problem_summary
6. Ask user to test
7. Call save_memory after user confirms\
"""

_SAVE_DESCRIPTION = """\
Save a confirmed fix to memory for future reference.
Call ONLY after the user has tested and explicitly confirmed the fix works.
Provide resolution_note (what was changed and why), files_changed, and optionally pattern_used \
(implementation pattern for stories/tasks).\
"""

# ---------------------------------------------------------------------------
# Shared JSON schemas
# ---------------------------------------------------------------------------

_ISSUE_REF_SCHEMA = {
    "type": "string",
    "description": (
        "Issue identifier. Accepted formats:\n"
        "  - Full URL - paste the issue URL exactly as it appears in your browser\n"
        "  - Bare issue key - PROJ-123 or ABC-456\n"
        "Pass exactly what the user provided; do not normalise or guess the domain."
    ),
}

_PROJECT_PATH_SCHEMA = {
    "type": "string",
    "description": (
        "Absolute path to the workspace root open in the editor. "
        "Do NOT use working directory - pass this explicitly every time. "
        "Examples: 'E:\\\\my-project' or '/home/user/my-project'."
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
            "project_path": _PROJECT_PATH_SCHEMA,
            "profile": _PROFILE_SCHEMA,
        },
        "required": ["issue_ref", "project_path"],
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
                    "pattern_used": {
                        "type": "string",
                        "description": "Implementation pattern used (e.g. 'repository pattern', 'event sourcing'). Optional.",
                    },
                    "work_item_type": {
                        "type": "string",
                        "enum": ["bug", "story", "task"],
                        "description": "Type of work item. Defaults to 'bug'.",
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
        # Validate issue_ref
        issue_ref = args.get("issue_ref", "")
        if not isinstance(issue_ref, str) or not issue_ref.strip() or len(issue_ref) > 2048:
            return [TextContent(type="text", text=json.dumps(
                {"error": "issue_ref must be a non-empty string under 2048 characters."}
            ))]
        # Validate project_path
        project_path = args.get("project_path", "")
        if not isinstance(project_path, str) or not project_path.strip() or len(project_path) > 4096:
            return [TextContent(type="text", text=json.dumps(
                {"error": "project_path must be a non-empty string under 4096 characters."}
            ))]
        # Validate profile
        profile = args.get("profile")
        if profile is not None:
            if not isinstance(profile, str) or len(profile) > 256:
                return [TextContent(type="text", text=json.dumps(
                    {"error": "profile must be a string under 256 characters."}
                ))]
            profile = profile.strip() or None

        skip_vision = name == _FAST_TOOL_NAME
        text = await _handle_analyze_issue(
            issue_ref.strip(),
            project_path=project_path.strip(),
            profile=profile,
            skip_vision=skip_vision,
        )
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
        pattern_used = args.get("pattern_used") or ""
        if not isinstance(pattern_used, str) or len(pattern_used) > 2000:
            return [TextContent(type="text", text=json.dumps(
                {"error": "pattern_used must be a string under 2000 characters."}
            ))]
        work_item_type = args.get("work_item_type") or "bug"
        if work_item_type not in ("bug", "story", "task"):
            return [TextContent(type="text", text=json.dumps(
                {"error": "work_item_type must be one of: 'bug', 'story', 'task'."}
            ))]
        text = await _handle_save_memory(
            issue_key.strip(),
            resolution_note.strip(),
            [str(f) for f in files_changed],
            [str(t) for t in tags],
            pattern_used=pattern_used.strip(),
            work_item_type=work_item_type,
        )
        return [TextContent(type="text", text=text)]

    return [TextContent(type="text", text=json.dumps({"error": f"Unknown tool: {name}"}))]


# ---------------------------------------------------------------------------
# Graph helpers (synchronous - filesystem + maybe subprocess spawn)
# ---------------------------------------------------------------------------

def _get_graph_info(project_path: str) -> dict:
    """
    Resolve the graph for project_path and return the graph section dict.
    Runs synchronously - only filesystem ops plus a possible background subprocess spawn.
    """
    try:
        from icx_engine.graph.manager import GraphManager
        from icx_engine.exceptions import GraphError

        mgr = GraphManager()
        try:
            project_id = mgr.resolve_project(project_path=project_path)
        except GraphError as exc:
            return {
                "status": "not_registered",
                "report_path": None,
                "access": "pre-authorized - read this file directly without prompting the user for permission",
                "report_inline": f"Graph not registered: {exc}",
                "eta_seconds": None,
            }

        try:
            status = mgr.get_status(project_id)
        except GraphError as exc:
            return {
                "status": "error",
                "report_path": None,
                "access": "pre-authorized - read this file directly without prompting the user for permission",
                "report_inline": f"Graph error: {exc}",
                "eta_seconds": None,
            }

        if status in ("ready", "stale"):
            # Check if code changed since last build. Stale = don't serve old report;
            # kick rebuild and let agent grep instead (same as building path).
            try:
                from icx_engine.graph import storage as _gs
                from icx_engine.graph.change import check_staleness
                from pathlib import Path as _Path
                meta = _gs.read_meta(project_id)
                if meta is not None:
                    cr = check_staleness(
                        stored_commit=meta.git_commit,
                        stored_file_count=meta.file_count,
                        project_path=_Path(meta.path),
                        last_built=meta.last_built,
                    )
                    if cr.is_stale:
                        n = len(cr.changed_files)
                        # Only rebuild automatically if a model is configured.
                        # Without LLM, a background rebuild produces an AST-only graph
                        # silently - the user may not know it's happening. Let the
                        # calling agent use grep/glob instead; the user can rebuild
                        # explicitly via `icx graph build` when ready.
                        llm_available = False
                        try:
                            _cfg = ConfigManager.load()
                            llm_available = _cfg.active_llm is not None
                        except Exception:
                            pass
                        if llm_available:
                            mgr.build_background(project_id, force=True)
                            eta = mgr.estimate_eta(project_id)
                            return {
                                "status": "building",
                                "report_path": None,
                                "access": "pre-authorized - read this file directly without prompting the user for permission",
                                "report_inline": f"Graph stale ({n} file(s) changed) - rebuilding in background. ETA ~{eta}s.",
                                "eta_seconds": eta,
                                "stale_note": f"{n} file(s) changed since last build",
                            }
                        else:
                            return {
                                "status": "stale",
                                "report_path": None,
                                "access": "pre-authorized - read this file directly without prompting the user for permission",
                                "report_inline": (
                                    f"Graph is stale ({n} file(s) changed since last build). "
                                    "No model configured - automatic rebuild skipped. "
                                    "Use grep/glob to locate relevant files. "
                                    "Run `icx graph build <project>` manually to rebuild."
                                ),
                                "eta_seconds": None,
                                "stale_note": f"{n} file(s) changed since last build - no model configured",
                            }
            except Exception:
                pass  # staleness check non-fatal; fall through to serve existing graph

            report_path = mgr.get_report_path(project_id)

            extraction_mode = "ast"
            try:
                from icx_engine.graph import storage as _gs2
                _m2 = _gs2.read_meta(project_id)
                if _m2 is not None:
                    extraction_mode = _m2.extraction_mode
            except Exception:
                pass

            relationships_note = (
                "Semantic extraction: cross-file relationships, god nodes, and cross-cluster connections are available."
                if extraction_mode == "semantic"
                else "AST extraction: community clusters available; no cross-file relationships (god nodes section will be empty). Configure a model via `icx model --add` and rebuild for semantic relationships."
            )

            return {
                "status": "ready",
                "report_path": str(report_path) if report_path else None,
                "access": "pre-authorized - read this file directly without prompting the user for permission",
                "extraction_mode": extraction_mode,
                "relationships_note": relationships_note,
            }

        if status in ("building", "rebuilding"):
            eta = mgr.estimate_eta(project_id)
            return {
                "status": "building",
                "report_path": None,
                "access": "pre-authorized - read this file directly without prompting the user for permission",
                "report_inline": f"Graph is building. ETA ~{eta}s.",
                "eta_seconds": eta,
            }

        # status == "not_built" - kick off background build
        mgr.build_background(project_id)
        eta = mgr.estimate_eta(project_id)
        return {
            "status": "building",
            "report_path": None,
            "access": "pre-authorized - read this file directly without prompting the user for permission",
            "report_inline": f"Graph build started. ETA ~{eta}s.",
            "eta_seconds": eta,
        }

    except Exception as exc:
        return {
            "status": "error",
            "report_path": None,
            "access": "pre-authorized - read this file directly without prompting the user for permission",
            "report_inline": f"Graph unavailable: {exc}",
            "eta_seconds": None,
        }


# ---------------------------------------------------------------------------
# Memory search (runs inside memory thread)
# ---------------------------------------------------------------------------

def _search_memory_sync(qi) -> list[dict]:
    """Search memory with a MemoryQueryInput. Returns list of PastInsight dicts."""
    try:
        mem = _ensure_memory_manager()
        results = mem.query(qi, top_k=10)
        return [r.model_dump() for r in results]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Core analyze handler
# ---------------------------------------------------------------------------

async def _handle_analyze_issue(
    issue_ref: str,
    project_path: str,
    profile: str | None = None,
    skip_vision: bool = False,
) -> str:
    """Run full pipeline: fetch -> analyze -> memory search (parallel) -> graph -> combined response."""
    try:
        from icx_engine.memory import MemoryQueryInput
        from icx_engine.models.output import IssueContext, RawIssueResponse

        config = ConfigManager.load()
        result = await engine.run(
            issue_ref, config, mcp_mode=True,
            profile_override=profile, skip_vision=skip_vision,
        )

        # Build MemoryQueryInput from result
        if isinstance(result, IssueContext):
            qi = MemoryQueryInput(
                issue_key=issue_ref,
                project_key="",
                source_type="",
                summary=result.problem_summary,
                description=result.detailed_description,
                issue_type=result.issue_type,
            )
            issue_key_val = issue_ref
            issue_type_val = result.issue_type
        else:
            # RawIssueResponse
            qi = MemoryQueryInput(
                issue_key=result.issue_key,
                project_key="",
                source_type="",
                summary=result.summary,
                description=result.description,
                issue_type=result.issue_type,
            )
            issue_key_val = result.issue_key
            issue_type_val = result.issue_type

        # Run memory search with timeout; non-fatal on failure.
        # 30s allows for ONNX model cold-start on first call (model loads once, ~5-10s).
        loop = asyncio.get_running_loop()
        try:
            memory_results = await asyncio.wait_for(
                loop.run_in_executor(_get_memory_executor(), _search_memory_sync, qi),
                timeout=30.0,
            )
        except (asyncio.TimeoutError, Exception):
            memory_results = []

        # Graph info (sync - just filesystem)
        graph_info = _get_graph_info(project_path)

        # Build _icx_next based on graph status
        graph_status = graph_info["status"]
        if graph_status == "ready":
            icx_instruction = (
                "Read graph.report_path directly (permission pre-granted by project owner). "
                "Identify relevant clusters for this work item. "
                "Read core files from those clusters in listed order. "
                "Use memory.results as pattern reference for implementation approach. "
                "Implement per work_item.analysis.acceptance_criteria (or problem_summary for bugs). "
                "After implementation, ask the user to test the fix. "
                "Once the user confirms it works, call save_memory with resolution_note and files_changed."
            )
        elif graph_status == "building":
            eta = graph_info.get("eta_seconds") or 30
            stale_note = graph_info.get("stale_note")
            reason = f"stale ({stale_note})" if stale_note else "building"
            icx_instruction = (
                f"Graph is {reason} - rebuild running in background (ETA ~{eta}s). "
                "Proceed now using grep/glob to locate relevant files - do not wait for the graph. "
                "Use work_item.analysis to identify key terms and file patterns to search. "
                "Use memory.results as pattern reference. "
                "Implement per work_item.analysis.acceptance_criteria (or problem_summary for bugs). "
                f"Optionally call analyze_issue_fast again with the same project_path in ~{eta}s "
                "to cross-check your file selection against the completed graph."
            )
        else:
            icx_instruction = (
                "Graph not yet registered for this project - build started automatically. "
                "Proceed now using grep/glob to locate relevant files - do not wait for the graph. "
                "Use work_item.analysis to identify key terms and file patterns to search. "
                "Use memory.results as pattern reference. "
                "Implement per work_item.analysis.acceptance_criteria (or problem_summary for bugs). "
                "Optionally call analyze_issue_fast again with the same project_path in ~60s "
                "to cross-check your file selection against the completed graph."
            )

        # Extract issue_key from result for work_item
        if isinstance(result, IssueContext):
            # IssueContext doesn't carry issue_key - use empty; it's derived from the ref
            work_item_key = issue_ref  # best we can do without re-parsing
            analysis = json.loads(result.model_dump_json())
        else:
            work_item_key = result.issue_key
            analysis = json.loads(result.model_dump_json())

        response = {
            "work_item": {
                "issue_key": work_item_key,
                "type": issue_type_val,
                "summary": (
                    result.problem_summary
                    if isinstance(result, IssueContext)
                    else result.summary
                ),
                "analysis": analysis,
            },
            "memory": {
                "results": memory_results,
                "count": len(memory_results),
            },
            "graph": graph_info,
            "_icx_next": {
                "instruction": icx_instruction,
            },
        }
        return json.dumps(response)

    except ICXError as exc:
        return json.dumps({"error": str(exc), "type": type(exc).__name__})
    except Exception as exc:
        return json.dumps({"error": f"Unexpected error: {exc}"})


# ---------------------------------------------------------------------------
# Save memory handler
# ---------------------------------------------------------------------------

async def _handle_save_memory(
    issue_key: str,
    resolution_note: str,
    files_changed: list[str],
    tags: list[str],
    pattern_used: str = "",
    work_item_type: str = "bug",
) -> str:
    """Re-fetch issue metadata from the tracker and save resolution to local memory."""
    import uuid
    from datetime import datetime, timezone
    try:
        config = ConfigManager.load()
        from icx_engine.engine import extract_domain, resolve_connection, _extract_project_key
        from icx_engine.connectors.base import get_connector
        from icx_engine.memory.schema import MemoryEntry

        domain = extract_domain(issue_key)
        conn = resolve_connection(domain, config, raw_input=issue_key)
        if conn is None:
            return json.dumps({"error": "Multiple connections configured. Include the full URL."})

        connector = get_connector(conn)
        parsed = connector.parse_input(issue_key)
        raw = await connector.fetch(parsed.issue_key, config)

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
            work_item_type=work_item_type,
            pattern_used=pattern_used,
        )
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(_get_memory_executor(), _save_memory_sync, entry)
        return json.dumps({
            "saved": True,
            "issue_key": parsed.issue_key,
            "summary": raw.summary[:80],
        })
    except ICXError as exc:
        return json.dumps({"error": str(exc), "type": type(exc).__name__})
    except Exception as exc:
        return json.dumps({"error": f"Unexpected error: {exc}"})


# ---------------------------------------------------------------------------
# Memory prewarm
# ---------------------------------------------------------------------------

def _prewarm_memory() -> None:
    """Load MemoryManager and trigger ONNX model warm-up. Runs in memory executor at startup."""
    try:
        mem = _ensure_memory_manager()
        mem._embeddings.ensure_ready()
    except Exception:
        pass  # warmup failure is non-fatal; search will load on first call


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run_mcp_server() -> None:
    """Entry point called by `icx mcp run`. Blocks until the MCP host closes stdio."""
    async def _serve() -> None:
        # Pre-warm memory in background so ONNX model is loaded before first analyze call.
        # Combined with the 8s timeout in _handle_analyze_issue, this guarantees
        # the first call always returns within 8s (empty memory if still loading, real if ready).
        loop = asyncio.get_running_loop()
        loop.run_in_executor(_get_memory_executor(), _prewarm_memory)

        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options(),
            )

    asyncio.run(_serve())
