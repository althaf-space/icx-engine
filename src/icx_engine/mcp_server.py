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
#   Avoids reloading the 110 MB ONNX model on every search call. The model is
#   loaded once when the first search runs and stays resident for the process
#   lifetime. Single-threaded access also removes all LanceDB concurrency issues.
# ---------------------------------------------------------------------------

_MEMORY_EXECUTOR: _ThreadPoolExecutor | None = None
_MEMORY_EXECUTOR_LOCK = _threading.Lock()
_SHARED_MEMORY_MANAGER: "MemoryManager | None" = None  # created inside memory thread on first use

# Memory readiness state - transitions: cold -> warming -> ready | failed
# Tool responses read this to decide whether to submit a search or return immediately.
_memory_state: str = "cold"  # cold | warming | ready | failed
_memory_state_lock = _threading.Lock()
_memory_setup_required: bool = False  # True when prewarm failed because models weren't downloaded

# Session context - process-scoped, cleared on server restart.
# Accumulates work items analyzed in this session so subsequent calls
# see prior items as context. Keyed by issue_key; max _SESSION_MAX entries.
_SESSION_CONTEXT: list[dict] = []
_SESSION_MAX = 10


def _get_memory_state() -> str:
    return _memory_state


def _set_memory_state(state: str) -> None:
    global _memory_state
    with _memory_state_lock:
        _memory_state = state


def _session_append(issue_key: str, summary: str, issue_type: str) -> None:
    global _SESSION_CONTEXT
    _SESSION_CONTEXT = (
        [e for e in _SESSION_CONTEXT if e["issue_key"] != issue_key]
        + [{"issue_key": issue_key, "summary": summary[:120], "issue_type": issue_type}]
    )[-_SESSION_MAX:]


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
_MEM_SEARCH_TOOL = "memory_search"
_GRAPH_CONTEXT_TOOL = "graph_find_context"
_GRAPH_SUBSYSTEM_TOOL = "graph_subsystem"
_GRAPH_CHAIN_TOOL = "graph_call_chain"
_GRAPH_IMPACT_TOOL = "graph_impact"
_GRAPH_CROSS_LINKS_TOOL = "graph_cross_links"
_MEM_HOTSPOTS_TOOL = "memory_get_hotspots"
_MEM_BY_FILE_TOOL = "memory_find_by_file"
_MEM_RELATED_TOOL = "memory_get_related"
_MEM_PATTERNS_TOOL = "memory_get_patterns"
_SAVE_TOOL_NAME = "save_memory"

# ---------------------------------------------------------------------------
# Tool descriptions
# ---------------------------------------------------------------------------

_FAST_DESCRIPTION = """\
ICX TOOL SEQUENCE - WORKFLOW ORDER (read this first):
  [1]  analyze_issue_fast / analyze_issue  <- you are here
  [2]  memory_search          [<1s]  MANDATORY after analysis - search with agent-generated tags
  [3]  graph_find_context     [~5s]  MANDATORY - replaces grep/glob entirely
  [4]  graph_subsystem        [~2s]  expand one file to its full feature cluster
  [5]  graph_call_chain       [~5s]  trace data flow through a specific component
  [6]  graph_impact           [~12s] MANDATORY before changing shared code
  [7]  graph_cross_links      [~2s]  microservices only - SKIP for monolith projects
  [8]  memory_get_hotspots    [~1s]  fragile file ranking - call at start of investigation
  [9]  memory_find_by_file    [<1s]  MANDATORY before editing each file
  [10] memory_get_related     [<1s]  hidden coupling - call after finding bug location
  [11] memory_get_patterns    [<1s]  systemic analysis - call for recurring bug categories
       --- implement fix here, only after explicit user approval ---
  [12] save_memory                   MANDATORY after user confirms fix works

Runs AI analysis on issue text only - attachments are not downloaded or processed. \
Use when the issue description and comments contain sufficient context, or for quick triage.
Pipeline: tracker fetch -> AI analysis (text only) -> graph status -> memory (if warm).
Attachments listed in pending_images, pending_audio, pending_documents, or pending_unsupported \
for manual review. attachment_processing='text_only' in response when LLM is configured.
Runtime: under 45 seconds.

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
**Tools called for this ticket** (ALL 10 required; show result or documented skip for each):
  1. memory_search:                 [N results OR skipped: memory.status!='ready']
  2. graph_find_context:            [N files returned, top score X.XX - NO VALID SKIP]
  3. graph_subsystem(file):         [cluster name, N files OR skipped: brand-new-file]
  4. graph_call_chain(node):        [upstream/downstream summary OR skipped: brand-new-node]
  5. graph_impact(node):            [N dependents OR skipped: brand-new-isolated-file]
  6. graph_cross_links:             [N links OR skipped: confirmed-single-monolith]
  7. memory_get_hotspots:           [result summary OR skipped: memory.status!='ready']
  8. memory_find_by_file (per file):[result per file OR skipped: brand-new-file]
  9. memory_get_related:            [top strength=X OR skipped: memory.status!='ready']
  10. memory_get_patterns:          [pattern summary OR skipped: memory.status!='ready']
  Any entry left blank or skipped with a vague reason is a VIOLATION.
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

RULE 6 - MANDATORY TOOL COMPLETENESS:
Before presenting the confirmation format, you MUST have called every tool whose skip condition is NOT met. \
Minimum required calls (check NOW before presenting):\n\
  graphs[0].status=='ready' AND memory.status=='ready':  9+ tools mandatory (items 1-6, 8-11; item 7 depends on project type)\n\
  graphs[0].status=='ready' AND memory.status!='ready':  4+ tools mandatory (items 3-6; item 7 depends on project type)\n\
  graphs[0].status!='ready' AND memory.status=='ready':  5 tools mandatory (items 1, 8, 9, 10, 11)\n\
  graphs[0].status!='ready' AND memory.status!='ready':  grep/glob only - none of items 1-11 apply\n\
memory_search (item 1) skips ONLY when memory.status != 'ready'. \
graph_find_context (item 3) has NO valid skip when graph is ready - calling it is not optional. \
memory_get_hotspots (item 8) and memory_get_patterns (item 11) skip ONLY when memory.status != 'ready' - \
NEVER skip them based on memory_search result count. \
memory_get_related (item 10) skips only when memory.status != 'ready'. Always pass files from graph_find_context results - works for new tickets. \
Presenting the confirmation block with any uncalled required tool is a VIOLATION. \
The 10-entry tool checklist is the evidence record - every entry must show a result or an EXACT skip condition from this rule.

================================================================================
WORKFLOW (follow in order, no skipping):
================================================================================
1. Identify relevant files using one of two options:
   OPTION A (recommended): Call graph_find_context with project_path from graphs[0].path and a task
   description derived from work_item.analysis. Returns all matching files ranked by relevance score.
   OPTION B (manual): Read graphs[0].report_path (pre-authorized) -> identify clusters from the
   compact index table -> read GRAPH_CLUSTERS/<name>.md -> read core files in listed order.
2. Apply RULE 2: present the confirmation format and STOP
3. Wait for explicit user approval (RULE 3)
4. Implement exactly the stated approach, using memory_search results as a pattern reference
5. Ask the user to test (RULE 5)
6. After the user confirms it works: call save_memory with resolution_note and files_changed\
"""

