"""MCP tool surface for the codebase knowledge graph. Owns its own Tool()
definitions and dispatch function - mcp_server.py's _list_tools()/_call_tool()
get a few additive lines only, no restructuring."""

from __future__ import annotations

import asyncio
import json
import logging

from mcp.types import TextContent, Tool

from icx_engine.mcp_server import (
    _cached_querier, _degraded_graph_response, _load_querier_simple, _resolve_graph_path,
)

_log = logging.getLogger(__name__)

_GRAPH_CONTEXT_TOOL = "graph_find_context"
_GRAPH_SUBSYSTEM_TOOL = "graph_subsystem"
_GRAPH_CHAIN_TOOL = "graph_call_chain"
_GRAPH_IMPACT_TOOL = "graph_impact"
_GRAPH_CROSS_LINKS_TOOL = "graph_cross_links"
_GRAPH_IMPORTANT_NODES_TOOL = "graph_important_nodes"
_GRAPH_BLAST_RADIUS_TOOL = "graph_blast_radius"
_GRAPH_CYCLES_TOOL = "graph_cycles"
_GRAPH_DEAD_CODE_TOOL = "graph_dead_code"
_GRAPH_OWNERSHIP_TOOL = "graph_ownership"

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


_GRAPH_IMPORTANT_NODES_DESCRIPTION = """\
USE WHEN: Starting work on an unfamiliar codebase, or planning a refactor and you need to know which files carry the highest blast radius BEFORE any file exploration.
Given top_k, returns files and functions ranked by architectural importance (0.50*PageRank + 0.30*degree + 0.20*betweenness centrality).

RETURNS: [{file, name, pagerank, betweenness, importance}] sorted by importance descending.
VALUE: Tells you where to look FIRST. High-importance nodes are touched by the most code paths - a change there ripples everywhere. Without this call, you may spend time on a low-importance file while missing the real architectural core that affects everything else.

WHEN TO CALL:
  - Before graph_find_context when you have no prior context on a codebase
  - When planning a refactor to identify which files are highest-risk candidates
  - When asked "what is the most critical part of this system?"
SKIP ONLY IF: You already know the architectural hotspots from prior graph exploration in this session.

EXAMPLE: graph_important_nodes(project_path="...") ->
  1. src/auth/token.py [importance=0.92, pagerank=0.95, betweenness=0.84]
  2. src/db/session.py [importance=0.88, pagerank=0.91, betweenness=0.78]
  -> These two files affect the most code paths. Change them with maximum caution and call graph_blast_radius before committing.

RUNTIME: ~1 second.
Graph must be built (icx graph build <name>) and graph.status == "ready".
project_path must be the absolute path to the project root.
If the tool returns an error with action_required, follow the action exactly before retrying.\
"""


_GRAPH_BLAST_RADIUS_DESCRIPTION = """\
MANDATORY BEFORE COMMITTING changes to any shared or high-importance file.
USE WHEN: You have decided which files to change and need to verify the full scope of impact - direct dependents, transitive dependents, risk level, and any files your changelist is missing.

RETURNS: {changed_files, direct_dependents, transitive_dependents, risk_score (0.0-1.0), missing_changes, total_affected}
  direct_dependents: files that directly import or call your changed files
  transitive_dependents: files reachable from direct dependents (recursive)
  risk_score: fraction of high-importance nodes in the blast zone (0=low, 1=critical)
  missing_changes: files that historically co-changed with your target files but are absent from your changelist - these are the regressions you did not know you had.

CRITICAL DIFFERENCE FROM graph_impact:
  graph_blast_radius = file-level pre-merge scope check (what is the full blast zone? what am I missing?)
  graph_impact       = node-level pre-refactor dependency check (who directly depends on this node?)

VALUE: missing_changes is the most important field. If a file co-appeared with your changed file in 8 of 10 prior commits but is absent from your changelist, there is a high probability the change is incomplete.

EXAMPLE: graph_blast_radius(changed_files=["src/auth/token.py"]) ->
  direct_dependents: [src/middleware/auth.py, src/api/login.py]
  transitive_dependents: [src/api/orders.py, src/api/profile.py]
  risk_score: 0.72 (HIGH - 3 of 4 high-importance nodes affected)
  missing_changes: [src/auth/session.py] (co-changed in 8 of last 10 commits with token.py)
  -> src/auth/session.py almost certainly needs to change too. Check it before creating the PR.

RUNTIME: ~3 seconds.
project_path must be the absolute path to the project root.
If the tool returns an error with action_required, follow the action exactly before retrying.\
"""


