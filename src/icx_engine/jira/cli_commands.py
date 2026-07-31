"""CLI front door for the Jira write-back (close-out) capability. This module
owns its own typer.Typer() app, mounted into cli.py with two lines - no
close-out logic lives in cli.py itself, matching git/cli_commands.py's shape."""
from __future__ import annotations

import asyncio
import mimetypes
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console

from icx_engine.jira import service

jira_app = typer.Typer(help="Jira write-back - discover and apply issue close-out updates.",
                        rich_markup_mode="rich")
console = Console()

# Comments genuinely have 4 verbs and would crowd `icx jira --help` next to
# create/delete/update as 4 flat top-level commands, so this is mounted as a
# nested Typer sub-app instead (`icx jira comment list/add/edit/delete`).
# Note (Task 2, verified): this is a NEW shape for this codebase - every
# existing Typer group here (git_app, jira_app, skills_app, ...) is mounted
# exactly one level deep onto `app` in cli.py, and no Typer app anywhere in
# this repo is nested inside another Typer app before this. Typer supports
# it natively (`jira_app.add_typer(comment_app, name="comment")`, mounted at
# the bottom of this file after comment_app's own commands are defined) - do
# not describe this as "matching an existing pattern" elsewhere in the repo,
# since none exists yet.
comment_app = typer.Typer(help="Comment CRUD on a Jira issue - list, add, edit, delete.",
                           rich_markup_mode="rich")

# Same nested-Typer shape as comment_app above (see its note) - link management
# genuinely has 3 verbs (types/create/delete), so `icx jira link types/create/delete`
# mirrors `icx jira comment list/add/edit/delete` rather than crowding jira_app
# with 3 more flat top-level commands.
link_app = typer.Typer(help="Issue link management - list link types, create, delete.",
                        rich_markup_mode="rich")

# Same nested-Typer shape as comment_app/link_app above - attachments have 2
# verbs (add/remove), matched to `icx jira attach add/remove` for the same
# reason the other two sub-apps exist: consistency with the established
# shape for this module now, not a flat top-level command each.
attach_app = typer.Typer(help="Attachment management - upload, remove.",
                          rich_markup_mode="rich")

# Same nested-Typer shape as comment_app/link_app/attach_app above - watcher
# management has 2 verbs (add/remove), matched to `icx jira watch add/remove`.
watch_app = typer.Typer(help="Watcher management - add/remove a watcher on an issue.",
                         rich_markup_mode="rich")

# Same nested-Typer shape as the sub-apps above - worklog genuinely has 4
# verbs (list/add/edit/delete), matched to `icx jira worklog list/add/edit/delete`,
# mirroring `icx jira comment list/add/edit/delete` exactly.
worklog_app = typer.Typer(help="Worklog CRUD on a Jira issue - list, add, edit, delete.",
                           rich_markup_mode="rich")

# Task 6 will mount jira_app into cli.py, at which point cli.py imports this
# module at module level BEFORE cli.py defines _guarded/DebugOpt/TracebackOpt
# (see cli.py:313's identical git_app mount for the precedent) - so
# `from icx_engine.cli import _guarded, ...` here would raise ImportError on
# a partially-initialized module once that mount lands. `import icx_engine.cli
# as _cli` is safe under that same circular load - Python registers a module
# in sys.modules before executing its body, so binding the module object
# itself never fails. Attribute access (_cli.DebugOpt, _cli._guarded, ...) is
# deferred past this module's own load: parameter annotations below are lazy
# strings (from __future__ import annotations, top of file) and _guarded's
# wrapper only touches _cli._guarded inside its call-time closure - both
# resolve no earlier than the first actual CLI invocation, by which point
# cli.py has finished loading regardless of which of the two modules was
# imported first. Built this way from the start (no naive try/except command
# needing a later retrofit), per the git/cli_commands.py hardening lesson.
import icx_engine.cli as _cli


def _guarded(fn):
    """Lazy local proxy for icx_engine.cli._guarded - see the import note above."""
    import functools

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return _cli._guarded(fn)(*args, **kwargs)
    return wrapper


@jira_app.callback()
def _jira_app_callback() -> None:
    """Jira write-back helpers. Forces this Typer app to stay in multi-command
    group mode even while it has a single subcommand (Typer collapses a
    one-command app into a bare command otherwise)."""