_FULL_DESCRIPTION = """\
ICX TOOL SEQUENCE - WORKFLOW ORDER (read this first):
  [1]  analyze_issue_fast / analyze_issue  <- you are here
  [2]  memory_search          [<1s]  MANDATORY after analysis - search with agent-generated tags
  [3]  graph_find_context     [~5s]  MANDATORY - replaces grep/glob entirely
  [4]  graph_subsystem        [~2s]  expand one file to its full feature cluster
  [5]  graph_call_chain       [~5s]  trace data flow through a specific component
  [6]  graph_impact           [~12s] MANDATORY before changing shared code
  [7]  graph_cross_links      [~2s]  microservices only - SKIP for monolith projects
  [8]  memory_get_hotspots    [~1s]  fragile file ranking - call at start of investigation
  [9]  memory_find_by_file    [<1s]  MANDATORY before editing each file
  [10] memory_get_related     [<1s]  hidden coupling - call after finding bug location
  [11] memory_get_patterns    [<1s]  systemic analysis - call for recurring bug categories
       --- implement fix here, only after explicit user approval ---
  [12] save_memory                   MANDATORY after user confirms fix works

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
**Tools called for this ticket** (ALL 10 required; show result or documented skip for each):
  1. memory_search:                 [N results OR skipped: memory.status!='ready']
  2. graph_find_context:            [N files returned, top score X.XX - NO VALID SKIP]
  3. graph_subsystem(file):         [cluster name, N files OR skipped: brand-new-file]
  4. graph_call_chain(node):        [upstream/downstream summary OR skipped: brand-new-node]
  5. graph_impact(node):            [N dependents OR skipped: brand-new-isolated-file]
  6. graph_cross_links:             [N links OR skipped: confirmed-single-monolith]
  7. memory_get_hotspots:           [result summary OR skipped: memory.status!='ready']
  8. memory_find_by_file (per file):[result per file OR skipped: brand-new-file]
  9. memory_get_related:            [top strength=X OR skipped: memory.status!='ready']
  10. memory_get_patterns:          [pattern summary OR skipped: memory.status!='ready']
  Any entry left blank or skipped with a vague reason is a VIOLATION.
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

RULE 6 - MANDATORY TOOL COMPLETENESS:
Before presenting the confirmation format, you MUST have called every tool whose skip condition is NOT met. \
Minimum required calls (check NOW before presenting):\n\
  graphs[0].status=='ready' AND memory.status=='ready':  9+ tools mandatory (items 1-6, 8-11; item 7 depends on project type)\n\
  graphs[0].status=='ready' AND memory.status!='ready':  4+ tools mandatory (items 3-6; item 7 depends on project type)\n\
  graphs[0].status!='ready' AND memory.status=='ready':  5 tools mandatory (items 1, 8, 9, 10, 11)\n\
  graphs[0].status!='ready' AND memory.status!='ready':  grep/glob only - none of items 1-11 apply\n\
memory_search (item 1) skips ONLY when memory.status != 'ready'. \
graph_find_context (item 3) has NO valid skip when graph is ready - calling it is not optional. \
memory_get_hotspots (item 8) and memory_get_patterns (item 11) skip ONLY when memory.status != 'ready' - \
NEVER skip them based on memory_search result count. \
memory_get_related (item 10) skips only when memory.status != 'ready'. Always pass files from graph_find_context results - works for new tickets. \
Presenting the confirmation block with any uncalled required tool is a VIOLATION. \
The 10-entry tool checklist is the evidence record - every entry must show a result or an EXACT skip condition from this rule.

================================================================================
WORKFLOW (follow in order, no skipping):
================================================================================
1. Identify relevant files using one of two options:
   OPTION A (recommended): Call graph_find_context with project_path from graphs[0].path and a task
   description derived from work_item.analysis. Returns all matching files ranked by relevance score.
   OPTION B (manual): Read graphs[0].report_path (pre-authorized) -> identify clusters from the
   compact index table -> read GRAPH_CLUSTERS/<name>.md -> read core files in listed order.
2. If work_item.image_paths is present: read those image files directly for visual context \
   (access is pre-authorized, no permission prompt needed)
3. Apply RULE 2: present the confirmation format and STOP
4. Wait for explicit user approval (RULE 3)
5. Implement exactly the stated approach, using memory_search results as a pattern reference
6. Ask the user to test (RULE 5)
7. After the user confirms it works: call save_memory with resolution_note and files_changed\
"""

