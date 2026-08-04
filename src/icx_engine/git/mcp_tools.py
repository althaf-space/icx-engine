"""MCP tool surface for the git-workflow lifecycle. Owns its own Tool()
definitions and dispatch function - mcp_server.py's _list_tools()/_call_tool()
get a few additive lines only, no restructuring (design spec Section 11)."""
from __future__ import annotations

import json
from pathlib import Path

from mcp.types import TextContent, Tool

from icx_engine.config_manager import ConfigManager
from icx_engine.confirm import issue_token, verify_token
from icx_engine.git.manager import GitLifecycleManager
from icx_engine.git.settings import read_repo_settings
from icx_engine.gitlab import ci_tags, service as gitlab_service
from icx_engine.gitlab.client import GitLabClient, project_path_from_remote_url
from icx_engine.mcp_server import _ICX_FALLBACK
from icx_engine.skills.hints import attach_skill_hint

_REPO_STATUS_TOOL = "git_repo_status"
_START_BRANCH_TOOL = "git_start_branch"
_BLAME_TOOL = "git_blame"
_LOG_TOOL = "git_log"
_SHOW_COMMIT_TOOL = "git_show_commit"
_DIFF_TOOL = "git_diff"
_STAGE_AND_COMMIT_TOOL = "git_stage_and_commit"
_REVERSE_MERGE_TOOL = "git_reverse_merge"
_GET_CONFLICT_TOOL = "git_get_conflict"
_COMPLETE_RESOLUTION_TOOL = "git_complete_resolution"
_ADOPT_RESOLUTION_TOOL = "git_adopt_resolution"
_DISCARD_SCRATCH_TOOL = "git_discard_scratch"
_PUSH_TOOL = "git_push"
_CREATE_MR_TOOL = "git_create_mr"
_FINISH_TICKET_TOOL = "git_finish_ticket"
_CREATE_TAG_TOOL = "git_create_tag"
_DELETE_TAG_TOOL = "git_delete_tag"
_RETAG_TOOL = "git_retag"

