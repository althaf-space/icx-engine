"""MCP tool surface for GitLab MR/commit history lookups. Owns its own Tool()
definitions and dispatch function - mcp_server.py's _list_tools()/_call_tool()
get a few additive lines only, no restructuring."""
from __future__ import annotations

import json
from pathlib import Path

from mcp.types import TextContent, Tool

from icx_engine.config_manager import ConfigManager
from icx_engine.git.gitcmd import remote_url
from icx_engine.gitlab.client import GitLabClient, project_path_from_remote_url
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
    ),
    Tool(
        name=_LIST_TAGS_TOOL,
        description=(
            "USE WHEN the human wants to see this project's REAL existing tags before creating a "
            "new one: MUST call this FIRST, before git_create_tag, with either project or "
            "repo_path - never invent an environment name or version series from guesswork. "
            "Returns every tag with its target commit and creation date, newest first per GitLab's "
            "own ordering. Read-only, UNGATED. Requires an active GitLab connection."
        ),
        inputSchema={
            "type": "object",
            "properties": {"project": {"type": "string"}, "repo_path": {"type": "string"}},
            "required": [],
        },
    ),
    Tool(
        name=_LIST_BRANCHES_TOOL,
        description=(
            "USE WHEN the human wants to see this project's REAL existing branches - e.g. before "
            "proposing a parent/base branch, or to confirm an exact branch name rather than "
            "guessing between similarly-named ones: MUST call this instead of inventing a branch "
            "name. Returns name/protected/default/last-commit-date per branch. `search` (if given) "
            "is GitLab's own server-side substring filter. Read-only, UNGATED. Requires an active "
            "GitLab connection."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project": {"type": "string"}, "repo_path": {"type": "string"},
                "search": {"type": "string"},
            },
            "required": [],
        },
    ),
    Tool(
        name=_LIST_PIPELINES_TOOL,
        description=(
            "USE WHEN the human wants to know whether a pipeline ran (or is running) for a "
            "branch/tag/MR - e.g. right after git_create_mr or git_create_tag, instead of "
            "guessing from timing alone whether a merge refusal means a real conflict or just an "
            "in-progress/not-yet-started pipeline. `ref` (branch or tag name) narrows to that ref; "
            "`status` (e.g. 'running', 'success', 'failed') narrows further. Returns the 20 most "
            "recent matching pipelines. Read-only, UNGATED. Requires an active GitLab connection."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project": {"type": "string"}, "repo_path": {"type": "string"},
                "ref": {"type": "string"}, "status": {"type": "string"},
            },
            "required": [],
        },
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
    ),
    Tool(
        name=_JOB_LOG_TOOL,
        description=(
            "USE WHEN the human wants to know WHY a specific job failed: returns that job's raw "
            "plain-text log (found via gitlab_pipeline_status's jobs list). Read-only, UNGATED. "
            "Requires an active GitLab connection."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project": {"type": "string"}, "repo_path": {"type": "string"},
                "job_id": {"type": "integer"},
            },
            "required": ["job_id"],
        },
    ),
]


def _ok(payload: dict) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps({"ok": True, **payload}))]


def _err(message: str) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps({"ok": False, "error": message}))]


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
            return _ok({"tags": result})
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
            return _ok({"branches": result})
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
            return _ok({"pipelines": result})
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
                result = await client.get_job_trace(project, job_id)
            return _ok({"log": result})
        except Exception as exc:
            return _err(str(exc))

    return None
