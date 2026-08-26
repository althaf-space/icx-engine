"""CLI front door for the git-workflow lifecycle. This module owns its own
typer.Typer() app and is mounted into cli.py with two lines - no lifecycle
logic lives in cli.py itself (design spec Section 11)."""
from __future__ import annotations

import asyncio
from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape

from icx_engine.config_manager import ConfigManager
from icx_engine.git.manager import GitLifecycleManager, GitWorkflowError
from icx_engine.gitlab import service as gitlab_service
from icx_engine.gitlab.client import GitLabClient, project_path_from_remote_url

git_app = typer.Typer(help="Git-workflow lifecycle helpers - branch, sync, commit safely.",
                       rich_markup_mode="rich")
console = Console()

# cli.py:313 imports git_app from this module BEFORE cli.py defines _guarded/DebugOpt/
# TracebackOpt (cli.py:323-343), so `from icx_engine.cli import _guarded, ...` here would
# raise ImportError on a partially-initialized module. `import icx_engine.cli as _cli`
# is safe under that same circular load - Python registers a module in sys.modules before
# executing its body, so binding the module object itself never fails. Attribute access
# (_cli.DebugOpt, _cli._guarded, ...) is deferred past this module's own load: parameter
# annotations below are lazy strings (from __future__ import annotations, top of file) and
# _guarded's wrapper only touches _cli._guarded inside its call-time closure - both resolve
# no earlier than the first actual CLI invocation, by which point cli.py has finished
# loading regardless of which of the two modules was imported first.
import icx_engine.cli as _cli


def _guarded(fn):
    """Lazy local proxy for icx_engine.cli._guarded - see the import note above."""
    import functools

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return _cli._guarded(fn)(*args, **kwargs)
    return wrapper


def _resolve_cwd() -> Path:
    return Path.cwd()


def _resolve_parent_or_ask(mgr: GitLifecycleManager, cli_value: str | None) -> str:
    """Always-confirm-with-remembered-default parent branch resolution for the bare
    CLI, built entirely on the existing GitLifecycleManager.resolve_parent_branch()/
    confirm_parent_branch() (Plan 1) - this function only adds the CLI-side
    human interaction on top, it introduces no persistence of its own. An
    explicit --parent always wins (validated against origin, then persisted).
    Otherwise a previously-confirmed value is never reused silently - it is
    offered as a fast one-tap default to confirm, and rejecting it prompts for
    a new one. If nothing is confirmed yet, ask - offering the manager's own
    proposed default (e.g. 'development' if it exists on origin) or, failing
    that, a manual pick from the detected available branches."""
    if cli_value:
        mgr.confirm_parent_branch(cli_value)
        return cli_value
    resolution = mgr.resolve_parent_branch()
    if resolution.status == "resolved":
        if typer.confirm(
            f"Use '{resolution.parent_branch}' as the parent/target branch (last confirmed for this repo)?",
            default=True,
        ):
            return resolution.parent_branch
        chosen = typer.prompt("Which branch is the parent/target branch?")
        mgr.confirm_parent_branch(chosen)
        return chosen
    console.print("[yellow]No parent branch confirmed yet for this repo.[/yellow]")
    if resolution.status == "needs_confirmation":
        if typer.confirm(f"Use '{resolution.proposed_default}' as your parent/main branch?", default=True):
            chosen = resolution.proposed_default
        else:
            chosen = typer.prompt("Which branch is your parent/main branch?")
    else:  # needs_manual_pick
        if resolution.available_branches:
            console.print(f"Detected on origin: {', '.join(resolution.available_branches)}")
        chosen = typer.prompt(
            "Which branch is your parent/main branch?",
            default=resolution.available_branches[0] if resolution.available_branches else None,
        )
    mgr.confirm_parent_branch(chosen)
    return chosen


def _resolve_project_code_or_ask(cli_value: str | None) -> str:
    """No remembered/derived default for this, by design - always asks fresh (unlike
    parent branch, which offers a last-confirmed value as a one-tap default)."""
    if cli_value:
        return cli_value
    return typer.prompt("This is a ticketless branch - what project code should it use (e.g. a Jira project key)?")


