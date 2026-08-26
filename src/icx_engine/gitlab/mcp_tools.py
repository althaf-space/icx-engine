"""MCP tool surface for GitLab MR/commit history lookups. Owns its own Tool()
definitions and dispatch function - mcp_server.py's _list_tools()/_call_tool()
get a few additive lines only, no restructuring."""
from __future__ import annotations

import json
import re
from pathlib import Path

from mcp.types import TextContent, Tool

from icx_engine.confirm import issue_token, verify_token
from icx_engine.config_manager import ConfigManager
from icx_engine.git.gitcmd import remote_url
from icx_engine.gitlab.client import GitLabClient, project_path_from_remote_url
from icx_engine.gitlab.service import classify_merge_status, diagnose_merge_refusal
from icx_engine.mcp_server import _ICX_FALLBACK

_LIST_MRS_TOOL = "gitlab_list_merge_requests"
_MR_CHANGES_TOOL = "gitlab_mr_changes"
_LIST_COMMITS_TOOL = "gitlab_list_commits"
_COMPARE_TOOL = "gitlab_compare"
_LIST_TAGS_TOOL = "gitlab_list_tags"
_LIST_BRANCHES_TOOL = "gitlab_list_branches"
_LIST_PIPELINES_TOOL = "gitlab_list_pipelines"
_PIPELINE_STATUS_TOOL = "gitlab_pipeline_status"
_JOB_LOG_TOOL = "gitlab_job_log"
_CLOSE_MR_TOOL = "gitlab_close_merge_request"
_REOPEN_MR_TOOL = "gitlab_reopen_merge_request"
_MERGE_MR_TOOL = "gitlab_merge_merge_request"
_REFRESH_MERGE_STATUS_TOOL = "gitlab_refresh_merge_status"

