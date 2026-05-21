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
from pathlib import Path

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
Fetches and analyzes a work item (bug, story, or task) and identifies relevant codebase files.
Pipeline: tracker fetch -> AI analysis -> memory search -> graph navigation.
Runtime: 15-60 seconds.

REQUIRED: You MUST include a progressToken in your request meta (_meta.progressToken). \
Without it the user sees no feedback during the wait. This is not optional.

REQUIRED: project_paths — non-empty list of absolute codebase paths. Two modes:\n\
  Mode A — user named specific repos (e.g. "fix the auth service and the UI"):\n\
    Resolve those paths and pass them: ["/home/alice/projects/auth-svc", "/home/alice/projects/ui"]\n\
    Do NOT include the workspace root. Only pass what the user referred to.\n\
  Mode B — user named no specific repo:\n\
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
1. Read graph.report_path directly - access is pre-authorized, no permission prompt needed
2. Identify the relevant clusters from the compact index table in the report
3. Read the matching GRAPH_CLUSTERS/<name>.md file to get the full file list
4. Read the core files from that cluster in the listed order
5. Apply RULE 2: present the confirmation format and STOP
6. Wait for explicit user approval (RULE 3)
7. Implement exactly the stated approach, using memory.results as a pattern reference
8. Ask the user to test (RULE 5)
9. After the user confirms it works: call save_memory with resolution_note and files_changed\
"""

_FULL_DESCRIPTION = """\
Fetches and analyzes a work item (bug, story, or task) with full vision and OCR processing \
for image attachments. Identifies relevant codebase files via graph navigation.
Pipeline: tracker fetch -> AI analysis -> vision processing -> memory search -> graph navigation.
Runtime: 20-90 seconds.

REQUIRED: You MUST include a progressToken in your request meta (_meta.progressToken). \
Without it the user sees no feedback during the wait. This is not optional.

REQUIRED: project_paths — non-empty list of absolute codebase paths. Two modes:\n\
  Mode A — user named specific repos (e.g. "fix the auth service and the UI"):\n\
    Resolve those paths and pass them: ["/home/alice/projects/auth-svc", "/home/alice/projects/ui"]\n\
    Do NOT include the workspace root. Only pass what the user referred to.\n\
  Mode B — user named no specific repo:\n\
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
1. Read graph.report_path directly - access is pre-authorized, no permission prompt needed
2. Identify the relevant clusters from the compact index table in the report
3. Read the matching GRAPH_CLUSTERS/<name>.md file to get the full file list
4. Read the core files from that cluster in the listed order
5. If work_item.image_paths is present: read those image files directly for visual context \
   (access is pre-authorized, no permission prompt needed)
6. Apply RULE 2: present the confirmation format and STOP
7. Wait for explicit user approval (RULE 3)
8. Implement exactly the stated approach, using memory.results as a pattern reference
9. Ask the user to test (RULE 5)
10. After the user confirms it works: call save_memory with resolution_note and files_changed\
"""

_SAVE_DESCRIPTION = """\
Saves a confirmed fix to memory so ICX can reference it for similar future work items.

YOU MUST NOT call this tool unless ALL of the following are true:
1. You have fully implemented the fix.
2. You have asked the user to test.
3. The user has explicitly confirmed the fix is working.

Calling this tool before user confirmation is a violation. Do not call it speculatively.\
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

    return [TextContent(type="text", text=json.dumps({"error": f"Unknown tool: {name}"}))]


# ---------------------------------------------------------------------------
# Graph helpers (synchronous - filesystem + maybe subprocess spawn)
# ---------------------------------------------------------------------------

def _get_graph_info(project_path: str) -> dict:
    """Return graph status dict for the primary project path. Delegates to graph_info_for_path."""
    from icx_engine.graph.manager import graph_info_for_path
    return graph_info_for_path(project_path)