@git_app.callback()
def _git_app_callback() -> None:
    """Git-workflow lifecycle helpers. Forces this Typer app to stay in
    multi-command group mode even while it has a single subcommand (Typer
    collapses a one-command app into a bare command otherwise)."""


@git_app.command("status")
@_guarded
def git_status(
    debug: _cli.DebugOpt = False,
    traceback: _cli.TracebackOpt = False,
) -> None:
    """Show repo validity, current branch, and working-tree cleanliness."""
    mgr = GitLifecycleManager(_resolve_cwd())
    mgr.validate()

    from icx_engine.git.gitcmd import current_branch
    branch = current_branch(mgr.repo_root)
    dirty_status = mgr.check_dirty_tree()

    console.print(f"Repo: {mgr.repo_root}")
    console.print(f"Branch: {branch}")
    if dirty_status.dirty:
        console.print("Working tree: [yellow]dirty[/yellow]")
        for f in dirty_status.files:
            console.print(f"  {f}")
    else:
        console.print("Working tree: [green]clean[/green]")

    leftover = mgr.check_leftover_state()
    if not leftover.is_clean:
        console.print("[yellow]Leftover state detected from a prior interrupted run:[/yellow]")
        for b in leftover.scratch_branches:
            console.print(f"  scratch branch: {b}")
        for s in leftover.icx_stashes:
            console.print(f"  stash: {s}")
        if leftover.merge_in_progress:
            console.print("  merge in progress")


@git_app.command("branch")
@_guarded
def git_branch(
    ticket: str = typer.Option(None, "--ticket", help="Ticket key (omit for a ticketless branch)"),
    name: str = typer.Option(..., "--name", help="Human-readable summary/preferred name used to derive the branch name"),
    parent: str = typer.Option(None, "--parent", help="Parent branch to branch from (asked every time if omitted; a saved parent branch is only offered as a default)"),
    project_code: str = typer.Option(None, "--project-code", help="Required for a ticketless branch (--ticket omitted) - asked interactively if not given"),
    debug: _cli.DebugOpt = False,
    traceback: _cli.TracebackOpt = False,
) -> None:
    """Create a feature branch (or switch to it if it already exists)."""
    mgr = GitLifecycleManager(_resolve_cwd())
    mgr.validate()
    resolved_parent = _resolve_parent_or_ask(mgr, parent)
    resolved_project_code = None if ticket else _resolve_project_code_or_ask(project_code)
    result = mgr.start_branch(ticket, name, resolved_parent, project_code=resolved_project_code)
    if result.switched_to_existing:
        console.print(f"[yellow]Branch '{result.branch_name}' already exists - switched to it.[/yellow]")
    else:
        console.print(f"[green]Created and switched to branch '{result.branch_name}'.[/green]")


@git_app.command("checkout")
@_guarded
def git_checkout(
    branch_name: str = typer.Argument(..., help="Exact existing branch name - never derived, slugified, or prefixed"),
    remote: str = typer.Option("origin", "--remote"),
    debug: _cli.DebugOpt = False,
    traceback: _cli.TracebackOpt = False,
) -> None:
    """Switch to a branch that already exists, by its exact name - use this instead of
    `branch` to avoid feature/ derivation. A dirty working tree is auto-stashed first,
    never discarded; retrieve it afterward with `git stash pop`."""
    mgr = GitLifecycleManager(_resolve_cwd())
    mgr.validate()
    result = mgr.checkout_branch(branch_name, remote=remote)
    if result.stashed:
        console.print("[yellow]Working tree was dirty - stashed before switching (retrieve with 'git stash pop').[/yellow]")
    source = "remote (new local tracking branch created)" if result.tracked_from_remote else "local"
    console.print(f"[green]Switched to '{result.branch_name}' ({source}).[/green]")


