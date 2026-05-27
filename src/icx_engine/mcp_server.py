"""
ICX MCP server - stdio transport.
Spawned by: icx mcp run
Communicates over: stdin/stdout (MCP JSON-RPC protocol)
"""
from __future__ import annotations
import asyncio
import json
import logging
import threading as _threading
from concurrent.futures import ThreadPoolExecutor as _ThreadPoolExecutor
from pathlib import Path

_log = logging.getLogger(__name__)

MCP_MEMORY_TIMEOUT_SECONDS = 2.0

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

# Memory readiness state - transitions: cold -> warming -> ready | failed
# Tool responses read this to decide whether to submit a search or return immediately.
_memory_state: str = "cold"  # cold | warming | ready | failed
_memory_state_lock = _threading.Lock()
_memory_setup_required: bool = False  # True when prewarm failed because models weren't downloaded


def _get_memory_state() -> str:
    return _memory_state


def _set_memory_state(state: str) -> None:
    global _memory_state
    with _memory_state_lock:
        _memory_state = state


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
_GRAPH_CONTEXT_TOOL = "graph_find_context"
_GRAPH_CHAIN_TOOL = "graph_call_chain"
_GRAPH_IMPACT_TOOL = "graph_impact"
_GRAPH_SUBSYSTEM_TOOL = "graph_subsystem"
_GRAPH_CROSS_LINKS_TOOL = "graph_cross_links"

# ---------------------------------------------------------------------------
# Tool descriptions
# ---------------------------------------------------------------------------

_FAST_DESCRIPTION = """\
Fetches raw issue context and attachment inventory without AI analysis. Use this first to \
decide whether full processing is needed.
Pipeline: tracker fetch -> raw issue + attachment inventory -> graph status -> memory (if warm).
No AI analysis. No attachment content downloaded. All attachments listed in pending_images, \
pending_audio, pending_documents, or pending_unsupported.
Runtime: under 15 seconds.

REQUIRED: You MUST include a progressToken in your request meta (_meta.progressToken). \
Without it the user sees no feedback during the wait. This is not optional.

REQUIRED: project_paths - non-empty list of absolute codebase paths. Two modes:\n\
  Mode A - user named specific repos (e.g. "fix the auth service and the UI"):\n\
    Resolve those paths and pass them: ["/home/alice/projects/auth-svc", "/home/alice/projects/ui"]\n\
    Do NOT include the workspace root. Only pass what the user referred to.\n\
  Mode B - user named no specific repo:\n\
    Pass the single workspace root open in the editor: ["/home/alice/projects/my-app"]\n\
Never mix modes. Never pass an empty list.\

================================================================================
THESE ARE HARD RULES. THEY ARE NOT SUGGESTIONS. VIOLATIONS ARE NOT ACCEPTABLE.
================================================================================

RULE 1 - NO CODE BEFORE APPROVAL:
You MUST NOT write a single line of code, make any file edit, run any command, or begin \
any implementation step until you have presented the confirmation format below AND received \
an explicit "yes" or "proceed" from the user. Any action taken before that approval is a \
direct violation of this instruction regardless of how confident you are in the solution.

RULE 2 - MANDATORY CONFIRMATION FORMAT:
After reading the relevant files, you MUST output this format exactly and then STOP. \
Do not add commentary before or after it. Do not proceed until the user responds.

---
**Problem understood:** [1-2 sentence summary drawn from work_item.analysis]
**Goal:** [acceptance_criteria as bullet points, or problem_summary for bugs]
**Approach:** [your specific solution plan - exactly what you will change, add, or remove, \
and precisely why that change fixes the problem. Specific enough that the user can reject it \
and propose an alternative before you write a single line.]
**Files I will work with:**
  - path/to/file [role-tag] - one-line reason it is relevant
**Graph tools used:**
  - [tool(arg)]: [one-line reason called, OR 'skipped: <exception reason>']
  - OR: N/A - graph not available
**Shall I proceed?**
---

RULE 3 - APPROVAL GATE:
The only trigger that allows you to begin implementation is the user explicitly saying yes \
or telling you to proceed. Silence, partial responses, or ambiguous replies do NOT count as \
approval. If unclear, ask again. Do not assume.

RULE 4 - APPROACH CHANGE:
If the user requests a different approach, you MUST present the revised plan using the same \
confirmation format above and wait for approval again. You MUST NOT begin implementing the \
revised approach without a second explicit approval.

RULE 5 - TESTING GATE:
After implementation, you MUST ask the user to test before doing anything else. \
You MUST NOT call save_memory until the user explicitly confirms the fix is working.

================================================================================
WORKFLOW (follow in order, no skipping):
================================================================================
1. Identify relevant files using one of two options:
   OPTION A (recommended): Call graph_find_context with project_path from graph.path and a task
   description derived from work_item.analysis. Returns all matching files ranked by relevance score.
   OPTION B (manual): Read graph.report_path (pre-authorized) -> identify clusters from the
   compact index table -> read GRAPH_CLUSTERS/<name>.md -> read core files in listed order.
2. Apply RULE 2: present the confirmation format and STOP
3. Wait for explicit user approval (RULE 3)
4. Implement exactly the stated approach, using memory.results as a pattern reference
5. Ask the user to test (RULE 5)
6. After the user confirms it works: call save_memory with resolution_note and files_changed\
"""