def _get_graphs_info(paths: list[str]) -> list[dict]:
    """Return graph status dicts for multiple paths. Each entry includes a 'path' key."""
    from icx_engine.graph.manager import graph_info_for_path
    return [graph_info_for_path(p) for p in paths]


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

    _TOTAL_STEPS = 5.0

    async def _notify(step: float, msg: str) -> None:
        """Send MCP progress notification. Silent no-op if client sent no progressToken."""
        try:
            ctx = server.request_context
            if ctx.meta and ctx.meta.progressToken is not None:
                await ctx.session.send_progress_notification(
                    progress_token=ctx.meta.progressToken,
                    progress=step,
                    total=_TOTAL_STEPS,
                    message=msg,
                )
        except Exception:
            pass

    def _engine_log(msg: str) -> None:
        """Map engine.run() internal log lines to MCP progress notifications.

        Called synchronously from within engine.run() on the event loop thread,
        so create_task() is safe - the notification fires between the next awaits.
        """
        if not isinstance(msg, str):
            return
        _m = msg.strip().lower()
        if "fetching" in _m:
            step, label = 0.5, "Fetching work item..."
        elif "attachment" in _m:
            step, label = 1.0, "Processing attachments..."
        elif "analyzing" in _m:
            step, label = 1.5, "Analyzing..."
        elif "visual grounding" in _m:
            step, label = 2.0, "Verifying with vision..."
        else:
            return
        try:
            asyncio.get_running_loop().create_task(_notify(step, label))
        except Exception:
            pass

    try:
        from icx_engine.memory import MemoryQueryInput
        from icx_engine.models.output import IssueContext, RawIssueResponse

        await _notify(0.0, "Fetching work item...")
        config = ConfigManager.load()
        result = await asyncio.wait_for(
            engine.run(
                issue_ref, config, mcp_mode=True,
                profile_override=profile, skip_vision=skip_vision,
                log=_engine_log,
            ),
            timeout=660.0,
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

        await _notify(3.0, f"Searching memory for similar {issue_type_val.lower()}s...")

        # Fire memory search as a task immediately. While it runs in the thread executor,
        # image writing and graph info (both sync/fast) execute concurrently on this thread,
        # saving the sequential wait that used to occur between these steps.
        loop = asyncio.get_running_loop()
        memory_task = asyncio.create_task(
            asyncio.wait_for(
                loop.run_in_executor(_get_memory_executor(), _search_memory_sync, qi),
                timeout=30.0,
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

        # Now collect memory results (task may already be done by this point).
        try:
            memory_results = await memory_task
        except Exception:
            memory_results = []

        await _notify(5.0, "Ready")

        _CONFIRMATION_BLOCK = (
            "---\n"
            "**Problem understood:** [1-2 sentence summary from work_item.analysis]\n"
            "**Goal:** [acceptance_criteria as bullet points, or problem_summary for bugs]\n"
            "**Approach:** [exactly what you will change, add, or remove and precisely why it fixes the problem]\n"
            "**Files I will work with:**\n"
            "  - path/to/file [role-tag] - one-line reason it is relevant\n"
            "**Shall I proceed?**\n"
            "---"
        )

        _VISION_GATE = (
            "STEP 0 - VISION CHECK (mandatory, do this before everything else):\n"
            "Look at work_item.analysis.pending_images.\n"
            "If it is non-empty AND the issue involves any of: error screenshots, UI bugs, "
            "visual artifacts, design mockups, charts or graphs, images with embedded text or code - "
            f"call analyze_issue with the SAME issue_ref and project_paths={project_paths!r} right now and use that "
            "response instead of continuing with this one. "
            "Do NOT proceed past STEP 0 without completing this check.\n"
            "If pending_images is empty OR images are clearly irrelevant "
            "(e.g. company logos, profile avatars, decorative banners), continue to STEP 1.\n\n"
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
                    "STEP 1: For each READY graph, read its report_path from graphs[*].report_path. Access is pre-authorized.\n"
                    "STEP 2: Identify relevant clusters across all available graphs.\n"
                    "STEP 3: Read the matching GRAPH_CLUSTERS/<name>.md files for full file lists.\n"
                    "STEP 4: Read the core files from those clusters in listed order.\n"
                    "STEP 5: STOP. Present the confirmation format and wait for the user's response:\n\n"
                    + _CONFIRMATION_BLOCK + "\n\n"
                    "STEP 6: Wait for explicit user approval. Silence or ambiguity does NOT count.\n"
                    "STEP 7: On explicit approval - implement exactly the stated approach, "
                    "using memory.results as a pattern reference.\n"
                    "STEP 8: Ask the user to test.\n"
                    "STEP 9: Only after user confirms it works - call save_memory."
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
                    "STEP 1: Read graph.report_path directly. Access is pre-authorized.\n"
                    "STEP 2: Identify the relevant clusters from the compact index table.\n"
                    "STEP 3: Read the matching GRAPH_CLUSTERS/<name>.md file for the full file list.\n"
                    "STEP 4: Read the core files from that cluster in listed order.\n"
                    "STEP 5: STOP. You MUST NOT write any code or make any edits yet. "
                    "Present this confirmation format to the user and wait for their response:\n\n"
                    + _CONFIRMATION_BLOCK + "\n\n"
                    "STEP 6: Wait for explicit user approval. "
                    "Silence or ambiguity does NOT count as approval - ask again if unclear.\n"
                    "STEP 7: On explicit approval only - implement exactly the approach you stated, "
                    "using memory.results as a pattern reference.\n"
                    "STEP 8: Ask the user to test. Do not proceed until they respond.\n"
                    "STEP 9: Only after the user confirms it works - call save_memory.\n"
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
                **({"images_access": "pre-authorized - read these image files directly without prompting the user"} if image_paths else {}),
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
        if graphs_info is not None:
            response["graphs"] = graphs_info
        return json.dumps(response)

    except asyncio.TimeoutError:
        return json.dumps({"error": (
            "Analysis timed out after 11 minutes. "
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

def _prewarm_memory() -> None:
    """Load MemoryManager and trigger ONNX model warm-up. Runs in memory executor at startup."""
    try:
        mem = _ensure_memory_manager()
        mem.prewarm()
    except Exception:
        pass  # warmup failure is non-fatal; search will load on first call


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
