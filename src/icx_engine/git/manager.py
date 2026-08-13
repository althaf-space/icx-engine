"""Orchestration layer for the git-workflow lifecycle. Every method returns a
structured result and never calls a prompt/confirm function directly - the
CLI and MCP layers own all human interaction, this layer only owns git state
and decisions (design spec: manager is UI-agnostic by design so the same
logic serves both front doors)."""
from __future__ import annotations

import asyncio
import base64
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from icx_engine.git.gitcmd import (
    is_git_repo, repo_root, fetch, remote_branch_exists, default_remote_head_branch,
    dirty_files, stash_push, create_branch_from, checkout, local_branch_exists,
    current_branch, fast_forward, stage_files, commit, added_lines_diff, GitCommandError,
    head_sha, merge_ref, merge_abort, conflicted_files, stash_pop,
    conflict_versions, find_conflict_markers, fast_forward_ref, delete_branch,
    commits_since, changed_files_since, remote_url, file_exists_at_ref, push,
    delete_remote_branch, unique_commit_count, is_ancestor, list_local_branches,
)
from icx_engine.git.naming import (
    derive_branch_name, ticketless_branch_name, parse_ticket_key_from_branch, slugify,
)
from icx_engine.git.policy import validate_branch_name, BranchPolicyResult
from icx_engine.git.settings import read_repo_settings, write_repo_settings
from icx_engine.git.safety import (
    detect_leftover_state, LeftoverState, create_backup, create_scratch_branch, prune_old_backups,
    sync_backup,
)
from icx_engine.gitlab.client import GitLabClient, project_path_from_remote_url
from icx_engine.gitlab.service import create_and_merge_mr


class GitWorkflowError(RuntimeError):
    """Raised for any lifecycle precondition failure - never for a git
    subprocess failure directly (those raise GitCommandError); this is the
    higher-level error the CLI/MCP layers catch and report to the human."""


def _gitlab_push_auth_env(gitlab_conn, origin_url: str) -> dict[str, str] | None:
    """Build extra env vars that make a `fetch`/`push` subprocess authenticate
    to GitLab with `gitlab_conn`'s stored personal access token - the real fix
    for git push failing with 'could not read Username' even though ICX's own
    GitLab connection is valid: that token was only ever used for GitLab's
    REST API (create_and_merge_mr, validate), never for raw git-over-HTTPS,
    since gitcmd.py deliberately disables credential.helper and terminal
    prompts on every git subprocess it runs (so an automated call never hangs
    on a prompt) without substituting anything in their place.

    Injected via GIT_CONFIG_COUNT/GIT_CONFIG_KEY_n/GIT_CONFIG_VALUE_n (git
    >=2.31) as an `http.extraheader` - never in the remote URL or argv, so the
    token never shows up in `git remote -v` or a process listing.

    Returns None (no injection - caller's fetch/push behaves exactly as
    before, relying on whatever git credential already exists) when:
    - `origin_url` is not http(s) - an SSH remote authenticates via the
      user's own SSH key, a separate credential path this never touches.
    - `origin_url`'s host does not match `gitlab_conn.url`'s host - never
      send a credential to a remote it wasn't configured for.
    - `gitlab_conn` has no token.
    """
    if not gitlab_conn or not gitlab_conn.token:
        return None
    origin_parsed = urlparse(origin_url)
    if origin_parsed.scheme not in ("http", "https"):
        return None
    conn_parsed = urlparse(gitlab_conn.url)
    if origin_parsed.netloc.lower() != conn_parsed.netloc.lower():
        return None

    basic = base64.b64encode(f"oauth2:{gitlab_conn.token}".encode()).decode()
    entries = [("http.extraheader", f"Authorization: Basic {basic}")]
    if not gitlab_conn.verify_tls:
        entries.append(("http.sslVerify", "false"))
    env = {"GIT_CONFIG_COUNT": str(len(entries))}
    for i, (key, value) in enumerate(entries):
        env[f"GIT_CONFIG_KEY_{i}"] = key
        env[f"GIT_CONFIG_VALUE_{i}"] = value
    return env