GITLAB_TOOLS: list[Tool] = [
    Tool(
        name=_LIST_MRS_TOOL,
        description=(
            "USE WHEN the user asks who merged a PR/MR, what merge requests exist, or wants a "
            "list of recent MRs for a GitLab project: MUST call gitlab_list_merge_requests with "
            "either project (a GitLab project path/ID) or repo_path (a local checkout to derive "
            "the project from). state defaults to 'merged' (one of merged/opened/closed), "
            "target_branch narrows to MRs targeting that branch, limit defaults to 20. Requires "
            "an active GitLab connection."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "repo_path": {"type": "string"},
                "state": {"type": "string"},
                "target_branch": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": [],
        },
            annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True},
    ),
    Tool(
        name=_MR_CHANGES_TOOL,
        description=(
            "USE WHEN the user wants the actual file diffs for one specific merge request by its "
            "iid: MUST call gitlab_mr_changes with mr_iid plus either project or repo_path. "
            "Requires an active GitLab connection."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "repo_path": {"type": "string"},
                "mr_iid": {"type": "integer"},
            },
            "required": ["mr_iid"],
        },
            annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True},
    ),
    Tool(
        name=_LIST_COMMITS_TOOL,
        description=(
            "USE WHEN the user wants commit history for a GitLab project (optionally scoped to a "
            "ref, a file path, or a since date): MUST call gitlab_list_commits with either "
            "project or repo_path. limit defaults to 20. Requires an active GitLab connection."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "repo_path": {"type": "string"},
                "ref": {"type": "string"},
                "path": {"type": "string"},
                "since": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": [],
        },
            annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
    ),
    Tool(
        name=_COMPARE_TOOL,
        description=(
            "USE WHEN the user wants to compare two refs (branches, tags, or commits) on a "
            "GitLab project: MUST call gitlab_compare with from_ref, to_ref, plus either project "
            "or repo_path. Requires an active GitLab connection."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "repo_path": {"type": "string"},
                "from_ref": {"type": "string"},
                "to_ref": {"type": "string"},
            },
            "required": ["from_ref", "to_ref"],
        },
            annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True},
    ),
    Tool(
        name=_LIST_TAGS_TOOL,
        description=(
            "USE WHEN the human wants to see this project's REAL existing tags before creating a "
            "new one: MUST call this FIRST, before git_create_tag, with either project or "
            "repo_path - never invent an environment name or version series from guesswork. "
            "Returns every tag with its target commit and creation date, newest first per GitLab's "
            "own ordering. Read-only, UNGATED. Optional limit/offset to page results. Requires an "
            "active GitLab connection."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project": {"type": "string"}, "repo_path": {"type": "string"},
                "limit": {"type": "integer"}, "offset": {"type": "integer"},
            },
            "required": [],
        },
            annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
    ),
    Tool(
        name=_LIST_BRANCHES_TOOL,
        description=(
            "USE WHEN the human wants to see this project's REAL existing branches - e.g. before "
            "proposing a parent/base branch, or to confirm an exact branch name rather than "
            "guessing between similarly-named ones: MUST call this instead of inventing a branch "
            "name. Returns name/protected/default/last-commit-date per branch. `search` (if given) "
            "is GitLab's own server-side substring filter. Read-only, UNGATED. Optional limit/offset "
            "to page results. Requires an active GitLab connection."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project": {"type": "string"}, "repo_path": {"type": "string"},
                "search": {"type": "string"},
                "limit": {"type": "integer"}, "offset": {"type": "integer"},
            },
            "required": [],
        },
            annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
    ),
    Tool(
        name=_LIST_PIPELINES_TOOL,
        description=(
            "USE WHEN the human wants to know whether a pipeline ran (or is running) for a "
            "branch/tag/MR - e.g. right after git_create_mr or git_create_tag, instead of "
            "guessing from timing alone whether a merge refusal means a real conflict or just an "
            "in-progress/not-yet-started pipeline. `ref` (branch or tag name) narrows to that ref; "
            "`status` (e.g. 'running', 'success', 'failed') narrows further. Returns the 20 most "
            "recent matching pipelines. Read-only, UNGATED. Optional limit/offset to page within "
            "those 20. Requires an active GitLab connection."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project": {"type": "string"}, "repo_path": {"type": "string"},
                "ref": {"type": "string"}, "status": {"type": "string"},
                "limit": {"type": "integer"}, "offset": {"type": "integer"},
            },
            "required": [],
        },
            annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
    ),
    Tool(
        name=_PIPELINE_STATUS_TOOL,
        description=(
            "USE WHEN the human wants the detailed status of one specific pipeline by id (found "
            "via gitlab_list_pipelines): returns the pipeline's own status/duration/user PLUS "
            "every job's name/status/stage in one call - the two are always wanted together for "
            "'did this pass, and if not, which job'. Call gitlab_job_log next for a failed job's "
            "actual error output. Read-only, UNGATED. Requires an active GitLab connection."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project": {"type": "string"}, "repo_path": {"type": "string"},
                "pipeline_id": {"type": "integer"},
            },
            "required": ["pipeline_id"],
        },
            annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
    ),
    Tool(
        name=_JOB_LOG_TOOL,
        description=(
            "USE WHEN the human wants to know WHY a specific job failed: returns that job's "
            "plain-text log (found via gitlab_pipeline_status's jobs list). MUST default to "
            "only_errors=true and tail_lines=200 for a first look at a long log - a full raw log "
            "can run to 100K+ characters and blow the response past any usable size; pass "
            "only_errors=false explicitly to see the complete tail instead. strip_ansi (default "
            "true) removes terminal color codes that otherwise clutter every line. Read-only, "
            "UNGATED. Requires an active GitLab connection."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project": {"type": "string"}, "repo_path": {"type": "string"},
                "job_id": {"type": "integer"},
                "tail_lines": {"type": "integer", "description": "Last N lines only. Default 200."},
                "strip_ansi": {"type": "boolean", "description": "Strip ANSI color codes. Default true."},
                "only_errors": {
                    "type": "boolean",
                    "description": "Keep only lines matching common error markers (ERR!, Error, FAILED, "
                                    "fatal:, Traceback, ##[error]). Default true.",
                },
            },
            "required": ["job_id"],
        },
            annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
    ),
    Tool(
        name=_CLOSE_MR_TOOL,
        description=(
            "USE WHEN an MR is stuck with a stale GitLab-side mergeability cache (a refusal that "
            "local git and the pipeline both contradict) and reopening it is the documented fix: "
            "MUST call gitlab_close_merge_request with mr_iid plus either project or repo_path. "
            "CONFIRMATION-GATED: first call (no confirm_token) returns pending_confirmation plus a "
            "token - show the human the MR being closed and why, then call again with confirm_token "
            "once they agree. Does not delete the MR or its branch. Requires an active GitLab "
            "connection."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project": {"type": "string"}, "repo_path": {"type": "string"},
                "mr_iid": {"type": "integer"}, "confirm_token": {"type": "string"},
            },
            "required": ["mr_iid"],
        },
            annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True},
    ),
    Tool(
        name=_REOPEN_MR_TOOL,
        description=(
            "USE WHEN a closed MR needs to come back open - most often right after "
            "gitlab_close_merge_request, to force GitLab to recompute a stale mergeability cache: "
            "MUST call gitlab_reopen_merge_request with mr_iid plus either project or repo_path. "
            "CONFIRMATION-GATED: first call (no confirm_token) returns pending_confirmation plus a "
            "token - show the human which MR is being reopened, then call again with confirm_token "
            "once they agree. Requires an active GitLab connection."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project": {"type": "string"}, "repo_path": {"type": "string"},
                "mr_iid": {"type": "integer"}, "confirm_token": {"type": "string"},
            },
            "required": ["mr_iid"],
        },
            annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True},
    ),
    Tool(
        name=_MERGE_MR_TOOL,
        description=(
            "USE WHEN retrying a merge on an MR that already exists (created by git_create_mr or "
            "found via gitlab_list_merge_requests) - a cheap, standalone merge attempt that does "
            "NOT push, create, or touch local git at all: MUST call gitlab_merge_merge_request "
            "with mr_iid plus either project or repo_path. NOT confirm-gated by ICX - GitLab's own "
            "server-side protected-branch/approval rules are the real backstop for a merge, the "
            "same way the read-only tools below are UNGATED; this only ever asks GitLab to do what "
            "was already approved when the MR was created. A refusal is returned as a normal "
            "result (merge_status one of CONFLICTED/BLOCKED/CHECKING/UNKNOWN, plus pipeline detail "
            "when a failed pipeline explains it), never raised. Requires an active GitLab "
            "connection."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project": {"type": "string"}, "repo_path": {"type": "string"},
                "mr_iid": {"type": "integer"},
            },
            "required": ["mr_iid"],
        },
            annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
    ),
    Tool(
        name=_REFRESH_MERGE_STATUS_TOOL,
        description=(
            "USE WHEN a push just landed on an MR's source branch and its mergeability needs "
            "re-checking without a full git_create_mr/gitlab_merge_merge_request retry: MUST call "
            "gitlab_refresh_merge_status with mr_iid plus either project or repo_path. Re-fetches "
            "the MR and reclassifies it (same MERGEABLE/CONFLICTED/CHECKING/BLOCKED/UNKNOWN bucket "
            "as everywhere else, with the legacy-cannot_be_merged-vs-failed-pipeline cross-check "
            "applied), never pushes or merges anything. Read-only, UNGATED. Requires an active "
            "GitLab connection."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project": {"type": "string"}, "repo_path": {"type": "string"},
                "mr_iid": {"type": "integer"},
            },
            "required": ["mr_iid"],
        },
            annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
    ),
]