def _print_transitions(transitions: list[dict]) -> None:
    if not transitions:
        console.print("No workflow transitions are available for this issue.")
        return
    console.print("Available transitions:")
    for idx, t in enumerate(transitions, start=1):
        required = [k for k, v in (t.get("fields") or {}).items() if v.get("required")]
        suffix = f" - requires: {', '.join(required)}" if required else ""
        console.print(f"  {idx}. {t.get('name')} (id={t.get('id')}){suffix}")


def _select_transition(transitions: list[dict]) -> dict | None:
    """Prompt which transition to use, or none for a field-only update.
    Returns the chosen transition dict, or None."""
    if not transitions:
        return None
    choice = typer.prompt(
        "Choose a transition by number, or press Enter for a field-only update (no transition)",
        default="",
    )
    if not choice.strip():
        return None
    try:
        index = int(choice.strip())
        if not (1 <= index <= len(transitions)):
            raise ValueError
    except ValueError:
        raise ValueError(f"'{choice}' is not a valid transition number.")
    return transitions[index - 1]


def _required_fields_for(transition: dict | None, editable_fields: dict) -> list[str]:
    if transition is not None:
        return [k for k, v in (transition.get("fields") or {}).items() if v.get("required")]
    return [k for k, v in editable_fields.items() if v.get("required")]


def _prompt_for_fields(keys: list[str], known: dict) -> dict:
    fields = dict(known)
    for key in keys:
        if key in fields:
            continue
        fields[key] = typer.prompt(f"Value for required field '{key}'")
    return fields


def _prompt_for_comment(transition: dict | None) -> str | None:
    """Only offered when a transition was chosen - service.apply_update raises
    ValueError for a comment with no transition_id, so a field-only update
    never gets a comment prompt."""
    if transition is None:
        return None
    if typer.confirm("Add a comment?", default=False):
        return typer.prompt("Comment")
    return None


def _print_summary(issue_key: str, transition: dict | None, fields: dict, comment: str | None) -> None:
    console.print(f"Issue: {issue_key}")
    console.print(f"Transition: {transition.get('name') if transition else '(none - field-only update)'}")
    console.print(f"Fields: {fields or '(none)'}")
    console.print(f"Comment: {comment or '(none)'}")


_DELETE_WARNING = (
    "WARNING: this is PERMANENT - no undo, no trash. Jira Cloud has no recycle bin for "
    "issues, so a deleted issue cannot be recovered."
)

# Verified (Task 2): Jira has no recovery mechanism for a deleted comment either
# (no recycle bin, no undo) - the same permanence as _DELETE_WARNING above, just
# without an issue-level "trash" concept to reference for a comment specifically.
_COMMENT_DELETE_WARNING = (
    "WARNING: this is PERMANENT - deleted comments cannot be recovered. Jira has no undo "
    "for a deleted comment."
)

# NOT a "permanent, no undo" claim like the two warnings above - verified: a Jira
# issue link CAN be recreated after deletion (it is not the same permanence class
# as deleting an issue or comment). The real risk is losing visibility of a real
# dependency between issues until someone notices it is missing and re-adds it.
_LINK_DELETE_WARNING = (
    "WARNING: removing a link can hide real dependency information between issues. A link "
    "of the same type can be recreated afterward if the relationship still applies, but "
    "anyone relying on it in the meantime sees an incomplete picture of how these issues "
    "relate."
)

# Verified (Task 5): Jira Cloud has no recycle bin/trash for attachments -
# the same permanence class as _DELETE_WARNING/_COMMENT_DELETE_WARNING above.
_ATTACHMENT_DELETE_WARNING = (
    "WARNING: this is PERMANENT - no undo, no trash. Jira Cloud has no recycle bin for "
    "attachments, so a deleted attachment cannot be recovered."
)