_GRAPH_CYCLES_DESCRIPTION = """\
USE WHEN: Debugging circular import errors, investigating why a module cannot be loaded, or auditing a codebase for architectural debt during a refactor planning session.
Returns circular dependency chains found only in structural edges (imports, calls, inheritance). Co-change and event-driven edges are excluded from cycle detection.

RETURNS: {cycles: [[file, file, ..., file], ...], cycle_count: N}
Each cycle is a file list where the first and last file are the same - the full loop.
VALUE: Circular imports are a leading cause of import errors, test isolation failures, and refactoring deadlocks. Each returned cycle is the exact loop you need to break - typically by extracting shared types or interfaces into a new file with no upward dependencies.
SKIP ONLY IF: The task is purely additive (new file, no existing structure change) and no circular import error is present.

EXAMPLE: graph_cycles(project_path="...") ->
  cycle_count: 2
  cycles: [
    ["src/auth/token.py", "src/auth/session.py", "src/auth/token.py"],
    ["src/billing/invoice.py", "src/billing/payment.py", "src/billing/invoice.py"]
  ]
  -> Break the auth cycle by extracting shared types to src/auth/models.py with no imports from token or session.

RUNTIME: ~2 seconds.
project_path must be the absolute path to the project root.
If the tool returns an error with action_required, follow the action exactly before retrying.\
"""


_GRAPH_DEAD_CODE_DESCRIPTION = """\
USE WHEN: Cleaning up a codebase, auditing for unused modules before a major refactor, or reducing package size.
Returns files with zero incoming edges - no imports, no callers, no references from any other indexed file.
Excludes entry points (main.py, app.py, server.go, Application.java, etc.) and test files, which have no callers by design.

RETURNS: {dead_code_candidates: [{file, node_count}], count: N} sorted by file path.
  node_count: how many symbols (classes, functions) are in that file - higher = more code to delete.
VALUE: Dead files carry maintenance cost with zero business value. Files with high node_count are the largest dead modules - remove them first. Verify each candidate manually: some files are loaded dynamically at runtime and will not appear in static analysis.
SKIP ONLY IF: The task is adding new code with no cleanup requirement.

EXAMPLE: graph_dead_code(project_path="...") ->
  [{file: "src/utils/legacy_csv.py", node_count: 12},
   {file: "src/integrations/old_sms.py", node_count: 4}]
  -> legacy_csv.py has 12 symbols and no callers. Confirm no dynamic loading, then delete.

RUNTIME: ~1 second.
project_path must be the absolute path to the project root.\
"""


_GRAPH_OWNERSHIP_DESCRIPTION = """\
USE WHEN: A change crosses team boundaries, you need to know who must review specific files, or you are adding a new dependency into another team's module.
Reads CODEOWNERS from project root, .github/CODEOWNERS, or docs/CODEOWNERS (in that order). Returns codeowners_found: false if no file exists.

RETURNS: {owners: ["@team", "@user", ...], owned_files: [...], cross_owner_dependencies: [{from, to, to_owners, edge_type, confidence}], codeowners_found: bool}
  owners: teams/users who own the queried file
  owned_files: all graph files owned by the same owners
  cross_owner_dependencies: edges where your team's code calls into another team's code - these require cross-team review
VALUE: Prevents missing required reviewers and ownership violations. cross_owner_dependencies reveals every interface where your team's code depends on another team's - each entry is a potential breaking contract that needs sign-off from both sides.
SKIP ONLY IF: No CODEOWNERS file exists (confirmed by codeowners_found: false in a prior call).

EXAMPLE: graph_ownership(file_path="src/billing/invoice.py", project_path="...") ->
  owners: ["@billing-team"]
  owned_files: [src/billing/invoice.py, src/billing/payment.py, src/billing/models.py]
  cross_owner_dependencies: [{from: "src/billing/invoice.py", to: "src/auth/token.py", to_owners: ["@security-team"], edge_type: "imports"}]
  -> This change requires review from @security-team because billing imports from auth. Notify them before merging.

RUNTIME: ~1 second.
project_path must be the absolute path to the project root.
If the tool returns an error with action_required, follow the action exactly before retrying.\
"""