def _ok(payload: dict) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps({"ok": True, **payload}))]


def _err(message: str) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps({"ok": False, "error": message}))]


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


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_ERROR_MARKERS = ("err!", "error", "failed", "fatal:", "traceback", "##[error]", "exception")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _filter_job_log(raw: str, tail_lines: int, strip_ansi: bool, only_errors: bool) -> dict:
    """Trims a job's raw log to a token-safe default shape - the full untruncated log can run
    to 100K+ characters and blow past any usable response size. Returns the trimmed log plus how
    many lines were dropped, so the caller knows more is available on request."""
    text = _strip_ansi(raw) if strip_ansi else raw
    lines = text.splitlines()
    total = len(lines)
    if only_errors:
        lines = [ln for ln in lines if any(marker in ln.lower() for marker in _ERROR_MARKERS)]
    kept = lines[-tail_lines:] if tail_lines and tail_lines > 0 else lines
    return {
        "log": "\n".join(kept), "total_lines": total, "lines_shown": len(kept),
        "lines_omitted": total - len(kept),
    }


def _no_gitlab_connection_err() -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps({
        "ok": False,
        "error": "No active GitLab connection. Run `icx gitlab --add` first.",
        "fallback": _ICX_FALLBACK("GitLab", "icx gitlab --add"),
    }))]