# NOT a permanence claim like the warnings above - watcher/worklog mutations are
# fully reversible (add a watcher back, edit a worklog again). The real risk here
# is acting on a DIFFERENT Jira user's identity without them knowing - the same
# self-vs-other gating decision enforced at the MCP layer, expressed at the CLI
# layer via a plain typer.confirm() instead of a confirm-token round-trip (the
# CLI path never uses icx_engine.confirm - a human is directly at the terminal).
_WATCH_OTHER_WARNING = (
    "WARNING: this changes the watcher status of a DIFFERENT Jira user, not your own."
)
_WORKLOG_OTHER_WARNING = (
    "WARNING: this changes a worklog entry belonging to a DIFFERENT Jira user, not your own."
)


def _adf_to_text(node) -> str:
    """Best-effort plain-text rendering of an ADF comment body for CLI
    display - walks 'content' recursively, joining any 'text' leaves. Not a
    full ADF renderer (no formatting/marks), just enough for a readable
    `icx jira comment list` output."""
    if not isinstance(node, dict):
        return ""
    if node.get("type") == "text":
        return node.get("text", "")
    return "".join(_adf_to_text(child) for child in node.get("content", []) or [])


def _select_issue_type(issue_types: list[dict], choice: str) -> dict | None:
    """Resolve a CLI choice against the listed issue types - by 1-based
    index, or by name (case-insensitive)."""
    choice = choice.strip()
    try:
        index = int(choice)
        if 1 <= index <= len(issue_types):
            return issue_types[index - 1]
    except ValueError:
        pass
    for it in issue_types:
        if str(it.get("name", "")).lower() == choice.lower():
            return it
    return None


@jira_app.command("update")
@_guarded
def jira_update(
    key: str = typer.Argument(..., help="Issue key to close out or update, e.g. ABC-123"),
    debug: _cli.DebugOpt = False,
    traceback: _cli.TracebackOpt = False,
) -> None:
    """Interactively discover what an issue needs to close out/update -
    available transitions and editable fields - then submit it. If Jira
    responds with a validation error naming additional required fields, you
    are prompted for those and the update is retried exactly once."""
    requirements = asyncio.run(service.get_close_requirements(key))
    transitions = requirements.get("transitions") or []
    editable_fields = requirements.get("editable_fields") or {}

    _print_transitions(transitions)
    transition = _select_transition(transitions)

    required_keys = _required_fields_for(transition, editable_fields)
    fields = _prompt_for_fields(required_keys, {})
    comment = _prompt_for_comment(transition)

    _print_summary(key, transition, fields, comment)
    if not typer.confirm("Proceed with this update?", default=True):
        console.print("Cancelled.")
        return

    transition_id = transition.get("id") if transition else None
    result = asyncio.run(service.apply_update(
        key, transition_id=transition_id, fields=fields or None, comment=comment,
    ))

    if not result.get("ok", False):
        needs_fields = result.get("needs_fields")
        if not needs_fields:
            console.print(f"[red]Update failed: {result.get('message', 'unknown error')}[/red]")
            raise typer.Exit(1)
        console.print("[yellow]Jira needs additional fields before this update can go through:[/yellow]")
        for field_key, message in needs_fields.items():
            console.print(f"  {field_key}: {message}")
        fields = _prompt_for_fields(list(needs_fields.keys()), fields)
        result = asyncio.run(service.apply_update(
            key, transition_id=transition_id, fields=fields or None, comment=comment,
        ))
        if not result.get("ok", False):
            console.print(f"[red]Update failed again: {result.get('message', 'unknown error')}[/red]")
            raise typer.Exit(1)

    console.print("[green]Issue updated successfully.[/green]")