GRAPH_TOOLS: list[Tool] = [
    Tool(
        name=_GRAPH_IMPORTANT_NODES_TOOL,
        description=_GRAPH_IMPORTANT_NODES_DESCRIPTION,
        inputSchema={
            "type": "object",
            "properties": {
                "project_path": {"type": "string"},
                "top_k": {
                    "type": "integer",
                    "default": 10,
                    "description": "How many nodes to return (default 10)",
                },
            },
            "required": ["project_path"],
        },
            annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
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
            annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
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
            annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    ),
    Tool(
        name=_GRAPH_OWNERSHIP_TOOL,
        description=_GRAPH_OWNERSHIP_DESCRIPTION,
        inputSchema={
            "type": "object",
            "properties": {
                "project_path": {"type": "string"},
                "file_path": {
                    "type": "string",
                    "description": "File to look up ownership for",
                },
            },
            "required": ["project_path", "file_path"],
        },
            annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
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
            annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
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
            annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
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
                "file_path": {
                    "type": "string",
                    "description": "Optional absolute path to a file in this project. "
                    "When given, also returns co_changed_files: files historically "
                    "committed together with this file (from the cochange resolver).",
                },
            },
            "required": ["project_path"],
        },
            annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    ),
    Tool(
        name=_GRAPH_BLAST_RADIUS_TOOL,
        description=_GRAPH_BLAST_RADIUS_DESCRIPTION,
        inputSchema={
            "type": "object",
            "properties": {
                "project_path": {"type": "string"},
                "changed_files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of changed file paths (relative or absolute)",
                },
                "max_depth": {
                    "type": "integer",
                    "default": 5,
                    "description": "Maximum traversal depth (default 5)",
                },
                "min_confidence": {
                    "type": "number",
                    "default": 0.3,
                    "description": "Minimum edge confidence to follow (default 0.3)",
                },
            },
            "required": ["project_path", "changed_files"],
        },
            annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    ),
    Tool(
        name=_GRAPH_CYCLES_TOOL,
        description=_GRAPH_CYCLES_DESCRIPTION,
        inputSchema={
            "type": "object",
            "properties": {
                "project_path": {"type": "string"},
                "max_cycles": {
                    "type": "integer",
                    "default": 20,
                    "description": "Maximum number of cycles to return (default 20)",
                },
            },
            "required": ["project_path"],
        },
            annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    ),
    Tool(
        name=_GRAPH_DEAD_CODE_TOOL,
        description=_GRAPH_DEAD_CODE_DESCRIPTION,
        inputSchema={
            "type": "object",
            "properties": {
                "project_path": {"type": "string"},
            },
            "required": ["project_path"],
        },
            annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    ),
]