_SAVE_DESCRIPTION = """\
Commits a confirmed fix to local memory. Future agents retrieve this when working on similar issues.

CALL GATE - do NOT call this tool unless ALL three are true:
1. Fix is fully implemented.
2. You asked the user to test it.
3. User explicitly confirmed it is working.
Calling speculatively or before confirmation is a violation.

FIELD REQUIREMENTS - every field you write is read by a future agent under time pressure:

summary: Your synthesized title of the root problem. NOT the raw Jira summary.
  Describe what was actually wrong at the code level, not the symptom.
  GOOD: "JWT expiry check uses < instead of <= rejecting tokens at exact expiry second"
  BAD: "JWT auth broken" / "Login not working"

problem_description: Your root cause analysis. Cover the exact failure mechanism, affected \
file(s)/function(s), and what condition triggered the bug. This is embedded for semantic search - \
precise agent-written text retrieves better than raw tracker descriptions.
  GOOD: "auth/middleware.py:validate_token() used strict less-than on token.exp. Tokens valid \
until their exact expiry second were rejected because exp == now evaluated false. Only manifested \
on requests arriving within the same second as expiry."
  BAD: "Token expiry was broken." / "Auth middleware had a bug."

resolution_note: What was changed, where, and the mechanical reason it works.
  GOOD: "Changed < to <= in validate_token() in auth/middleware.py line 47. Added 5s clock-skew \
buffer via TOKEN_SKEW_SECONDS env var to handle distributed clock drift."
  BAD: "Fixed the check." / "Updated middleware."

files_changed: Every file path you modified. Drives structural relation detection and hotspot \
analysis - incomplete lists degrade future recall.

tags: 3-6 specific lowercase tags covering problem domain, affected system layer, failure type. \
PRIMARY retrieval signal - this memory surfaces only when tags match a future query.
  GOOD: ["jwt-expiry", "auth-middleware", "token-validation", "clock-skew"]
  BAD: ["auth", "bug", "fix", "backend"]
  Rules: no generic words (bug/fix/error/issue/update), hyphenate multi-word, no duplicates.

work_item_type: Pass the exact value from work_item.type in the analyze_issue response. \
This is the tracker-authoritative issue type - do not infer or substitute.\
"""

_GRAPH_CONTEXT_DESCRIPTION = """\
CALL THIS FIRST - replaces grep, glob, and manual file search entirely.
USE WHEN: Starting work on any graph-ready project before reading any files.
Given a task description, returns source files ranked by relevance score, with node_ids for deeper graph calls \
and file paths for graph_subsystem.

RETURNS: Ranked list of {node_id, path, cluster, role, score (0.0-1.0)}.
VALUE: Structure-aware retrieval beats keyword search - it understands import graphs, not just text. \
Gets you the right files in one call instead of 10+ grep attempts.

TASK QUALITY MATTERS - specific descriptions score far better than vague ones:
  GOOD: "JWT token expiry not enforced on POST /api/orders route"
  GOOD: "campaign list date filter ignores selected range after page reload"
  BAD: "fix auth bug" / "date filter broken"

EXAMPLE: task="login timeout not expiring" ->
  src/auth/session.py [service] score=0.91
  src/auth/token.py [service] score=0.87
  src/middleware/auth.py [middleware] score=0.74

RUNTIME: ~5 seconds.
PREREQUISITE: Graph must be built (icx graph build <name>) and graph.status == "ready".
project_path must be the absolute path to the project root.
If the tool returns an error with action_required, follow the action exactly before retrying.\
"""

_GRAPH_CHAIN_DESCRIPTION = """\
USE WHEN: You need to trace data flow through the system or understand who calls a specific component.
Given a node_id, returns a bidirectional call chain up to depth hops: who calls it AND what it calls.

RETURNS: {upstream: [files that import/call this node], downstream: [what this node imports/calls]}
VALUE: Reveals entry points and direct dependencies - essential before changing a function signature \
or tracing how a request flows from API layer to database.

CRITICAL DIFFERENCE FROM graph_impact:
  graph_call_chain = bidirectional, depth-bounded (3 hops default) - answers "how does data flow through X?"
  graph_impact     = unidirectional (dependents only), fully transitive - answers "what breaks if I change X?"

EXAMPLE: graph_call_chain("auth_service") ->
  upstream: [api_routes -> auth_service]
  downstream: [auth_service -> user_repo, auth_service -> token_store]
  -> Now you know: api_routes is the entry point; user_repo and token_store are the dependencies to watch.

RUNTIME: ~5 seconds.
node_id comes from graph_find_context results. Graph must be ready.
project_path must be the absolute path to the project root.
If the tool returns an error with action_required, follow the action exactly before retrying.\
"""

_GRAPH_IMPACT_DESCRIPTION = """\
MANDATORY before changing any shared function, class, component, or utility.
USE WHEN: Before refactoring a node - reveals EVERYTHING that will break, including indirect dependents.

RETURNS: All dependents grouped by confidence tier (high/medium/low risk):
  direct: files that directly import or call this node
  transitive: files that depend on the direct dependents (recursively, unlimited depth)
VALUE: Prevents the most common production bug - changing shared code while missing hidden dependents. \
Skipping this call guarantees blind spots when touching utils, models, services, or middleware. \
When uncertain whether code is shared: CALL IT. Takes under 15 seconds.

CRITICAL DIFFERENCE FROM graph_call_chain:
  graph_impact     = unidirectional (dependents only), fully transitive - answers "what breaks if I change X?"
  graph_call_chain = bidirectional, depth-bounded (3 hops default) - answers "how does data flow through X?"

EXAMPLE: graph_impact("UserRepository") ->
  direct: [AuthService]
  transitive: [LoginController (via AuthService), ApiGateway (via LoginController)]
  -> 3 files at risk - all must be reviewed before changing UserRepository.

RUNTIME: ~12 seconds. This is the exact cost. It is not optional. Call it.
node_id comes from graph_find_context results. Graph must be ready.
project_path must be the absolute path to the project root.
If the tool returns an error with action_required, follow the action exactly before retrying.\
"""