@git_app.command("sync")
@_guarded
def git_sync(
    parent: str = typer.Option(None, "--parent", help="Parent branch to reverse-merge in (asked every time if omitted; a saved parent branch is only offered as a default)"),
    ticket: str = typer.Option(..., "--ticket", help="Ticket key for backup/scratch naming"),
    debug: _cli.DebugOpt = False,
    traceback: _cli.TracebackOpt = False,
) -> None:
    """Reverse-merge the parent branch into the current feature branch. Clean
    merges complete automatically; a conflict creates a scratch branch and
    reports which files need resolution - resolve them there, then use
    the MCP tools or a future `icx git resolve` command to complete and adopt."""
    mgr = GitLifecycleManager(_resolve_cwd())
    mgr.validate()
    resolved_parent = _resolve_parent_or_ask(mgr, parent)
    result = mgr.reverse_merge_standard(resolved_parent, ticket)
    if result.status == "clean":
        console.print("[green]Reverse merge clean - feature branch is up to date with parent.[/green]")
    else:
        session = mgr.start_conflict_resolution(resolved_parent, ticket)
        console.print(f"[yellow]Conflict detected - quarantined on {session.scratch_branch}[/yellow]")
        console.print("Conflicted files:")
        for f in session.conflicted_files:
            console.print(f"  {f}")
        console.print(
            "Resolve conflict markers in each file above, then complete and adopt "
            "the resolution (tooling for this CLI step lands in a later task)."
        )


@git_app.command("push")
@_guarded
def git_push(
    remote: str = typer.Option("origin", "--remote"),
    debug: _cli.DebugOpt = False,
    traceback: _cli.TracebackOpt = False,
) -> None:
    """Push the current branch to the remote - plain push, no force, no rebase."""
    mgr = GitLifecycleManager(_resolve_cwd())
    mgr.validate()
    from icx_engine.git.gitcmd import current_branch, push
    branch = current_branch(mgr.repo_root)
    console.print(f"About to push '{branch}' to '{remote}'.")
    if not typer.confirm("Proceed?", default=True):
        console.print("Cancelled.")
        return
    push(mgr.repo_root, branch, remote=remote, extra_env=mgr._auth_env(remote))
    console.print(f"[green]Pushed '{branch}' to {remote}.[/green]")


@git_app.command("mr")
@_guarded
def git_mr(
    parent: str = typer.Option(None, "--parent", help="Target/parent branch for the MR (asked every time if omitted; a saved parent branch is only offered as a default)"),
    ticket: str = typer.Option(..., "--ticket", help="Ticket key"),
    summary: str = typer.Option(..., "--summary", help="Ticket summary for the MR title"),
    debug: _cli.DebugOpt = False,
    traceback: _cli.TracebackOpt = False,
) -> None:
    """Create an MR (or reuse an existing one for this branch) and attempt one
    immediate merge. Requires an active GitLab connection (`icx gitlab --add`)."""
    mgr = GitLifecycleManager(_resolve_cwd())
    mgr.validate()
    resolved_parent = _resolve_parent_or_ask(mgr, parent)
    conn = ConfigManager.load().active_gitlab_connection()
    if conn is None:
        raise GitWorkflowError("No active GitLab connection. Run `icx gitlab --add` first.")
    console.print(f"About to create/merge an MR: {ticket} {summary} -> {resolved_parent}")
    if not typer.confirm("Proceed?", default=True):
        console.print("Cancelled.")
        return
    result = asyncio.run(mgr.create_mr_for_ticket(resolved_parent, ticket, summary, conn))
    if result.merged:
        console.print(f"[green]MR !{result.mr_iid} merged.[/green]")
    else:
        console.print(f"[yellow]MR !{result.mr_iid} created but not merged: {result.refusal_reason}[/yellow]")


@git_app.command("finish")
@_guarded
def git_finish(
    parent: str = typer.Option(None, "--parent", help="Parent branch to fast-forward (asked every time if omitted; a saved parent branch is only offered as a default)"),
    feature: str = typer.Option(..., "--feature"),
    ticket: str = typer.Option(..., "--ticket"),
    mr_iid: int = typer.Option(..., "--mr-iid"),
    debug: _cli.DebugOpt = False,
    traceback: _cli.TracebackOpt = False,
) -> None:
    """Post-merge cleanup - verifies the MR actually merged, then updates parent and
    removes the feature branch locally, on GitLab, and both backup tiers."""
    mgr = GitLifecycleManager(_resolve_cwd())
    mgr.validate()
    resolved_parent = _resolve_parent_or_ask(mgr, parent)
    conn = ConfigManager.load().active_gitlab_connection()
    if conn is None:
        raise GitWorkflowError("No active GitLab connection. Run `icx gitlab --add` first.")
    console.print(
        f"About to clean up after merge: delete '{feature}' (local + remote), fast-forward "
        f"'{resolved_parent}', and remove its backup branches."
    )
    if not typer.confirm("Proceed?", default=True):
        console.print("Cancelled.")
        return
    result = asyncio.run(mgr.post_merge_cleanup(resolved_parent, feature, ticket, conn, mr_iid))
    console.print(f"[green]Cleaned up. Feature branch deleted: {result.feature_branch_deleted} (remote: {result.remote_branch_deleted}).[/green]")


