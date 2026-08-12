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
_DIFF_WORKTREE_TOOL = "git_diff_worktree"
_STAGE_AND_COMMIT_TOOL = "git_stage_and_commit"
_REVERSE_MERGE_TOOL = "git_reverse_merge"
_GET_CONFLICT_TOOL = "git_get_conflict"
_READ_FILE_AT_REF_TOOL = "git_read_file_at_ref"
_COMPLETE_RESOLUTION_TOOL = "git_complete_resolution"
_ADOPT_RESOLUTION_TOOL = "git_adopt_resolution"
_DISCARD_SCRATCH_TOOL = "git_discard_scratch"
_PUSH_TOOL = "git_push"
_CREATE_MR_TOOL = "git_create_mr"
_FINISH_TICKET_TOOL = "git_finish_ticket"
_CREATE_TAG_TOOL = "git_create_tag"
_DELETE_TAG_TOOL = "git_delete_tag"
_RETAG_TOOL = "git_retag"
_STASH_CREATE_TOOL = "git_stash_create"
_STASH_LIST_TOOL = "git_stash_list"
_STASH_APPLY_TOOL = "git_stash_apply"
_STASH_POP_TOOL = "git_stash_pop"
_STASH_DROP_TOOL = "git_stash_drop"
_FETCH_TOOL = "git_fetch"
_PULL_TOOL = "git_pull"
_SYNC_TOOL = "git_sync"
_DELETE_BRANCH_TOOL = "git_delete_branch"
_GET_CONFLICT_DETAILS_TOOL = "git_get_conflict_details"
_CONFLICT_TAKE_OURS_TOOL = "git_conflict_take_ours"
_CONFLICT_TAKE_THEIRS_TOOL = "git_conflict_take_theirs"
_CONFLICT_APPLY_RESOLUTION_TOOL = "git_conflict_apply_resolution"
_CONFLICT_MARK_RESOLVED_TOOL = "git_conflict_mark_resolved"
_CONFLICT_ABORT_TOOL = "git_conflict_abort"
_CHECK_BRANCH_POLICY_TOOL = "git_check_branch_name_policy"
_SET_BRANCH_POLICY_TOOL = "git_set_branch_policy"

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
            "git_push/git_create_mr/git_stash_create/git_fetch/git_pull/git_sync/git_delete_branch/"
            "git_conflict_take_ours/git_conflict_take_theirs/git_conflict_apply_resolution/"
            "git_conflict_mark_resolved/git_conflict_abort instead - NEVER run `git stash`/"
            "`git fetch`/`git pull`/`git checkout --ours`/`--theirs`/`git add` on a conflicted "
            "file/`git merge --abort`/`git rebase --abort`/`git cherry-pick --abort` directly "
            "either, same rule as commit/checkout/push. This is what enforces the no-rebase/no-force-push "
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
            "answer. If this repo has require_ticket_in_branch_name enabled (see "
            "git_set_branch_policy/git_check_branch_name_policy - default OFF, preserves ticketless "
            "branches unless a repo explicitly opts in) and ticket_key is null, this REFUSES before "
            "creating anything, with an error naming the expected pattern - never creates a locally "
            "valid branch a remote pre-receive hook will then reject. Requires a valid git "
            "repository at repo_path."
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
                "ticket_key": {"type": ["string", "null"]},
            },
            "required": ["repo_path", "ticket_key"],
        },
    ),
    Tool(
        name=_DIFF_WORKTREE_TOOL,
        description=(
            "USE WHEN the human wants to see LOCAL uncommitted changes - what's staged, what's "
            "still unstaged, or everything uncommitted combined - as opposed to git_diff (which "
            "only compares two existing refs/branches/commits, never the working tree or index). "
            "MUST call with mode set to 'staged' (index vs HEAD - what the next commit would "
            "contain), 'unstaged' (working tree vs index - changes not yet staged), or 'combined' "
            "(working tree vs HEAD - every uncommitted change, staged or not). Pass relpath to "
            "scope to one file, omit for every changed file. Returns the same per-file "
            "status/insertions/deletions shape as git_diff. Read-only, UNGATED. Requires a valid "
            "git repository at repo_path."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "repo_path": {"type": "string"},
                "mode": {"type": "string", "enum": ["staged", "unstaged", "combined"]},
                "relpath": {"type": "string"},
            },
            "required": ["repo_path", "mode"],
        },
    ),
    Tool(
        name=_GET_CONFLICT_TOOL,
        description=(
            "USE WHEN a conflict needs inspecting and only whole-file ours/theirs content is "
            "needed - use git_get_conflict_details instead for base content and per-hunk line "
            "numbers. Works for ANY in-progress conflict, not just one git_reverse_merge produced - "
            "reads real index stages, so a manual `git merge`/`git pull`, a rebase, or a "
            "cherry-pick conflict inspects identically; never assumes ICX started it. Call once per "
            "file in conflicted_files. On ICX's own scratch-branch flow, suggest a resolution to the "
            "human; they edit the file directly, then you call git_complete_resolution - for an "
            "in-place conflict (no scratch branch), use git_conflict_take_ours/take_theirs/"
            "apply_resolution + git_conflict_mark_resolved instead. Requires a valid git repository "
            "at repo_path."
        ),
        inputSchema={"type": "object",
                     "properties": {"repo_path": {"type": "string"}, "file": {"type": "string"}},
                     "required": ["repo_path", "file"]},
    ),
    Tool(
        name=_READ_FILE_AT_REF_TOOL,
        description=(
            "USE WHEN the human or agent needs a file's exact content at a specific point in git "
            "history - HEAD, MERGE_HEAD (mid-conflict), a branch, origin/<branch>, or a commit sha "
            "- e.g. diagnosing a dependency-pin mismatch or inspecting what a merge would bring "
            "in. MUST call with ref and path. Read-only, local, no network call. Fails clearly if "
            "ref does not resolve or path does not exist at that ref. Requires a valid git "
            "repository at repo_path."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "repo_path": {"type": "string"},
                "ref": {"type": "string"},
                "path": {"type": "string"},
            },
            "required": ["repo_path", "ref", "path"],
        },
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
            "parent_branch set to their answer. ticket_key is nullable - pass null if there is no "
            "ticket; the MR title is then just ticket_summary with no prefix, never a manufactured "
            "ticket id. A merge refusal right after creation is not final: GitLab computes "
            "mergeability asynchronously, so a refusal while it is still CHECKING is polled (bounded "
            "by max_poll_attempts/poll_delay_seconds, default 5 attempts / 2s apart - never "
            "indefinitely) and the merge is retried exactly once if it settles on MERGEABLE. The "
            "response's merge_status is one of MERGEABLE/CONFLICTED/CHECKING/BLOCKED/UNKNOWN - "
            "CHECKING means it never left that state within the poll budget (raise "
            "max_poll_attempts/poll_delay_seconds and retry rather than assuming failure); BLOCKED "
            "covers every named non-conflict refusal (ci_still_running/not_approved/need_rebase/"
            "discussions_not_resolved/draft_status/policies_denied/etc, from refusal_reason). "
            "Requires an active GitLab connection."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "repo_path": {"type": "string"},
                "parent_branch": {"type": "string"},
                "ticket_key": {"type": ["string", "null"]},
                "ticket_summary": {"type": "string"},
                "max_poll_attempts": {"type": "integer"},
                "poll_delay_seconds": {"type": "number"},
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
            "the human, then call again with parent_branch set to their answer. ticket_key is "
            "nullable - pass null if there is no ticket (used only for backup naming when "
            "delete_backups is set; never invent a ticket id). Requires an "
            "active GitLab connection."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "repo_path": {"type": "string"},
                "parent_branch": {"type": "string"},
                "feature_branch": {"type": "string"},
                "ticket_key": {"type": ["string", "null"]},
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
    Tool(
        name=_STASH_CREATE_TOOL,
        description=(
            "USE WHEN the human wants to set aside uncommitted changes without committing them - "
            "e.g. before pulling/syncing with a dirty tree. MUST call this via ICX - NEVER run `git "
            "stash push`/`git stash` directly yourself. Stashes staged, "
            "unstaged, AND untracked changes together (matches git_repo_status's dirty_files scope) "
            "- NOT confirmation-gated, nothing is lost by stashing (git_stash_list/apply/pop can "
            "always retrieve it). Requires a valid git repository at repo_path."
        ),
        inputSchema={
            "type": "object",
            "properties": {"repo_path": {"type": "string"}, "message": {"type": "string"}},
            "required": ["repo_path", "message"],
        },
    ),
    Tool(
        name=_STASH_LIST_TOOL,
        description=(
            "USE WHEN the human wants to see what's currently stashed in this repo. Returns every "
            "stash newest-first, each with index, ref (the exact stash@{N} string to pass to "
            "git_stash_apply/git_stash_pop/git_stash_drop), and message. Read-only, UNGATED. "
            "Requires a valid git repository at repo_path."
        ),
        inputSchema={"type": "object", "properties": {"repo_path": {"type": "string"}},
                     "required": ["repo_path"]},
    ),
    Tool(
        name=_STASH_APPLY_TOOL,
        description=(
            "USE WHEN the human wants a stash's changes back in the working tree WITHOUT removing "
            "it from the stash list - use git_stash_pop instead to apply-and-remove in one step. "
            "MUST call this via ICX - NEVER run `git stash apply` directly yourself. "
            "Call git_stash_list first to get the exact ref if not applying the most recent (default "
            "stash@{0}). Fails clearly (never silently drops data) if applying would conflict with "
            "the current working tree. NOT confirmation-gated - the stash itself is never lost even "
            "on failure. Requires a valid git repository at repo_path."
        ),
        inputSchema={
            "type": "object",
            "properties": {"repo_path": {"type": "string"}, "ref": {"type": "string"}},
            "required": ["repo_path"],
        },
    ),
    Tool(
        name=_STASH_POP_TOOL,
        description=(
            "USE WHEN the human wants a stash's changes back in the working tree AND removed from "
            "the stash list in one step - use git_stash_apply instead to keep it in the list. "
            "MUST call this via ICX - NEVER run `git stash pop` directly yourself. Call "
            "git_stash_list first to get the exact ref if popping something other than the most "
            "recent (default stash@{0}). If popping conflicts with the current working tree, git "
            "keeps the stash in the list rather than losing it - the error says so. NOT "
            "confirmation-gated - matches git_reverse_merge's own internal stash-pop behavior. "
            "Requires a valid git repository at repo_path."
        ),
        inputSchema={
            "type": "object",
            "properties": {"repo_path": {"type": "string"}, "ref": {"type": "string"}},
            "required": ["repo_path"],
        },
    ),
    Tool(
        name=_STASH_DROP_TOOL,
        description=(
            "USE WHEN the human explicitly wants a stash permanently discarded without ever "
            "applying it. MUST call this via ICX - NEVER run `git stash drop` directly yourself. "
            "Call git_stash_list first to show the human the real message for the "
            "exact ref about to be dropped - never guess which stash they mean. CONFIRMATION-GATED: "
            "the first call (no confirm_token) returns pending_confirmation with ref and message - "
            "show both to the human and get explicit agreement before calling again with "
            "confirm_token. This is NOT recoverable through any ICX tool once dropped. Requires a "
            "valid git repository at repo_path."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "repo_path": {"type": "string"},
                "ref": {"type": "string"},
                "confirm_token": {"type": "string"},
            },
            "required": ["repo_path"],
        },
    ),
    Tool(
        name=_FETCH_TOOL,
        description=(
            "USE WHEN the human wants to download remote refs WITHOUT touching the working tree or "
            "any local branch - git_fetch only updates remote-tracking refs (origin/<branch>), it "
            "never changes what's checked out. MUST call this via ICX - NEVER run `git fetch` "
            "directly yourself. Use git_pull/git_sync instead to actually integrate "
            "those changes into the current branch. Read-only w.r.t. the working tree, UNGATED. "
            "Pass ref to fetch one specific branch instead of everything, prune=true to also delete "
            "local remote-tracking refs for branches removed on the remote. Requires a valid git "
            "repository at repo_path."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "repo_path": {"type": "string"},
                "remote": {"type": "string"},
                "ref": {"type": "string"},
                "prune": {"type": "boolean"},
            },
            "required": ["repo_path"],
        },
    ),
    Tool(
        name=_PULL_TOOL,
        description=(
            "USE WHEN the human wants the CURRENT branch brought up to date with its OWN remote "
            "counterpart (plain `git pull` semantics) - MUST call this via ICX, NEVER run `git pull` "
            "directly yourself; use git_reverse_merge instead to bring a "
            "DIFFERENT parent/target branch's changes in. strategy='ff-only' (default) refuses "
            "(status='diverged_needs_merge') rather than ever creating a merge commit - the safe "
            "default. strategy='merge' performs a real, conflict-capable merge (never rebase) with "
            "the exact same backup-first/stash-if-dirty/conflict-quarantine safety net as "
            "git_reverse_merge - a conflict returns status='conflict' plus scratch_branch and "
            "conflicted_files; use git_get_conflict/git_complete_resolution/git_adopt_resolution to "
            "finish, identically. NOT confirmation-gated - matches git_reverse_merge's own "
            "ungated-because-safe-by-construction convention. ticket_key is nullable (backup/stash "
            "naming only). Requires a valid git repository at repo_path."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "repo_path": {"type": "string"},
                "remote": {"type": "string"},
                "strategy": {"type": "string", "enum": ["ff-only", "merge"]},
                "ticket_key": {"type": ["string", "null"]},
            },
            "required": ["repo_path"],
        },
    ),
    Tool(
        name=_SYNC_TOOL,
        description=(
            "USE WHEN the human just says 'sync my branch' / 'update from remote' with no further "
            "detail - MUST call this via ICX, NEVER run `git pull`/`git fetch`/`git stash` directly "
            "yourself. A one-shot, opinionated convenience wrapper: fetches, stashes any dirty "
            "working tree automatically, integrates the current branch's own remote counterpart via "
            "a real conflict-capable merge (equivalent to git_pull with strategy='merge'), and "
            "restores the stash afterward - all in one call, same safety net as git_reverse_merge/"
            "git_pull (backup-first, never rebase, conflict quarantines onto a scratch branch rather "
            "than ever touching the real branch destructively). A conflict returns status='conflict' "
            "plus scratch_branch and conflicted_files; use git_get_conflict/git_complete_resolution/"
            "git_adopt_resolution to finish. For anything more specific - a DIFFERENT target branch, "
            "or explicit control over merge vs ff-only - use git_reverse_merge or git_pull directly "
            "instead. NOT confirmation-gated, same reasoning as git_pull. ticket_key is nullable "
            "(backup/stash naming only). Requires a valid git repository at repo_path."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "repo_path": {"type": "string"},
                "remote": {"type": "string"},
                "ticket_key": {"type": ["string", "null"]},
            },
            "required": ["repo_path"],
        },
    ),
    Tool(
        name=_DELETE_BRANCH_TOOL,
        description=(
            "USE WHEN the human wants a branch deleted - local, remote, or both - e.g. after "
            "merging without going through git_finish_ticket. NEVER run `git push origin --delete` "
            "or `git branch -D` directly yourself. target is required - the branch that must still "
            "contain every commit being deleted (e.g. the parent/development branch); the tool "
            "computes unique_commits (commits on branch unreachable from target - would be LOST) "
            "and REFUSES outright, before any token is even issued, if unique_commits > 0 and "
            "force is not true. Deleting the CURRENT checked-out branch is refused unconditionally "
            "- not overridable by force, a hard git constraint. CONFIRMATION-GATED (once safety "
            "checks pass): the first call (no confirm_token) returns pending_confirmation with "
            "branch, target, unique_commits, delete_local, and delete_remote - show all of this to "
            "the human and get explicit agreement before calling again with confirm_token. "
            "delete_local defaults true, delete_remote defaults false - set both explicitly for "
            "full cleanup. Remote deletion is a real push (`--delete`) and equally permanent. "
            "Requires a valid git repository at repo_path."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "repo_path": {"type": "string"},
                "branch": {"type": "string"},
                "target": {"type": "string"},
                "remote": {"type": "string"},
                "delete_local": {"type": "boolean"},
                "delete_remote": {"type": "boolean"},
                "force": {"type": "boolean"},
                "confirm_token": {"type": "string"},
            },
            "required": ["repo_path", "branch", "target"],
        },
    ),
    Tool(
        name=_GET_CONFLICT_DETAILS_TOOL,
        description=(
            "USE WHEN a conflicted file needs full inspection beyond ours/theirs alone - base "
            "(common-ancestor) content, and every conflict hunk with exact start_line/end_line plus "
            "its own ours/theirs text, matching what a human sees open in an editor. Works for ANY "
            "in-progress conflict regardless of what caused it - ICX's own git_reverse_merge/"
            "git_pull/git_sync scratch-branch quarantine, a manual `git merge`/`git pull`, a rebase, "
            "or a cherry-pick - NEVER assumes ICX started it. base is null when no common ancestor "
            "exists for this path (e.g. an add/add conflict); ours/theirs are null on the deleting "
            "side of a delete/modify conflict. Also returns conflict_state (CONFLICT_DETECTED/"
            "STAGED/CLEAN) - a live label computed fresh from real repo state every call, never "
            "stored. Read-only, UNGATED. Requires a valid git repository at repo_path."
        ),
        inputSchema={
            "type": "object",
            "properties": {"repo_path": {"type": "string"}, "file": {"type": "string"}},
            "required": ["repo_path", "file"],
        },
    ),
    Tool(
        name=_CONFLICT_TAKE_OURS_TOOL,
        description=(
            "USE WHEN the human has decided, for ONE specific conflicted file, that the OURS side "
            "is fully correct and the THEIRS side should be discarded entirely for that file - use "
            "git_conflict_apply_resolution instead if neither side alone is correct. MUST call this "
            "via ICX - NEVER run `git checkout --ours` directly yourself. Resolves the file's "
            "on-disk content to its ours version - does NOT stage it (still shows as unmerged until "
            "git_conflict_mark_resolved). CONFIRMATION-GATED: the first call (no confirm_token) shows "
            "the human the file and the exact ours content that will replace the current conflicted "
            "content - only call again with confirm_token once they explicitly agree. NEVER combine "
            "this with staging or committing in the same call - each stays a separate, inspectable "
            "step. Requires a valid git repository at repo_path."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "repo_path": {"type": "string"},
                "file": {"type": "string"},
                "confirm_token": {"type": "string"},
            },
            "required": ["repo_path", "file"],
        },
    ),
    Tool(
        name=_CONFLICT_TAKE_THEIRS_TOOL,
        description=(
            "USE WHEN the human has decided, for ONE specific conflicted file, that the THEIRS side "
            "is fully correct and the OURS side should be discarded entirely for that file - use "
            "git_conflict_apply_resolution instead if neither side alone is correct. MUST call this "
            "via ICX - NEVER run `git checkout --theirs` directly yourself. Resolves the file's "
            "on-disk content to its theirs version - does NOT stage it (still shows as unmerged "
            "until git_conflict_mark_resolved). CONFIRMATION-GATED: the first call (no confirm_token) "
            "shows the human the file and the exact theirs content that will replace the current "
            "conflicted content - only call again with confirm_token once they explicitly agree. "
            "NEVER combine this with staging or committing in the same call. Requires a valid git "
            "repository at repo_path."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "repo_path": {"type": "string"},
                "file": {"type": "string"},
                "confirm_token": {"type": "string"},
            },
            "required": ["repo_path", "file"],
        },
    ),
    Tool(
        name=_CONFLICT_APPLY_RESOLUTION_TOOL,
        description=(
            "USE WHEN a conflicted file's ENTIRE content needs replacing with a specific hand- or "
            "agent-resolved version that the human has reviewed - use this instead of "
            "git_conflict_take_ours/take_theirs when neither side alone is correct (e.g. the real "
            "fix combines parts of both, or is neither). MUST call this via ICX - resolved_content "
            "IS the full new file content, not a patch or a diff. Does NOT stage it. "
            "CONFIRMATION-GATED: the first call (no confirm_token) returns a unified diff between "
            "the CURRENT on-disk (still-conflicted) content and resolved_content - show this diff to "
            "the human, get explicit agreement, then call again with confirm_token. NEVER combine "
            "with staging or committing in the same call. Requires a valid git repository at "
            "repo_path."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "repo_path": {"type": "string"},
                "file": {"type": "string"},
                "resolved_content": {"type": "string"},
                "confirm_token": {"type": "string"},
            },
            "required": ["repo_path", "file", "resolved_content"],
        },
    ),
    Tool(
        name=_CONFLICT_MARK_RESOLVED_TOOL,
        description=(
            "USE WHEN every conflicted file the human wants resolved right now has already been "
            "fixed (via git_conflict_take_ours/take_theirs/apply_resolution, or the human editing "
            "them directly) and is ready to be staged - this is the STAGE step, deliberately "
            "separate from committing; use git_stage_and_commit afterward for that, as its own "
            "separate gate. MUST call this via ICX - NEVER run `git add` on a conflicted file "
            "yourself. Hard-blocks (refuses, never issues a token) if ANY listed file still has "
            "literal conflict-marker text, or is not currently an unmerged/conflicted path - never "
            "stages a file this tool did not itself verify. CONFIRMATION-GATED: the first call (no "
            "confirm_token) returns pending_confirmation listing exactly the files about to be "
            "staged - show these to the human, get explicit agreement, then call again with "
            "confirm_token. Requires a valid git repository at repo_path."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "repo_path": {"type": "string"},
                "files": {"type": "array", "items": {"type": "string"}},
                "confirm_token": {"type": "string"},
            },
            "required": ["repo_path", "files"],
        },
    ),
    Tool(
        name=_CONFLICT_ABORT_TOOL,
        description=(
            "USE WHEN the human wants to abandon an in-progress merge, cherry-pick, or rebase "
            "ENTIRELY - not just one file, the whole operation - discarding all conflict-resolution "
            "progress made so far and restoring the pre-operation state. Detects which of the three "
            "is actually in progress from real repo state - NEVER assumes merge specifically. MUST "
            "call this via ICX - NEVER run `git merge --abort`/`git cherry-pick --abort`/"
            "`git rebase --abort` directly yourself. This never rewrites history and never starts, "
            "continues, or drives a rebase - it only ever backs one out. Real content-losing "
            "operation for any resolution work not yet committed - CONFIRMATION-GATED: the first "
            "call (no confirm_token) shows the human which operation and which conflicted files are "
            "about to be abandoned, get explicit agreement, then call again with confirm_token. "
            "Fails clearly if nothing is actually in progress - never silently no-ops. Requires a "
            "valid git repository at repo_path."
        ),
        inputSchema={
            "type": "object",
            "properties": {"repo_path": {"type": "string"}, "confirm_token": {"type": "string"}},
            "required": ["repo_path"],
        },
    ),
    Tool(
        name=_CHECK_BRANCH_POLICY_TOOL,
        description=(
            "USE WHEN a candidate branch name needs validating BEFORE calling git_start_branch (or "
            "before pushing an already-existing branch) - e.g. deciding whether to ask the human "
            "for a ticket key first. Validates branch_name against this repo's configured policy - "
            "require_ticket_suffix in the response reflects the actual per-repo setting (see "
            "git_set_branch_policy), never guessed. Reuses the exact same trailing-ticket-key "
            "pattern naming.py already parses branch names with - one source of truth, no separate "
            "org-specific pattern invented here. valid=false includes a reason formatted as "
            "'Invalid branch name / Expected pattern / Received / Missing JIRA/ticket identifier' - "
            "show this verbatim to the human. Read-only, UNGATED. Requires a valid git repository "
            "at repo_path."
        ),
        inputSchema={
            "type": "object",
            "properties": {"repo_path": {"type": "string"}, "branch_name": {"type": "string"}},
            "required": ["repo_path", "branch_name"],
        },
    ),
    Tool(
        name=_SET_BRANCH_POLICY_TOOL,
        description=(
            "USE WHEN the human wants this repo to require (or stop requiring) a trailing ticket "
            "key on every feature branch ICX creates or pushes - e.g. after a remote pre-receive "
            "hook rejected a ticketless branch ICX created successfully locally. Defaults OFF for "
            "every repo (preserves the existing, documented ticketless-branch feature) - this is "
            "the only way to turn it on; ICX never infers an org's real policy automatically. "
            "Purely local - writes this repo's ICX settings file, never touches git or GitLab. Not "
            "destructive, trivially reversible (call again with the opposite value) - NOT "
            "confirmation-gated. Requires a valid git repository at repo_path."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "repo_path": {"type": "string"},
                "require_ticket_in_branch_name": {"type": "boolean"},
            },
            "required": ["repo_path", "require_ticket_in_branch_name"],
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


def _dispatch_take_side(arguments: dict, side: str) -> list[TextContent]:
    """Shared implementation for git_conflict_take_ours/take_theirs - identical shape
    except which index stage (2=ours, 3=theirs) is resolved onto the working tree."""
    action = f"conflict_take_{side}"
    confirm_token = arguments.get("confirm_token")
    if not confirm_token:
        repo_path = arguments.get("repo_path")
        if not repo_path or not isinstance(repo_path, str):
            return _err("repo_path is required and must be a non-empty string.")
        file = arguments.get("file")
        if not file or not isinstance(file, str):
            return _err("file is required and must be a non-empty string.")
        try:
            from icx_engine.git import gitcmd
            mgr = GitLifecycleManager(Path(repo_path))
            mgr.validate()
            if file not in gitcmd.conflicted_files(mgr.repo_root):
                return _err(
                    f"'{file}' is not currently a conflicted path. Call git_repo_status or "
                    "git_get_conflict_details to see real conflicted files."
                )
            stage = 2 if side == "ours" else 3
            content = gitcmd.conflict_stage(mgr.repo_root, file, stage)
            if content is None:
                return _err(
                    f"'{file}' has no {side} content for this conflict (e.g. it was added or "
                    "deleted on that side) - nothing to take."
                )
            token = issue_token(action, {"repo_path": repo_path, "file": file})
            return [TextContent(type="text", text=json.dumps({
                "status": "pending_confirmation",
                "token": token,
                "file": file,
                "side": side,
                "content": content,
                "instruction": f"Show the human the file and the exact {side} content that will "
                               "replace the current conflicted content on disk (not staged, not "
                               "committed). Only call again with confirm_token once they explicitly "
                               "agree.",
            }))]
        except Exception as exc:
            return _err(str(exc))
    try:
        payload = verify_token(confirm_token, action)
        if payload is None:
            return _err("Invalid or already-used confirm_token. Call again without a token to get a fresh one.")
        from icx_engine.git import gitcmd
        mgr = GitLifecycleManager(Path(payload["repo_path"]))
        mgr.validate()
        gitcmd.checkout_conflict_side(mgr.repo_root, payload["file"], side)
        return _ok({
            "file": payload["file"], "resolved_to": side,
            "conflict_state": gitcmd.conflict_state(mgr.repo_root),
        })
    except Exception as exc:
        return _err(str(exc))


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
            from icx_engine.git import gitcmd
            from icx_engine.git.gitcmd import current_branch
            dirty_status = mgr.check_dirty_tree()
            leftover = mgr.check_leftover_state()
            rich = gitcmd.structured_status(mgr.repo_root)
            return _ok(attach_skill_hint({
                "current_branch": current_branch(mgr.repo_root),
                "dirty": dirty_status.dirty,
                "dirty_files": dirty_status.files,
                "leftover_state_clean": leftover.is_clean,
                "scratch_branches": leftover.scratch_branches,
                "icx_stashes": leftover.icx_stashes,
                "merge_in_progress": leftover.merge_in_progress,
                "staged": rich["staged"],
                "unstaged": rich["unstaged"],
                "untracked": rich["untracked"],
                "deleted": rich["deleted"],
                "renamed": rich["renamed"],
                "conflicted": rich["conflicted"],
                "ahead": rich["ahead"],
                "behind": rich["behind"],
                "upstream": rich["upstream"],
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

    if name == _DIFF_WORKTREE_TOOL:
        repo_path = arguments.get("repo_path")
        if not repo_path or not isinstance(repo_path, str):
            return _err("repo_path is required and must be a non-empty string.")
        mode = arguments.get("mode")
        if mode not in ("staged", "unstaged", "combined"):
            return _err("mode is required and must be 'staged', 'unstaged', or 'combined'.")
        relpath = arguments.get("relpath")
        if relpath is not None and not isinstance(relpath, str):
            return _err("relpath must be a string.")
        try:
            from icx_engine.git import gitcmd
            mgr = GitLifecycleManager(Path(repo_path))
            mgr.validate()
            result = gitcmd.diff_worktree(mgr.repo_root, mode=mode, relpath=relpath)
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
        if "ticket_key" not in arguments:
            return _err("ticket_key is required (pass null if there is no ticket).")
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

    if name == _READ_FILE_AT_REF_TOOL:
        repo_path = arguments.get("repo_path")
        if not repo_path or not isinstance(repo_path, str):
            return _err("repo_path is required and must be a non-empty string.")
        ref = arguments.get("ref")
        if not ref or not isinstance(ref, str):
            return _err("ref is required and must be a non-empty string.")
        file_path = arguments.get("path")
        if not file_path or not isinstance(file_path, str):
            return _err("path is required and must be a non-empty string.")
        try:
            from icx_engine.git import gitcmd
            mgr = GitLifecycleManager(Path(repo_path))
            mgr.validate()
            content = gitcmd.read_file_at_ref(mgr.repo_root, ref, file_path)
            return _ok({"ref": ref, "path": file_path, "content": content})
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
                policy = mgr.check_branch_name_policy(branch)
                if not policy.valid:
                    return _err(policy.reason)
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
                if "ticket_key" not in arguments:
                    return _err("ticket_key is required (pass null if there is no ticket).")
                ticket_key = arguments.get("ticket_key")
                if ticket_key is not None and not isinstance(ticket_key, str):
                    return _err("ticket_key must be a string or null.")
                ticket_summary = arguments.get("ticket_summary")
                if not ticket_summary or not isinstance(ticket_summary, str):
                    return _err("ticket_summary is required and must be a non-empty string.")
                mgr = GitLifecycleManager(Path(repo_path))
                mgr.validate()
                from icx_engine.git.gitcmd import current_branch
                source_branch = current_branch(mgr.repo_root)
                policy = mgr.check_branch_name_policy(source_branch)
                if not policy.valid:
                    return _err(policy.reason)
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
            max_poll_attempts = payload.get("max_poll_attempts") or 5
            poll_delay_seconds = payload.get("poll_delay_seconds") or 2.0
            result = await mgr.create_mr_for_ticket(
                payload["parent_branch"], payload["ticket_key"], payload["ticket_summary"], conn,
                max_poll_attempts=max_poll_attempts, poll_delay_seconds=poll_delay_seconds,
            )
            return _ok({
                "mr_iid": result.mr_iid, "created": result.created,
                "merged": result.merged, "merge_status": result.merge_status,
                "refusal_reason": result.refusal_reason,
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
                if "ticket_key" not in arguments:
                    return _err("ticket_key is required (pass null if there is no ticket).")
                ticket_key = arguments.get("ticket_key")
                if ticket_key is not None and not isinstance(ticket_key, str):
                    return _err("ticket_key must be a string or null.")
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

    if name == _STASH_CREATE_TOOL:
        repo_path = arguments.get("repo_path")
        if not repo_path or not isinstance(repo_path, str):
            return _err("repo_path is required and must be a non-empty string.")
        message = arguments.get("message")
        if not message or not isinstance(message, str):
            return _err("message is required and must be a non-empty string.")
        try:
            from icx_engine.git import gitcmd
            mgr = GitLifecycleManager(Path(repo_path))
            mgr.validate()
            gitcmd.stash_push(mgr.repo_root, message)
            return _ok({"stashed": True, "message": message})
        except Exception as exc:
            return _err(str(exc))

    if name == _STASH_LIST_TOOL:
        repo_path = arguments.get("repo_path")
        if not repo_path or not isinstance(repo_path, str):
            return _err("repo_path is required and must be a non-empty string.")
        try:
            from icx_engine.git import gitcmd
            mgr = GitLifecycleManager(Path(repo_path))
            mgr.validate()
            return _ok({"stashes": gitcmd.stash_list(mgr.repo_root)})
        except Exception as exc:
            return _err(str(exc))

    if name == _STASH_APPLY_TOOL:
        repo_path = arguments.get("repo_path")
        if not repo_path or not isinstance(repo_path, str):
            return _err("repo_path is required and must be a non-empty string.")
        ref = arguments.get("ref") or "stash@{0}"
        try:
            from icx_engine.git import gitcmd
            mgr = GitLifecycleManager(Path(repo_path))
            mgr.validate()
            gitcmd.stash_apply(mgr.repo_root, ref)
            return _ok({"applied": ref})
        except Exception as exc:
            return _err(str(exc))

    if name == _STASH_POP_TOOL:
        repo_path = arguments.get("repo_path")
        if not repo_path or not isinstance(repo_path, str):
            return _err("repo_path is required and must be a non-empty string.")
        ref = arguments.get("ref")
        try:
            from icx_engine.git import gitcmd
            mgr = GitLifecycleManager(Path(repo_path))
            mgr.validate()
            gitcmd.stash_pop(mgr.repo_root, ref)
            return _ok({"popped": ref or "stash@{0}"})
        except Exception as exc:
            return _err(str(exc))

    if name == _STASH_DROP_TOOL:
        confirm_token = arguments.get("confirm_token")
        if not confirm_token:
            repo_path = arguments.get("repo_path")
            if not repo_path or not isinstance(repo_path, str):
                return _err("repo_path is required and must be a non-empty string.")
            ref = arguments.get("ref") or "stash@{0}"
            try:
                from icx_engine.git import gitcmd
                mgr = GitLifecycleManager(Path(repo_path))
                mgr.validate()
                stashes = {s["ref"]: s["message"] for s in gitcmd.stash_list(mgr.repo_root)}
                if ref not in stashes:
                    return _err(f"No stash found at '{ref}'. Call git_stash_list to see real refs.")
                token = issue_token("stash_drop", {"repo_path": repo_path, "ref": ref})
                return [TextContent(type="text", text=json.dumps({
                    "status": "pending_confirmation",
                    "token": token,
                    "ref": ref,
                    "message": stashes[ref],
                    "instruction": "Show the human the exact ref and message of the stash about to "
                                   "be PERMANENTLY discarded. Only call again with confirm_token once "
                                   "they explicitly agree.",
                }))]
            except Exception as exc:
                return _err(str(exc))
        try:
            payload = verify_token(confirm_token, "stash_drop")
            if payload is None:
                return _err("Invalid or already-used confirm_token. Call again without a token to get a fresh one.")
            from icx_engine.git import gitcmd
            mgr = GitLifecycleManager(Path(payload["repo_path"]))
            mgr.validate()
            gitcmd.stash_drop(mgr.repo_root, payload["ref"])
            return _ok({"dropped": payload["ref"]})
        except Exception as exc:
            return _err(str(exc))

    if name == _FETCH_TOOL:
        repo_path = arguments.get("repo_path")
        if not repo_path or not isinstance(repo_path, str):
            return _err("repo_path is required and must be a non-empty string.")
        remote = arguments.get("remote") or "origin"
        ref = arguments.get("ref")
        prune = bool(arguments.get("prune", False))
        try:
            from icx_engine.git import gitcmd
            mgr = GitLifecycleManager(Path(repo_path))
            mgr.validate()
            gitcmd.fetch(mgr.repo_root, remote=remote, ref=ref, prune=prune, extra_env=mgr._auth_env(remote))
            return _ok({"remote": remote, "ref": ref, "prune": prune, "fetched": True})
        except Exception as exc:
            return _err(str(exc))

    if name == _PULL_TOOL:
        repo_path = arguments.get("repo_path")
        if not repo_path or not isinstance(repo_path, str):
            return _err("repo_path is required and must be a non-empty string.")
        remote = arguments.get("remote") or "origin"
        strategy = arguments.get("strategy") or "ff-only"
        if strategy not in ("ff-only", "merge"):
            return _err("strategy must be 'ff-only' or 'merge'.")
        ticket_key = arguments.get("ticket_key")
        if ticket_key is not None and not isinstance(ticket_key, str):
            return _err("ticket_key must be a string or null.")
        try:
            mgr = GitLifecycleManager(Path(repo_path))
            mgr.validate()
            result = mgr.pull(remote=remote, strategy=strategy, ticket_key=ticket_key)
            return _ok({
                "status": result.status, "conflicted_files": result.conflicted_files,
                "scratch_branch": result.scratch_branch,
            })
        except Exception as exc:
            return _err(str(exc))

    if name == _SYNC_TOOL:
        repo_path = arguments.get("repo_path")
        if not repo_path or not isinstance(repo_path, str):
            return _err("repo_path is required and must be a non-empty string.")
        remote = arguments.get("remote") or "origin"
        ticket_key = arguments.get("ticket_key")
        if ticket_key is not None and not isinstance(ticket_key, str):
            return _err("ticket_key must be a string or null.")
        try:
            mgr = GitLifecycleManager(Path(repo_path))
            mgr.validate()
            result = mgr.pull(remote=remote, strategy="merge", ticket_key=ticket_key)
            return _ok({
                "status": result.status, "conflicted_files": result.conflicted_files,
                "scratch_branch": result.scratch_branch,
            })
        except Exception as exc:
            return _err(str(exc))

    if name == _DELETE_BRANCH_TOOL:
        confirm_token = arguments.get("confirm_token")
        if not confirm_token:
            repo_path = arguments.get("repo_path")
            if not repo_path or not isinstance(repo_path, str):
                return _err("repo_path is required and must be a non-empty string.")
            branch = arguments.get("branch")
            if not branch or not isinstance(branch, str):
                return _err("branch is required and must be a non-empty string.")
            target = arguments.get("target")
            if not target or not isinstance(target, str):
                return _err(
                    "target is required and must be a non-empty string - the branch that must "
                    "still contain every commit being deleted."
                )
            remote = arguments.get("remote") or "origin"
            delete_local = arguments.get("delete_local", True)
            delete_remote = arguments.get("delete_remote", False)
            force = bool(arguments.get("force", False))
            try:
                from icx_engine.git import gitcmd
                mgr = GitLifecycleManager(Path(repo_path))
                mgr.validate()
                if gitcmd.current_branch(mgr.repo_root) == branch:
                    return _err(
                        f"'{branch}' is the currently checked-out branch - switch to a different "
                        "branch before deleting it. Not overridable by force."
                    )
                branch_exists_locally = gitcmd.local_branch_exists(mgr.repo_root, branch)
                if delete_local and not branch_exists_locally:
                    return _err(f"Local branch '{branch}' does not exist - nothing to delete locally.")
                unique = gitcmd.unique_commit_count(mgr.repo_root, branch, target) if branch_exists_locally else 0
                if unique > 0 and not force:
                    return _err(
                        f"Branch '{branch}' cannot be safely deleted - {unique} commit(s) are not "
                        f"reachable from '{target}' and would be lost. Call again with force=true "
                        "(no confirm_token) if the human explicitly wants to delete it anyway."
                    )
                token = issue_token("delete_branch", {
                    "repo_path": repo_path, "branch": branch, "target": target, "remote": remote,
                    "delete_local": delete_local, "delete_remote": delete_remote, "force": force,
                })
                return [TextContent(type="text", text=json.dumps({
                    "status": "pending_confirmation",
                    "token": token,
                    "branch": branch,
                    "target": target,
                    "unique_commits": unique,
                    "delete_local": delete_local,
                    "delete_remote": delete_remote,
                    "instruction": "Show the human branch, target, and unique_commits (commits that "
                                   "would become unreachable if deleted). Only call again with "
                                   "confirm_token once they explicitly agree.",
                }))]
            except Exception as exc:
                return _err(str(exc))
        try:
            payload = verify_token(confirm_token, "delete_branch")
            if payload is None:
                return _err("Invalid or already-used confirm_token. Call again without a token to get a fresh one.")
            mgr = GitLifecycleManager(Path(payload["repo_path"]))
            mgr.validate()
            result = mgr.delete_branch_safely(
                payload["branch"], payload["target"], remote=payload["remote"],
                delete_local=payload["delete_local"], delete_remote=payload["delete_remote"],
                force=payload["force"],
            )
            return _ok({
                "branch": result.branch, "local_deleted": result.local_deleted,
                "remote_deleted": result.remote_deleted, "unique_commits": result.unique_commits,
            })
        except Exception as exc:
            return _err(str(exc))

    if name == _GET_CONFLICT_DETAILS_TOOL:
        repo_path = arguments.get("repo_path")
        if not repo_path or not isinstance(repo_path, str):
            return _err("repo_path is required and must be a non-empty string.")
        conflict_file = arguments.get("file")
        if not conflict_file or not isinstance(conflict_file, str):
            return _err("file is required and must be a non-empty string.")
        try:
            from icx_engine.git import gitcmd
            mgr = GitLifecycleManager(Path(repo_path))
            mgr.validate()
            base = gitcmd.conflict_stage(mgr.repo_root, conflict_file, 1)
            ours = gitcmd.conflict_stage(mgr.repo_root, conflict_file, 2)
            theirs = gitcmd.conflict_stage(mgr.repo_root, conflict_file, 3)
            hunks = gitcmd.parse_conflict_hunks(mgr.repo_root, conflict_file)
            return _ok({
                "file": conflict_file, "base": base, "ours": ours, "theirs": theirs,
                "hunks": hunks, "conflict_state": gitcmd.conflict_state(mgr.repo_root),
            })
        except Exception as exc:
            return _err(str(exc))

    if name == _CONFLICT_TAKE_OURS_TOOL:
        return _dispatch_take_side(arguments, "ours")

    if name == _CONFLICT_TAKE_THEIRS_TOOL:
        return _dispatch_take_side(arguments, "theirs")

    if name == _CONFLICT_APPLY_RESOLUTION_TOOL:
        confirm_token = arguments.get("confirm_token")
        if not confirm_token:
            repo_path = arguments.get("repo_path")
            if not repo_path or not isinstance(repo_path, str):
                return _err("repo_path is required and must be a non-empty string.")
            conflict_file = arguments.get("file")
            if not conflict_file or not isinstance(conflict_file, str):
                return _err("file is required and must be a non-empty string.")
            resolved_content = arguments.get("resolved_content")
            if resolved_content is None or not isinstance(resolved_content, str):
                return _err("resolved_content is required and must be a string.")
            try:
                from icx_engine.git import gitcmd
                mgr = GitLifecycleManager(Path(repo_path))
                mgr.validate()
                if conflict_file not in gitcmd.conflicted_files(mgr.repo_root):
                    return _err(
                        f"'{conflict_file}' is not currently a conflicted path. Call "
                        "git_repo_status or git_get_conflict_details to see real conflicted files."
                    )
                current_content = (mgr.repo_root / conflict_file).read_text(encoding="utf-8", errors="replace")
                import difflib
                diff = "".join(difflib.unified_diff(
                    current_content.splitlines(keepends=True),
                    resolved_content.splitlines(keepends=True),
                    fromfile=f"{conflict_file} (current, conflicted)",
                    tofile=f"{conflict_file} (proposed resolution)",
                ))
                token = issue_token("conflict_apply_resolution", {
                    "repo_path": repo_path, "file": conflict_file, "resolved_content": resolved_content,
                })
                return [TextContent(type="text", text=json.dumps({
                    "status": "pending_confirmation",
                    "token": token,
                    "file": conflict_file,
                    "diff": diff,
                    "instruction": "Show the human this diff between the current conflicted content "
                                   "and the proposed resolution. Only call again with confirm_token "
                                   "once they explicitly agree.",
                }))]
            except Exception as exc:
                return _err(str(exc))
        try:
            payload = verify_token(confirm_token, "conflict_apply_resolution")
            if payload is None:
                return _err("Invalid or already-used confirm_token. Call again without a token to get a fresh one.")
            from icx_engine.git import gitcmd
            mgr = GitLifecycleManager(Path(payload["repo_path"]))
            mgr.validate()
            (mgr.repo_root / payload["file"]).write_text(payload["resolved_content"], encoding="utf-8")
            return _ok({
                "file": payload["file"], "applied": True,
                "conflict_state": gitcmd.conflict_state(mgr.repo_root),
            })
        except Exception as exc:
            return _err(str(exc))

    if name == _CONFLICT_MARK_RESOLVED_TOOL:
        confirm_token = arguments.get("confirm_token")
        if not confirm_token:
            repo_path = arguments.get("repo_path")
            if not repo_path or not isinstance(repo_path, str):
                return _err("repo_path is required and must be a non-empty string.")
            files = arguments.get("files")
            if not isinstance(files, list) or not files:
                return _err("files is required and must be a non-empty list.")
            try:
                from icx_engine.git import gitcmd
                mgr = GitLifecycleManager(Path(repo_path))
                mgr.validate()
                unmerged = set(gitcmd.conflicted_files(mgr.repo_root))
                not_conflicted = [f for f in files if f not in unmerged]
                if not_conflicted:
                    return _err(
                        "These files are not currently conflicted paths - refusing to stage them "
                        f"here: {', '.join(not_conflicted)}. Use git_stage_and_commit for ordinary "
                        "staging."
                    )
                remaining = gitcmd.find_conflict_markers(mgr.repo_root, files)
                if remaining:
                    bad_files = ", ".join(remaining.keys())
                    return _err(
                        f"Conflict markers still present in: {bad_files}. Resolve them fully before "
                        "marking resolved."
                    )
                token = issue_token("conflict_mark_resolved", {"repo_path": repo_path, "files": files})
                return [TextContent(type="text", text=json.dumps({
                    "status": "pending_confirmation",
                    "token": token,
                    "files": files,
                    "instruction": "Show the human exactly these files about to be staged (not "
                                   "committed). Only call again with confirm_token once they "
                                   "explicitly agree.",
                }))]
            except Exception as exc:
                return _err(str(exc))
        try:
            payload = verify_token(confirm_token, "conflict_mark_resolved")
            if payload is None:
                return _err("Invalid or already-used confirm_token. Call again without a token to get a fresh one.")
            from icx_engine.git import gitcmd
            mgr = GitLifecycleManager(Path(payload["repo_path"]))
            mgr.validate()
            gitcmd.stage_files(mgr.repo_root, payload["files"])
            return _ok({"staged": payload["files"], "conflict_state": gitcmd.conflict_state(mgr.repo_root)})
        except Exception as exc:
            return _err(str(exc))

    if name == _CONFLICT_ABORT_TOOL:
        confirm_token = arguments.get("confirm_token")
        if not confirm_token:
            repo_path = arguments.get("repo_path")
            if not repo_path or not isinstance(repo_path, str):
                return _err("repo_path is required and must be a non-empty string.")
            try:
                from icx_engine.git import gitcmd
                mgr = GitLifecycleManager(Path(repo_path))
                mgr.validate()
                git_dir = mgr.repo_root / ".git"
                if (git_dir / "MERGE_HEAD").exists():
                    operation = "merge"
                elif (git_dir / "CHERRY_PICK_HEAD").exists():
                    operation = "cherry-pick"
                elif (git_dir / "rebase-merge").exists() or (git_dir / "rebase-apply").exists():
                    operation = "rebase"
                else:
                    return _err("No merge, cherry-pick, or rebase is currently in progress - nothing to abort.")
                conflicted = gitcmd.conflicted_files(mgr.repo_root)
                token = issue_token("conflict_abort", {"repo_path": repo_path})
                return [TextContent(type="text", text=json.dumps({
                    "status": "pending_confirmation",
                    "token": token,
                    "operation": operation,
                    "conflicted_files": conflicted,
                    "instruction": f"Show the human that a {operation} is about to be COMPLETELY "
                                   "abandoned, discarding all resolution progress on the listed "
                                   "conflicted_files. Only call again with confirm_token once they "
                                   "explicitly agree.",
                }))]
            except Exception as exc:
                return _err(str(exc))
        try:
            payload = verify_token(confirm_token, "conflict_abort")
            if payload is None:
                return _err("Invalid or already-used confirm_token. Call again without a token to get a fresh one.")
            from icx_engine.git import gitcmd
            mgr = GitLifecycleManager(Path(payload["repo_path"]))
            mgr.validate()
            aborted = gitcmd.abort_in_progress_operation(mgr.repo_root)
            return _ok({"aborted": aborted, "conflict_state": gitcmd.conflict_state(mgr.repo_root)})
        except Exception as exc:
            return _err(str(exc))

    if name == _CHECK_BRANCH_POLICY_TOOL:
        repo_path = arguments.get("repo_path")
        if not repo_path or not isinstance(repo_path, str):
            return _err("repo_path is required and must be a non-empty string.")
        branch_name = arguments.get("branch_name")
        if not branch_name or not isinstance(branch_name, str):
            return _err("branch_name is required and must be a non-empty string.")
        try:
            mgr = GitLifecycleManager(Path(repo_path))
            mgr.validate()
            policy = mgr.check_branch_name_policy(branch_name)
            return _ok({
                "valid": policy.valid,
                "branch": policy.branch,
                "require_ticket_suffix": policy.require_ticket_suffix,
                "reason": policy.reason,
                "expected_pattern": policy.expected_pattern,
                "missing_ticket": policy.missing_ticket,
            })
        except Exception as exc:
            return _err(str(exc))

    if name == _SET_BRANCH_POLICY_TOOL:
        repo_path = arguments.get("repo_path")
        if not repo_path or not isinstance(repo_path, str):
            return _err("repo_path is required and must be a non-empty string.")
        if "require_ticket_in_branch_name" not in arguments:
            return _err("require_ticket_in_branch_name is required and must be a boolean.")
        require_ticket = arguments.get("require_ticket_in_branch_name")
        if not isinstance(require_ticket, bool):
            return _err("require_ticket_in_branch_name must be a boolean.")
        try:
            mgr = GitLifecycleManager(Path(repo_path))
            mgr.validate()
            mgr.set_branch_name_policy(require_ticket)
            return _ok({"require_ticket_in_branch_name": require_ticket})
        except Exception as exc:
            return _err(str(exc))

    return None