@jira_app.command("create")
@_guarded
def jira_create(
    debug: _cli.DebugOpt = False,
    traceback: _cli.TracebackOpt = False,
) -> None:
    """Interactively create a new Jira issue: prompts for project, issue
    type, and summary, fetches the chosen issue type's create-time required
    fields and prompts for those, confirms, then creates the issue."""
    project = typer.prompt("Project key")

    issue_types = asyncio.run(service.list_issue_types(project))
    if not issue_types:
        console.print(f"[red]No issue types are available for project '{project}'.[/red]")
        raise typer.Exit(1)
    console.print("Available issue types:")
    for idx, it in enumerate(issue_types, start=1):
        console.print(f"  {idx}. {it.get('name')}")
    choice = typer.prompt("Choose an issue type by number or name")
    issuetype = _select_issue_type(issue_types, choice)
    if issuetype is None:
        console.print(f"[red]'{choice}' is not a valid issue type.[/red]")
        raise typer.Exit(1)

    summary = typer.prompt("Summary")

    createmeta_fields = asyncio.run(service.get_createmeta_fields(project, issuetype["id"]))
    required_keys = [
        k for k, v in createmeta_fields.items()
        if v.get("required") and k not in ("project", "issuetype", "summary", "reporter")
    ]
    fields = _prompt_for_fields(required_keys, {})

    console.print(f"Project: {project}")
    console.print(f"Issue type: {issuetype.get('name')}")
    console.print(f"Summary: {summary}")
    console.print(f"Fields: {fields or '(none)'}")
    if not typer.confirm("Create this issue?", default=True):
        console.print("Cancelled.")
        return

    result = asyncio.run(service.create_issue(
        project, issuetype.get("name"), summary, fields=fields or None,
    ))
    console.print(f"[green]Created {result['issue_key']}.[/green]")


@jira_app.command("delete")
@_guarded
def jira_delete(
    key: str = typer.Argument(..., help="Issue key to permanently delete, e.g. ABC-123"),
    delete_subtasks: bool = typer.Option(
        False, "--delete-subtasks", help="Also delete this issue's subtasks (required if it has any)."
    ),
    debug: _cli.DebugOpt = False,
    traceback: _cli.TracebackOpt = False,
) -> None:
    """Permanently delete a Jira issue. WARNING: this is permanent - no
    undo, no trash. Jira Cloud has no recycle bin for issues."""
    console.print(f"[red]{_DELETE_WARNING}[/red]")
    suffix = " (including its subtasks)" if delete_subtasks else ""
    console.print(f"Issue to delete: {key}{suffix}")
    if not typer.confirm("Are you sure you want to permanently delete this issue?", default=False):
        console.print("Cancelled.")
        return

    asyncio.run(service.delete_issue(key, delete_subtasks=delete_subtasks))
    console.print(f"[green]Deleted {key}.[/green]")


@comment_app.command("list")
@_guarded
def jira_comment_list(
    key: str = typer.Argument(..., help="Issue key to list comments for, e.g. ABC-123"),
    debug: _cli.DebugOpt = False,
    traceback: _cli.TracebackOpt = False,
) -> None:
    """List all comments on a Jira issue."""
    result = asyncio.run(service.list_comments(key))
    comments = result.get("comments") or []
    if not comments:
        console.print("No comments on this issue.")
        return
    for c in comments:
        author = (c.get("author") or {}).get("displayName", "unknown")
        body = _adf_to_text(c.get("body"))
        console.print(f"[{c.get('id')}] {author}: {body}")


@comment_app.command("add")
@_guarded
def jira_comment_add(
    key: str = typer.Argument(..., help="Issue key to comment on, e.g. ABC-123"),
    comment: str = typer.Argument(..., help="Comment text to add"),
    debug: _cli.DebugOpt = False,
    traceback: _cli.TracebackOpt = False,
) -> None:
    """Add a plain-text comment to a Jira issue."""
    result = asyncio.run(service.add_comment(key, comment))
    comment_id = (result.get("comment") or {}).get("id", "?")
    console.print(f"[green]Added comment {comment_id} to {key}.[/green]")


@comment_app.command("edit")
@_guarded
def jira_comment_edit(
    key: str = typer.Argument(..., help="Issue key the comment belongs to, e.g. ABC-123"),
    comment_id: str = typer.Argument(..., help="Comment id to edit"),
    comment: str = typer.Argument(..., help="New comment text"),
    debug: _cli.DebugOpt = False,
    traceback: _cli.TracebackOpt = False,
) -> None:
    """Edit the text of an existing comment on a Jira issue."""
    asyncio.run(service.edit_comment(key, comment_id, comment))
    console.print(f"[green]Updated comment {comment_id} on {key}.[/green]")