@git_app.command("tag")
@_guarded
def git_tag(
    environment: str = typer.Option(None, "--env", help="Environment/channel label (e.g. qa, staging, prod)"),
    branch: str = typer.Option(None, "--branch", help="Branch to tag from (asked every time; a saved parent branch is only offered as a default)"),
    tag_name_override: str = typer.Option(None, "--tag-name", help="Exact tag name to create, overriding the proposed one"),
    debug: _cli.DebugOpt = False,
    traceback: _cli.TracebackOpt = False,
) -> None:
    """Create a GitLab tag - server-side, no local push needed. Shows every
    environment's own latest tag, proposes the next one for the chosen
    environment, and requires explicit approval before creating anything. A
    wrong proposal is never fatal - pass --tag-name to use an exact name
    instead."""
    mgr = GitLifecycleManager(_resolve_cwd())
    mgr.validate()
    conn = ConfigManager.load().active_gitlab_connection()
    if conn is None:
        raise GitWorkflowError("No active GitLab connection. Run `icx gitlab --add` first.")
    origin = mgr.repo_root
    from icx_engine.git.gitcmd import remote_url
    project_path = project_path_from_remote_url(remote_url(origin))
    if project_path is None:
        raise GitWorkflowError("Could not resolve a GitLab project from origin remote.")

    async def _list_tags() -> list[dict]:
        async with GitLabClient(conn.url, conn.token, conn.verify_tls) as client:
            return await client.list_tags(project_path)

    grouped = gitlab_service.group_tags_by_environment(asyncio.run(_list_tags()))
    if not grouped:
        console.print("[yellow]No existing tags found in this project's convention.[/yellow]")
    else:
        console.print("Existing tag lineages by environment:")
        for env, tags in grouped.items():
            console.print(f"  {env}: latest {tags[0].name} ({len(tags)} total)")

    resolved_env = environment or typer.prompt("Which environment is this tag for? (e.g. qa, staging, prod)")
    latest = grouped.get(resolved_env, [None])[0] if grouped.get(resolved_env) else None
    proposed = tag_name_override or gitlab_service.propose_next_tag(resolved_env, latest)

    console.print(f"Previous tag for '{resolved_env}': {latest.name if latest else '(none yet)'}")
    console.print(f"Proposed new tag: {proposed}")
    if not typer.confirm("Create this tag?", default=True):
        manual = typer.prompt("Enter the exact tag name to create instead (blank to cancel)", default="")
        if not manual.strip():
            console.print("Cancelled.")
            return
        proposed = manual.strip()

    # Tag creation always asks which branch, even if a parent branch is already
    # confirmed for sync/mr/finish - tagging is deliberate and occasional, so the
    # saved parent branch is only offered as a convenient default to accept, never
    # silently reused without asking (design spec: "we will ask user from which
    # branch" - a stronger guarantee than sync/mr/finish's ask-once-then-remember).
    # Uses resolve_parent_branch() read-only, purely for a suggested default text -
    # never calls confirm_parent_branch() here, since tagging must not silently
    # persist a parent-branch choice as a side effect of picking a tag source.
    _parent_hint = mgr.resolve_parent_branch()
    suggested_branch = (
        branch or _parent_hint.parent_branch or _parent_hint.proposed_default
        or (_parent_hint.available_branches[0] if _parent_hint.available_branches else "")
    )
    resolved_branch = branch or typer.prompt(
        "Which branch do you want to create this tag from?", default=suggested_branch or None,
    )

    async def _create() -> dict:
        async with GitLabClient(conn.url, conn.token, conn.verify_tls) as client:
            return await client.create_tag(project_path, proposed, resolved_branch, f"{resolved_env} build")

    result = asyncio.run(_create())
    console.print(f"[green]Created tag '{result['name']}' from branch '{resolved_branch}'.[/green]")