_FULL_DESCRIPTION = """\
Fetches and analyzes a work item (bug, story, or task) with full vision and OCR processing \
for image attachments. Identifies relevant codebase files via graph navigation.
Pipeline: tracker fetch -> AI analysis -> vision processing -> memory search -> graph navigation.
Runtime: 20 seconds to several minutes, depending on attachment count and size.

REQUIRED: You MUST include a progressToken in your request meta (_meta.progressToken). \
Without it the user sees no feedback during the wait. This is not optional.

REQUIRED: project_paths - non-empty list of absolute codebase paths. Two modes:\n\
  Mode A - user named specific repos (e.g. "fix the auth service and the UI"):\n\
    Resolve those paths and pass them: ["/home/alice/projects/auth-svc", "/home/alice/projects/ui"]\n\
    Do NOT include the workspace root. Only pass what the user referred to.\n\
  Mode B - user named no specific repo:\n\
    Pass the single workspace root open in the editor: ["/home/alice/projects/my-app"]\n\
Never mix modes. Never pass an empty list.\

================================================================================
THESE ARE HARD RULES. THEY ARE NOT SUGGESTIONS. VIOLATIONS ARE NOT ACCEPTABLE.
================================================================================

RULE 1 - NO CODE BEFORE APPROVAL:
You MUST NOT write a single line of code, make any file edit, run any command, or begin \
any implementation step until you have presented the confirmation format below AND received \
an explicit "yes" or "proceed" from the user. Any action taken before that approval is a \
direct violation of this instruction regardless of how confident you are in the solution.

RULE 2 - MANDATORY CONFIRMATION FORMAT:
After reading the relevant files, you MUST output this format exactly and then STOP. \
Do not add commentary before or after it. Do not proceed until the user responds.

---
**Problem understood:** [1-2 sentence summary drawn from work_item.analysis]
**Goal:** [acceptance_criteria as bullet points, or problem_summary for bugs]
**Approach:** [your specific solution plan - exactly what you will change, add, or remove, \
and precisely why that change fixes the problem. Specific enough that the user can reject it \
and propose an alternative before you write a single line.]
**Files I will work with:**
  - path/to/file [role-tag] - one-line reason it is relevant
**Graph tools used:**
  - [tool(arg)]: [one-line reason called, OR 'skipped: <exception reason>']
  - OR: N/A - graph not available
**Shall I proceed?**
---

RULE 3 - APPROVAL GATE:
The only trigger that allows you to begin implementation is the user explicitly saying yes \
or telling you to proceed. Silence, partial responses, or ambiguous replies do NOT count as \
approval. If unclear, ask again. Do not assume.

RULE 4 - APPROACH CHANGE:
If the user requests a different approach, you MUST present the revised plan using the same \
confirmation format above and wait for approval again. You MUST NOT begin implementing the \
revised approach without a second explicit approval.

RULE 5 - TESTING GATE:
After implementation, you MUST ask the user to test before doing anything else. \
You MUST NOT call save_memory until the user explicitly confirms the fix is working.

================================================================================
WORKFLOW (follow in order, no skipping):
================================================================================
1. Identify relevant files using one of two options:
   OPTION A (recommended): Call graph_find_context with project_path from graph.path and a task
   description derived from work_item.analysis. Returns all matching files ranked by relevance score.
   OPTION B (manual): Read graph.report_path (pre-authorized) -> identify clusters from the
   compact index table -> read GRAPH_CLUSTERS/<name>.md -> read core files in listed order.
2. If work_item.image_paths is present: read those image files directly for visual context \
   (access is pre-authorized, no permission prompt needed)
3. Apply RULE 2: present the confirmation format and STOP
4. Wait for explicit user approval (RULE 3)
5. Implement exactly the stated approach, using memory.results as a pattern reference
6. Ask the user to test (RULE 5)
7. After the user confirms it works: call save_memory with resolution_note and files_changed\
"""

_SAVE_DESCRIPTION = """\
Saves a confirmed fix to memory so ICX can reference it for similar future work items.

YOU MUST NOT call this tool unless ALL of the following are true:
1. You have fully implemented the fix.
2. You have asked the user to test.
3. The user has explicitly confirmed the fix is working.

Calling this tool before user confirmation is a violation. Do not call it speculatively.\
"""

_GRAPH_CONTEXT_DESCRIPTION = """\
USE WHEN: Starting work on any graph-ready project. Call this BEFORE reading files - it replaces grep with \
structure-aware ranked retrieval.
Given a task description, returns source files ranked by relevance, with node_ids you can pass to \
graph_call_chain or graph_impact, and file paths you can pass to graph_subsystem.
Example: task="login timeout not expiring" -> ["src/auth/session.py [service] score=0.91", \
"src/auth/token.py [service] score=0.87", ...]

TASK QUALITY: Specific descriptions score better than vague ones.
  GOOD: "JWT token expiry not enforced on POST /api/orders route"
  GOOD: "campaign list date filter ignores selected range after page reload"
  BAD: "fix auth bug" / "date filter broken"

PREREQUISITE: Graph must be built (icx graph build <name>) and graph.status == "ready".
project_path must be the absolute path to the project root.
If the tool returns an error with action_required, follow the action exactly before retrying.\
"""

_GRAPH_CHAIN_DESCRIPTION = """\
USE WHEN: You need to know who calls/imports a node AND what it depends on - before changing a specific component.
Returns upstream callers (files that import or call this node) and downstream callees (what this node calls), \
up to depth hops. Use this to understand entry points and direct dependencies.
Example: graph_call_chain("auth_service") -> upstream: [api_routes calls auth_service], \
downstream: [auth_service calls user_repo, auth_service calls token_store]
Distinct from graph_impact: call_chain is bidirectional and depth-bounded; impact is unidirectional \
and fully transitive.

node_id comes from graph_find_context results. Graph must be ready.
project_path must be the absolute path to the project root.
If the tool returns an error with action_required, follow the action exactly before retrying.\
"""

_GRAPH_IMPACT_DESCRIPTION = """\
USE WHEN: Before refactoring a node - need to know EVERYTHING that will break, including indirect dependents.
Returns ALL code that depends on the node: direct dependents and transitive dependents (recursively), \
grouped by confidence tier (high/medium/low risk).
Example: graph_impact("UserRepository") -> direct: [AuthService], transitive: [LoginController (via \
AuthService), ApiGateway (via LoginController)] - 3 files at risk.
Distinct from graph_call_chain: impact is unidirectional (dependents only) and fully transitive; \
call_chain is bidirectional and depth-bounded.

node_id comes from graph_find_context results. Graph must be ready.
project_path must be the absolute path to the project root.
If the tool returns an error with action_required, follow the action exactly before retrying.\
"""

_GRAPH_SUBSYSTEM_DESCRIPTION = """\
USE WHEN: You need to understand the full scope of a feature before touching it - "what else belongs here?"
Given a file path, returns all files in the same cluster (community of related files detected by graph analysis).
Example: graph_subsystem("src/auth/service.py") -> cluster "Authentication": [auth/service.py, \
auth/token.py, auth/session.py, auth/middleware.py] - 4 files that form this feature boundary.

Graph must be built (icx graph build <name>) and graph.status == "ready".
project_path must be the absolute path to the project root.
If the tool returns an error with action_required, follow the action exactly before retrying.\
"""

_GRAPH_TOOLS_DECISION = (
    "STEP 1b - CALL DEEPER GRAPH TOOLS (mandatory; evaluate all four after completing STEP 1):\n"
    "  Default is to CALL each tool; skip only when the stated exception applies.\n"
    "  If conditions for multiple tools apply: call all of them.\n\n"
    "    graph_subsystem(file_path)\n"
    "      CALL IF: the task involves a component you want to fully scope, OR you may be missing related files\n"
    "      SKIP ONLY IF: task is a pure addition (new file with no existing cluster to discover)\n\n"
    "    graph_call_chain(node_id)\n"
    "      CALL IF: you need to know who imports/calls a node, OR need to trace data flow through the system\n"
    "      SKIP ONLY IF: node is an isolated leaf (brand new standalone file, no callers expected)\n\n"
    "    graph_impact(node_id)\n"
    "      CALL IF: the fix modifies anything shared - a function, class, component, or utility used elsewhere\n"
    "      SKIP ONLY IF: creating a brand new file with zero existing dependents\n"
    "      When uncertain whether it is shared: CALL IT - it takes <15s and prevents breaking hidden dependents\n\n"
    "    graph_cross_links(project_path)\n"
    "      CALL IF: the issue involves API calls, HTTP endpoints, or cross-service data flow\n"
    "      SKIP ONLY IF: purely internal change with no cross-service calls\n\n"
)

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