_GRAPH_SUBSYSTEM_DESCRIPTION = """\
USE WHEN: You found one relevant file but need to see the COMPLETE feature boundary before touching it.
Given a file path, returns all files in the same cluster (community of functionally related files \
detected by import graph analysis).

RETURNS: Cluster name + all file paths that belong to the same functional unit.
VALUE: Prevents partial fixes. You cannot fully understand a bug in auth/service.py without seeing \
every file in the Authentication cluster. Missing one causes incomplete fixes and regressions.

EXAMPLE:
  Input:  graph_subsystem("src/auth/service.py")
  Output: cluster "Authentication": [auth/service.py, auth/token.py, auth/session.py, auth/middleware.py]
  -> Without this call, you would have seen only auth/service.py and missed 3 related files.

WHEN TO CALL:
  - After graph_find_context returns initial files - expand each core file to see its full cluster
  - When the bug description mentions a feature name (auth, billing, orders) - find its cluster boundary
  - Before writing the implementation plan - ensures you see the whole picture, not just one entry point
SKIP ONLY IF: Task adds a brand new file with no existing cluster to discover.

RUNTIME: ~2 seconds.
Graph must be built (icx graph build <name>) and graph.status == "ready".
project_path must be the absolute path to the project root.
If the tool returns an error with action_required, follow the action exactly before retrying.\
"""