_DEBUG_PATTERNS = (
    re.compile(r"console\.log\("),
    re.compile(r"System\.out\.println\("),
    re.compile(r"\bprint\("),
    re.compile(r"\bdebugger\b"),
)
_AI_ATTRIBUTION_RE = re.compile(
    r"^(Co-Authored-By:.*Claude.*|Generated with.*Claude.*|Powered by.*Claude.*)$",
    re.IGNORECASE,
)


def strip_ai_attribution(message: str) -> str:
    lines = [ln for ln in message.splitlines() if not _AI_ATTRIBUTION_RE.match(ln.strip())]
    return "\n".join(lines).rstrip()


@dataclass
class ParentResolution:
    status: str  # "resolved" | "needs_confirmation" | "needs_manual_pick"
    parent_branch: str | None = None
    proposed_default: str | None = None
    available_branches: list[str] = field(default_factory=list)


@dataclass
class DirtyTreeStatus:
    dirty: bool
    files: list[str]


@dataclass
class BranchStartResult:
    branch_name: str
    created: bool
    switched_to_existing: bool
    commits_behind_parent: int = 0


@dataclass
class SyncResult:
    status: str  # "up_to_date" | "fast_forwarded" | "diverged_needs_merge"


@dataclass
class PullResult:
    status: str  # "up_to_date" | "fast_forwarded" | "merged" | "conflict" | "diverged_needs_merge"
    conflicted_files: list[str] = field(default_factory=list)
    scratch_branch: str | None = None


@dataclass
class DebugLeftover:
    file: str
    line: str


@dataclass
class CommitResult:
    sha: str
    debug_warnings: list[DebugLeftover]


@dataclass
class ReverseMergeResult:
    status: str  # "clean" | "conflict"
    conflicted_files: list[str] = field(default_factory=list)


@dataclass
class ScratchSession:
    scratch_branch: str
    conflicted_files: list[str]


@dataclass
class ConflictPayload:
    file: str
    ours: str
    theirs: str


@dataclass
class MrDescription:
    change_summary: str
    api_impact: str
    db_changes: str
    config_changes: str
    deployment_notes: str
    rollback_notes: str


@dataclass
class CreateMrResult:
    mr_iid: int
    created: bool
    merged: bool
    merge_status: str = "UNKNOWN"  # MERGEABLE | CONFLICTED | CHECKING | BLOCKED | UNKNOWN
    refusal_reason: str | None = None
    has_conflicts: bool | None = None
    pipeline: dict | None = None


@dataclass
class CleanupResult:
    parent_branch: str
    feature_branch_deleted: bool
    backups_deleted: list[str] = field(default_factory=list)


@dataclass
class BranchDeleteResult:
    branch: str
    local_deleted: bool
    remote_deleted: bool
    unique_commits: int


_MIGRATION_PATH_RE = re.compile(r"(^|/)migrations?/", re.IGNORECASE)
_CONFIG_FILE_RE = re.compile(r"\.(ya?ml|env|properties|toml|ini)$|(^|/)config(\.|/)", re.IGNORECASE)
_API_PATH_RE = re.compile(r"(^|/)(controllers?|routes?|api)/|openapi", re.IGNORECASE)