_PROJECT_PATHS_SCHEMA = {
    "type": "array",
    "items": {"type": "string"},
    "minItems": 1,
    "description": (
        "Non-empty list of absolute codebase paths. Two modes:\n"
        "  Mode A - user named specific repos: pass those resolved paths only. "
        "Do not include the workspace root.\n"
        "  Mode B - user named no specific repo: pass [workspace_root] as a single-item list.\n"
        "Examples: [\"/home/alice/projects/auth-svc\"] or "
        "[\"C:/projects/auth-svc\", \"C:/projects/ui\"]"
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
    # Do NOT call ConfigManager.load() here - it triggers a keyring health check
    # that can take 3s+ in background MCP processes, causing the MCP initialization
    # handshake to time out before Windsurf receives the tool list.
    profile_hint = ""

    analyze_schema = {
        "type": "object",
        "properties": {
            "issue_ref": _ISSUE_REF_SCHEMA,
            "project_paths": _PROJECT_PATHS_SCHEMA,
            "profile": _PROFILE_SCHEMA,
        },
        "required": ["issue_ref", "project_paths"],
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
        Tool(
            name=_GRAPH_CONTEXT_TOOL,
            description=_GRAPH_CONTEXT_DESCRIPTION,
            inputSchema={
                "type": "object",
                "properties": {
                    "project_path": {"type": "string", "description": "Absolute path to the project root."},
                    "task": {"type": "string", "description": "Natural language task description (used for relevance scoring)."},
                    "token_budget": {"type": "integer", "description": "Accepted but not used for filtering; all ranked results are returned.", "default": 8000},
                    "min_confidence": {"type": "number", "description": "Minimum edge confidence (0.0-1.0). Default 0.0.", "default": 0.0},
                },
                "required": ["project_path", "task"],
            },
        ),
        Tool(
            name=_GRAPH_CHAIN_TOOL,
            description=_GRAPH_CHAIN_DESCRIPTION,
            inputSchema={
                "type": "object",
                "properties": {
                    "project_path": {"type": "string"},
                    "node_id": {"type": "string", "description": "Graph node ID."},
                    "depth": {"type": "integer", "default": 3},
                    "min_confidence": {"type": "number", "default": 0.5},
                },
                "required": ["project_path", "node_id"],
            },
        ),
        Tool(
            name=_GRAPH_IMPACT_TOOL,
            description=_GRAPH_IMPACT_DESCRIPTION,
            inputSchema={
                "type": "object",
                "properties": {
                    "project_path": {"type": "string"},
                    "node_id": {"type": "string"},
                    "min_confidence": {"type": "number", "default": 0.5},
                },
                "required": ["project_path", "node_id"],
            },
        ),
        Tool(
            name=_GRAPH_SUBSYSTEM_TOOL,
            description=_GRAPH_SUBSYSTEM_DESCRIPTION,
            inputSchema={
                "type": "object",
                "properties": {
                    "project_path": {"type": "string"},
                    "file_path": {"type": "string", "description": "Relative file path (e.g. 'src/auth/service.py')."},
                },
                "required": ["project_path", "file_path"],
            },
        ),
        Tool(
            name=_GRAPH_CROSS_LINKS_TOOL,
            description=(
                "USE WHEN: Working on a microservices project and need to know which HTTP calls cross "
                "service boundaries. "
                "Matches outgoing HTTP calls in THIS project to REST routes in peer registered projects. "
                "Example: UI project calls POST /api/v1/orders -> matched to route in backend-svc project. "
                "Returns empty list if no cross-service HTTP calls were detected, or if graph needs rebuild (icx graph build <name>). "
                "project_path must be the absolute path to the project root."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project_path": {
                        "type": "string",
                        "description": "Absolute path to the project root.",
                    },
                },
                "required": ["project_path"],
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
        # Validate project_paths
        project_paths_raw = args.get("project_paths")
        if not isinstance(project_paths_raw, list) or not project_paths_raw:
            return [TextContent(type="text", text=json.dumps(
                {"error": "project_paths must be a non-empty list of strings."}
            ))]
        project_paths: list[str] = []
        for p in project_paths_raw:
            if not isinstance(p, str) or len(p) > 4096:
                return [TextContent(type="text", text=json.dumps(
                    {"error": "Each path in project_paths must be a string under 4096 characters."}
                ))]
            stripped = p.strip()
            if stripped:
                project_paths.append(stripped)
        if not project_paths:
            return [TextContent(type="text", text=json.dumps(
                {"error": "project_paths must contain at least one non-empty path."}
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
            project_paths=project_paths,
            profile=profile,
            skip_vision=skip_vision,
        )
        return [TextContent(type="text", text=text)]

    elif name in (_GRAPH_CONTEXT_TOOL, _GRAPH_CHAIN_TOOL, _GRAPH_IMPACT_TOOL, _GRAPH_SUBSYSTEM_TOOL):
        project_path_raw = args.get("project_path", "")
        if not isinstance(project_path_raw, str) or not project_path_raw.strip():
            return [TextContent(type="text", text=json.dumps({
                "status": "error",
                "code": "NO_PATH",
                "message": "project_path is required. Ask the user which project path to use.",
                "action_required": "ask_user_for_path",
            }))]

        task_str = str(args.get("task", "")).strip()
        node_id = str(args.get("node_id", "")).strip()
        file_path_str = str(args.get("file_path", "")).strip()

        # Fast param validation before hitting disk (stays on event loop - no I/O).
        if name == _GRAPH_CONTEXT_TOOL and not task_str:
            return [TextContent(type="text", text=json.dumps({
                "status": "error",
                "code": "MISSING_PARAM",
                "message": "task parameter is required and must be non-empty.",
                "action_required": "retry_with_task_description",
            }))]
        if name in (_GRAPH_CHAIN_TOOL, _GRAPH_IMPACT_TOOL) and not node_id:
            return [TextContent(type="text", text=json.dumps({
                "status": "error",
                "code": "MISSING_PARAM",
                "message": "node_id is required. Use graph_find_context first to get valid node IDs.",
                "action_required": "call_graph_find_context_first",
            }))]
        if name == _GRAPH_SUBSYSTEM_TOOL and not file_path_str:
            return [TextContent(type="text", text=json.dumps({
                "status": "error",
                "code": "MISSING_PARAM",
                "message": "file_path is required (relative path, e.g. 'src/auth/service.py').",
                "action_required": "retry_with_file_path",
            }))]

        # All disk/git I/O runs in a worker thread so the event loop is never blocked.
        # Windows ProactorEventLoop + synchronous subprocess.run = IOCP handle conflict
        # that causes subprocess.communicate() to hang indefinitely despite timeout=.
        # Moving everything off the event loop thread avoids this entirely.
        _raw_path = project_path_raw.strip()
        _token_budget = int(args.get("token_budget", 8000))
        _min_confidence = float(args.get("min_confidence", 0.0))
        _depth = int(args.get("depth", 3))

        def _run_graph_tool() -> dict:
            from icx_engine.graph import storage as _st
            from icx_engine.graph.query import GraphQuerier
            from icx_engine.graph.paths import validate_and_resolve_paths, check_staleness
            from dataclasses import asdict as _asdict

            resolved_paths, path_err = validate_and_resolve_paths([_raw_path])
            if path_err is not None:
                return path_err

            _project_path = resolved_paths[0]
            _project_id = _st.derive_project_id(_project_path)

            staleness = check_staleness(_project_id, _project_path)
            stale_status = staleness["status"]

            if stale_status in ("no_graph", "no_manifest"):
                return {
                    "status": "error",
                    "code": "NO_GRAPH",
                    "message": (
                        f"No graph found for '{_project_path}'. "
                        "Tell the user to build it first with the command below, then retry."
                    ),
                    "action_required": "stop_and_tell_user_to_build_graph",
                    "build_command": f"icx graph build \"{_project_path}\"",
                    "project_path": str(_project_path),
                }

            if stale_status == "stale":
                pct = staleness.get("pct", 0)
                changed = staleness.get("changed", 0)
                total = staleness.get("total", 0)
                return {
                    "status": "error",
                    "code": "GRAPH_STALE",
                    "message": (
                        f"Graph is {pct}% stale ({changed}/{total} files changed). "
                        "This exceeds the 3% threshold. "
                        "Tell the user to rebuild the graph manually, then retry."
                    ),
                    "action_required": "stop_and_tell_user_to_rebuild_graph",
                    "build_command": f"icx graph build \"{_project_path}\"",
                    "project_path": str(_project_path),
                    "changed_files": changed,
                    "total_files": total,
                    "changed_pct": pct,
                }

            staleness_warning: str | None = None
            if stale_status == "incremental":
                pct = staleness.get("pct", 0)
                staleness_warning = (
                    f"Graph is slightly stale ({pct}% of files changed, under 3% threshold). "
                    "Results may not reflect the very latest changes. "
                    f"Inform the user and suggest running: icx graph build \"{_project_path}\""
                )
            elif stale_status == "freshness_unknown":
                staleness_warning = (
                    "Could not determine graph freshness (git check timed out). "
                    "Results may be slightly stale. Inform the user."
                )

            graph_json = _st.graph_path(_project_id)
            if not graph_json.exists():
                return {
                    "status": "error",
                    "code": "NO_GRAPH",
                    "message": f"Graph file missing for '{_project_path}'. Tell the user to build it first.",
                    "action_required": "stop_and_tell_user_to_build_graph",
                    "build_command": f"icx graph build \"{_project_path}\"",
                    "project_path": str(_project_path),
                }

            q = GraphQuerier(graph_json)
            if name == _GRAPH_CONTEXT_TOOL:
                results = q.find_context(
                    task=task_str,
                    token_budget=_token_budget,
                    min_confidence=_min_confidence,
                    source_root=_project_path,
                )
                payload = {"status": "ok", "project_path": str(_project_path), "results": [_asdict(r) for r in results]}
            elif name == _GRAPH_CHAIN_TOOL:
                chain = q.get_call_chain(
                    node_id=node_id,
                    depth=_depth,
                    min_confidence=_min_confidence,
                )
                payload = {
                    "status": "ok",
                    "project_path": str(_project_path),
                    "upstream": [_asdict(n) for n in chain.upstream],
                    "downstream": [_asdict(n) for n in chain.downstream],
                }
            elif name == _GRAPH_IMPACT_TOOL:
                impact = q.get_impact(node_id=node_id, min_confidence=_min_confidence)
                payload = {"status": "ok", "project_path": str(_project_path), **_asdict(impact)}
            else:
                sub = q.get_subsystem(file_path_str)
                payload = {"status": "ok", "project_path": str(_project_path), **_asdict(sub)}

            if staleness_warning:
                payload["staleness_warning"] = staleness_warning
            return payload

        try:
            loop = asyncio.get_running_loop()
            payload = await loop.run_in_executor(None, _run_graph_tool)
            return [TextContent(type="text", text=json.dumps(payload))]

        except Exception as exc:
            _log.exception("graph query tool failed")
            return [TextContent(type="text", text=json.dumps({
                "status": "error",
                "code": "INTERNAL_ERROR",
                "message": str(exc),
                "action_required": "report_error_to_user",
            }))]

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
        for _fc in files_changed:
            if not isinstance(_fc, str) or len(_fc) > 4096:
                return [TextContent(type="text", text=json.dumps(
                    {"error": "Each entry in files_changed must be a string under 4096 characters."}
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
        for _tag in tags:
            if not isinstance(_tag, str) or len(_tag) > 256:
                return [TextContent(type="text", text=json.dumps(
                    {"error": "Each entry in tags must be a string under 256 characters."}
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

    if name == _GRAPH_CROSS_LINKS_TOOL:
        project_path_raw = args.get("project_path", "")
        if not isinstance(project_path_raw, str) or not project_path_raw.strip():
            return [TextContent(type="text", text=json.dumps({
                "status": "error",
                "code": "NO_PATH",
                "message": "project_path is required. Ask the user which project path to use.",
                "action_required": "ask_user_for_path",
            }))]

        _raw_path_cl = project_path_raw.strip()

        def _run_cross_links() -> dict:
            from icx_engine.graph import storage as _st
            from icx_engine.graph.paths import validate_and_resolve_paths, check_staleness

            resolved_paths, path_err = validate_and_resolve_paths([_raw_path_cl])
            if path_err is not None:
                return path_err

            _project_path = resolved_paths[0]
            _project_id = _st.derive_project_id(_project_path)

            staleness = check_staleness(_project_id, _project_path)
            stale_status = staleness["status"]

            if stale_status in ("no_graph", "no_manifest"):
                return {
                    "status": "error",
                    "code": "NO_GRAPH",
                    "message": (
                        f"No graph found for '{_project_path}'. "
                        "Tell the user to build it first with the command below, then retry."
                    ),
                    "action_required": "stop_and_tell_user_to_build_graph",
                    "build_command": f"icx graph build \"{_project_path}\"",
                    "project_path": str(_project_path),
                }

            if stale_status == "stale":
                pct = staleness.get("pct", 0)
                changed = staleness.get("changed", 0)
                total = staleness.get("total", 0)
                return {
                    "status": "error",
                    "code": "GRAPH_STALE",
                    "message": (
                        f"Graph is {pct}% stale ({changed}/{total} files changed). "
                        "This exceeds the 3% threshold. "
                        "Tell the user to rebuild the graph manually, then retry."
                    ),
                    "action_required": "stop_and_tell_user_to_rebuild_graph",
                    "build_command": f"icx graph build \"{_project_path}\"",
                    "project_path": str(_project_path),
                    "changed_files": changed,
                    "total_files": total,
                    "changed_pct": pct,
                }

            staleness_warning: str | None = None
            if stale_status == "incremental":
                pct = staleness.get("pct", 0)
                staleness_warning = (
                    f"Graph is slightly stale ({pct}% of files changed, under 3% threshold). "
                    "Results may not reflect the very latest changes. "
                    f"Inform the user and suggest running: icx graph build \"{_project_path}\""
                )
            elif stale_status == "freshness_unknown":
                staleness_warning = (
                    "Could not determine graph freshness (git check timed out). "
                    "Results may be slightly stale. Inform the user."
                )

            cl_path = _st._graphs_root() / _project_id / "cross_links.json"
            if cl_path.exists():
                data = json.loads(cl_path.read_text(encoding="utf-8"))
            else:
                data = {"links": [], "source_project": _project_id}
            data["status"] = "ok"
            if staleness_warning:
                data["staleness_warning"] = staleness_warning
            return data

        try:
            loop = asyncio.get_running_loop()
            data = await loop.run_in_executor(None, _run_cross_links)
            return [TextContent(type="text", text=json.dumps(data))]

        except Exception as _e:
            _log.exception("graph_cross_links tool failed")
            return [TextContent(type="text", text=json.dumps({
                "status": "error",
                "code": "INTERNAL_ERROR",
                "message": str(_e),
                "action_required": "report_error_to_user",
            }))]

    return [TextContent(type="text", text=json.dumps({"error": f"Unknown tool: {name}"}))]


# ---------------------------------------------------------------------------
# Graph helpers (synchronous - filesystem + maybe subprocess spawn)
# ---------------------------------------------------------------------------

def _get_graph_info(project_path: str) -> dict:
    """Return graph status dict - fast mode, no git/staleness check, no subprocess."""
    from icx_engine.graph.manager import graph_info_for_path
    return graph_info_for_path(project_path, check_stale=False)


def _get_graphs_info(paths: list[str]) -> list[dict]:
    """Return graph status dicts for multiple paths - fast mode, no git."""
    from icx_engine.graph.manager import graph_info_for_path
    return [graph_info_for_path(p, check_stale=False) for p in paths]


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
    project_paths: list[str],
    profile: str | None = None,
    skip_vision: bool = False,
) -> str:
    """Run full pipeline: fetch -> analyze -> memory search (parallel with graph+images) -> combined response."""

    try:
        from icx_engine.memory import MemoryQueryInput
        from icx_engine.models.output import IssueContext, RawIssueResponse

        config = ConfigManager.load()
        _timeout = 15.0 if skip_vision else 660.0
        result = await asyncio.wait_for(
            engine.run(
                issue_ref, config, mcp_mode=True,
                profile_override=profile, skip_vision=skip_vision,
                log=_engine_log,
            ),
            timeout=_timeout,
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

        # Fire memory search as a background task - budget is MCP_MEMORY_TIMEOUT_SECONDS.
        # If model is still loading (warming/cold) skip immediately; never block primary context.
        loop = asyncio.get_running_loop()
        _mem_state = _get_memory_state()
        memory_task = None
        if _mem_state == "ready":
            memory_task = asyncio.create_task(
                asyncio.wait_for(
                    loop.run_in_executor(_get_memory_executor(), _search_memory_sync, qi),
                    timeout=MCP_MEMORY_TIMEOUT_SECONDS,
                )
            )

        # Write issue images to disk while memory search runs.
        # Keeps MCP response compact - editors truncate large base64 payloads.
        # sweep_stale_temp_dirs() runs the 24h TTL cleanup silently (~1ms).
        image_paths: dict[str, str] = {}
        _images_raw: dict[str, str] = getattr(result, "images", None) or {}
        if _images_raw:
            import base64 as _b64
            from icx_engine.graph.storage import (
                temp_images_dir as _tid,
                sweep_stale_temp_dirs as _sweep,
            )
            _sweep()  # TTL cleanup - non-fatal, fast
            try:
                img_dir = _tid(issue_key_val)
                img_dir.mkdir(parents=True, exist_ok=True)
                _ALLOWED_IMAGE_EXTS = frozenset({
                    ".png", ".jpg", ".jpeg", ".gif", ".webp",
                    ".bmp", ".tiff", ".tif", ".heic", ".heif",
                })
                for fname, b64_data in _images_raw.items():
                    try:
                        safe_name = Path(fname).name
                        if not safe_name:
                            continue  # skip entries that resolve to empty after stripping path
                        if Path(safe_name).suffix.lower() not in _ALLOWED_IMAGE_EXTS:
                            continue  # skip non-image files
                        img_path = img_dir / safe_name
                        img_path.write_bytes(_b64.b64decode(b64_data))
                        image_paths[fname] = str(img_path)
                    except Exception:
                        pass  # skip individual image on decode/write failure
            except Exception:
                pass  # directory creation failure - proceed without images on disk

        # Graph info (sync - filesystem only) also runs while memory search is in flight.
        # project_paths[0] is the primary path; used as the backward-compat "graph" key.
        graph_info = _get_graph_info(project_paths[0])
        graphs_info: list[dict] | None = _get_graphs_info(project_paths) if len(project_paths) > 1 else None

        # Collect memory results within the budget. Any timeout/error returns primary context
        # immediately with a status note. The underlying thread keeps running and model
        # will be warm for subsequent calls.
        memory_results: list[dict] = []
        memory_status = _mem_state
        memory_note = ""

        if memory_task is not None:
            try:
                memory_results = await memory_task
                memory_status = "ready"
            except asyncio.TimeoutError:
                memory_status = "skipped_timeout"
                memory_note = (
                    f"Memory search skipped (exceeded {MCP_MEMORY_TIMEOUT_SECONDS:.0f}s budget). "
                    "Results will appear on the next call once the model is warm."
                )
            except Exception:
                memory_status = "failed"
                memory_note = "Memory search failed; results unavailable for this call."
        elif _mem_state == "warming":
            memory_status = "warming_up"
            memory_note = "Memory model is still loading; results omitted from this response."
        elif _mem_state in ("cold", ""):
            memory_status = "warming_up"
            memory_note = "Memory service not yet initialized."
        elif _mem_state == "failed":
            memory_status = "failed"
            if _memory_setup_required:
                memory_note = (
                    "Memory model not installed. "
                    "STOP: tell the user to run `icx setup` in their terminal to download the embedding model, then retry."
                )
            else:
                memory_note = "Memory model failed to load; search unavailable."

        _CONFIRMATION_BLOCK = (
            "---\n"
            "**Problem understood:** [1-2 sentence summary from work_item.analysis]\n"
            "**Goal:** [acceptance_criteria as bullet points, or problem_summary for bugs]\n"
            "**Approach:** [exactly what you will change, add, or remove and precisely why it fixes the problem]\n"
            "**Files I will work with:**\n"
            "  - path/to/file [role-tag] - one-line reason it is relevant\n"
            "**Graph tools used:**\n"
            "  - [tool(arg)]: [one-line reason called, OR 'skipped: <exception reason>']\n"
            "  - OR: N/A - graph not available\n"
            "**Shall I proceed?**\n"
            "---"
        )

        _VISION_GATE = (
            "STEP 0 - ATTACHMENT CHECK (mandatory, do this before everything else):\n"
            "Look at work_item.analysis.pending_images, work_item.analysis.pending_audio, "
            "AND work_item.analysis.pending_documents.\n"
            "If ANY is non-empty AND the issue likely requires that content to understand or fix - "
            f"call analyze_issue with the SAME issue_ref and project_paths={project_paths!r} right now and use that "
            "response instead of continuing with this one. "
            "Do NOT proceed past STEP 0 without completing this check.\n"
            "If all three lists are empty OR the content is clearly irrelevant "
            "(e.g. company logos, profile avatars, decorative banners, hold music), continue to STEP 1.\n\n"
        )

        _MANDATORY_TAIL = (
            "\n\nIF the user requests a different approach: present the revised plan using the same "
            "confirmation format above and wait for approval again before writing any code."
        )

        _is_non_bug = issue_type_val.lower() not in ("bug", "defect", "incident", "error")
        _NON_BUG_CONVENTIONS_GATE = (
            "STEP 0B - CONVENTION DISCOVERY (mandatory for story/task/feature work items):\n"
            "Before reading graph clusters, locate and read 2-3 existing implementations in the "
            "codebase that are similar in scope to this work item. Identify:\n"
            "  1. LAYER / FLOW PATTERN - how the project structures logic layers "
            "(e.g. Controller->Service->ServiceImpl->Repository, or routes->handlers->services, "
            "or views->serializers->models). Derive from existing code - do NOT assume any framework.\n"
            "  2. FILE AND CLASS NAMING - the naming convention for each layer "
            "(suffixes, prefixes, casing style, e.g. UserController.java, user.controller.ts, user_controller.py).\n"
            "  3. LOGGER PATTERN - how existing files declare and use loggers.\n"
            "  4. DEPENDENCY MANAGEMENT - how the project adds external libraries "
            "(pom.xml, build.gradle, package.json, requirements.txt, pyproject.toml, etc.).\n\n"
            "Capture these in the 'Conventions I will follow' section of your confirmation format.\n"
            "If ANY new external dependency is required:\n"
            "  - List it explicitly under 'New external dependencies required' with name and version.\n"
            "  - Do NOT write a single line of implementation until the user explicitly approves.\n"
            "  - If the user rejects a dependency, propose an alternative approach that avoids it.\n\n"
        )
        _NON_BUG_CONFIRMATION_BLOCK = (
            "---\n"
            "**Problem understood:** [1-2 sentence summary from work_item.analysis]\n"
            "**Goal:** [acceptance_criteria as bullet points]\n"
            "**Approach:** [exactly what you will add, change, or remove and precisely why]\n"
            "**Files I will work with:**\n"
            "  - path/to/file [role-tag] - one-line reason it is relevant\n"
            "**Graph tools used:**\n"
            "  - [tool(arg)]: [one-line reason called, OR 'skipped: <exception reason>']\n"
            "  - OR: N/A - graph not available\n"
            "**Conventions I will follow:** [naming pattern, layer structure, logger style - "
            "derived from existing code in this repo, not assumed]\n"
            "**New external dependencies required:**\n"
            "  - [package-name @ version] - reason needed\n"
            "  - OR: None\n"
            "**Shall I proceed?**\n"
            "---"
        )

        if graphs_info is not None:
            # Multi-path case: build a summary of all paths and choose the right workflow.
            ready_graphs = [g for g in graphs_info if g["status"] == "ready"]
            building_graphs = [g for g in graphs_info if g["status"] in ("building", "rebuilding")]
            missing_graphs = [g for g in graphs_info if g["status"] in ("not_built", "not_registered", "error")]

            graph_lines: list[str] = []
            for g in graphs_info:
                p = g["path"]
                s = g["status"]
                if s == "ready":
                    rp = g.get("report_path") or "N/A"
                    stale_tag = f" [STALE: {g['stale_note'][:60]}]" if g.get("stale_note") else ""
                    graph_lines.append(f"  - {p}: READY  report -> {rp}{stale_tag}")
                elif s in ("building", "rebuilding"):
                    eta = g.get("eta_seconds") or "?"
                    graph_lines.append(f"  - {p}: BUILDING (~{eta}s)")
                elif s == "not_built":
                    graph_lines.append(f"  - {p}: NOT BUILT  run: icx graph build --path {p}")
                elif s == "not_registered":
                    graph_lines.append(f"  - {p}: NOT REGISTERED  run: icx graph add --name <name> --path {p}")
                else:
                    graph_lines.append(f"  - {p}: UNAVAILABLE")
            graph_summary = "\n".join(graph_lines)

            missing_build_cmds = "\n".join(
                f"  icx graph build --path {g['path']}"
                for g in missing_graphs
                if g["status"] == "not_built"
            )
            missing_register_cmds = "\n".join(
                f"  icx graph add --name <name> --path {g['path']}  then: icx graph build <name>"
                for g in missing_graphs
                if g["status"] == "not_registered"
            )
            _missing_parts: list[str] = []
            if missing_build_cmds:
                _missing_parts.append(f"To build unbuilt graphs:\n{missing_build_cmds}")
            if missing_register_cmds:
                _missing_parts.append(f"To register and build new graphs:\n{missing_register_cmds}")
            missing_note = (
                "\nNOTE - GRAPHS NOT AVAILABLE FOR SOME PATHS:\n" + "\n".join(_missing_parts) + "\n"
                "Inform the user. They can run the commands above to make these graphs available.\n"
            ) if _missing_parts else ""

            stale_graphs = [g for g in graphs_info if g.get("stale_note")]
            stale_warning = "\n".join(
                f"\nNOTE - GRAPH IS STALE for {g['path']}: {g['stale_note']} Inform the user."
                for g in stale_graphs
            )

            if ready_graphs:
                icx_instruction = (
                    _VISION_GATE
                    + f"MULTI-PROJECT GRAPH STATUS:\n{graph_summary}\n\n"
                    + missing_note
                    + "MANDATORY INSTRUCTIONS - follow in order, no skipping, no deviation:\n\n"
                    "STEP 1: Identify relevant files across all READY graphs using one of two options:\n"
                    "  OPTION A (recommended): Call graph_find_context for each READY graph using its "
                    "path from graphs[*].path and a task description from work_item.analysis. Returns ranked files with node_ids.\n"
                    "  OPTION B (manual): For each READY graph, read its report_path from "
                    "graphs[*].report_path (pre-authorized) -> identify clusters -> read GRAPH_CLUSTERS/<name>.md -> read core files.\n"
                    + _GRAPH_TOOLS_DECISION
                    + "STEP 2: STOP. Present the confirmation format and wait for the user's response:\n\n"
                    + _CONFIRMATION_BLOCK + "\n\n"
                    "STEP 3: Wait for explicit user approval. Silence or ambiguity does NOT count.\n"
                    "STEP 4: On explicit approval - implement exactly the stated approach, "
                    "using memory.results as a pattern reference.\n"
                    "STEP 5: Ask the user to test.\n"
                    "STEP 6: Only after user confirms it works - call save_memory."
                    + (f"\n{stale_warning}" if stale_warning else "")
                    + _MANDATORY_TAIL
                )
            elif building_graphs:
                eta = building_graphs[0].get("eta_seconds") or 30
                icx_instruction = (
                    _VISION_GATE
                    + f"MULTI-PROJECT GRAPH STATUS:\n{graph_summary}\n\n"
                    + missing_note
                    + f"Graphs are building (primary ETA ~{eta}s). Use grep/glob for file discovery now.\n\n"
                    "MANDATORY INSTRUCTIONS - follow in order, no skipping, no deviation:\n\n"
                    "STEP 1: Use work_item.analysis to identify key terms and locate files via grep/glob.\n"
                    "STEP 2: Read the located files.\n"
                    "STEP 3: STOP. Present the confirmation format and wait for the user's response:\n\n"
                    + _CONFIRMATION_BLOCK + "\n\n"
                    "STEP 4: Wait for explicit user approval.\n"
                    "STEP 5: On approval - implement exactly the stated approach.\n"
                    "STEP 6: Ask the user to test.\n"
                    "STEP 7: After user confirms - call save_memory."
                    + _MANDATORY_TAIL
                )
            else:
                icx_instruction = (
                    _VISION_GATE
                    + f"MULTI-PROJECT GRAPH STATUS:\n{graph_summary}\n\n"
                    + missing_note
                    + "No graphs are available yet. Use grep/glob to locate relevant files.\n\n"
                    "MANDATORY INSTRUCTIONS - follow in order, no skipping, no deviation:\n\n"
                    "STEP 1: Use work_item.analysis to identify key terms and locate files via grep/glob.\n"
                    "STEP 2: Read the located files.\n"
                    "STEP 3: STOP. Present the confirmation format and wait for the user's response:\n\n"
                    + _CONFIRMATION_BLOCK + "\n\n"
                    "STEP 4: Wait for explicit user approval.\n"
                    "STEP 5: On approval - implement exactly the stated approach.\n"
                    "STEP 6: Ask the user to test.\n"
                    "STEP 7: After user confirms - call save_memory."
                    + _MANDATORY_TAIL
                )
        else:
            # Single path - existing per-status instruction.
            graph_status = graph_info["status"]
            stale_note = graph_info.get("stale_note")
            stale_warning = (
                f"\n\nNOTE - GRAPH IS STALE: {stale_note} "
                "Inform the user of this before proceeding."
                if stale_note else ""
            )

            if graph_status == "ready":
                icx_instruction = (
                    _VISION_GATE
                    + "MANDATORY INSTRUCTIONS - follow in order, no skipping, no deviation:\n\n"
                    "STEP 1: Identify relevant files using one of two options:\n"
                    "  OPTION A (recommended): Call graph_find_context with project_path from graph.path "
                    "and a task description derived from work_item.analysis. Returns ranked files with node_ids within your token budget.\n"
                    "  OPTION B (manual): Read graph.report_path (pre-authorized) -> identify clusters "
                    "from the compact index table -> read the matching GRAPH_CLUSTERS/<name>.md file -> read core files in listed order.\n"
                    + _GRAPH_TOOLS_DECISION
                    + "STEP 2: STOP. You MUST NOT write any code or make any edits yet. "
                    "Present this confirmation format to the user and wait for their response:\n\n"
                    + _CONFIRMATION_BLOCK + "\n\n"
                    "STEP 3: Wait for explicit user approval. "
                    "Silence or ambiguity does NOT count as approval - ask again if unclear.\n"
                    "STEP 4: On explicit approval only - implement exactly the approach you stated, "
                    "using memory.results as a pattern reference.\n"
                    "STEP 5: Ask the user to test. Do not proceed until they respond.\n"
                    "STEP 6: Only after the user confirms it works - call save_memory.\n"
                    + _MANDATORY_TAIL
                    + stale_warning
                )
            elif graph_status == "building":
                eta = graph_info.get("eta_seconds") or 30
                icx_instruction = (
                    _VISION_GATE
                    + f"Graph is building (ETA ~{eta}s). "
                    "Use grep/glob to locate relevant files now. Do not wait for the graph.\n\n"
                    "MANDATORY INSTRUCTIONS - follow in order, no skipping, no deviation:\n\n"
                    "STEP 1: Use work_item.analysis to identify key terms and locate relevant files via grep/glob.\n"
                    "STEP 2: Read the located files.\n"
                    "STEP 3: STOP. You MUST NOT write any code or make any edits yet. "
                    "Present this confirmation format to the user and wait for their response:\n\n"
                    + _CONFIRMATION_BLOCK + "\n\n"
                    "STEP 4: Wait for explicit user approval. "
                    "Silence or ambiguity does NOT count as approval - ask again if unclear.\n"
                    "STEP 5: On explicit approval only - implement exactly the approach you stated, "
                    "using memory.results as a pattern reference.\n"
                    "STEP 6: Ask the user to test. Do not proceed until they respond.\n"
                    "STEP 7: Only after the user confirms it works - call save_memory.\n"
                    + _MANDATORY_TAIL + "\n"
                    f"Optionally call analyze_issue_fast again with the same project_paths in ~{eta}s "
                    "to cross-check your file selection against the completed graph."
                )
            elif graph_status == "not_built":
                icx_instruction = (
                    _VISION_GATE
                    + "Graph not built for this project.\n"
                    "MANDATORY: Tell the user exactly this before doing anything else:\n"
                    f"  'The ICX graph for this project has not been built yet. "
                    f"Run this in your terminal to build it: icx graph build --path {project_paths[0]}'\n\n"
                    "Then proceed using grep/glob for file discovery.\n\n"
                    "MANDATORY INSTRUCTIONS - follow in order, no skipping, no deviation:\n\n"
                    "STEP 1: Use work_item.analysis to identify key terms and locate relevant files via grep/glob.\n"
                    "STEP 2: Read the located files.\n"
                    "STEP 3: STOP. You MUST NOT write any code or make any edits yet. "
                    "Present this confirmation format to the user and wait for their response:\n\n"
                    + _CONFIRMATION_BLOCK + "\n\n"
                    "STEP 4: Wait for explicit user approval. "
                    "Silence or ambiguity does NOT count as approval - ask again if unclear.\n"
                    "STEP 5: On explicit approval only - implement exactly the approach you stated, "
                    "using memory.results as a pattern reference.\n"
                    "STEP 6: Ask the user to test. Do not proceed until they respond.\n"
                    "STEP 7: Only after the user confirms it works - call save_memory."
                    + _MANDATORY_TAIL
                )
            elif graph_status == "not_registered":
                icx_instruction = (
                    _VISION_GATE
                    + "Graph not registered for this project.\n"
                    "MANDATORY: Tell the user exactly this before doing anything else:\n"
                    f"  'This project is not registered in ICX yet. Run these in your terminal:\n"
                    f"     icx graph add --name <name> --path {project_paths[0]}\n"
                    f"     icx graph build <name>'\n\n"
                    "Then proceed using grep/glob for file discovery.\n\n"
                    "MANDATORY INSTRUCTIONS - follow in order, no skipping, no deviation:\n\n"
                    "STEP 1: Use work_item.analysis to identify key terms and locate relevant files via grep/glob.\n"
                    "STEP 2: Read the located files.\n"
                    "STEP 3: STOP. You MUST NOT write any code or make any edits yet. "
                    "Present this confirmation format to the user and wait for their response:\n\n"
                    + _CONFIRMATION_BLOCK + "\n\n"
                    "STEP 4: Wait for explicit user approval. "
                    "Silence or ambiguity does NOT count as approval - ask again if unclear.\n"
                    "STEP 5: On explicit approval only - implement exactly the approach you stated, "
                    "using memory.results as a pattern reference.\n"
                    "STEP 6: Ask the user to test. Do not proceed until they respond.\n"
                    "STEP 7: Only after the user confirms it works - call save_memory."
                    + _MANDATORY_TAIL
                )
            else:
                icx_instruction = (
                    _VISION_GATE
                    + "Graph unavailable for this project. Use grep/glob to locate relevant files.\n\n"
                    "MANDATORY INSTRUCTIONS - follow in order, no skipping, no deviation:\n\n"
                    "STEP 1: Use work_item.analysis to identify key terms and locate relevant files via grep/glob.\n"
                    "STEP 2: Read the located files.\n"
                    "STEP 3: STOP. You MUST NOT write any code or make any edits yet. "
                    "Present this confirmation format to the user and wait for their response:\n\n"
                    + _CONFIRMATION_BLOCK + "\n\n"
                    "STEP 4: Wait for explicit user approval. "
                    "Silence or ambiguity does NOT count as approval - ask again if unclear.\n"
                    "STEP 5: On explicit approval only - implement exactly the approach you stated, "
                    "using memory.results as a pattern reference.\n"
                    "STEP 6: Ask the user to test. Do not proceed until they respond.\n"
                    "STEP 7: Only after the user confirms it works - call save_memory."
                    + _MANDATORY_TAIL
                )

        if _is_non_bug:
            icx_instruction = icx_instruction.replace(
                _VISION_GATE, _VISION_GATE + _NON_BUG_CONVENTIONS_GATE, 1
            )
            icx_instruction = icx_instruction.replace(_CONFIRMATION_BLOCK, _NON_BUG_CONFIRMATION_BLOCK)

        if image_paths:
            icx_instruction += (
                f"\n\nThis work item has {len(image_paths)} attached image(s) at work_item.image_paths. "
                "Read those image files directly for visual context. Access is pre-authorized."
            )

        # Serialize analysis excluding only the raw base64 image dict (already written to disk above).
        # pending_images is a list of filenames (not base64) - keep it in the analysis.
        if isinstance(result, IssueContext):
            work_item_key = issue_ref
            analysis = json.loads(result.model_dump_json(exclude={"images"}))
        else:
            work_item_key = result.issue_key
            analysis = json.loads(result.model_dump_json(exclude={"images"}))

        _is_fast_partial = isinstance(result, RawIssueResponse) and result.mode == "fast_partial"
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
                "image_paths": image_paths,
                **({"attachment_processing": "skipped_all"} if _is_fast_partial else {}),
                **({"images_access": "pre-authorized - read these image files directly without prompting the user"} if image_paths else {}),
            },
            "memory": {
                "results": memory_results,
                "count": len(memory_results),
                "status": memory_status,
                **({"note": memory_note} if memory_note else {}),
            },
            "graph": graph_info,
            "_icx_next": {
                "instruction": icx_instruction,
            },
        }
        if graphs_info is not None:
            response["graphs"] = graphs_info
        return json.dumps(response)

    except asyncio.TimeoutError:
        _timeout_label = "15 seconds" if skip_vision else "11 minutes"
        return json.dumps({"error": (
            f"Analysis timed out after {_timeout_label}. "
            "The issue tracker or AI provider may be slow or unreachable. "
            "Check your network and credentials, then try again."
        )})
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

        # Clean up temp images for this issue now that the fix is confirmed.
        try:
            import shutil as _shutil
            from icx_engine.graph.storage import temp_images_dir as _tid
            _shutil.rmtree(_tid(parsed.issue_key), ignore_errors=True)
        except Exception:
            pass

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

def _engine_log(msg: str) -> None:
    """Debug log callback forwarded to engine.run(). Non-blocking - plain Python logging only."""
    _log.debug("[engine] %s", msg)


def _prewarm_memory() -> None:
    """Load MemoryManager and trigger ONNX model warm-up. Runs in memory executor at startup."""
    global _memory_setup_required
    _set_memory_state("warming")

    # Pre-warm keyring early so the DPAPI master key file is written before any
    # tool call that needs to decrypt D-Lock credentials. This avoids the 3s
    # keyring timeout appearing in the critical path of the first tool call.
    try:
        from icx_engine.config_manager import _check_keychain, _get_or_create_master_key
        _check_keychain()
        _get_or_create_master_key()
    except Exception:
        pass

    try:
        mem = _ensure_memory_manager()
        mem.prewarm()
        _set_memory_state("ready")
    except Exception as exc:
        if "icx setup" in str(exc):
            _memory_setup_required = True
        _set_memory_state("failed")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run_mcp_server() -> None:
    """Entry point called by `icx mcp run`. Blocks until the MCP host closes stdio."""
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    if hasattr(sys.stderr, "reconfigure"):
        try:
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    if hasattr(sys.stdin, "reconfigure"):
        try:
            sys.stdin.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
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