def _resolve_project(arguments: dict) -> str | None:
    """project (used as-is) takes precedence over repo_path (a local git checkout
    whose origin remote is parsed into a GitLab project path). Returns None if
    neither is given, or repo_path's remote isn't a recognized GitLab URL shape."""
    project = arguments.get("project")
    if project:
        return project
    repo_path = arguments.get("repo_path")
    if not repo_path:
        return None
    try:
        return project_path_from_remote_url(remote_url(Path(repo_path)))
    except Exception:
        return None


async def dispatch_gitlab_tool(name: str, arguments: dict) -> list[TextContent] | None:
    """Returns None when `name` is not a gitlab tool, so mcp_server.py's existing
    dispatcher can fall through to its own chain unmodified."""
    if name == _LIST_MRS_TOOL:
        try:
            conn = ConfigManager.load().active_gitlab_connection()
            if conn is None:
                return _no_gitlab_connection_err()
            project = _resolve_project(arguments)
            if project is None:
                return _err("Either project or repo_path is required (a repo_path remote must be a recognized GitLab URL).")
            async with GitLabClient(conn.url, conn.token, conn.verify_tls) as client:
                result = await client.list_merge_requests(
                    project,
                    state=arguments.get("state") or "merged",
                    target_branch=arguments.get("target_branch"),
                    limit=arguments.get("limit") or 20,
                )
            return _ok({"merge_requests": result})
        except Exception as exc:
            return _err(str(exc))

    if name == _MR_CHANGES_TOOL:
        mr_iid = arguments.get("mr_iid")
        if not isinstance(mr_iid, int) or isinstance(mr_iid, bool):
            return _err("mr_iid is required and must be an integer.")
        try:
            conn = ConfigManager.load().active_gitlab_connection()
            if conn is None:
                return _no_gitlab_connection_err()
            project = _resolve_project(arguments)
            if project is None:
                return _err("Either project or repo_path is required (a repo_path remote must be a recognized GitLab URL).")
            async with GitLabClient(conn.url, conn.token, conn.verify_tls) as client:
                result = await client.get_merge_request_changes(project, mr_iid)
            return _ok(result)
        except Exception as exc:
            return _err(str(exc))

    if name == _LIST_COMMITS_TOOL:
        try:
            conn = ConfigManager.load().active_gitlab_connection()
            if conn is None:
                return _no_gitlab_connection_err()
            project = _resolve_project(arguments)
            if project is None:
                return _err("Either project or repo_path is required (a repo_path remote must be a recognized GitLab URL).")
            async with GitLabClient(conn.url, conn.token, conn.verify_tls) as client:
                result = await client.list_commits(
                    project,
                    ref=arguments.get("ref"),
                    path=arguments.get("path"),
                    since=arguments.get("since"),
                    limit=arguments.get("limit") or 20,
                )
            return _ok({"commits": result})
        except Exception as exc:
            return _err(str(exc))

    if name == _COMPARE_TOOL:
        from_ref = arguments.get("from_ref")
        if not from_ref or not isinstance(from_ref, str):
            return _err("from_ref is required and must be a non-empty string.")
        to_ref = arguments.get("to_ref")
        if not to_ref or not isinstance(to_ref, str):
            return _err("to_ref is required and must be a non-empty string.")
        try:
            conn = ConfigManager.load().active_gitlab_connection()
            if conn is None:
                return _no_gitlab_connection_err()
            project = _resolve_project(arguments)
            if project is None:
                return _err("Either project or repo_path is required (a repo_path remote must be a recognized GitLab URL).")
            async with GitLabClient(conn.url, conn.token, conn.verify_tls) as client:
                result = await client.compare(project, from_ref, to_ref)
            return _ok(result)
        except Exception as exc:
            return _err(str(exc))

    if name == _LIST_TAGS_TOOL:
        try:
            conn = ConfigManager.load().active_gitlab_connection()
            if conn is None:
                return _no_gitlab_connection_err()
            project = _resolve_project(arguments)
            if project is None:
                return _err("Either project or repo_path is required (a repo_path remote must be a recognized GitLab URL).")
            async with GitLabClient(conn.url, conn.token, conn.verify_tls) as client:
                result = await client.list_tags(project)
            result, extra = _paginate(result, arguments.get("limit"), arguments.get("offset"))
            return _ok({"tags": result, **extra})
        except Exception as exc:
            return _err(str(exc))

    if name == _LIST_BRANCHES_TOOL:
        try:
            conn = ConfigManager.load().active_gitlab_connection()
            if conn is None:
                return _no_gitlab_connection_err()
            project = _resolve_project(arguments)
            if project is None:
                return _err("Either project or repo_path is required (a repo_path remote must be a recognized GitLab URL).")
            async with GitLabClient(conn.url, conn.token, conn.verify_tls) as client:
                result = await client.list_branches(project, search=arguments.get("search") or "")
            result, extra = _paginate(result, arguments.get("limit"), arguments.get("offset"))
            return _ok({"branches": result, **extra})
        except Exception as exc:
            return _err(str(exc))

    if name == _LIST_PIPELINES_TOOL:
        try:
            conn = ConfigManager.load().active_gitlab_connection()
            if conn is None:
                return _no_gitlab_connection_err()
            project = _resolve_project(arguments)
            if project is None:
                return _err("Either project or repo_path is required (a repo_path remote must be a recognized GitLab URL).")
            async with GitLabClient(conn.url, conn.token, conn.verify_tls) as client:
                result = await client.list_pipelines(
                    project, ref=arguments.get("ref") or "", status=arguments.get("status") or "",
                )
            result, extra = _paginate(result, arguments.get("limit"), arguments.get("offset"))
            return _ok({"pipelines": result, **extra})
        except Exception as exc:
            return _err(str(exc))

    if name == _PIPELINE_STATUS_TOOL:
        pipeline_id = arguments.get("pipeline_id")
        if not isinstance(pipeline_id, int) or isinstance(pipeline_id, bool):
            return _err("pipeline_id is required and must be an integer.")
        try:
            conn = ConfigManager.load().active_gitlab_connection()
            if conn is None:
                return _no_gitlab_connection_err()
            project = _resolve_project(arguments)
            if project is None:
                return _err("Either project or repo_path is required (a repo_path remote must be a recognized GitLab URL).")
            async with GitLabClient(conn.url, conn.token, conn.verify_tls) as client:
                result = await client.get_pipeline(project, pipeline_id)
            return _ok({"pipeline": result})
        except Exception as exc:
            return _err(str(exc))

    if name == _JOB_LOG_TOOL:
        job_id = arguments.get("job_id")
        if not isinstance(job_id, int) or isinstance(job_id, bool):
            return _err("job_id is required and must be an integer.")
        try:
            conn = ConfigManager.load().active_gitlab_connection()
            if conn is None:
                return _no_gitlab_connection_err()
            project = _resolve_project(arguments)
            if project is None:
                return _err("Either project or repo_path is required (a repo_path remote must be a recognized GitLab URL).")
            async with GitLabClient(conn.url, conn.token, conn.verify_tls) as client:
                raw = await client.get_job_trace(project, job_id)
            tail_lines = arguments.get("tail_lines")
            if tail_lines is None:
                tail_lines = 200
            strip_ansi = arguments.get("strip_ansi")
            strip_ansi = True if strip_ansi is None else bool(strip_ansi)
            only_errors = arguments.get("only_errors")
            only_errors = True if only_errors is None else bool(only_errors)
            return _ok(_filter_job_log(raw, tail_lines, strip_ansi, only_errors))
        except Exception as exc:
            return _err(str(exc))

    if name == _CLOSE_MR_TOOL:
        return await _dispatch_mr_state_change(arguments, "close", "close_merge_request")

    if name == _REOPEN_MR_TOOL:
        return await _dispatch_mr_state_change(arguments, "reopen", "reopen_merge_request")

    if name == _MERGE_MR_TOOL:
        mr_iid = arguments.get("mr_iid")
        if not isinstance(mr_iid, int) or isinstance(mr_iid, bool):
            return _err("mr_iid is required and must be an integer.")
        try:
            conn = ConfigManager.load().active_gitlab_connection()
            if conn is None:
                return _no_gitlab_connection_err()
            project = _resolve_project(arguments)
            if project is None:
                return _err("Either project or repo_path is required (a repo_path remote must be a recognized GitLab URL).")
            async with GitLabClient(conn.url, conn.token, conn.verify_tls) as client:
                merge_result = await client.attempt_merge(project, mr_iid)
                if merge_result["merged"]:
                    return _ok({"mr_iid": mr_iid, "merged": True, "merge_status": "MERGEABLE", "refusal_reason": None})
                mr = await client.get_merge_request(project, mr_iid)
                diagnosis = await diagnose_merge_refusal(client, project, mr, mr.get("source_branch", ""))
            return _ok({
                "mr_iid": mr_iid, "merged": False, "merge_status": diagnosis["status"],
                "refusal_reason": merge_result.get("reason"), "has_conflicts": mr.get("has_conflicts"),
                "pipeline": diagnosis["pipeline"],
            })
        except Exception as exc:
            return _err(str(exc))

    if name == _REFRESH_MERGE_STATUS_TOOL:
        mr_iid = arguments.get("mr_iid")
        if not isinstance(mr_iid, int) or isinstance(mr_iid, bool):
            return _err("mr_iid is required and must be an integer.")
        try:
            conn = ConfigManager.load().active_gitlab_connection()
            if conn is None:
                return _no_gitlab_connection_err()
            project = _resolve_project(arguments)
            if project is None:
                return _err("Either project or repo_path is required (a repo_path remote must be a recognized GitLab URL).")
            async with GitLabClient(conn.url, conn.token, conn.verify_tls) as client:
                mr = await client.get_merge_request(project, mr_iid)
                diagnosis = await diagnose_merge_refusal(client, project, mr, mr.get("source_branch", ""))
            return _ok({
                "mr_iid": mr_iid, "merge_status": diagnosis["status"],
                "has_conflicts": mr.get("has_conflicts"), "pipeline": diagnosis["pipeline"],
            })
        except Exception as exc:
            return _err(str(exc))

    return None