async def dispatch_graph_tool(name: str, arguments: dict) -> list[TextContent] | None:
    args = arguments or {}

    if name in (_GRAPH_CONTEXT_TOOL, _GRAPH_CHAIN_TOOL, _GRAPH_IMPACT_TOOL, _GRAPH_SUBSYSTEM_TOOL):
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
            from dataclasses import asdict as _asdict

            _result = _resolve_graph_path(_raw_path)
            if isinstance(_result, dict):
                return _result
            _project_path, _project_id, staleness_warning = _result

            graph_json = _st.graph_path(_project_id)
            if not graph_json.exists():
                return _degraded_graph_response(
                    code="NO_GRAPH",
                    project_path=_project_path,
                    warn_user=(
                        f"The codebase graph is not built for '{_project_path}', so I'm "
                        "answering from direct file search (grep/read) instead. For richer "
                        f"results, build the graph: icx graph build \"{_project_path}\""
                    ),
                )

            q = _cached_querier(graph_json)
            if name == _GRAPH_CONTEXT_TOOL:
                results = q.find_context(
                    task=task_str,
                    token_budget=_token_budget,
                    min_confidence=_min_confidence,
                    source_root=_project_path,
                )
                all_results = [_asdict(r) for r in results]
                # find_context's own token_budget param is a documented no-op ("accepted for
                # backward compatibility but unused" - graph/query.py:179) - it returns every
                # scored file unconditionally. Real bug: this produced 700K+ char single-call
                # responses that had to be spilled to disk and parsed externally. The actual
                # cap is applied here, at the MCP boundary, via a coarse ~4-chars-per-token
                # estimate on each result's serialized size - a rough, standard heuristic, not
                # a real tokenizer count, but enough to keep this call's own token_budget
                # honest without changing find_context's ranking/selection behavior itself.
                kept: list[dict] = []
                running_chars = 0
                char_budget = max(_token_budget, 1) * 4
                for r in all_results:
                    r_chars = len(json.dumps(r))
                    if kept and running_chars + r_chars > char_budget:
                        break
                    kept.append(r)
                    running_chars += r_chars
                payload = {
                    "status": "ok", "project_path": str(_project_path), "results": kept,
                    "total_matched": len(all_results),
                }
                if len(kept) < len(all_results):
                    payload["truncated"] = True
                    payload["note"] = (
                        f"{len(all_results) - len(kept)} more result(s) omitted to stay within "
                        f"token_budget ({_token_budget}) - raise token_budget to see more, or "
                        "narrow the task description to rank fewer files higher."
                    )
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

        _cochange_file = (args.get("file_path") or "").strip()

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

            if _cochange_file:
                try:
                    _graph_json = _st.graph_path(_project_id)
                    if _graph_json.exists():
                        _q = _cached_querier(_graph_json)
                        data["co_changed_files"] = _q.get_cochange_partners(_cochange_file)
                    else:
                        data["co_changed_files"] = []
                except Exception:
                    data["co_changed_files"] = []

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

    if name == _GRAPH_IMPORTANT_NODES_TOOL:
        project_path = args.get("project_path", "")
        if not isinstance(project_path, str) or not project_path.strip():
            return [TextContent(type="text", text=json.dumps({
                "status": "error",
                "code": "NO_PATH",
                "message": "project_path is required.",
            }))]
        top_k_raw = args.get("top_k", 10)
        try:
            top_k = max(1, min(100, int(top_k_raw)))
        except (TypeError, ValueError):
            top_k = 10

        def _run_important_nodes() -> dict:
            _r = _load_querier_simple(project_path)
            if isinstance(_r, dict):
                return _r
            q, _proj = _r
            important = q.get_important_nodes(top_k)
            result_nodes = [
                {
                    "file": n.get("source_file", n.get("file", "")),
                    "name": n.get("label", n.get("id", "")),
                    "pagerank": n.get("pagerank", 0.0),
                    "betweenness": n.get("betweenness", 0.0),
                    "importance": n.get("importance", 0.0),
                }
                for n in important
            ]
            return {"important_nodes": result_nodes, "total": len(result_nodes)}

        try:
            loop = asyncio.get_running_loop()
            result_dict = await loop.run_in_executor(None, _run_important_nodes)
            return [TextContent(type="text", text=json.dumps(result_dict))]
        except Exception as exc:
            return [TextContent(type="text", text=json.dumps({"error": str(exc)}))]

    if name == _GRAPH_BLAST_RADIUS_TOOL:
        project_path = args.get("project_path", "")
        if not isinstance(project_path, str) or not project_path.strip():
            return [TextContent(type="text", text=json.dumps({
                "status": "error", "code": "NO_PATH",
                "message": "project_path is required.",
                "action_required": "ask_user_for_path",
            }))]
        changed_files_raw = args.get("changed_files", [])
        if not isinstance(changed_files_raw, list):
            return [TextContent(type="text", text=json.dumps(
                {"error": "changed_files must be a list of strings."}
            ))]
        changed_files = [str(f) for f in changed_files_raw]
        try:
            max_depth = max(1, min(20, int(args.get("max_depth", 5))))
        except (TypeError, ValueError):
            max_depth = 5
        try:
            min_confidence = float(args.get("min_confidence", 0.3))
            min_confidence = max(0.0, min(1.0, min_confidence))
        except (TypeError, ValueError):
            min_confidence = 0.3

        def _run_blast_radius() -> dict:
            _r = _load_querier_simple(project_path)
            if isinstance(_r, dict):
                return _r
            q, _proj = _r
            return q.get_blast_radius(changed_files, max_depth=max_depth, min_confidence=min_confidence)

        try:
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(None, _run_blast_radius)
            return [TextContent(type="text", text=json.dumps(result))]
        except Exception as exc:
            return [TextContent(type="text", text=json.dumps({"error": str(exc)}))]

    if name == _GRAPH_CYCLES_TOOL:
        project_path = args.get("project_path", "")
        if not isinstance(project_path, str) or not project_path.strip():
            return [TextContent(type="text", text=json.dumps({
                "status": "error", "code": "NO_PATH",
                "message": "project_path is required.",
                "action_required": "ask_user_for_path",
            }))]
        try:
            max_cycles = max(1, min(100, int(args.get("max_cycles", 20))))
        except (TypeError, ValueError):
            max_cycles = 20

        def _run_cycles() -> dict:
            _r = _load_querier_simple(project_path)
            if isinstance(_r, dict):
                return _r
            q, _proj = _r
            cycles = q.get_cycles(max_cycles=max_cycles)
            return {"cycles": cycles, "cycle_count": len(cycles)}

        try:
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(None, _run_cycles)
            return [TextContent(type="text", text=json.dumps(result))]
        except Exception as exc:
            return [TextContent(type="text", text=json.dumps({"error": str(exc)}))]

    if name == _GRAPH_DEAD_CODE_TOOL:
        project_path = args.get("project_path", "")
        if not isinstance(project_path, str) or not project_path.strip():
            return [TextContent(type="text", text=json.dumps({
                "status": "error", "code": "NO_PATH",
                "message": "project_path is required.",
                "action_required": "ask_user_for_path",
            }))]

        def _run_dead_code() -> dict:
            _r = _load_querier_simple(project_path)
            if isinstance(_r, dict):
                return _r
            q, _proj = _r
            dead = q.get_dead_code()
            return {"dead_code_candidates": dead, "count": len(dead)}

        try:
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(None, _run_dead_code)
            return [TextContent(type="text", text=json.dumps(result))]
        except Exception as exc:
            return [TextContent(type="text", text=json.dumps({"error": str(exc)}))]

    if name == _GRAPH_OWNERSHIP_TOOL:
        project_path = args.get("project_path", "")
        if not isinstance(project_path, str) or not project_path.strip():
            return [TextContent(type="text", text=json.dumps({
                "status": "error", "code": "NO_PATH",
                "message": "project_path is required.",
                "action_required": "ask_user_for_path",
            }))]
        file_path = args.get("file_path", "")
        if not isinstance(file_path, str) or not file_path.strip():
            return [TextContent(type="text", text=json.dumps(
                {"error": "file_path must be a non-empty string."}
            ))]
        file_path = file_path.strip()
        _fp_norm = file_path.replace("\\", "/")
        if (
            "\x00" in file_path
            or any(p == ".." for p in _fp_norm.split("/"))
            or _fp_norm.startswith("/")
            or (len(file_path) > 1 and file_path[1] == ":")
        ):
            return [TextContent(type="text", text=json.dumps({
                "error": "file_path must be a relative path with no directory traversal."
            }))]

        def _run_ownership() -> dict:
            _r = _load_querier_simple(project_path)
            if isinstance(_r, dict):
                return _r
            q, _proj = _r
            return q.get_ownership(file_path, str(_proj))

        try:
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(None, _run_ownership)
            return [TextContent(type="text", text=json.dumps(result))]
        except Exception as exc:
            return [TextContent(type="text", text=json.dumps({"error": str(exc)}))]

    return None