_GRAPH_TOOLS_DECISION = (
    "STEP 1b - DEEPER GRAPH TOOLS (YOU MUST attempt all four; skipping without a documented technical reason = VIOLATION):\n"
    "  Vague skip reasons ('not applicable', 'not needed', 'seems fine', 'I think it is internal') are REJECTED.\n"
    "  Each skip must cite the EXACT technical condition met. When uncertain: CALL IT.\n\n"
    "    graph_subsystem(file_path)\n"
    "      CALL: for every file that EXISTS in the codebase that you plan to read or modify.\n"
    "      SKIP ONLY IF: you are creating a file from scratch that has never existed.\n"
    "      VALID skip: 'Creating new file src/utils/newHelper.py - confirmed does not exist in graph'\n"
    "      INVALID skip: 'Also adding a new file so skipping' - you must still call for EXISTING files in the same task.\n\n"
    "    graph_call_chain(node_id)\n"
    "      CALL: always, using node_id from graph_find_context results.\n"
    "      SKIP ONLY IF: node_id belongs to a file you are creating from scratch (zero existing callers confirmed by graph).\n\n"
    "    graph_impact(node_id)\n"
    "      CALL: always, especially when touching shared code. Takes ~12s - not a valid reason to skip.\n"
    "      SKIP ONLY IF: the fix creates a brand-new isolated file with zero existing dependents.\n"
    "      'I think it is not shared' is NOT an accepted skip reason. When uncertain: CALL IT.\n\n"
    "    graph_cross_links(project_path)\n"
    "      CALL: always unless you can confirm single monolith from evidence already in hand.\n"
    "      SKIP ONLY IF: graph_find_context returned results from exactly ONE project path AND you saw no HTTP client calls (fetch/axios/requests/httpx) in those files.\n"
    "      'I think it is a monolith' is NOT accepted. The evidence must come from what the tools returned.\n\n"
    "STEP 1c - MEMORY FILE CHECK (mandatory for every file you plan to edit):\n"
    "  For each file from STEP 1 and STEP 1b that you intend to modify:\n"
    "    Call memory_find_by_file(file_path). Under 1 second. No time excuse accepted.\n"
    "  ONLY accepted skip: file did not exist before this ticket (brand new, confirmed).\n"
    "  'I do not think there is history here' is NOT accepted - call it and let the result decide.\n"
    "  What to do with results:\n"
    "    - resolution_note matches current bug pattern -> reference it explicitly in your Approach\n"
    "    - file has 3+ past bugs -> mark it [fragile] in your Files list, add extra test coverage\n"
    "    - empty results -> proceed normally\n\n"
    "STEP 1d - RELATED ISSUE DISCOVERY (mandatory when memory.status == 'ready'):\n"
    "  Call memory_get_related(files=[...files from graph_find_context...]).\n"
    "  For reopened tickets: also pass issue_key=work_item.issue_key to check prior edges.\n"
    "  If any result has strength >= 0.5: read its resolution_note before writing your Approach.\n"
    "  ONLY accepted skip: memory.status != 'ready'.\n\n"
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
        # ------------------------------------------------------------------ #
        # [1-2] Entry points - always start here                             #
        # ------------------------------------------------------------------ #
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
        # ------------------------------------------------------------------ #
        # [2] Memory search - agent-driven tag search after analysis         #
        # ------------------------------------------------------------------ #
        Tool(
            name=_MEM_SEARCH_TOOL,
            description=(
                "CALL IMMEDIATELY AFTER analyze_issue - before any graph or file exploration.\n"
                "Search memory using tags YOU generate from work_item.analysis. "
                "The analysis result is what teaches you the problem domain - use it now.\n\n"
                "HOW TO GENERATE TAGS: From work_item.analysis extract 3-6 specific lowercase tags covering:\n"
                "  - Problem domain (e.g. 'jwt-expiry', 'db-connection-pool', 'websocket-reconnect')\n"
                "  - Affected system layer (e.g. 'auth-middleware', 'data-access-layer', 'event-handler')\n"
                "  - Failure type (e.g. 'race-condition', 'null-reference', 'timeout', 'off-by-one')\n"
                "  Rules: no generic words (bug/fix/error/issue/backend), hyphenate multi-word concepts.\n"
                "  GOOD: ['jwt-expiry', 'auth-middleware', 'clock-skew']\n"
                "  BAD: ['auth', 'bug', 'backend']\n\n"
                "SKIP ONLY IF: memory.status != 'ready' in the analyze_issue response.\n\n"
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
        ),
        # ------------------------------------------------------------------ #
        # [3-7] Graph tools - file discovery, scope, flow, impact, contracts #
        # ------------------------------------------------------------------ #
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
            name=_GRAPH_CROSS_LINKS_TOOL,
            description=(
                "MICROSERVICES ONLY - SKIP IF SINGLE MONOLITH.\n"
                "USE WHEN: Working on a project that makes HTTP calls to peer services and you need to know "
                "which calls cross service boundaries.\n"
                "Matches outgoing HTTP calls in THIS project to REST routes in peer registered projects.\n"
                "RETURNS: [{source_project, call_site, method, route, target_project, matched_route}] "
                "listing every cross-service HTTP link.\n"
                "VALUE: Reveals which API contracts are in play for this change - prevents breaking a "
                "consumer or producer contract you did not know existed.\n"
                "EXAMPLE: UI project calls POST /api/v1/orders -> matched to route in backend-svc project. "
                "Without this call, you would not know the UI depends on that exact contract.\n"
                "Returns empty list if no cross-service HTTP calls were detected, or if graph needs rebuild "
                "(icx graph build <name>).\n"
                "RUNTIME: ~2 seconds.\n"
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
        # ------------------------------------------------------------------ #
        # [8-11] Historical memory tools - hotspots, per-file, relations     #
        # ------------------------------------------------------------------ #
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
                },
                "required": ["file_path"],
            },
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
                },
                "required": [],
            },
        ),
        Tool(
            name=_MEM_PATTERNS_TOOL,
            description=(
                "ANSWERS: 'Which BUG CATEGORIES keep recurring?' - detects systemic failure patterns across all saved work items.\n"
                "USE WHEN THE SAME BUG CATEGORY KEEPS RECURRING OR BEFORE MAJOR REFACTORING - "
                "reveals systemic weaknesses in the codebase.\n"
                "Patterns are recomputed every 10 saves.\n"
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
                },
                "required": [],
            },
        ),
        # ------------------------------------------------------------------ #
        # [12] Commit to memory - only after user confirms fix works         #
        # ------------------------------------------------------------------ #
        Tool(
            name=_SAVE_TOOL_NAME,
            description=_SAVE_DESCRIPTION,
            inputSchema={
                "type": "object",
                "properties": {
                    "issue_key": {
                        "type": "string",
                        "description": "Issue key (e.g. PROJ-456).",
                    },
                    "summary": {
                        "type": "string",
                        "description": "Agent-synthesized root problem title. NOT the raw tracker summary. Describe what was wrong at the code level. Max 500 characters.",
                    },
                    "problem_description": {
                        "type": "string",
                        "description": "Agent root cause analysis: exact failure mechanism, affected file(s)/function(s), triggering condition. This text is embedded for semantic search - precise analysis retrieves better than raw tracker descriptions. Max 2000 characters.",
                    },
                    "resolution_note": {
                        "type": "string",
                        "description": "What was changed, exact file/function/line, and the mechanical reason it works. Minimum 2-3 sentences. Vague notes provide zero value to future agents.",
                    },
                    "files_changed": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Every file path you modified. Drives structural relation detection and hotspot analysis - incomplete lists degrade future recall.",
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "3-6 specific lowercase tags covering problem domain, affected system layer, failure type. PRIMARY retrieval signal - this memory surfaces only when tags match a future query. GOOD: ['jwt-expiry', 'auth-middleware', 'token-validation']. BAD: ['auth', 'bug', 'fix']. No generic words, hyphenate multi-word, no duplicates.",
                    },
                    "work_item_type": {
                        "type": "string",
                        "description": "Exact value from work_item.type in the analyze_issue response. Tracker-authoritative - do not infer or substitute.",
                    },
                    "pattern_used": {
                        "type": "string",
                        "description": "Implementation pattern applied (e.g. 'repository pattern', 'event sourcing'). Optional - omit if none applies.",
                    },
                },
                "required": ["issue_key", "summary", "problem_description", "resolution_note", "files_changed", "tags", "work_item_type"],
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
            results = await loop.run_in_executor(
                _get_memory_executor(),
                functools.partial(_search_memory_sync, qi, top_k),
            )
            return [TextContent(type="text", text=json.dumps(
                {"results": results, "count": len(results), "status": "ok"}
            ))]
        except Exception as exc:
            return [TextContent(type="text", text=json.dumps({"error": str(exc)}))]

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
            from dataclasses import asdict as _asdict

            _result = _resolve_graph_path(_raw_path)
            if isinstance(_result, dict):
                return _result
            _project_path, _project_id, staleness_warning = _result

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

            _result_cl = _resolve_graph_path(_raw_path_cl)
            if isinstance(_result_cl, dict):
                return _result_cl
            _project_path, _project_id, staleness_warning = _result_cl

            cl_path = _st.cross_links_path(_project_id)
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
            return [TextContent(type="text", text=json.dumps({"results": entries, "count": len(entries)}))]
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
            return [TextContent(type="text", text=json.dumps({"results": related, "count": len(related)}))]
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
            return [TextContent(type="text", text=json.dumps({"results": patterns, "count": len(patterns)}))]
        except Exception as exc:
            return [TextContent(type="text", text=json.dumps({"error": str(exc)}))]

    if name == _SAVE_TOOL_NAME:
        issue_key = args.get("issue_key", "")
        if not isinstance(issue_key, str) or not issue_key.strip() or len(issue_key) > 2048:
            return [TextContent(type="text", text=json.dumps(
                {"error": "issue_key must be a non-empty string under 2048 characters."}
            ))]
        summary = args.get("summary", "")
        if not isinstance(summary, str) or not summary.strip() or len(summary) > 500:
            return [TextContent(type="text", text=json.dumps(
                {"error": "summary must be a non-empty string under 500 characters."}
            ))]
        problem_description = args.get("problem_description", "")
        if not isinstance(problem_description, str) or not problem_description.strip() or len(problem_description) > 2000:
            return [TextContent(type="text", text=json.dumps(
                {"error": "problem_description must be a non-empty string under 2000 characters."}
            ))]
        resolution_note = args.get("resolution_note", "")
        if not isinstance(resolution_note, str) or not resolution_note.strip() or len(resolution_note) > 10000:
            return [TextContent(type="text", text=json.dumps(
                {"error": "resolution_note must be a non-empty string under 10000 characters."}
            ))]
        files_changed = args.get("files_changed")
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
        tags = args.get("tags")
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
        work_item_type = args.get("work_item_type", "")
        if not isinstance(work_item_type, str) or not work_item_type.strip() or len(work_item_type) > 100:
            return [TextContent(type="text", text=json.dumps(
                {"error": "work_item_type must be a non-empty string under 100 characters."}
            ))]
        pattern_used = args.get("pattern_used") or ""
        if not isinstance(pattern_used, str) or len(pattern_used) > 2000:
            return [TextContent(type="text", text=json.dumps(
                {"error": "pattern_used must be a string under 2000 characters."}
            ))]
        text = await _handle_save_memory(
            issue_key.strip(),
            summary.strip(),
            problem_description.strip(),
            resolution_note.strip(),
            [str(f) for f in files_changed],
            [str(t) for t in tags],
            work_item_type.strip(),
            pattern_used=pattern_used.strip(),
        )
        return [TextContent(type="text", text=text)]

    return [TextContent(type="text", text=json.dumps({"error": f"Unknown tool: {name}"}))]


# ---------------------------------------------------------------------------
# Graph helpers (synchronous - filesystem + maybe subprocess spawn)
# ---------------------------------------------------------------------------

def _resolve_graph_path(raw_path: str):
    """Validate path, derive project_id, check staleness.

    Returns (project_path, project_id, staleness_warning) on success,
    or an error dict on failure.
    """
    from icx_engine.graph import storage as _st
    from icx_engine.graph.paths import validate_and_resolve_paths, check_staleness

    resolved_paths, path_err = validate_and_resolve_paths([raw_path])
    if path_err is not None:
        return path_err

    project_path = resolved_paths[0]
    project_id = _st.derive_project_id(project_path)

    staleness = check_staleness(project_id, project_path)
    stale_status = staleness["status"]

    if stale_status in ("no_graph", "no_manifest"):
        return {
            "status": "error",
            "code": "NO_GRAPH",
            "message": (
                f"No graph found for '{project_path}'. "
                "Tell the user to build it first with the command below, then retry."
            ),
            "action_required": "stop_and_tell_user_to_build_graph",
            "build_command": f"icx graph build \"{project_path}\"",
            "project_path": str(project_path),
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
            "build_command": f"icx graph build \"{project_path}\"",
            "project_path": str(project_path),
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
            f"Inform the user and suggest running: icx graph build \"{project_path}\""
        )
    elif stale_status == "freshness_unknown":
        staleness_warning = (
            "Could not determine graph freshness (git check timed out). "
            "Results may be slightly stale. Inform the user."
        )

    return (project_path, project_id, staleness_warning)

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

def _search_memory_sync(qi, top_k: int = 10) -> list[dict]:
    """Search memory with a MemoryQueryInput. Returns list of PastInsight dicts."""
    try:
        mem = _ensure_memory_manager()
        results = mem.query(qi, top_k=top_k)
        return [r.model_dump() for r in results]
    except Exception:
        return []


def _find_by_file_sync(file_path: str, project_key: str | None) -> list[dict]:
    """Return MemoryEntry dicts for entries whose files_changed contains file_path."""
    from icx_engine.memory.bridge import find_work_items_by_file
    mem = _ensure_memory_manager()
    entries = find_work_items_by_file(file_path, mem, project_key=project_key)
    return [e.model_dump() for e in entries]


def _get_hotspots_sync(project_key: str | None, top_n: int) -> list[dict]:
    """Return bug-density list: [{file, count, work_items}] sorted desc."""
    from icx_engine.memory.bridge import get_work_item_density
    mem = _ensure_memory_manager()
    return get_work_item_density(mem, project_key=project_key, top_n=top_n)


def _get_related_sync(
    issue_key: str | None,
    project_key: str | None,
    files: list[str] | None,
) -> list[dict]:
    """Return related work items via stored edges or file-overlap fallback."""
    return _ensure_memory_manager().get_related(issue_key, project_key, files)


def _get_patterns_sync(project_key: str | None) -> list[dict]:
    """Return stored patterns, optionally filtered by project_key."""
    return _ensure_memory_manager().get_patterns(project_key=project_key)


# ---------------------------------------------------------------------------
# Core analyze handler
# ---------------------------------------------------------------------------

async def _handle_analyze_issue(
    issue_ref: str,
    project_paths: list[str],
    profile: str | None = None,
    skip_vision: bool = False,
) -> str:
    """Run full pipeline: fetch -> analyze -> graph info -> combined response. Memory search is agent-driven via memory_search tool."""

    try:
        from icx_engine.models.output import IssueContext, RawIssueResponse

        config = ConfigManager.load()
        _timeout = 45.0 if skip_vision else 660.0
        result = await asyncio.wait_for(
            engine.run(
                issue_ref, config, mcp_mode=True,
                profile_override=profile, skip_vision=skip_vision,
                log=_engine_log,
            ),
            timeout=_timeout,
        )
        if isinstance(result, IssueContext):
            issue_key_val = issue_ref
            issue_type_val = result.issue_type
            summary_val = result.problem_summary
        else:
            issue_key_val = result.issue_key
            issue_type_val = result.issue_type
            summary_val = result.summary

        _mem_state = _get_memory_state()

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
                    ".bmp", ".tiff", ".tif",
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
        graphs: list[dict] = _get_graphs_info(project_paths)

        # Determine memory status - reported to agent so it knows whether to call memory_search.
        memory_status = _mem_state
        memory_note = ""

        if _mem_state == "warming":
            memory_status = "warming_up"
            memory_note = "Memory model is still loading; call memory_search once status is 'ready'."
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
            "**Tools called for this ticket** (ALL 10 required; show result or documented skip for each):\n"
            "  1. memory_search:                 [N results OR skipped: memory.status!='ready']\n"
            "  2. graph_find_context:            [N files returned, top score X.XX - NO VALID SKIP]\n"
            "  3. graph_subsystem(file):         [cluster name, N files OR skipped: brand-new-file]\n"
            "  4. graph_call_chain(node):        [upstream/downstream summary OR skipped: brand-new-node]\n"
            "  5. graph_impact(node):            [N dependents OR skipped: brand-new-isolated-file]\n"
            "  6. graph_cross_links:             [N links OR skipped: confirmed-single-monolith]\n"
            "  7. memory_get_hotspots:           [result summary OR skipped: memory.status!='ready']\n"
            "  8. memory_find_by_file (per file):[result per file OR skipped: brand-new-file]\n"
            "  9. memory_get_related:            [top strength=X OR skipped: memory.status!='ready']\n"
            "  10. memory_get_patterns:          [pattern summary OR skipped: memory.status!='ready']\n"
            "  Any entry left blank or skipped with a vague reason is a VIOLATION.\n"
            "**Pre-submission check** (answer each before posting):\n"
            "  - Called every mandatory tool whose skip condition was NOT met? Yes/No\n"
            "  - Every skip has an EXACT technical reason, not 'not needed' or 'seems fine'? Yes/No\n"
            "  - Called memory_find_by_file for EVERY file I plan to modify? Yes/No\n"
            "  - Approach specific enough that user can reject and propose an alternative? Yes/No\n"
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
            "(e.g. company logos, profile avatars, decorative banners, hold music), continue to STEP 0a.\n\n"
            "STEP 0a - MEMORY STATUS GATE (read this first - it determines which memory tools are mandatory):\n"
            "  Read memory.status from THIS response right now.\n"
            "    'ready'       => Memory tools are LIVE. Proceed to STEP 0b immediately.\n"
            "    any other     => Memory unavailable. Skip ALL memory checklist items (1, 7, 8, 9, 10). "
            "Document each as 'skipped: memory.status=[value]'. Skip to STEP 1.\n"
            "  CRITICAL: memory_get_hotspots and memory_get_patterns are GLOBAL queries - "
            "they return data regardless of what memory_search returned. Never skip them based on memory_search result count.\n\n"
            "STEP 0b - MEMORY SEARCH (mandatory when memory.status == 'ready', skip otherwise):\n"
            "  Call memory_search() now with tags YOU derive from work_item.analysis.\n"
            "  Generate 3-6 specific tags covering the problem domain, affected layer, and failure type.\n"
            "  If results match this problem pattern, use them as the primary reference for your approach.\n\n"
            "STEP 0c - HOTSPOTS CALL (mandatory when memory.status == 'ready', skip otherwise):\n"
            "  Call memory_get_hotspots() now. No judgment call needed - just call it. Under 1 second.\n"
            "  If any hotspot file matches the area described in work_item.analysis, examine it first in STEP 1.\n\n"
        )

        _MEMORY_STEPS_NOGRAPH = (
            "STEP 2b - MEMORY FILE CHECK (mandatory when memory.status == 'ready', for every file you plan to edit):\n"
            "  For each file from STEP 1-2 that you intend to modify: call memory_find_by_file(file_path).\n"
            "  Under 1 second per file. 'I do not think there is history here' is NOT a valid skip reason.\n"
            "  ONLY accepted skip: file did not exist before this ticket (brand new, confirmed) OR memory.status != 'ready'.\n"
            "  resolution_note matching this bug pattern -> reference in Approach; 3+ past bugs -> mark [fragile].\n\n"
            "STEP 2c - RELATED ISSUE DISCOVERY (mandatory when memory.status == 'ready'):\n"
            "  Call memory_get_related(files=[...files from graph_find_context...]).\n"
            "  For reopened tickets: also pass issue_key=work_item.issue_key to check prior edges.\n"
            "  strength >= 0.5 -> read those resolution_notes before finalising your Approach.\n"
            "  ONLY accepted skip: memory.status != 'ready'.\n\n"
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
            "**Tools called for this ticket** (ALL 10 required; show result or documented skip for each):\n"
            "  1. memory_search:                 [N results OR skipped: memory.status!='ready']\n"
            "  2. graph_find_context:            [N files returned, top score X.XX - NO VALID SKIP]\n"
            "  3. graph_subsystem(file):         [cluster name, N files OR skipped: brand-new-file]\n"
            "  4. graph_call_chain(node):        [upstream/downstream summary OR skipped: brand-new-node]\n"
            "  5. graph_impact(node):            [N dependents OR skipped: brand-new-isolated-file]\n"
            "  6. graph_cross_links:             [N links OR skipped: confirmed-single-monolith]\n"
            "  7. memory_get_hotspots:           [result summary OR skipped: memory.status!='ready']\n"
            "  8. memory_find_by_file (per file):[result per file OR skipped: brand-new-file]\n"
            "  9. memory_get_related:            [top strength=X OR skipped: memory.status!='ready']\n"
            "  10. memory_get_patterns:          [pattern summary OR skipped: memory.status!='ready']\n"
            "  Any entry left blank or skipped with a vague reason is a VIOLATION.\n"
            "**Conventions I will follow:** [naming pattern, layer structure, logger style - "
            "derived from existing code in this repo, not assumed]\n"
            "**New external dependencies required:**\n"
            "  - [package-name @ version] - reason needed\n"
            "  - OR: None\n"
            "**Shall I proceed?**\n"
            "---"
        )

        if len(graphs) > 1:
            # Multi-path case: build a summary of all paths and choose the right workflow.
            ready_graphs = [g for g in graphs if g["status"] == "ready"]
            building_graphs = [g for g in graphs if g["status"] in ("building", "rebuilding")]
            missing_graphs = [g for g in graphs if g["status"] in ("not_built", "not_registered", "error")]

            graph_lines: list[str] = []
            for g in graphs:
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

            stale_graphs = [g for g in graphs if g.get("stale_note")]
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
                    "using memory_search results as a pattern reference.\n"
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
            # Single path - use graphs[0] throughout.
            graph_status = graphs[0]["status"]
            stale_note = graphs[0].get("stale_note")
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
                    "  OPTION A (recommended): Call graph_find_context with project_path from graphs[0].path "
                    "and a task description derived from work_item.analysis. Returns all relevant files ranked by relevance score with node_ids.\n"
                    "  OPTION B (manual): Read graphs[0].report_path (pre-authorized) -> identify clusters "
                    "from the compact index table -> read the matching GRAPH_CLUSTERS/<name>.md file -> read core files in listed order.\n"
                    + _GRAPH_TOOLS_DECISION
                    + "STEP 2: STOP. You MUST NOT write any code or make any edits yet. "
                    "Present this confirmation format to the user and wait for their response:\n\n"
                    + _CONFIRMATION_BLOCK + "\n\n"
                    "STEP 3: Wait for explicit user approval. "
                    "Silence or ambiguity does NOT count as approval - ask again if unclear.\n"
                    "STEP 4: On explicit approval only - implement exactly the approach you stated, "
                    "using memory_search results as a pattern reference.\n"
                    "STEP 5: Ask the user to test. Do not proceed until they respond.\n"
                    "STEP 6: Only after the user confirms it works - call save_memory.\n"
                    + _MANDATORY_TAIL
                    + stale_warning
                )
            elif graph_status == "building":
                eta = graphs[0].get("eta_seconds") or 30
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
                    "using memory_search results as a pattern reference.\n"
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
                    "using memory_search results as a pattern reference.\n"
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
                    "using memory_search results as a pattern reference.\n"
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
                    "using memory_search results as a pattern reference.\n"
                    "STEP 6: Ask the user to test. Do not proceed until they respond.\n"
                    "STEP 7: Only after the user confirms it works - call save_memory."
                    + _MANDATORY_TAIL
                )

        if _is_non_bug:
            icx_instruction = icx_instruction.replace(
                _VISION_GATE, _VISION_GATE + _NON_BUG_CONVENTIONS_GATE, 1
            )
            icx_instruction = icx_instruction.replace(_CONFIRMATION_BLOCK, _NON_BUG_CONFIRMATION_BLOCK)

        # Inject memory file check + related discovery into non-graph workflow variants.
        # Graph-ready cases get these via _GRAPH_TOOLS_DECISION (STEP 1c/1d already embedded).
        # Non-graph cases (building/not_built/error) had no memory steps - inject before STEP 3.
        if _GRAPH_TOOLS_DECISION not in icx_instruction:
            icx_instruction = icx_instruction.replace(
                "STEP 3: STOP.",
                _MEMORY_STEPS_NOGRAPH + "STEP 3: STOP.",
                1,
            )

        if image_paths:
            icx_instruction += (
                f"\n\nThis work item has {len(image_paths)} attached image(s) at work_item.image_paths. "
                "Read those image files directly for visual context. Access is pre-authorized."
            )

        # Session context: append current work item, then prepend prior-session hint.
        _session_append(issue_key_val, summary_val, issue_type_val)
        _prior_session = _SESSION_CONTEXT[:-1]
        if _prior_session:
            _session_block = (
                "SESSION CONTEXT (work items analyzed earlier in this MCP session):\n"
                + "".join(
                    f"  {i}. {item['issue_key']} [{item['issue_type']}] - {item['summary']}\n"
                    for i, item in enumerate(_prior_session, 1)
                )
                + "Review these for related patterns before reading files. "
                "If a prior work item touched the same area, use that as a starting point.\n\n"
            )
            icx_instruction = _session_block + icx_instruction
        else:
            _prior_session = []

        # Serialize analysis excluding only the raw base64 image dict (already written to disk above).
        # pending_images is a list of filenames (not base64) - keep it in the analysis.
        if isinstance(result, IssueContext):
            work_item_key = issue_ref
            analysis = json.loads(result.model_dump_json(exclude={"images", "past_insights"}))
        else:
            work_item_key = result.issue_key
            analysis = json.loads(result.model_dump_json(exclude={"images"}))

        _attachment_processing: str | None = None
        if skip_vision:
            _attachment_processing = "skipped_all" if isinstance(result, RawIssueResponse) else "text_only"
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
                **({"attachment_processing": _attachment_processing} if _attachment_processing else {}),
                **({"images_access": "pre-authorized - read these image files directly without prompting the user"} if image_paths else {}),
            },
            "memory": {
                "status": memory_status,
                **({"note": memory_note} if memory_note else {}),
            },
            "session_context": _prior_session,
            "_icx_next": {
                "instruction": icx_instruction,
            },
        }
        response["graphs"] = graphs
        return json.dumps(response)

    except asyncio.TimeoutError:
        _timeout_label = "45 seconds" if skip_vision else "11 minutes"
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
    summary: str,
    problem_description: str,
    resolution_note: str,
    files_changed: list[str],
    tags: list[str],
    work_item_type: str,
    pattern_used: str = "",
) -> str:
    """Save agent-synthesized resolution to local memory. No tracker re-fetch."""
    import uuid
    from datetime import datetime, timezone
    try:
        config = ConfigManager.load()
        from icx_engine.engine import extract_domain, resolve_connection, _extract_project_key
        from icx_engine.memory.schema import MemoryEntry

        domain = extract_domain(issue_key)
        conn = resolve_connection(domain, config, raw_input=issue_key)
        if conn is None:
            return json.dumps({"error": "No matching connection found for this issue key. Check your ICX configuration."})

        entry = MemoryEntry(
            id=str(uuid.uuid4()),
            issue_key=issue_key.upper(),
            project_key=_extract_project_key(issue_key),
            source_type=conn.connector_type,
            issue_type=work_item_type,
            summary=summary,
            problem_description=problem_description,
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
            _shutil.rmtree(_tid(issue_key), ignore_errors=True)
        except Exception:
            pass

        return json.dumps({
            "saved": True,
            "issue_key": entry.issue_key,
            "summary": summary[:80],
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