@comment_app.command("delete")
@_guarded
def jira_comment_delete(
    key: str = typer.Argument(..., help="Issue key the comment belongs to, e.g. ABC-123"),
    comment_id: str = typer.Argument(..., help="Comment id to permanently delete"),
    debug: _cli.DebugOpt = False,
    traceback: _cli.TracebackOpt = False,
) -> None:
    """Permanently delete a comment from a Jira issue. WARNING: this is
    permanent - deleted comments cannot be recovered."""
    console.print(f"[red]{_COMMENT_DELETE_WARNING}[/red]")
    console.print(f"Comment to delete: {comment_id} (on {key})")
    if not typer.confirm("Are you sure you want to permanently delete this comment?", default=False):
        console.print("Cancelled.")
        return

    asyncio.run(service.delete_comment(key, comment_id))
    console.print(f"[green]Deleted comment {comment_id} on {key}.[/green]")


@jira_app.command("search")
@_guarded
def jira_search(
    jql: str = typer.Argument(..., help='JQL query, e.g. \'project = ABC AND status = "In Progress"\''),
    max_results: int = typer.Option(
        50, "--max-results", help="Max results to return (server-side capped at 100)."
    ),
    debug: _cli.DebugOpt = False,
    traceback: _cli.TracebackOpt = False,
) -> None:
    """Search Jira issues by JQL and print matching issue keys/summaries. A
    lightweight, raw search - no LLM analysis."""
    result = asyncio.run(service.search(jql, max_results=max_results))
    issues = result.get("issues") or []
    if not issues:
        console.print("No matching issues.")
        return
    for issue in issues:
        key = issue.get("key", "?")
        summary = (issue.get("fields") or {}).get("summary", "")
        console.print(f"{key}: {summary}")
    if not result.get("is_last", True):
        console.print(f"[dim]More results available - next_page_token: {result.get('next_page_token')}[/dim]")


@jira_app.command("get")
@_guarded
def jira_get(
    key: str = typer.Argument(..., help="Issue key for a lightweight raw fetch, e.g. ABC-123"),
    debug: _cli.DebugOpt = False,
    traceback: _cli.TracebackOpt = False,
) -> None:
    """Print an issue's raw current fields. A lightweight, raw fetch - not a
    replacement for `icx analyze`, which runs full LLM analysis."""
    result = asyncio.run(service.get_issue(key))
    raw = result.get("raw") or {}
    fields = raw.get("fields") or {}
    console.print(f"Issue: {raw.get('key', key)}")
    if not fields:
        console.print("(no fields returned)")
        return
    for field_key, value in fields.items():
        console.print(f"  {field_key}: {value}")


@link_app.command("types")
@_guarded
def jira_link_types(
    debug: _cli.DebugOpt = False,
    traceback: _cli.TracebackOpt = False,
) -> None:
    """List the link types available for linking Jira issues together."""
    result = asyncio.run(service.link_types())
    link_types = result.get("link_types") or []
    if not link_types:
        console.print("No link types available.")
        return
    console.print("Available link types:")
    for lt in link_types:
        console.print(
            f"  {lt.get('name')} (id={lt.get('id')}): inward='{lt.get('inward')}', "
            f"outward='{lt.get('outward')}'"
        )


@link_app.command("create")
@_guarded
def jira_link_create(
    link_type: str = typer.Argument(..., help="Link type name, e.g. 'Blocks' (see `icx jira link types`)"),
    inward_key: str = typer.Argument(..., help="Inward issue key, e.g. ABC-1"),
    outward_key: str = typer.Argument(..., help="Outward issue key, e.g. ABC-2"),
    debug: _cli.DebugOpt = False,
    traceback: _cli.TracebackOpt = False,
) -> None:
    """Link two Jira issues together with the given link type."""
    asyncio.run(service.create_link(link_type, inward_key, outward_key))
    console.print(f"[green]Linked {inward_key} <-> {outward_key} ({link_type}).[/green]")


@link_app.command("delete")
@_guarded
def jira_link_delete(
    issue_key: str = typer.Argument(
        ..., help="An issue key on either end of the link, e.g. ABC-123 (used to resolve the connection)"
    ),
    link_id: str = typer.Argument(..., help="Link id to remove"),
    debug: _cli.DebugOpt = False,
    traceback: _cli.TracebackOpt = False,
) -> None:
    """Remove a link between two Jira issues. See warning: not the same
    permanence class as deleting an issue/comment, but still hides real
    dependency information until someone re-adds it."""
    console.print(f"[red]{_LINK_DELETE_WARNING}[/red]")
    console.print(f"Link to delete: {link_id} (resolved via {issue_key})")
    if not typer.confirm("Are you sure you want to remove this link?", default=False):
        console.print("Cancelled.")
        return

    asyncio.run(service.delete_link(issue_key, link_id))
    console.print(f"[green]Deleted link {link_id}.[/green]")