@git_app.command("blame")
@_guarded
def git_blame(
    file: str = typer.Argument(..., help="File path relative to the repo root"),
    from_line: int = typer.Option(None, "--from-line", help="Start line, 1-based - requires --to-line"),
    to_line: int = typer.Option(None, "--to-line", help="End line, 1-based - requires --from-line"),
    debug: _cli.DebugOpt = False,
    traceback: _cli.TracebackOpt = False,
) -> None:
    """Per-line blame - commit sha, author, and content for each line of a file."""
    if (from_line is None) != (to_line is None):
        raise GitWorkflowError("--from-line and --to-line must be given together, or neither.")
    mgr = GitLifecycleManager(_resolve_cwd())
    mgr.validate()
    from icx_engine.git.gitcmd import blame
    line_range = (from_line, to_line) if from_line is not None else None
    entries = blame(mgr.repo_root, file, line_range=line_range)
    for e in entries:
        console.print(
            f"{e['line_no']:>5}  [cyan]{e['commit_sha'][:8]}[/cyan]  {escape(e['author']):<20}  {escape(e['content'])}"
        )


@git_app.command("log")
@_guarded
def git_log(
    file: str = typer.Option(None, "--file", help="Scope history to commits touching this file"),
    author: str = typer.Option(None, "--author", help="Filter by author"),
    since: str = typer.Option(None, "--since", help="Only commits after this date, git --since syntax"),
    limit: int = typer.Option(20, "--limit", help="Max commits to show"),
    debug: _cli.DebugOpt = False,
    traceback: _cli.TracebackOpt = False,
) -> None:
    """Commit history, newest first - short sha, author, date, subject."""
    mgr = GitLifecycleManager(_resolve_cwd())
    mgr.validate()
    from icx_engine.git.gitcmd import log
    commits = log(mgr.repo_root, relpath=file, limit=limit, author=author, since=since)
    for c in commits:
        console.print(f"[cyan]{c['sha'][:8]}[/cyan]  {escape(c['author']):<20}  {c['date']}  {escape(c['subject'])}")


@git_app.command("show")
@_guarded
def git_show(
    sha: str = typer.Argument(..., help="Commit sha to show"),
    debug: _cli.DebugOpt = False,
    traceback: _cli.TracebackOpt = False,
) -> None:
    """Full detail for one commit - message plus changed files."""
    mgr = GitLifecycleManager(_resolve_cwd())
    mgr.validate()
    from icx_engine.git.gitcmd import show_commit
    result = show_commit(mgr.repo_root, sha)
    console.print(f"[cyan]{result['sha']}[/cyan]")
    console.print(f"Author: {escape(result['author'])} <{result['author_email']}>")
    console.print(f"Date:   {result['date']}")
    console.print(f"\n    {escape(result['subject'])}")
    if result["body"]:
        console.print(f"\n{escape(result['body'])}")
    console.print("\nFiles:")
    for f in result["files"]:
        console.print(f"  {f['status']}  {f['path']}")


@git_app.command("diff")
@_guarded
def git_diff(
    ref_a: str = typer.Argument(..., help="First ref - branch, tag, or commit"),
    ref_b: str = typer.Argument(..., help="Second ref - branch, tag, or commit"),
    debug: _cli.DebugOpt = False,
    traceback: _cli.TracebackOpt = False,
) -> None:
    """Per-file status plus insertions/deletions between two refs."""
    mgr = GitLifecycleManager(_resolve_cwd())
    mgr.validate()
    from icx_engine.git.gitcmd import diff_between
    result = diff_between(mgr.repo_root, ref_a, ref_b)
    for f in result["files"]:
        if f["insertions"] is None and f["deletions"] is None:
            counts = "(binary)"
        else:
            counts = f"+{f['insertions']} -{f['deletions']}"
        console.print(f"  {f['status']}  {f['path']}  {counts}")