class GitLifecycleManager:
    def __init__(self, repo_path: Path, gitlab_conn=None):
        self._repo_path = Path(repo_path)
        self._repo_root: Path | None = None
        # None means "not resolved yet", not "no connection" - _auth_env()
        # lazily resolves via ConfigManager the first time it's needed and
        # caches the result, so a caller never has to remember to pass one in.
        self._gitlab_conn = gitlab_conn
        self._gitlab_conn_resolved = gitlab_conn is not None

    def validate(self) -> Path:
        if not is_git_repo(self._repo_path):
            raise GitWorkflowError(
                f"'{self._repo_path}' is not a git repository. Run this from inside one."
            )
        self._repo_root = repo_root(self._repo_path)
        return self._repo_root

    @property
    def repo_root(self) -> Path:
        if self._repo_root is None:
            raise GitWorkflowError("validate() must be called before using this manager.")
        return self._repo_root

    def _auth_env(self, remote: str = "origin") -> dict[str, str] | None:
        """The single source of git-network auth for every fetch/remote_branch_exists/
        push call this manager makes. Exists so a new method never has to remember to
        wire this in separately - the exact failure mode that first shipped this fix
        only for push, then had to be patched again for git_create_mr's own fetch/
        ls-remote calls, which were still running with no credential at all. Every
        network call in this class must route through here instead of computing its
        own auth env."""
        if not self._gitlab_conn_resolved:
            from icx_engine.config_manager import ConfigManager  # noqa: PLC0415
            self._gitlab_conn = ConfigManager.load().active_gitlab_connection()
            self._gitlab_conn_resolved = True
        return _gitlab_push_auth_env(self._gitlab_conn, remote_url(self.repo_root, remote))

    def check_leftover_state(self) -> LeftoverState:
        return detect_leftover_state(self.repo_root)

    def resolve_parent_branch(self) -> ParentResolution:
        auth_env = self._auth_env()
        fetch(self.repo_root, extra_env=auth_env)
        stored = read_repo_settings(self.repo_root).get("parent_branch")
        if stored and remote_branch_exists(self.repo_root, stored, extra_env=auth_env):
            return ParentResolution(status="resolved", parent_branch=stored)

        if remote_branch_exists(self.repo_root, "development", extra_env=auth_env):
            return ParentResolution(status="needs_confirmation", proposed_default="development")

        head = default_remote_head_branch(self.repo_root)
        available = [head] if head else []
        return ParentResolution(status="needs_manual_pick", available_branches=available)

    def confirm_parent_branch(self, chosen: str) -> None:
        """Normalizes an accidental `origin/<branch>` prefix to the bare branch
        name before checking/storing it - real bug this fixes: passing
        'origin/development' got rejected as 'does not exist' (remote_branch_exists
        checks a bare branch name against `ls-remote --heads`, which never has an
        'origin/' prefix in its own ref names), and if it HAD been stored with the
        prefix, every downstream `f"origin/{parent_branch}"` call (start_branch,
        reverse_merge_standard, ...) would have built a broken
        'origin/origin/development' ref."""
        normalized = chosen[len("origin/"):] if chosen.startswith("origin/") else chosen
        if not remote_branch_exists(self.repo_root, normalized, extra_env=self._auth_env()):
            raise GitWorkflowError(
                f"Branch '{normalized}' does not exist on origin. Check the name and try again "
                "(pass just the branch name, e.g. 'development' - not 'origin/development')."
            )
        write_repo_settings(self.repo_root, parent_branch=normalized)

    def check_dirty_tree(self) -> DirtyTreeStatus:
        files = dirty_files(self.repo_root)
        return DirtyTreeStatus(dirty=len(files) > 0, files=files)

    def stash_dirty_tree(self, ticket_key: str) -> None:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        stash_push(self.repo_root, f"icx:{ticket_key}:{timestamp}")

    def check_branch_name_policy(self, branch_name: str) -> BranchPolicyResult:
        """Validates branch_name against this repo's configured naming policy -
        require_ticket_in_branch_name (git/settings.py, default False - preserves
        the existing ticketless-branch feature for repos that never opted into
        stricter enforcement). See git/policy.py for what "valid" means."""
        require_ticket = bool(read_repo_settings(self.repo_root).get("require_ticket_in_branch_name", False))
        return validate_branch_name(branch_name, require_ticket_suffix=require_ticket)

    def set_branch_name_policy(self, require_ticket_in_branch_name: bool) -> None:
        write_repo_settings(self.repo_root, require_ticket_in_branch_name=require_ticket_in_branch_name)

    def start_branch(
        self, ticket_key: str | None, summary_or_preferred_name: str, parent_branch: str, remote: str = "origin",
    ) -> BranchStartResult:
        """Fetches `remote` FIRST, always - real bug this fixes: confirm_parent_branch
        only verifies a branch exists on the remote (a live `ls-remote`, always fresh)
        but never updates the LOCAL `origin/<parent_branch>` tracking ref this method
        branches from - a caller that already has parent_branch in hand (skipping
        resolve_parent_branch's own fetch) could otherwise branch off a tracking ref
        that's arbitrarily stale, however recently it was last fetched."""
        fetch(self.repo_root, remote=remote, extra_env=self._auth_env(remote))
        branch_name = (
            derive_branch_name(ticket_key, summary_or_preferred_name)
            if ticket_key else ticketless_branch_name(summary_or_preferred_name)
        )
        policy = self.check_branch_name_policy(branch_name)
        if not policy.valid:
            raise GitWorkflowError(policy.reason)
        parent_ref = f"{remote}/{parent_branch}"
        if local_branch_exists(self.repo_root, branch_name):
            checkout(self.repo_root, branch_name)
            behind = unique_commit_count(self.repo_root, parent_ref, branch_name)
            return BranchStartResult(
                branch_name=branch_name, created=False, switched_to_existing=True,
                commits_behind_parent=behind,
            )

        create_branch_from(self.repo_root, branch_name, parent_ref)
        checkout(self.repo_root, branch_name)
        return BranchStartResult(branch_name=branch_name, created=True, switched_to_existing=False)

    def current_ticket_key(self) -> str | None:
        return parse_ticket_key_from_branch(current_branch(self.repo_root))

    def sync_with_remote(self, remote: str = "origin") -> SyncResult:
        branch = current_branch(self.repo_root)
        fetch(self.repo_root, remote=remote, extra_env=self._auth_env(remote))
        before = head_sha(self.repo_root)
        try:
            fast_forward(self.repo_root, branch, remote=remote)
        except GitCommandError:
            return SyncResult(status="diverged_needs_merge")
        after = head_sha(self.repo_root)
        return SyncResult(status="fast_forwarded" if after != before else "up_to_date")

    def pull(
        self, remote: str = "origin", strategy: str = "ff-only", ticket_key: str | None = None,
    ) -> PullResult:
        """git pull's fetch+integrate step, scoped to the CURRENT branch's own
        remote-tracking counterpart (origin/<current-branch>) - never a
        different parent branch (see reverse_merge_standard/git_reverse_merge
        for that). strategy='ff-only' (default) is sync_with_remote's existing
        safe behavior - refuses (status='diverged_needs_merge') rather than
        ever creating a merge commit. strategy='merge' reuses
        reverse_merge_standard/start_conflict_resolution verbatim, passing the
        current branch itself as "parent_branch" - a clean divergence
        auto-fast-forwards (git's own merge default), a real divergence
        creates a merge commit, and a genuine conflict quarantines onto a
        disposable scratch branch exactly like git_reverse_merge (same
        backup-first, stash-if-dirty, conflict-quarantine safety net - never
        rebase, forbidden by this module's safety doctrine)."""
        if strategy not in ("ff-only", "merge"):
            raise GitWorkflowError(f"strategy must be 'ff-only' or 'merge', got {strategy!r}")
        if strategy == "ff-only":
            sync_result = self.sync_with_remote(remote)
            return PullResult(status=sync_result.status)
        branch = current_branch(self.repo_root)
        result = self.reverse_merge_standard(branch, ticket_key, remote=remote)
        if result.status == "clean":
            return PullResult(status="merged")
        session = self.start_conflict_resolution(branch, ticket_key, remote=remote)
        return PullResult(
            status="conflict", conflicted_files=session.conflicted_files, scratch_branch=session.scratch_branch,
        )

    def scan_staged_debug_leftovers(self) -> list[DebugLeftover]:
        findings: list[DebugLeftover] = []
        for filename, lines in added_lines_diff(self.repo_root).items():
            for line in lines:
                if any(p.search(line) for p in _DEBUG_PATTERNS):
                    findings.append(DebugLeftover(file=filename, line=line.strip()))
        return findings

    def stage_and_commit(
        self, files: list[str], message: str, ticket_key: str | None,
    ) -> CommitResult:
        cleaned = strip_ai_attribution(message)
        first_line = cleaned.splitlines()[0] if cleaned.splitlines() else ""
        if ticket_key:
            if not first_line.startswith(ticket_key):
                raise GitWorkflowError(
                    f"Commit message must start with '{ticket_key}'. Got: {first_line!r}"
                )
            description = first_line[len(ticket_key):].strip()
        else:
            description = first_line.strip()
        if not description:
            raise GitWorkflowError("Commit message needs a real description, not just the ticket key.")

        stage_files(self.repo_root, files)
        warnings = self.scan_staged_debug_leftovers()
        sha = commit(self.repo_root, cleaned)
        branch = current_branch(self.repo_root)
        backup_key = ticket_key or slugify(branch)
        sync_backup(self.repo_root, branch, backup_key)
        return CommitResult(sha=sha, debug_warnings=warnings)

    def _pop_stash_or_explain(self) -> None:
        try:
            stash_pop(self.repo_root)
        except GitCommandError as exc:
            raise GitWorkflowError(
                "Reverse merge succeeded, but restoring your stashed changes conflicted with "
                "the newly merged content. Your stashed work is NOT lost (still in 'git stash "
                "list') - resolve the conflict markers in your working tree by hand, then run "
                "'git stash drop' once you're satisfied, or discard local changes and "
                "'git stash pop' again to retry from a clean state."
            ) from exc

    def reverse_merge_standard(
        self, parent_branch: str, ticket_key: str | None, remote: str = "origin",
    ) -> ReverseMergeResult:
        """Standard path (design spec Section 7.1): stash if dirty, merge parent
        into the current branch, pop the stash back. On conflict, abort the
        merge completely and still pop the stash back before returning - the
        feature branch is never left in a conflicted or half-stashed state.
        ticket_key is nullable - when absent, backup/stash naming falls back
        to a slug of the current branch (same fallback as stage_and_commit).
        Passes self._auth_env() to fetch - the real fix for git_reverse_merge
        failing with "could not read Username" on an HTTPS origin even with a
        valid GitLab connection: this was the one fetch call in this class
        that _auth_env's own docstring warned about missing (every OTHER
        network call - sync_with_remote, create_mr_for_ticket,
        post_merge_cleanup - already routed through it)."""
        fetch(self.repo_root, remote=remote, extra_env=self._auth_env(remote))
        branch = current_branch(self.repo_root)
        backup_key = ticket_key or slugify(branch)
        create_backup(self.repo_root, branch, backup_key)

        was_dirty = len(dirty_files(self.repo_root)) > 0
        if was_dirty:
            self.stash_dirty_tree(backup_key)

        try:
            try:
                merge_ref(self.repo_root, f"{remote}/{parent_branch}")
            except GitCommandError:
                conflicts = conflicted_files(self.repo_root)
                if not conflicts:
                    raise
                merge_abort(self.repo_root)
                return ReverseMergeResult(status="conflict", conflicted_files=conflicts)
            return ReverseMergeResult(status="clean", conflicted_files=[])
        finally:
            if was_dirty:
                self._pop_stash_or_explain()

    def start_conflict_resolution(
        self, parent_branch: str, ticket_key: str | None, remote: str = "origin",
    ) -> ScratchSession:
        """Escalation path (design spec Section 7.2 steps 2-4): back up the
        current branch, stash if dirty, create a disposable scratch branch off
        it, and redo the reverse merge there. The real feature branch is never
        checked out again during this call - it stays exactly where it was,
        untouched. Any stashed work is popped back onto the SCRATCH branch
        (not feature) once the merge attempt completes, so it sits alongside
        whatever conflict-resolution work happens there rather than being lost
        or left stranded on feature. ticket_key is nullable - see
        reverse_merge_standard's docstring for the fallback naming."""
        feature_branch = current_branch(self.repo_root)
        backup_key = ticket_key or slugify(feature_branch)
        create_backup(self.repo_root, feature_branch, backup_key)

        was_dirty = len(dirty_files(self.repo_root)) > 0
        if was_dirty:
            self.stash_dirty_tree(backup_key)

        try:
            scratch_branch = create_scratch_branch(self.repo_root, feature_branch, backup_key)
            try:
                merge_ref(self.repo_root, f"{remote}/{parent_branch}")
            except GitCommandError:
                conflicts = conflicted_files(self.repo_root)
                if not conflicts:
                    # Not a real conflict - some other failure (bad ref, network, etc). Don't
                    # silently report "nothing to resolve"; let the caller see the real error.
                    raise
                return ScratchSession(scratch_branch=scratch_branch, conflicted_files=conflicts)
            # Merge succeeded cleanly on scratch (e.g. remote state shifted between the standard-path
            # attempt and this call) - nothing to resolve, caller can complete/adopt immediately.
            return ScratchSession(scratch_branch=scratch_branch, conflicted_files=[])
        finally:
            if was_dirty:
                self._pop_stash_or_explain()

    def get_conflict(self, relpath: str) -> ConflictPayload:
        """Read ours/theirs content for one conflicted file on the CURRENTLY
        checked-out branch - reads real index stages (2=ours, 3=theirs), so
        this works regardless of what produced the conflict: ICX's own
        scratch-branch quarantine flow, a manual `git merge`/`git pull`, a
        rebase, or a cherry-pick. Not scratch-branch-specific despite living
        alongside that flow in this class."""
        ours, theirs = conflict_versions(self.repo_root, relpath)
        return ConflictPayload(file=relpath, ours=ours, theirs=theirs)

    def list_conflicted_files(self) -> list[str]:
        return conflicted_files(self.repo_root)

    def complete_scratch_resolution(self, files: list[str], message: str) -> str:
        """Validates every given file has no remaining conflict markers (design
        spec Section 7.2 step 5 - universal, language-agnostic check), then
        stages and commits them on the CURRENTLY checked-out scratch branch.
        Hard-blocks (raises) if any marker remains - never commits a partial
        or unresolved file."""
        remaining = find_conflict_markers(self.repo_root, files)
        if remaining:
            bad_files = ", ".join(remaining.keys())
            raise GitWorkflowError(
                f"Conflict markers still present in: {bad_files}. Resolve them fully before completing."
            )
        stage_files(self.repo_root, files)
        return commit(self.repo_root, message)

    def adopt_scratch_resolution(self, feature_branch: str, scratch_branch: str) -> str:
        """Atomically adopt the scratch branch's resolved result onto the real
        feature branch (design spec Section 7.2 step 5, closing sentence) - a
        fast-forward-only merge, always safe since scratch's history contains
        feature's pre-quarantine tip as an ancestor. Deletes the scratch
        branch afterward."""
        checkout(self.repo_root, feature_branch)
        fast_forward_ref(self.repo_root, scratch_branch)
        delete_branch(self.repo_root, scratch_branch, force=True)
        return head_sha(self.repo_root)

    def discard_scratch_resolution(self, feature_branch: str, scratch_branch: str) -> None:
        """Self-heal discard (design spec Section 10) - abandon an interrupted
        or unwanted scratch-branch resolution attempt. The feature branch was
        never touched by the scratch flow. If the scratch branch still has an
        unresolved merge in progress, git refuses any checkout away from it
        until that's cleared - abort it first (the scratch branch is being
        deleted anyway, so there is nothing left to preserve), then checkout
        and delete."""
        if (self.repo_root / ".git" / "MERGE_HEAD").exists():
            merge_abort(self.repo_root)
        checkout(self.repo_root, feature_branch)
        delete_branch(self.repo_root, scratch_branch, force=True)

    def build_mr_description(self, base_ref: str) -> MrDescription:
        """Mechanical, CLI-side MR description fill (design spec Section 8.5) -
        commit list plus file-pattern-based section flags. The MCP path lets
        the agent write a richer version; this is the deterministic floor
        that works identically with no LLM."""
        commits = commits_since(self.repo_root, base_ref)
        files = changed_files_since(self.repo_root, base_ref)

        change_summary = "\n".join(f"- {line}" for line in commits) if commits else "-"

        db_files = [f for f in files if _MIGRATION_PATH_RE.search(f)]
        config_files = [f for f in files if _CONFIG_FILE_RE.search(f)]
        api_files = [f for f in files if _API_PATH_RE.search(f)]

        return MrDescription(
            change_summary=change_summary,
            api_impact=", ".join(api_files) if api_files else "-",
            db_changes=", ".join(db_files) if db_files else "-",
            config_changes=", ".join(config_files) if config_files else "-",
            deployment_notes="-",
            rollback_notes="-",
        )

    async def create_mr_for_ticket(
        self, parent_branch: str, ticket_key: str | None, ticket_summary: str, gitlab_conn,
        max_poll_attempts: int = 5, poll_delay_seconds: float = 2.0,
    ) -> CreateMrResult:
        """MR creation + one immediate merge attempt (design spec Section 8.3-
        8.4). Order matters and is deliberate: resolve the GitLab project from
        the origin remote URL (raises GitWorkflowError if unrecognizable -
        never guesses) -> validate the GitLab connection (one cheap request,
        fails fast on a bad token before any git work) -> push the feature
        branch to origin (the branch must exist on the remote before GitLab
        can create an MR from it) -> create/reuse + attempt merge. ticket_key
        is nullable - the MR title is just ticket_summary with no prefix when
        absent, never a manufactured ticket id."""
        origin = remote_url(self.repo_root)
        project_path = project_path_from_remote_url(origin)
        if project_path is None:
            raise GitWorkflowError(
                f"Could not resolve a GitLab project from origin remote '{origin}'. "
                "Is this repo actually hosted on the connected GitLab server?"
            )
        # Validate the GitLab connection FIRST - a single cheap request - before any git fetch/push
        # work (which can take up to ~120s combined). Checking this last meant an invalid token was
        # only discovered after the user had already waited through the entire git operation.
        async with GitLabClient(gitlab_conn.url, gitlab_conn.token, gitlab_conn.verify_tls) as client:
            identity = await client.validate()
        if not identity.get("valid"):
            raise GitWorkflowError(
                f"GitLab connection is not valid (HTTP {identity.get('status_code')}) - "
                "run `icx gitlab --add` again."
            )
        assignee_id = identity["user"]["id"]

        self._gitlab_conn = gitlab_conn
        self._gitlab_conn_resolved = True
        auth_env = self._auth_env()
        feature_branch = current_branch(self.repo_root)
        fetch(self.repo_root, extra_env=auth_env)
        push(self.repo_root, feature_branch, extra_env=auth_env)
        description_sections = self.build_mr_description(parent_branch)
        description = (
            f"## Change summary\n{description_sections.change_summary}\n\n"
            f"## API impact\n{description_sections.api_impact}\n\n"
            f"## Database changes\n{description_sections.db_changes}\n\n"
            f"## Config changes\n{description_sections.config_changes}\n\n"
            f"## Deployment notes\n{description_sections.deployment_notes}\n\n"
            f"## Rollback notes\n{description_sections.rollback_notes}\n"
        )

        title = f"{ticket_key} {ticket_summary}" if ticket_key else ticket_summary
        result = await create_and_merge_mr(
            gitlab_conn, project_path, feature_branch, parent_branch,
            title, description, assignee_id,
            max_poll_attempts=max_poll_attempts, poll_delay_seconds=poll_delay_seconds,
        )
        return CreateMrResult(
            mr_iid=result["mr_iid"], created=result["created"], merged=result["merged"],
            merge_status=result.get("merge_status", "UNKNOWN"), refusal_reason=result.get("refusal_reason"),
            has_conflicts=result.get("has_conflicts"), pipeline=result.get("pipeline"),
        )

    def post_merge_cleanup(
        self, parent_branch: str, feature_branch: str, ticket_key: str | None,
        delete_backups: bool, gitlab_conn, mr_iid: int,
    ) -> CleanupResult:
        """Post-merge cleanup (design spec Section 8.6). Never trusts the
        caller's word that the MR merged - re-checks via the GitLab API first
        and refuses if it isn't actually merged. The only local write to
        parent is the fast-forward pointer-move (never a merge)."""
        origin = remote_url(self.repo_root)
        project_path = project_path_from_remote_url(origin)
        if project_path is None:
            raise GitWorkflowError(f"Could not resolve a GitLab project from origin remote '{origin}'.")
        self._gitlab_conn = gitlab_conn
        self._gitlab_conn_resolved = True

        async def _check_merged() -> bool:
            async with GitLabClient(gitlab_conn.url, gitlab_conn.token, gitlab_conn.verify_tls) as client:
                mr = await client.get_merge_request(project_path, mr_iid)
            return mr.get("state") == "merged"

        if not asyncio.run(_check_merged()):
            raise GitWorkflowError(
                f"MR !{mr_iid} is not actually merged yet - refusing to clean up. "
                "Finish the review/merge in GitLab first."
            )

        # "Verify changes present" - proves added files genuinely landed on parent (existence
        # check). For files the feature branch only modified, this confirms the path still
        # exists but does not re-verify the specific content change - a real but partial
        # content check, not blind trust of the API status alone.
        # Captured now, while HEAD is still the feature branch, so the diff is
        # parent_branch...HEAD(feature) - the real set of files the feature branch changed.
        touched = changed_files_since(self.repo_root, parent_branch) if local_branch_exists(self.repo_root, feature_branch) else []

        checkout(self.repo_root, parent_branch)
        fetch(self.repo_root, extra_env=self._auth_env())
        fast_forward(self.repo_root, parent_branch)

        if touched:
            missing = [f for f in touched if not file_exists_at_ref(self.repo_root, "HEAD", f)]
            if missing:
                raise GitWorkflowError(
                    f"Parent branch was updated but these files from the feature branch are "
                    f"still missing: {', '.join(missing)}. Something is wrong - stopping before "
                    "deleting the local feature branch."
                )

        deleted = False
        if local_branch_exists(self.repo_root, feature_branch):
            # force=True: git's own ancestor-reachability heuristic ("branch -d") can refuse
            # a squash-merged feature branch, since the parent's new squash commit is not a
            # descendant of the feature tip. Safe here - the merged-state check and the content
            # check above already independently proved the changes genuinely landed on parent.
            delete_branch(self.repo_root, feature_branch, force=True)
            deleted = True

        backups_deleted: list[str] = []
        if delete_backups:
            backup_key = ticket_key or slugify(feature_branch)
            backups_deleted = prune_old_backups(self.repo_root, backup_key, keep=0)

        return CleanupResult(parent_branch=parent_branch, feature_branch_deleted=deleted, backups_deleted=backups_deleted)

    def delete_branch_safely(
        self, branch: str, target: str, remote: str = "origin",
        delete_local: bool = True, delete_remote: bool = False, force: bool = False,
    ) -> BranchDeleteResult:
        """Safety-gated branch deletion - never loses a commit silently.
        Refuses unconditionally (never overridable by force - a hard git
        constraint, not a judgment call) if branch is the currently
        checked-out branch. Refuses (raises, force=True required to proceed)
        if branch has commits not reachable from target - equivalent to the
        manually-run `git merge-base --is-ancestor`/`git rev-list --count`
        this replaces. delete_local/delete_remote are independent - either or
        both."""
        if current_branch(self.repo_root) == branch:
            raise GitWorkflowError(
                f"'{branch}' is the currently checked-out branch - switch to a different branch "
                "before deleting it. This is a hard git constraint, not overridable by force."
            )
        unique = 0
        if local_branch_exists(self.repo_root, branch):
            unique = unique_commit_count(self.repo_root, branch, target)
            if unique > 0 and not force:
                raise GitWorkflowError(
                    f"Branch '{branch}' cannot be safely deleted - {unique} commit(s) are not "
                    f"reachable from '{target}' and would be lost. Use force=true only if you "
                    "explicitly want to delete the branch anyway."
                )
        elif delete_local:
            raise GitWorkflowError(f"Local branch '{branch}' does not exist - nothing to delete locally.")

        local_deleted = False
        if delete_local:
            delete_branch(self.repo_root, branch, force=force)
            local_deleted = True
        remote_deleted = False
        if delete_remote:
            delete_remote_branch(self.repo_root, branch, remote=remote, extra_env=self._auth_env(remote))
            remote_deleted = True
        return BranchDeleteResult(
            branch=branch, local_deleted=local_deleted, remote_deleted=remote_deleted, unique_commits=unique,
        )

    def list_merged_branches(self, target: str, older_than_days: int = 0) -> list[dict]:
        """Local branches SAFE to delete via delete_branch_safely without force -
        fully merged into target (is_ancestor, same check delete_branch_safely
        itself uses), excluding target and the currently checked-out branch
        (deleting either is refused there anyway, so listing them here would just
        be noise). older_than_days (0 = no age filter) further restricts to
        branches whose tip commit is older than that, using each branch's own
        author date - a real, buildable answer to "which branches can I clean up"
        without requiring a raw `git branch --merged` + manual date-squinting."""
        current = current_branch(self.repo_root)
        cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days) if older_than_days > 0 else None
        results = []
        for entry in list_local_branches(self.repo_root):
            name = entry["branch"]
            if name in (target, current):
                continue
            if not is_ancestor(self.repo_root, name, target):
                continue
            if cutoff is not None and datetime.fromisoformat(entry["date"]) > cutoff:
                continue
            results.append(entry)
        return results