@jira_app.command("assign")
@_guarded
def jira_assign(
    key: str = typer.Argument(..., help="Issue key to assign, e.g. ABC-123"),
    account_id: Optional[str] = typer.Argument(
        None, help="Account id to assign to. Omit when using --unassign or --default."
    ),
    unassign: bool = typer.Option(False, "--unassign", help="Remove the assignee (sends null)."),
    default: bool = typer.Option(
        False, "--default", help="Assign the project's default assignee (Jira's '-1' sentinel)."
    ),
    debug: _cli.DebugOpt = False,
    traceback: _cli.TracebackOpt = False,
) -> None:
    """Assign, unassign, or reset to the project default assignee for a Jira
    issue. --unassign and --default spare the human from needing to know
    Jira's magic '-1' sentinel string."""
    if unassign and default:
        console.print("[red]Pass only one of --unassign or --default, not both.[/red]")
        raise typer.Exit(1)

    if unassign:
        resolved_account_id: Optional[str] = None
    elif default:
        resolved_account_id = "-1"
    elif account_id:
        resolved_account_id = account_id
    else:
        console.print("[red]ACCOUNT_ID is required unless --unassign or --default is given.[/red]")
        raise typer.Exit(1)

    asyncio.run(service.set_assignee(key, resolved_account_id))
    if resolved_account_id is None:
        console.print(f"[green]Unassigned {key}.[/green]")
    else:
        console.print(f"[green]Assigned {key} to {resolved_account_id}.[/green]")


@attach_app.command("add")
@_guarded
def jira_attach_add(
    key: str = typer.Argument(..., help="Issue key to attach the file to, e.g. ABC-123"),
    file_path: Path = typer.Argument(..., help="Local path to the file to upload"),
    debug: _cli.DebugOpt = False,
    traceback: _cli.TracebackOpt = False,
) -> None:
    """Upload a local file as an attachment to a Jira issue. Content type is
    inferred from the file extension via Python's mimetypes module when
    possible; omitted otherwise, and Jira defaults it to
    application/octet-stream in that case."""
    content_bytes = file_path.read_bytes()
    content_type, _ = mimetypes.guess_type(file_path.name)
    result = asyncio.run(service.upload_attachment(
        key, file_path.name, content_bytes, content_type=content_type,
    ))
    attachments = result.get("attachments") or []
    ids = ", ".join(str(a.get("id", "?")) for a in attachments) or "?"
    console.print(f"[green]Uploaded {file_path.name} to {key} (attachment id(s): {ids}).[/green]")


@attach_app.command("remove")
@_guarded
def jira_attach_remove(
    issue_key: str = typer.Argument(
        ..., help="An issue key the attachment belongs to, e.g. ABC-123 (used to resolve the connection)"
    ),
    attachment_id: str = typer.Argument(..., help="Attachment id to permanently remove"),
    debug: _cli.DebugOpt = False,
    traceback: _cli.TracebackOpt = False,
) -> None:
    """Permanently delete an attachment from a Jira issue. WARNING: this is
    permanent - deleted attachments cannot be recovered."""
    console.print(f"[red]{_ATTACHMENT_DELETE_WARNING}[/red]")
    console.print(f"Attachment to delete: {attachment_id} (resolved via {issue_key})")
    if not typer.confirm("Are you sure you want to permanently delete this attachment?", default=False):
        console.print("Cancelled.")
        return

    asyncio.run(service.delete_attachment(issue_key, attachment_id))
    console.print(f"[green]Deleted attachment {attachment_id}.[/green]")


@jira_app.command("whoami")
@_guarded
def jira_whoami(
    debug: _cli.DebugOpt = False,
    traceback: _cli.TracebackOpt = False,
) -> None:
    """Print the caller's own Jira identity (accountId, displayName) - useful
    context before deciding whether a watcher/worklog action targets
    yourself or someone else."""
    result = asyncio.run(service.get_current_user())
    console.print(f"accountId: {result.get('accountId', '?')}")
    display_name = result.get("displayName")
    if display_name:
        console.print(f"displayName: {display_name}")