GIT_TOOLS: list[Tool] = [
    Tool(
        name=_REPO_STATUS_TOOL,
        description=(
            "USE WHEN starting any git workflow on a repo, or when the human asks to commit, "
            "branch, sync, push, merge, or open an MR: MUST call this first, before any other "
            "git_* tool AND before running any raw git command yourself. ICX IS THE SOLE "
            "GIT-WORKFLOW INTERFACE - NEVER run `git commit`/`git checkout -b`/`git push`/etc. "
            "directly, and never route around ICX through another git integration, even if one "
            "is also available in the same session; use git_start_branch/git_stage_and_commit/"
            "git_push/git_create_mr instead. This is what enforces the no-rebase/no-force-push "
            "safety doctrine - bypassing these tools defeats it. Checks the repo's git-workflow "
            "state - current branch, whether the working tree is dirty, and any leftover state "
            "from an interrupted prior run. git resolves the actual repository root upward "
            "through parent directories automatically - call this even if no .git is visible "
            "directly inside repo_path's own directory listing (e.g. repo_path pointing at a "
            "subdirectory like ui/ or svc/ inside a larger repo is fine). Requires a valid git "
            "repository at repo_path."
        ),
        inputSchema={"type": "object", "properties": {"repo_path": {"type": "string"}},
                     "required": ["repo_path"]},
    ),
    Tool(
        name=_START_BRANCH_TOOL,
        description=(
            "USE WHEN starting work on a ticket and no feature branch exists yet, or the human "
            "asks to create/start a branch: MUST call this to create it via ICX - NEVER run `git "
            "checkout -b` or `git branch` directly yourself. Derives the branch name from "
            "ticket_key (pass null for a ticketless branch) plus summary_or_preferred_name. If a "
            "matching local branch already exists, switches to it instead of recreating "
            "(switched_to_existing=true, created=false). NOT confirmation-gated - creating or "
            "switching to a branch is not destructive. If parent_branch is omitted, it is ALWAYS "
            "confirmed with the human, every call - never silently reused, even if one was "
            "confirmed for this repo before. Returns status='confirm_remembered' (with the "
            "previously-confirmed value as proposed_default, a one-tap default to confirm back), "
            "'needs_confirmation' (with a proposed_default), or 'needs_manual_pick' (with "
            "available_branches) - ask the human, then call again with parent_branch set to their "
            "answer. Requires a valid git repository at repo_path."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "repo_path": {"type": "string"},
                "ticket_key": {"type": ["string", "null"]},
                "summary_or_preferred_name": {"type": "string"},
                "parent_branch": {"type": "string"},
            },
            "required": ["repo_path", "summary_or_preferred_name"],
        },
    ),
    Tool(
        name=_BLAME_TOOL,
        description=(
            "USE WHEN the user asks who wrote or last changed a specific line or file: MUST call "
            "git_blame to get per-line commit sha, author, and timestamp for relpath. Pass "
            "line_start and line_end together to narrow to a range - passing only one of them "
            "fails. Requires a valid git repository at repo_path."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "repo_path": {"type": "string"},
                "relpath": {"type": "string"},
                "line_start": {"type": "integer"},
                "line_end": {"type": "integer"},
            },
            "required": ["repo_path", "relpath"],
        },
    ),
    Tool(
        name=_LOG_TOOL,
        description=(
            "USE WHEN the user wants commit history for a repo or one file: MUST call git_log to "
            "fetch commits newest first, optionally scoped by relpath, author, or since, with "
            "limit defaulting to 20. Requires a valid git repository at repo_path."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "repo_path": {"type": "string"},
                "relpath": {"type": "string"},
                "limit": {"type": "integer"},
                "author": {"type": "string"},
                "since": {"type": "string"},
            },
            "required": ["repo_path"],
        },
    ),
    Tool(
        name=_SHOW_COMMIT_TOOL,
        description=(
            "USE WHEN the user wants the full details of one specific commit by its sha: MUST "
            "call git_show_commit to get its author, date, message, and changed files. Requires "
            "a valid git repository at repo_path."
        ),
        inputSchema={
            "type": "object",
            "properties": {"repo_path": {"type": "string"}, "sha": {"type": "string"}},
            "required": ["repo_path", "sha"],
        },
    ),
    Tool(
        name=_DIFF_TOOL,
        description=(
            "USE WHEN the user wants to compare two branches, tags, or commits: MUST call "
            "git_diff with ref_a and ref_b to get per-file status plus insertion/deletion counts. "
            "Requires a valid git repository at repo_path."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "repo_path": {"type": "string"},
                "ref_a": {"type": "string"},
                "ref_b": {"type": "string"},
            },
            "required": ["repo_path", "ref_a", "ref_b"],
        },
    ),
    Tool(
        name=_STAGE_AND_COMMIT_TOOL,
        description=(
            "USE WHEN the human wants to stage and commit specific files: MUST stage exactly the "
            "given files and commit them. NEVER pass a wildcard - list every file explicitly. The "
            "message must start with the ticket key when ticket_key is set. This is a "
            "CONFIRMATION-GATED tool: the first call (no confirm_token) returns "
            "pending_confirmation plus a one-time token after showing the human the exact files "
            "and message - you MUST show that to the human and get an explicit yes before calling "
            "again with confirm_token set. Calling with a wrong or reused token fails. If the "
            "current branch is this repo's confirmed parent/shared branch, the pending_confirmation "
            "response sets on_parent_branch=true and the instruction warns the human before they "
            "commit directly there - it never blocks the commit, only strengthens the warning; the "
            "human can still choose to commit on the parent branch anyway. On success, a local-only "
            "backup-latest/<ticket-or-branch-slug> branch is moved to the new commit automatically - "
            "this keeps a fallback pointer in sync with every commit, never trailing behind; it is "
            "never pushed, no separate confirmation needed for it. Requires a "
            "valid git repository at repo_path."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "repo_path": {"type": "string"},
                "files": {"type": "array", "items": {"type": "string"}},
                "message": {"type": "string"},
                "ticket_key": {"type": ["string", "null"]},
                "confirm_token": {"type": "string"},
            },
            "required": ["repo_path", "files", "message", "ticket_key"],
        },
    ),
    Tool(
        name=_REVERSE_MERGE_TOOL,
        description=(
            "USE WHEN the human wants to bring the parent branch's changes into the current "
            "feature branch before creating an MR: MUST reverse-merge the parent branch into the "
            "current feature branch. Clean merges complete automatically (status='clean'). A "
            "conflict quarantines onto a disposable scratch branch (status='conflict', "
            "scratch_branch + conflicted_files returned) - the real feature branch is left "
            "untouched. Use git_get_conflict per file, then git_complete_resolution and "
            "git_adopt_resolution to finish. If parent_branch is omitted, it is ALWAYS confirmed "
            "with the human, every call - never silently reused, even if one was confirmed for "
            "this repo before. Returns status='confirm_remembered' (with the previously-confirmed "
            "value as proposed_default, a one-tap default to confirm back), 'needs_confirmation' "
            "(with a proposed_default), or 'needs_manual_pick' (with available_branches) - ask "
            "the human, then call again with parent_branch set to their answer. Requires a valid "
            "git repository at repo_path."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "repo_path": {"type": "string"},
                "parent_branch": {"type": "string"},
                "ticket_key": {"type": "string"},
            },
            "required": ["repo_path", "ticket_key"],
        },
    ),
    Tool(
        name=_GET_CONFLICT_TOOL,
        description=(
            "USE WHEN git_reverse_merge reported a conflict and one conflicted file needs "
            "inspecting: MUST fetch the ours/theirs content for that file here. Call once per "
            "file in conflicted_files. Suggest a resolution to the human; they edit the file "
            "directly, then you call git_complete_resolution. Requires a valid git repository at "
            "repo_path."
        ),
        inputSchema={"type": "object",
                     "properties": {"repo_path": {"type": "string"}, "file": {"type": "string"}},
                     "required": ["repo_path", "file"]},
    ),
    Tool(
        name=_COMPLETE_RESOLUTION_TOOL,
        description=(
            "USE WHEN the human has finished editing every conflicted file on the scratch branch: "
            "MUST complete conflict resolution there - validates every listed file has no "
            "remaining conflict markers, then stages and commits them. Hard-blocks if any marker "
            "remains. CONFIRMATION-GATED: first call (no confirm_token) returns "
            "pending_confirmation plus a token - show the human every file and the message, get "
            "explicit agreement, then call again with confirm_token. Requires a valid git "
            "repository at repo_path."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "repo_path": {"type": "string"},
                "files": {"type": "array", "items": {"type": "string"}},
                "message": {"type": "string"},
                "confirm_token": {"type": "string"},
            },
            "required": ["repo_path", "files", "message"],
        },
    ),
    Tool(
        name=_ADOPT_RESOLUTION_TOOL,
        description=(
            "USE WHEN git_complete_resolution succeeded and the resolved scratch branch is ready "
            "to land: MUST atomically adopt the resolved scratch branch onto the real feature "
            "branch (a safe fast-forward, never a conflict-capable operation) and delete the "
            "scratch branch. Call only after git_complete_resolution succeeded. "
            "CONFIRMATION-GATED: first call (no confirm_token) returns pending_confirmation plus "
            "a token - show the human what's about to be adopted, get explicit agreement, then "
            "call again with confirm_token. Requires a valid git repository at repo_path."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "repo_path": {"type": "string"},
                "feature_branch": {"type": "string"},
                "scratch_branch": {"type": "string"},
                "confirm_token": {"type": "string"},
            },
            "required": ["repo_path", "feature_branch", "scratch_branch"],
        },
    ),
    Tool(
        name=_DISCARD_SCRATCH_TOOL,
        description=(
            "USE WHEN git_repo_status reports a leftover scratch branch the human wants to "
            "discard rather than resume: MUST abandon the interrupted or unwanted "
            "conflict-resolution attempt - switches back to the feature branch and FORCE-DELETES "
            "the scratch branch, permanently discarding any conflict-resolution work on it. The "
            "feature branch itself was never touched by the scratch flow. "
            "CONFIRMATION-GATED: first call (no confirm_token) returns pending_confirmation plus "
            "a token - show the human which scratch branch is about to be permanently deleted, "
            "get explicit agreement, then call again with confirm_token. Requires a valid git "
            "repository at repo_path."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "repo_path": {"type": "string"},
                "feature_branch": {"type": "string"},
                "scratch_branch": {"type": "string"},
                "confirm_token": {"type": "string"},
            },
            "required": ["repo_path", "feature_branch", "scratch_branch"],
        },
    ),
    Tool(
        name=_PUSH_TOOL,
        description=(
            "USE WHEN the human wants to push the current branch to the remote WITHOUT opening a "
            "merge request yet - e.g. sharing progress with another developer working on the same "
            "feature branch for the same ticket: MUST push the current branch to remote. Plain push "
            "only - no force, no rebase, no history rewrite. CONFIRMATION-GATED: the first call (no "
            "confirm_token) returns pending_confirmation plus a one-time token after showing the "
            "human the exact branch and remote - you MUST show that to the human and get an "
            "explicit yes before calling again with confirm_token set. Calling with a wrong or "
            "reused token fails. git_create_mr already pushes automatically before creating its "
            "MR, so only use this tool when an MR should NOT be opened yet. Requires a valid git "
            "repository at repo_path."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "repo_path": {"type": "string"},
                "remote": {"type": "string"},
                "confirm_token": {"type": "string"},
            },
            "required": ["repo_path"],
        },
    ),
    Tool(
        name=_CREATE_MR_TOOL,
        description=(
            "USE WHEN the human wants to open a merge request for the current feature branch: "
            "MUST create (or reuse an existing open) merge request for the current feature branch "
            "and attempt one immediate merge. CONFIRMATION-GATED: first call (no confirm_token) "
            "returns pending_confirmation plus a token - show the human BOTH source_branch (the "
            "current feature branch this MR merges FROM) and parent_branch (the target it merges "
            "INTO), plus ticket/summary, get explicit agreement, then call again with confirm_token. "
            "If parent_branch "
            "is omitted, it is ALWAYS confirmed with the human, every call - never silently "
            "reused, even if one was confirmed for this repo before. Returns "
            "status='confirm_remembered' (with the previously-confirmed value as proposed_default, "
            "a one-tap default to confirm back), 'needs_confirmation' (with a proposed_default), or "
            "'needs_manual_pick' (with available_branches) - ask the human, then call again with "
            "parent_branch set to their answer. Requires an active GitLab connection."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "repo_path": {"type": "string"},
                "parent_branch": {"type": "string"},
                "ticket_key": {"type": "string"},
                "ticket_summary": {"type": "string"},
                "confirm_token": {"type": "string"},
            },
            "required": ["repo_path", "ticket_key", "ticket_summary"],
        },
    ),
    Tool(
        name=_FINISH_TICKET_TOOL,
        description=(
            "USE WHEN an MR has merged and the ticket's branch needs post-merge cleanup: MUST "
            "re-verify the MR is actually merged (never trust the caller's word alone), "
            "fast-forward the parent branch, and delete the local feature branch. "
            "CONFIRMATION-GATED: first call (no confirm_token) returns pending_confirmation plus a "
            "token - show the human what's about to be cleaned up, get explicit agreement, then "
            "call again with confirm_token. If parent_branch is omitted, it is ALWAYS confirmed "
            "with the human, every call - never silently reused, even if one was confirmed for "
            "this repo before. Returns status='confirm_remembered' (with the previously-confirmed "
            "value as proposed_default, a one-tap default to confirm back), 'needs_confirmation' "
            "(with a proposed_default), or 'needs_manual_pick' (with available_branches) - ask "
            "the human, then call again with parent_branch set to their answer. Requires an "
            "active GitLab connection."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "repo_path": {"type": "string"},
                "parent_branch": {"type": "string"},
                "feature_branch": {"type": "string"},
                "ticket_key": {"type": "string"},
                "mr_iid": {"type": "integer"},
                "delete_backups": {"type": "boolean"},
                "confirm_token": {"type": "string"},
            },
            "required": ["repo_path", "feature_branch", "ticket_key", "mr_iid"],
        },
    ),
    Tool(
        name=_CREATE_TAG_TOOL,
        description=(
            "USE WHEN the human wants to tag a build for a specific environment: MUST call "
            "gitlab_list_tags first to see this project's REAL existing tag names/environments - "
            "never invent an environment token (e.g. 'DEV') from guesswork. MUST create a "
            "GitLab tag for that chosen environment (server-side, no local push needed). Before "
            "proposing anything, this tool fetches the project's real .gitlab-ci.yml (at `branch`) "
            "and REJECTS an `environment` that matches none of its real tag-trigger patterns "
            "(case-insensitive) - the error names the real values found. It also checks the "
            "PROPOSED tag name against those same patterns and REFUSES to proceed if no CI pattern "
            "would match (creating such a tag builds nothing - a silent no-op) unless "
            "`override_ci_check=true` is passed. If the CI file itself can't be fetched, this "
            "degrades to a `ci_check_error` warning rather than blocking - show it to the human, "
            "never silently treat it as validated. CONFIRMATION-GATED and a HARD GATE: the first "
            "call (no confirm_token) returns pending_confirmation with previous_tag and "
            "proposed_tag, plus `ci_pipeline_will_trigger` (true/false/null-if-uncheckable) - you "
            "MUST show all of this to the human and get explicit agreement (or a tag_name_override "
            "they specify instead) before calling again with confirm_token. If previous_tag is "
            "null, the response carries an explicit `warning` - this can mean the environment name "
            "is WRONG, not that it's genuinely the first tag ever for it; relay that warning, don't "
            "treat null as a mere footnote. 'Latest tag' is always scoped to the chosen environment "
            "only - never assume which environment the human means. Requires an active GitLab "
            "connection."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "repo_path": {"type": "string"},
                "environment": {"type": "string"},
                "branch": {"type": "string"},
                "tag_name_override": {"type": "string"},
                "override_ci_check": {"type": "boolean"},
                "confirm_token": {"type": "string"},
            },
            "required": ["repo_path", "environment", "branch"],
        },
    ),
    Tool(
        name=_DELETE_TAG_TOOL,
        description=(
            "USE WHEN the human explicitly wants a specific existing GitLab tag permanently "
            "removed: MUST call gitlab_list_tags first to confirm the exact real tag name - never "
            "guess or assume one exists. This fetches the REAL tag from GitLab first (fails with a "
            "clear 'not found' error, never silently no-ops, if it doesn't exist) and reports its "
            "target commit sha before anything is deleted. CONFIRMATION-GATED and a HARD GATE: the "
            "first call (no confirm_token) returns pending_confirmation with tag_name and "
            "target_commit - you MUST show both to the human and get explicit agreement before "
            "calling again with confirm_token. Deleting a tag does NOT touch the commit or any "
            "branch it pointed at - only the tag reference itself is removed, permanently (GitLab "
            "has no tag recycle bin) - a new tag with the same name can be created again later "
            "pointing at any ref, but this exact tag object cannot be recovered. Requires an active "
            "GitLab connection."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "repo_path": {"type": "string"},
                "tag_name": {"type": "string"},
                "confirm_token": {"type": "string"},
            },
            "required": ["repo_path", "tag_name"],
        },
    ),
    Tool(
        name=_RETAG_TOOL,
        description=(
            "USE WHEN the human wants an EXISTING tag moved to point at a different ref (e.g. "
            "re-triggering a build from a newer commit under the same tag name) - NOT for creating "
            "a brand-new tag (use git_create_tag for that). Atomically deletes the existing tag and "
            "recreates it under the same name against the new `branch`. MUST call gitlab_list_tags "
            "first to confirm tag_name really exists - fails with a clear error if it doesn't "
            "(suggests git_create_tag instead). CONFIRMATION-GATED and a HARD GATE: the first call "
            "(no confirm_token) returns pending_confirmation with tag_name, previous_target (the "
            "tag's current commit sha), new_target (the real tip commit of `branch`, resolved via a "
            "live branch lookup - never guessed), and `ci_pipeline_will_trigger` "
            "(true/false/null-if-uncheckable, from the project's real .gitlab-ci.yml) - if "
            "previous_target equals new_target this is a no-op, say so plainly. Show all of this to "
            "the human and get explicit agreement before calling again with confirm_token. If the "
            "delete succeeds but recreation then fails for any reason, the response reports the "
            "ORIGINAL target commit sha so the tag can be recreated manually at that exact commit - "
            "this is a genuine partial-failure risk of a delete+create pair, surfaced rather than "
            "hidden. Requires an active GitLab connection."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "repo_path": {"type": "string"},
                "tag_name": {"type": "string"},
                "branch": {"type": "string"},
                "confirm_token": {"type": "string"},
            },
            "required": ["repo_path", "tag_name", "branch"],
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


def _needs_parent_branch(mgr: GitLifecycleManager) -> list[TextContent]:
    """Always returns an ask-payload when the caller omitted parent_branch - never silently reuses
    a remembered value, even if one is already confirmed for this repo (explicit design choice: the
    human must confirm the target branch on every call). A remembered value is surfaced as
    proposed_default so confirming it back is a single round-trip, not a blind re-pick."""
    result = mgr.resolve_parent_branch()
    if result.status == "resolved":
        return [TextContent(type="text", text=json.dumps({
            "status": "confirm_remembered",
            "proposed_default": result.parent_branch,
            "available_branches": [],
            "instruction": "This repo's last-confirmed parent/target branch is proposed_default. "
                           "Ask the human to confirm it is still correct for THIS operation "
                           "before proceeding - never assume it silently. Call this tool again "
                           "with parent_branch set to their answer (their confirmation of the "
                           "same value, or a different one).",
        }))]
    return [TextContent(type="text", text=json.dumps({
        "status": result.status,
        "proposed_default": result.proposed_default,
        "available_branches": result.available_branches,
        "instruction": "Ask the human which branch is their parent/target branch for this repo "
                       "(proposed_default/available_branches are suggestions, not decisions). "
                       "Call this tool again with parent_branch set to their answer.",
    }))]


async def dispatch_git_tool(name: str, arguments: dict) -> list[TextContent] | None:
    """Returns None when `name` is not a git tool, so mcp_server.py's existing
    dispatcher can fall through to its own chain unmodified."""
    if name == _REPO_STATUS_TOOL:
        repo_path = arguments.get("repo_path")
        if not repo_path or not isinstance(repo_path, str):
            return _err("repo_path is required and must be a non-empty string.")
        try:
            mgr = GitLifecycleManager(Path(repo_path))
            mgr.validate()
            from icx_engine.git.gitcmd import current_branch
            dirty_status = mgr.check_dirty_tree()
            leftover = mgr.check_leftover_state()
            return _ok(attach_skill_hint({
                "current_branch": current_branch(mgr.repo_root),
                "dirty": dirty_status.dirty,
                "dirty_files": dirty_status.files,
                "leftover_state_clean": leftover.is_clean,
                "scratch_branches": leftover.scratch_branches,
                "icx_stashes": leftover.icx_stashes,
                "merge_in_progress": leftover.merge_in_progress,
            }, "safe-git-workflow", rank_prompt="git workflow branch commit merge", archetype="git"))
        except Exception as exc:
            return _err(str(exc))

    if name == _START_BRANCH_TOOL:
        repo_path = arguments.get("repo_path")
        if not repo_path or not isinstance(repo_path, str):
            return _err("repo_path is required and must be a non-empty string.")
        summary_or_preferred_name = arguments.get("summary_or_preferred_name")
        if not summary_or_preferred_name or not isinstance(summary_or_preferred_name, str):
            return _err("summary_or_preferred_name is required and must be a non-empty string.")
        ticket_key = arguments.get("ticket_key")
        if ticket_key is not None and not isinstance(ticket_key, str):
            return _err("ticket_key must be a string or null.")
        try:
            mgr = GitLifecycleManager(Path(repo_path))
            mgr.validate()
            given_parent = arguments.get("parent_branch")
            if not given_parent:
                return _needs_parent_branch(mgr)
            mgr.confirm_parent_branch(given_parent)
            parent_branch = given_parent
            result = mgr.start_branch(ticket_key, summary_or_preferred_name, parent_branch)
            return _ok({
                "branch_name": result.branch_name,
                "created": result.created,
                "switched_to_existing": result.switched_to_existing,
            })
        except Exception as exc:
            return _err(str(exc))

    if name == _BLAME_TOOL:
        repo_path = arguments.get("repo_path")
        if not repo_path or not isinstance(repo_path, str):
            return _err("repo_path is required and must be a non-empty string.")
        relpath = arguments.get("relpath")
        if not relpath or not isinstance(relpath, str):
            return _err("relpath is required and must be a non-empty string.")
        line_start = arguments.get("line_start")
        line_end = arguments.get("line_end")
        if (line_start is None) != (line_end is None):
            return _err("line_start and line_end must both be given together, or neither.")
        try:
            from icx_engine.git import gitcmd
            mgr = GitLifecycleManager(Path(repo_path))
            mgr.validate()
            line_range = (line_start, line_end) if line_start is not None else None
            result = gitcmd.blame(mgr.repo_root, relpath, line_range=line_range)
            return _ok({"lines": result})
        except Exception as exc:
            return _err(str(exc))

    if name == _LOG_TOOL:
        repo_path = arguments.get("repo_path")
        if not repo_path or not isinstance(repo_path, str):
            return _err("repo_path is required and must be a non-empty string.")
        try:
            from icx_engine.git import gitcmd
            mgr = GitLifecycleManager(Path(repo_path))
            mgr.validate()
            result = gitcmd.log(
                mgr.repo_root,
                relpath=arguments.get("relpath"),
                limit=arguments.get("limit") or 20,
                author=arguments.get("author"),
                since=arguments.get("since"),
            )
            return _ok({"commits": result})
        except Exception as exc:
            return _err(str(exc))

    if name == _SHOW_COMMIT_TOOL:
        repo_path = arguments.get("repo_path")
        if not repo_path or not isinstance(repo_path, str):
            return _err("repo_path is required and must be a non-empty string.")
        sha = arguments.get("sha")
        if not sha or not isinstance(sha, str):
            return _err("sha is required and must be a non-empty string.")
        try:
            from icx_engine.git import gitcmd
            mgr = GitLifecycleManager(Path(repo_path))
            mgr.validate()
            result = gitcmd.show_commit(mgr.repo_root, sha)
            return _ok(result)
        except Exception as exc:
            return _err(str(exc))

    if name == _DIFF_TOOL:
        repo_path = arguments.get("repo_path")
        if not repo_path or not isinstance(repo_path, str):
            return _err("repo_path is required and must be a non-empty string.")
        ref_a = arguments.get("ref_a")
        if not ref_a or not isinstance(ref_a, str):
            return _err("ref_a is required and must be a non-empty string.")
        ref_b = arguments.get("ref_b")
        if not ref_b or not isinstance(ref_b, str):
            return _err("ref_b is required and must be a non-empty string.")
        try:
            from icx_engine.git import gitcmd
            mgr = GitLifecycleManager(Path(repo_path))
            mgr.validate()
            result = gitcmd.diff_between(mgr.repo_root, ref_a, ref_b)
            return _ok(result)
        except Exception as exc:
            return _err(str(exc))

    if name == _STAGE_AND_COMMIT_TOOL:
        try:
            confirm_token = arguments.get("confirm_token")
            if not confirm_token:
                repo_path = arguments.get("repo_path")
                if not repo_path or not isinstance(repo_path, str):
                    return _err("repo_path is required and must be a non-empty string.")
                files = arguments.get("files")
                if not isinstance(files, list) or not files:
                    return _err("files is required and must be a non-empty list.")
                message = arguments.get("message")
                if not message or not isinstance(message, str):
                    return _err("message is required and must be a non-empty string.")
                if "ticket_key" not in arguments:
                    return _err("ticket_key is required (pass null if there is no ticket).")
                from icx_engine.git.gitcmd import current_branch
                mgr = GitLifecycleManager(Path(repo_path))
                mgr.validate()
                branch = current_branch(mgr.repo_root)
                stored_parent = read_repo_settings(mgr.repo_root).get("parent_branch")
                on_parent_branch = bool(stored_parent) and branch == stored_parent
                token = issue_token("stage_and_commit", arguments)
                instruction = (
                    "Show these exact files, message, AND branch to the human. Only call this "
                    "tool again with confirm_token set once they explicitly agree."
                )
                if on_parent_branch:
                    instruction = (
                        f"WARNING: current branch '{branch}' IS this repo's confirmed parent/shared "
                        "branch - committing directly here affects everyone who branches from it. "
                        "Ask the human: commit here anyway, or create a feature branch first "
                        "(git_start_branch) and commit there instead? Only call this tool again with "
                        "confirm_token if they explicitly choose to commit on the parent branch anyway."
                    )
                return [TextContent(type="text", text=json.dumps({
                    "status": "pending_confirmation",
                    "token": token,
                    "branch": branch,
                    "on_parent_branch": on_parent_branch,
                    "files": files,
                    "message": message,
                    "instruction": instruction,
                }))]
            payload = verify_token(confirm_token, "stage_and_commit")
            if payload is None:
                return _err("Invalid or already-used confirm_token. Call again without a token to get a fresh one.")
            mgr = GitLifecycleManager(Path(payload["repo_path"]))
            mgr.validate()
            result = mgr.stage_and_commit(
                payload["files"], payload["message"], payload.get("ticket_key"),
            )
            return _ok({
                "sha": result.sha,
                "debug_warnings": [{"file": w.file, "line": w.line} for w in result.debug_warnings],
            })
        except Exception as exc:
            return _err(str(exc))

    if name == _REVERSE_MERGE_TOOL:
        repo_path = arguments.get("repo_path")
        if not repo_path or not isinstance(repo_path, str):
            return _err("repo_path is required and must be a non-empty string.")
        ticket_key = arguments.get("ticket_key")
        if not ticket_key or not isinstance(ticket_key, str):
            return _err("ticket_key is required and must be a non-empty string.")
        try:
            mgr = GitLifecycleManager(Path(repo_path))
            mgr.validate()
            given_parent = arguments.get("parent_branch")
            if not given_parent:
                return _needs_parent_branch(mgr)
            mgr.confirm_parent_branch(given_parent)
            parent_branch = given_parent
            result = mgr.reverse_merge_standard(parent_branch, ticket_key)
            if result.status == "clean":
                return _ok({"status": "clean"})
            session = mgr.start_conflict_resolution(parent_branch, ticket_key)
            return _ok({
                "status": "conflict",
                "scratch_branch": session.scratch_branch,
                "conflicted_files": session.conflicted_files,
            })
        except Exception as exc:
            return _err(str(exc))

    if name == _GET_CONFLICT_TOOL:
        repo_path = arguments.get("repo_path")
        if not repo_path or not isinstance(repo_path, str):
            return _err("repo_path is required and must be a non-empty string.")
        conflict_file = arguments.get("file")
        if not conflict_file or not isinstance(conflict_file, str):
            return _err("file is required and must be a non-empty string.")
        try:
            mgr = GitLifecycleManager(Path(repo_path))
            mgr.validate()
            payload = mgr.get_conflict(conflict_file)
            return _ok({"file": payload.file, "ours": payload.ours, "theirs": payload.theirs})
        except Exception as exc:
            return _err(str(exc))

    if name == _COMPLETE_RESOLUTION_TOOL:
        try:
            confirm_token = arguments.get("confirm_token")
            if not confirm_token:
                repo_path = arguments.get("repo_path")
                if not repo_path or not isinstance(repo_path, str):
                    return _err("repo_path is required and must be a non-empty string.")
                files = arguments.get("files")
                if not isinstance(files, list) or not files:
                    return _err("files is required and must be a non-empty list.")
                message = arguments.get("message")
                if not message or not isinstance(message, str):
                    return _err("message is required and must be a non-empty string.")
                from icx_engine.git.gitcmd import current_branch
                mgr = GitLifecycleManager(Path(repo_path))
                mgr.validate()
                token = issue_token("complete_resolution", arguments)
                return [TextContent(type="text", text=json.dumps({
                    "status": "pending_confirmation",
                    "token": token,
                    "branch": current_branch(mgr.repo_root),
                    "files": files,
                    "message": message,
                    "instruction": "Show these exact files, message, AND branch to the human. "
                                   "Only call this tool again with confirm_token set once they "
                                   "explicitly agree.",
                }))]
            payload = verify_token(confirm_token, "complete_resolution")
            if payload is None:
                return _err("Invalid or already-used confirm_token. Call again without a token to get a fresh one.")
            mgr = GitLifecycleManager(Path(payload["repo_path"]))
            mgr.validate()
            sha = mgr.complete_scratch_resolution(payload["files"], payload["message"])
            return _ok({"sha": sha})
        except Exception as exc:
            return _err(str(exc))

    if name == _ADOPT_RESOLUTION_TOOL:
        try:
            confirm_token = arguments.get("confirm_token")
            if not confirm_token:
                repo_path = arguments.get("repo_path")
                if not repo_path or not isinstance(repo_path, str):
                    return _err("repo_path is required and must be a non-empty string.")
                feature_branch = arguments.get("feature_branch")
                if not feature_branch or not isinstance(feature_branch, str):
                    return _err("feature_branch is required and must be a non-empty string.")
                scratch_branch = arguments.get("scratch_branch")
                if not scratch_branch or not isinstance(scratch_branch, str):
                    return _err("scratch_branch is required and must be a non-empty string.")
                token = issue_token("adopt_resolution", arguments)
                return [TextContent(type="text", text=json.dumps({
                    "status": "pending_confirmation",
                    "token": token,
                    "feature_branch": feature_branch,
                    "scratch_branch": scratch_branch,
                    "instruction": "Show the human which scratch branch is about to be adopted onto "
                                   "which feature branch. Only call again with confirm_token once they agree.",
                }))]
            payload = verify_token(confirm_token, "adopt_resolution")
            if payload is None:
                return _err("Invalid or already-used confirm_token. Call again without a token to get a fresh one.")
            mgr = GitLifecycleManager(Path(payload["repo_path"]))
            mgr.validate()
            sha = mgr.adopt_scratch_resolution(payload["feature_branch"], payload["scratch_branch"])
            return _ok({"sha": sha})
        except Exception as exc:
            return _err(str(exc))

    if name == _DISCARD_SCRATCH_TOOL:
        try:
            confirm_token = arguments.get("confirm_token")
            if not confirm_token:
                repo_path = arguments.get("repo_path")
                if not repo_path or not isinstance(repo_path, str):
                    return _err("repo_path is required and must be a non-empty string.")
                feature_branch = arguments.get("feature_branch")
                if not feature_branch or not isinstance(feature_branch, str):
                    return _err("feature_branch is required and must be a non-empty string.")
                scratch_branch = arguments.get("scratch_branch")
                if not scratch_branch or not isinstance(scratch_branch, str):
                    return _err("scratch_branch is required and must be a non-empty string.")
                token = issue_token("discard_scratch", arguments)
                return [TextContent(type="text", text=json.dumps({
                    "status": "pending_confirmation",
                    "token": token,
                    "feature_branch": feature_branch,
                    "scratch_branch": scratch_branch,
                    "instruction": "Show the human which scratch branch is about to be PERMANENTLY "
                                   "deleted (force-delete, discards any conflict-resolution work on "
                                   "it) and which feature branch they'll be switched back to. Only "
                                   "call again with confirm_token once they explicitly agree.",
                }))]
            payload = verify_token(confirm_token, "discard_scratch")
            if payload is None:
                return _err("Invalid or already-used confirm_token. Call again without a token to get a fresh one.")
            mgr = GitLifecycleManager(Path(payload["repo_path"]))
            mgr.validate()
            mgr.discard_scratch_resolution(payload["feature_branch"], payload["scratch_branch"])
            return _ok({"discarded": payload["scratch_branch"]})
        except Exception as exc:
            return _err(str(exc))

    if name == _PUSH_TOOL:
        try:
            confirm_token = arguments.get("confirm_token")
            if not confirm_token:
                repo_path = arguments.get("repo_path")
                if not repo_path or not isinstance(repo_path, str):
                    return _err("repo_path is required and must be a non-empty string.")
                remote = arguments.get("remote") or "origin"
                from icx_engine.git.gitcmd import current_branch
                mgr = GitLifecycleManager(Path(repo_path))
                mgr.validate()
                branch = current_branch(mgr.repo_root)
                token = issue_token("push", {**arguments, "remote": remote})
                return [TextContent(type="text", text=json.dumps({
                    "status": "pending_confirmation",
                    "token": token,
                    "branch": branch,
                    "remote": remote,
                    "instruction": "Show the human the exact branch and remote about to be pushed "
                                   "to. Only call this tool again with confirm_token set once they "
                                   "explicitly agree.",
                }))]
            payload = verify_token(confirm_token, "push")
            if payload is None:
                return _err("Invalid or already-used confirm_token. Call again without a token to get a fresh one.")
            from icx_engine.git import gitcmd
            from icx_engine.git.gitcmd import current_branch
            mgr = GitLifecycleManager(Path(payload["repo_path"]))
            mgr.validate()
            branch = current_branch(mgr.repo_root)
            remote = payload["remote"]
            gitcmd.push(mgr.repo_root, branch, remote=remote, extra_env=mgr._auth_env(remote))
            return _ok({"branch": branch, "remote": remote, "pushed": True})
        except Exception as exc:
            return _err(str(exc))

    if name == _CREATE_MR_TOOL:
        try:
            confirm_token = arguments.get("confirm_token")
            if not confirm_token:
                repo_path = arguments.get("repo_path")
                if not repo_path or not isinstance(repo_path, str):
                    return _err("repo_path is required and must be a non-empty string.")
                ticket_key = arguments.get("ticket_key")
                if not ticket_key or not isinstance(ticket_key, str):
                    return _err("ticket_key is required and must be a non-empty string.")
                ticket_summary = arguments.get("ticket_summary")
                if not ticket_summary or not isinstance(ticket_summary, str):
                    return _err("ticket_summary is required and must be a non-empty string.")
                mgr = GitLifecycleManager(Path(repo_path))
                mgr.validate()
                from icx_engine.git.gitcmd import current_branch
                source_branch = current_branch(mgr.repo_root)
                given_parent = arguments.get("parent_branch")
                if not given_parent:
                    return _needs_parent_branch(mgr)
                mgr.confirm_parent_branch(given_parent)
                parent_branch = given_parent
                token = issue_token("create_mr", {**arguments, "parent_branch": parent_branch})
                return [TextContent(type="text", text=json.dumps({
                    "status": "pending_confirmation",
                    "token": token,
                    "ticket_key": ticket_key,
                    "ticket_summary": ticket_summary,
                    "source_branch": source_branch,
                    "parent_branch": parent_branch,
                    "instruction": "Show the human the ticket, summary, SOURCE branch, and TARGET "
                                   "(parent) branch - confirm both, not just the target. Only call "
                                   "this tool again with confirm_token set once they explicitly agree.",
                }))]
            payload = verify_token(confirm_token, "create_mr")
            if payload is None:
                return _err("Invalid or already-used confirm_token. Call again without a token to get a fresh one.")
            conn = ConfigManager.load().active_gitlab_connection()
            if conn is None:
                return _no_gitlab_connection_err()
            mgr = GitLifecycleManager(Path(payload["repo_path"]))
            mgr.validate()
            result = await mgr.create_mr_for_ticket(
                payload["parent_branch"], payload["ticket_key"], payload["ticket_summary"], conn,
            )
            return _ok({
                "mr_iid": result.mr_iid, "created": result.created,
                "merged": result.merged, "refusal_reason": result.refusal_reason,
            })
        except Exception as exc:
            return _err(str(exc))

    if name == _FINISH_TICKET_TOOL:
        try:
            confirm_token = arguments.get("confirm_token")
            if not confirm_token:
                repo_path = arguments.get("repo_path")
                if not repo_path or not isinstance(repo_path, str):
                    return _err("repo_path is required and must be a non-empty string.")
                feature_branch = arguments.get("feature_branch")
                if not feature_branch or not isinstance(feature_branch, str):
                    return _err("feature_branch is required and must be a non-empty string.")
                ticket_key = arguments.get("ticket_key")
                if not ticket_key or not isinstance(ticket_key, str):
                    return _err("ticket_key is required and must be a non-empty string.")
                mr_iid = arguments.get("mr_iid")
                if not isinstance(mr_iid, int) or isinstance(mr_iid, bool):
                    return _err("mr_iid is required and must be an integer.")
                mgr = GitLifecycleManager(Path(repo_path))
                mgr.validate()
                given_parent = arguments.get("parent_branch")
                if not given_parent:
                    return _needs_parent_branch(mgr)
                mgr.confirm_parent_branch(given_parent)
                parent_branch = given_parent
                token = issue_token("finish_ticket", {**arguments, "parent_branch": parent_branch})
                return [TextContent(type="text", text=json.dumps({
                    "status": "pending_confirmation",
                    "token": token,
                    "feature_branch": feature_branch,
                    "parent_branch": parent_branch,
                    "instruction": "Show the human the SOURCE (feature_branch, about to be deleted "
                                   "locally) and TARGET (parent_branch, whose local pointer moves "
                                   "forward) - confirm both. Only call again with confirm_token once "
                                   "they agree.",
                }))]
            payload = verify_token(confirm_token, "finish_ticket")
            if payload is None:
                return _err("Invalid or already-used confirm_token. Call again without a token to get a fresh one.")
            conn = ConfigManager.load().active_gitlab_connection()
            if conn is None:
                return _no_gitlab_connection_err()
            mgr = GitLifecycleManager(Path(payload["repo_path"]))
            mgr.validate()
            result = mgr.post_merge_cleanup(
                payload["parent_branch"], payload["feature_branch"], payload["ticket_key"],
                payload.get("delete_backups", False), conn, payload["mr_iid"],
            )
            return _ok({
                "parent_branch": result.parent_branch,
                "feature_branch_deleted": result.feature_branch_deleted,
                "backups_deleted": result.backups_deleted,
            })
        except Exception as exc:
            return _err(str(exc))

    if name == _CREATE_TAG_TOOL:
        confirm_token = arguments.get("confirm_token")
        if not confirm_token:
            repo_path = arguments.get("repo_path")
            if not repo_path or not isinstance(repo_path, str):
                return _err("repo_path is required and must be a non-empty string.")
            environment = arguments.get("environment")
            if not environment or not isinstance(environment, str):
                return _err("environment is required and must be a non-empty string.")
            branch = arguments.get("branch")
            if not branch or not isinstance(branch, str):
                return _err("branch is required and must be a non-empty string.")
            try:
                mgr = GitLifecycleManager(Path(repo_path))
                mgr.validate()
                conn = ConfigManager.load().active_gitlab_connection()
                if conn is None:
                    return _no_gitlab_connection_err()
                from icx_engine.git.gitcmd import remote_url
                project_path = project_path_from_remote_url(remote_url(mgr.repo_root))
                if project_path is None:
                    return _err("Could not resolve a GitLab project from origin remote.")

                async def _list_tags() -> list[dict]:
                    async with GitLabClient(conn.url, conn.token, conn.verify_tls) as client:
                        return await client.list_tags(project_path)

                # Fetch the project's real .gitlab-ci.yml and validate BOTH the environment
                # token and the proposed tag name against it, before ever proposing anything -
                # real bug fixed: a free-text environment (e.g. "DEV") used to be accepted
                # silently, and the resulting tag triggered no pipeline at all (CI only
                # matches lowercase "dev"/"qa" tag patterns) - a silent no-op, the most
                # expensive kind of success. ci_check degrades to a warning (never a hard
                # block) if the CI file itself can't be fetched - genuine uncertainty is
                # surfaced, not guessed away.
                ci_yaml_text: str | None = None
                ci_check_error: str | None = None
                try:
                    async with GitLabClient(conn.url, conn.token, conn.verify_tls) as client:
                        ci_yaml_text = await client.get_repository_file(project_path, ".gitlab-ci.yml", branch)
                except Exception as exc:
                    ci_check_error = str(exc)

                if ci_yaml_text is not None:
                    real_envs = ci_tags.valid_environments(ci_yaml_text)
                    if real_envs and environment.lower() not in real_envs:
                        return _err(
                            f"'{environment}' does not match any tag-triggering environment in this "
                            f"project's .gitlab-ci.yml on branch '{branch}'. Real values found: "
                            f"{sorted(real_envs)}. Pass one of those (case-insensitive), or if this is "
                            "intentional (e.g. a brand-new environment CI doesn't know about yet), "
                            "confirm with the human before proceeding - creating this tag would trigger "
                            "no pipeline."
                        )
                    if real_envs and environment.lower() in real_envs:
                        # Normalize to the real observed casing (always lowercase in every captured
                        # pattern) BEFORE generating the tag - real bug this prevents: "DEV"/"QA"
                        # passing the environment check case-insensitively but then producing a
                        # wrong-case tag (e.g. v0.0.1-DEV-...) that matches no CI pattern anyway,
                        # since CI patterns are case-sensitive. Silently correcting the case here is
                        # strictly better than erroring a second time over what is really the same typo.
                        environment = environment.lower()

                grouped = gitlab_service.group_tags_by_environment(await _list_tags())
                latest = grouped.get(environment, [None])[0] if grouped.get(environment) else None
                proposed = arguments.get("tag_name_override") or gitlab_service.propose_next_tag(environment, latest)

                ci_match = ci_tags.matches_any_pattern(proposed, ci_yaml_text) if ci_yaml_text is not None else None
                if ci_match is False and not arguments.get("override_ci_check"):
                    return _err(
                        f"Proposed tag '{proposed}' does not match any tag-triggering pattern in this "
                        f"project's .gitlab-ci.yml on branch '{branch}' - creating it would build NOTHING "
                        "(a silent no-op, not a conflict or error). Show the human this exact reason. If "
                        "they want to create it anyway, call again with override_ci_check=true (no "
                        "confirm_token) to get a token for it."
                    )

                token = issue_token("create_tag", {**arguments, "project_path": project_path})
                response = {
                    "status": "pending_confirmation",
                    "token": token,
                    "environment": environment,
                    "previous_tag": latest.name if latest else None,
                    "proposed_tag": proposed,
                    "branch": branch,
                    "ci_pipeline_will_trigger": ci_match,
                    "instruction": "Show the human the environment, previous_tag, proposed_tag, and "
                                   "branch. If they want a different exact name, call again (no token) "
                                   "with tag_name_override set. Only call again with confirm_token once "
                                   "they explicitly approve the tag shown.",
                }
                if latest is None:
                    response["warning"] = (
                        f"No prior tags found for environment '{environment}' on this project - this "
                        "could mean the environment name is wrong (verify it against a real "
                        ".gitlab-ci.yml / gitlab_list_tags output) rather than genuinely being the first "
                        "tag ever created for it."
                    )
                if ci_check_error:
                    response["ci_check_error"] = (
                        f"Could not fetch .gitlab-ci.yml to validate this tag would trigger a pipeline: "
                        f"{ci_check_error}. Proceeding is possible but unverified - tell the human this "
                        "check could not run."
                    )
                return [TextContent(type="text", text=json.dumps(response))]
            except Exception as exc:
                return _err(str(exc))
        try:
            payload = verify_token(confirm_token, "create_tag")
            if payload is None:
                return _err("Invalid or already-used confirm_token. Call again without a token to get a fresh one.")
            conn = ConfigManager.load().active_gitlab_connection()
            if conn is None:
                return _no_gitlab_connection_err()
            environment = payload["environment"]
            tag_name = payload.get("tag_name_override")
            if not tag_name:
                async def _list_tags_again() -> list[dict]:
                    async with GitLabClient(conn.url, conn.token, conn.verify_tls) as client:
                        return await client.list_tags(payload["project_path"])
                grouped = gitlab_service.group_tags_by_environment(await _list_tags_again())
                latest = grouped.get(environment, [None])[0] if grouped.get(environment) else None
                tag_name = gitlab_service.propose_next_tag(environment, latest)

            async def _create() -> dict:
                async with GitLabClient(conn.url, conn.token, conn.verify_tls) as client:
                    return await client.create_tag(payload["project_path"], tag_name, payload["branch"], f"{environment} build")

            result = await _create()
            return _ok({"tag": result["name"], "branch": payload["branch"]})
        except Exception as exc:
            return _err(str(exc))

    if name == _DELETE_TAG_TOOL:
        confirm_token = arguments.get("confirm_token")
        if not confirm_token:
            repo_path = arguments.get("repo_path")
            if not repo_path or not isinstance(repo_path, str):
                return _err("repo_path is required and must be a non-empty string.")
            tag_name = arguments.get("tag_name")
            if not tag_name or not isinstance(tag_name, str):
                return _err("tag_name is required and must be a non-empty string.")
            try:
                mgr = GitLifecycleManager(Path(repo_path))
                mgr.validate()
                conn = ConfigManager.load().active_gitlab_connection()
                if conn is None:
                    return _no_gitlab_connection_err()
                from icx_engine.git.gitcmd import remote_url
                project_path = project_path_from_remote_url(remote_url(mgr.repo_root))
                if project_path is None:
                    return _err("Could not resolve a GitLab project from origin remote.")
                async with GitLabClient(conn.url, conn.token, conn.verify_tls) as client:
                    tag = await client.get_tag(project_path, tag_name)
                target_commit = (tag.get("commit") or {}).get("id") or tag.get("target")
                token = issue_token("delete_tag", {**arguments, "project_path": project_path})
                return [TextContent(type="text", text=json.dumps({
                    "status": "pending_confirmation",
                    "token": token,
                    "tag_name": tag_name,
                    "target_commit": target_commit,
                    "instruction": "Show the human the exact tag_name and its target_commit. This "
                                   "permanently deletes the tag object (never the commit/branch it "
                                   "points at) - GitLab has no tag recycle bin. Only call again with "
                                   "confirm_token once they explicitly agree.",
                }))]
            except Exception as exc:
                return _err(str(exc))
        try:
            payload = verify_token(confirm_token, "delete_tag")
            if payload is None:
                return _err("Invalid or already-used confirm_token. Call again without a token to get a fresh one.")
            conn = ConfigManager.load().active_gitlab_connection()
            if conn is None:
                return _no_gitlab_connection_err()
            async with GitLabClient(conn.url, conn.token, conn.verify_tls) as client:
                await client.delete_tag(payload["project_path"], payload["tag_name"])
            return _ok({"deleted": payload["tag_name"]})
        except Exception as exc:
            return _err(str(exc))

    if name == _RETAG_TOOL:
        confirm_token = arguments.get("confirm_token")
        if not confirm_token:
            repo_path = arguments.get("repo_path")
            if not repo_path or not isinstance(repo_path, str):
                return _err("repo_path is required and must be a non-empty string.")
            tag_name = arguments.get("tag_name")
            if not tag_name or not isinstance(tag_name, str):
                return _err("tag_name is required and must be a non-empty string.")
            branch = arguments.get("branch")
            if not branch or not isinstance(branch, str):
                return _err("branch is required and must be a non-empty string.")
            try:
                mgr = GitLifecycleManager(Path(repo_path))
                mgr.validate()
                conn = ConfigManager.load().active_gitlab_connection()
                if conn is None:
                    return _no_gitlab_connection_err()
                from icx_engine.git.gitcmd import remote_url
                project_path = project_path_from_remote_url(remote_url(mgr.repo_root))
                if project_path is None:
                    return _err("Could not resolve a GitLab project from origin remote.")
                async with GitLabClient(conn.url, conn.token, conn.verify_tls) as client:
                    old_tag = await client.get_tag(project_path, tag_name)
                    branches = await client.list_branches(project_path, search=branch)
                    matching = [b for b in branches if b.get("name") == branch]
                    if not matching:
                        return _err(
                            f"Branch '{branch}' not found on this project via gitlab_list_branches - "
                            "verify the real branch name before retagging."
                        )
                    new_target = (matching[0].get("commit") or {}).get("id")
                    ci_yaml_text: str | None = None
                    ci_check_error: str | None = None
                    try:
                        ci_yaml_text = await client.get_repository_file(project_path, ".gitlab-ci.yml", branch)
                    except Exception as exc:
                        ci_check_error = str(exc)
                previous_target = (old_tag.get("commit") or {}).get("id") or old_tag.get("target")
                ci_match = ci_tags.matches_any_pattern(tag_name, ci_yaml_text) if ci_yaml_text is not None else None
                token = issue_token("retag", {
                    "repo_path": repo_path, "tag_name": tag_name, "branch": branch,
                    "project_path": project_path, "previous_target": previous_target,
                })
                response = {
                    "status": "pending_confirmation",
                    "token": token,
                    "tag_name": tag_name,
                    "previous_target": previous_target,
                    "new_target": new_target,
                    "no_op": previous_target == new_target,
                    "ci_pipeline_will_trigger": ci_match,
                    "instruction": "Show the human tag_name, previous_target, and new_target - if "
                                   "no_op is true, this retag changes nothing, tell them plainly. "
                                   "This deletes then recreates the tag - if recreation fails after "
                                   "delete succeeds, the response will report previous_target so the "
                                   "tag can be recreated manually at that exact commit. Only call "
                                   "again with confirm_token once they explicitly agree.",
                }
                if ci_check_error:
                    response["ci_check_error"] = (
                        f"Could not fetch .gitlab-ci.yml to validate this retag would trigger a "
                        f"pipeline: {ci_check_error}. Proceeding is possible but unverified."
                    )
                return [TextContent(type="text", text=json.dumps(response))]
            except Exception as exc:
                return _err(str(exc))
        try:
            payload = verify_token(confirm_token, "retag")
            if payload is None:
                return _err("Invalid or already-used confirm_token. Call again without a token to get a fresh one.")
            conn = ConfigManager.load().active_gitlab_connection()
            if conn is None:
                return _no_gitlab_connection_err()
            async with GitLabClient(conn.url, conn.token, conn.verify_tls) as client:
                await client.delete_tag(payload["project_path"], payload["tag_name"])
                try:
                    result = await client.create_tag(payload["project_path"], payload["tag_name"], payload["branch"])
                except Exception as exc:
                    return _err(
                        f"Retag failed AFTER the old tag was already deleted: {exc}. The tag "
                        f"'{payload['tag_name']}' no longer exists. Its previous target commit was "
                        f"{payload['previous_target']} - recreate it manually at that exact commit "
                        "with git_create_tag's tag_name_override if this was unintended."
                    )
            return _ok({
                "tag": result["name"], "branch": payload["branch"],
                "previous_target": payload["previous_target"],
            })
        except Exception as exc:
            return _err(str(exc))

    return None