async def _dispatch_mr_state_change(arguments: dict, state_event: str, action: str) -> list[TextContent]:
    """Shared confirm-gated close/reopen flow - both are the same shape (mr_iid in, a
    state_event PUT, the updated MR back out), differing only in which client method runs."""
    confirm_token = arguments.get("confirm_token")
    if not confirm_token:
        mr_iid = arguments.get("mr_iid")
        if not isinstance(mr_iid, int) or isinstance(mr_iid, bool):
            return _err("mr_iid is required and must be an integer.")
        project = _resolve_project(arguments)
        if project is None:
            return _err("Either project or repo_path is required (a repo_path remote must be a recognized GitLab URL).")
        if ConfigManager.load().active_gitlab_connection() is None:
            return _no_gitlab_connection_err()
        token = issue_token(action, {"project": project, "mr_iid": mr_iid})
        return [TextContent(type="text", text=json.dumps({
            "status": "pending_confirmation",
            "token": token,
            "project": project,
            "mr_iid": mr_iid,
            "instruction": (
                f"Show the human which MR (!{mr_iid} in {project}) is about to be {state_event}d. "
                "Only call this tool again with confirm_token set once they explicitly agree."
            ),
        }))]
    payload = verify_token(confirm_token, action)
    if payload is None:
        return _err("Invalid or already-used confirm_token. Call again without a token to get a fresh one.")
    try:
        conn = ConfigManager.load().active_gitlab_connection()
        if conn is None:
            return _no_gitlab_connection_err()
        async with GitLabClient(conn.url, conn.token, conn.verify_tls) as client:
            method = client.close_merge_request if state_event == "close" else client.reopen_merge_request
            result = await method(payload["project"], payload["mr_iid"])
        return _ok({"mr_iid": payload["mr_iid"], "state": result.get("state")})
    except Exception as exc:
        return _err(str(exc))