@watch_app.command("add")
@_guarded
def jira_watch_add(
    key: str = typer.Argument(..., help="Issue key to watch, e.g. ABC-123"),
    account_id: Optional[str] = typer.Argument(
        None, help="Account id to add as a watcher. Omit to watch it yourself."
    ),
    debug: _cli.DebugOpt = False,
    traceback: _cli.TracebackOpt = False,
) -> None:
    """Add a watcher to a Jira issue. Adding yourself is immediate - no
    confirmation needed. Adding a different user shows a warning and asks
    for confirmation first, the same self-vs-other distinction enforced by
    the jira_set_watcher MCP tool."""
    me = asyncio.run(service.get_current_user(issue_key=key))
    own_account_id = me.get("accountId")
    target = account_id or own_account_id

    if target != own_account_id:
        console.print(f"[yellow]{_WATCH_OTHER_WARNING}[/yellow]")
        console.print(f"Issue: {key}, account to add as watcher: {target}")
        if not typer.confirm("Are you sure you want to add this watcher for someone else?", default=False):
            console.print("Cancelled.")
            return

    asyncio.run(service.add_watcher(key, target))
    console.print(f"[green]{target} is now watching {key}.[/green]")


@watch_app.command("remove")
@_guarded
def jira_watch_remove(
    key: str = typer.Argument(..., help="Issue key to stop watching, e.g. ABC-123"),
    account_id: Optional[str] = typer.Argument(
        None, help="Account id to remove as a watcher. Omit to stop watching it yourself."
    ),
    debug: _cli.DebugOpt = False,
    traceback: _cli.TracebackOpt = False,
) -> None:
    """Remove a watcher from a Jira issue. Removing yourself is immediate -
    no confirmation needed. Removing a different user shows a warning and
    asks for confirmation first."""
    me = asyncio.run(service.get_current_user(issue_key=key))
    own_account_id = me.get("accountId")
    target = account_id or own_account_id

    if target != own_account_id:
        console.print(f"[yellow]{_WATCH_OTHER_WARNING}[/yellow]")
        console.print(f"Issue: {key}, account to remove as watcher: {target}")
        if not typer.confirm("Are you sure you want to remove this watcher for someone else?", default=False):
            console.print("Cancelled.")
            return

    asyncio.run(service.remove_watcher(key, target))
    console.print(f"[green]{target} is no longer watching {key}.[/green]")


@worklog_app.command("list")
@_guarded
def jira_worklog_list(
    key: str = typer.Argument(..., help="Issue key to list worklogs for, e.g. ABC-123"),
    debug: _cli.DebugOpt = False,
    traceback: _cli.TracebackOpt = False,
) -> None:
    """List the worklog entries on a Jira issue."""
    result = asyncio.run(service.list_worklogs(key))
    worklogs = result.get("worklogs") or []
    if not worklogs:
        console.print("No worklog entries on this issue.")
        return
    for wl in worklogs:
        author = (wl.get("author") or {}).get("displayName", "unknown")
        seconds = wl.get("timeSpentSeconds", 0)
        console.print(f"[{wl.get('id')}] {author}: {seconds}s (started {wl.get('started')})")


@worklog_app.command("add")
@_guarded
def jira_worklog_add(
    key: str = typer.Argument(..., help="Issue key to log time against, e.g. ABC-123"),
    time_spent_seconds: int = typer.Argument(..., help="Time spent, in seconds"),
    started: str = typer.Argument(
        ..., help="Start time, ISO 8601 e.g. 2026-07-28T10:00:00 (naive times assumed UTC)"
    ),
    comment: Optional[str] = typer.Option(None, "--comment", help="Optional comment to attach"),
    debug: _cli.DebugOpt = False,
    traceback: _cli.TracebackOpt = False,
) -> None:
    """Log time against a Jira issue. Always logged as yourself - Jira's
    worklog creation endpoint has no author-override field, so there is
    nothing to confirm here (see jira_worklog_add's MCP tool description for
    the full reasoning)."""
    result = asyncio.run(service.add_worklog(key, time_spent_seconds, started, comment=comment))
    worklog_id = (result.get("worklog") or {}).get("id", "?")
    console.print(f"[green]Logged {time_spent_seconds}s on {key} (worklog id: {worklog_id}).[/green]")


def _resolve_worklog_author_id(key: str, worklog_id: str) -> str | None:
    """Look up a worklog entry's author.accountId via list_worklogs - the
    lookup half of jira_worklog_edit/jira_worklog_delete's self-vs-other
    gating decision, mirroring mcp_tools.py's _resolve_worklog_author."""
    result = asyncio.run(service.list_worklogs(key))
    for wl in result.get("worklogs") or []:
        if str(wl.get("id")) == str(worklog_id):
            return (wl.get("author") or {}).get("accountId")
    return None


@worklog_app.command("edit")
@_guarded
def jira_worklog_edit(
    key: str = typer.Argument(..., help="Issue key the worklog belongs to, e.g. ABC-123"),
    worklog_id: str = typer.Argument(..., help="Worklog id to edit"),
    time_spent_seconds: Optional[int] = typer.Option(
        None, "--time-spent-seconds", help="New time spent, in seconds"
    ),
    started: Optional[str] = typer.Option(None, "--started", help="New start time, ISO 8601"),
    comment: Optional[str] = typer.Option(None, "--comment", help="New comment text"),
    debug: _cli.DebugOpt = False,
    traceback: _cli.TracebackOpt = False,
) -> None:
    """Edit an existing worklog entry. Editing your own worklog is immediate
    - no confirmation needed. Editing someone else's shows a warning and
    asks for confirmation first, the same self-vs-other distinction enforced
    by the jira_worklog_edit MCP tool."""
    if time_spent_seconds is None and started is None and comment is None:
        console.print("[red]Provide at least one of --time-spent-seconds, --started, or --comment.[/red]")
        raise typer.Exit(1)

    author_account_id = _resolve_worklog_author_id(key, worklog_id)
    me = asyncio.run(service.get_current_user(issue_key=key))
    own_account_id = me.get("accountId")
    is_self = author_account_id is not None and author_account_id == own_account_id

    if not is_self:
        console.print(f"[yellow]{_WORKLOG_OTHER_WARNING}[/yellow]")
        console.print(f"Worklog to edit: {worklog_id} (on {key})")
        if not typer.confirm("Are you sure you want to edit this worklog entry for someone else?", default=False):
            console.print("Cancelled.")
            return

    asyncio.run(service.edit_worklog(
        key, worklog_id, time_spent_seconds=time_spent_seconds, started=started, comment=comment,
    ))
    console.print(f"[green]Updated worklog {worklog_id} on {key}.[/green]")


@worklog_app.command("delete")
@_guarded
def jira_worklog_delete(
    key: str = typer.Argument(..., help="Issue key the worklog belongs to, e.g. ABC-123"),
    worklog_id: str = typer.Argument(..., help="Worklog id to delete"),
    debug: _cli.DebugOpt = False,
    traceback: _cli.TracebackOpt = False,
) -> None:
    """Delete a worklog entry. Deleting your own worklog is immediate - no
    confirmation needed. Deleting someone else's shows a warning and asks
    for confirmation first."""
    author_account_id = _resolve_worklog_author_id(key, worklog_id)
    me = asyncio.run(service.get_current_user(issue_key=key))
    own_account_id = me.get("accountId")
    is_self = author_account_id is not None and author_account_id == own_account_id

    if not is_self:
        console.print(f"[yellow]{_WORKLOG_OTHER_WARNING}[/yellow]")
        console.print(f"Worklog to delete: {worklog_id} (on {key})")
        if not typer.confirm("Are you sure you want to delete this worklog entry for someone else?", default=False):
            console.print("Cancelled.")
            return

    asyncio.run(service.delete_worklog(key, worklog_id))
    console.print(f"[green]Deleted worklog {worklog_id} on {key}.[/green]")


jira_app.add_typer(comment_app, name="comment")
jira_app.add_typer(link_app, name="link")
jira_app.add_typer(attach_app, name="attach")
jira_app.add_typer(watch_app, name="watch")
jira_app.add_typer(worklog_app, name="worklog")
