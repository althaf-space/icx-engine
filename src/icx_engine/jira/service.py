"""Jira write-back service (close-out logic + create/delete + comments CRUD).

Sibling to `sonar/service.py`/`gitlab/service.py`: independent of `ConnectorBase`,
which stays read-only. `get_close_requirements` discovers what a specific issue
needs to transition/update (transitions + editable fields vary per project/
workflow); `apply_update` submits a transition and/or field update, optionally
with a comment. `list_issue_types`/`get_createmeta_fields` are the create-time
analogs; `create_issue`/`delete_issue` are the create/delete pair; `list_comments`/
`add_comment`/`edit_comment`/`delete_comment` are the standalone comment CRUD
pair - all plain pass-throughs, GATED reasoning lives at the MCP/CLI layer.
`search`/`get_issue` are a lightweight JQL search and raw single-issue fetch -
distinct from the read pipeline's `JiraConnector.fetch()`/`RawIssueData`, and
from `analyze_issue_fast`/`analyze_issue`'s full LLM analysis; both UNGATED.
`link_types`/`create_link`/`delete_link` are issue-link CRUD (`link_types` is
a global lookup resolved by domain, like `create_issue`/`search`;
`create_link` resolves by its `inward_key`; `delete_link` takes an
`issue_key` purely to resolve a connection, see its own docstring) and
`set_assignee` sets/clears/resets an issue's assignee - all plain
pass-throughs, GATED reasoning (only `delete_link` is gated) lives at the
MCP/CLI layer. `upload_attachment`/`delete_attachment` are the attachment
CRUD pair (`delete_attachment` takes `issue_key` purely to resolve a
connection, the same reasoning as `delete_link` - see its own docstring);
`upload_attachment` is UNGATED, `delete_attachment` is GATED with the same
verified permanent/no-undo warning as `delete_comment`.

`get_current_user`/`list_watchers`/`add_watcher`/`remove_watcher`/
`list_worklogs`/`add_worklog`/`edit_worklog`/`delete_worklog` are the
watcher/worklog surface (Task 6) - every one of them is a plain pass-through,
same as everything else in this module. The self-vs-other GATING DECISION
(acting on your own identity is UNGATED, acting on a different user's is
GATED) deliberately does NOT live here - it lives at the MCP/CLI layer,
which calls `get_current_user` first (itself just a lookup) to learn the
caller's own accountId before deciding whether to route a watcher/worklog
mutation through the confirm-token machinery. `get_current_user` resolves
its connection by `issue_key` when one is given - the SAME connection a
subsequent watcher/worklog call on that issue will use, which matters
because accountId is scoped per Jira Cloud site/connection - and falls back
to `_resolve_client_by_domain` (like `create_issue`/`search`/`link_types`)
for the standalone "who am I" lookup with no issue in view.

`apply_update`'s own comment restriction (below) is about its own transport
path only, not about Jira's API as a whole: `transition_issue` can only attach
a comment alongside a transition (see its docstring), so `apply_update` raises
`ValueError` if `comment` is given with `transition_id=None` rather than
silently dropping it. A standalone comment - with no transition attached -
goes through `add_comment` instead, which calls the dedicated
`.../issue/{key}/comment` endpoint.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from icx_engine.config_manager import ConfigManager
from icx_engine.connectors.jira.auth import build_auth_header
from icx_engine.connectors.jira.client import JiraClient
from icx_engine.connectors.jira.config import JiraConnection, JiraOAuthAuth
from icx_engine.connectors.jira.oauth import refresh_oauth_if_needed
from icx_engine.engine import resolve_connection
from icx_engine.exceptions import JiraValidationError, NoConnectionError

_AMBIGUOUS_MSG = (
    "Multiple Jira connections are configured and none is unambiguous for '{issue_key}'. "
    "Run `icx connection --active <domain>` to set a default connection."
)

_AMBIGUOUS_DOMAIN_MSG = (
    "Multiple Jira connections are configured and none is set as default. "
    "Pass `domain` to select one, or run `icx connection --active <domain>` to set a default connection."
)

# ICX-side hard cap on search cost, not optional - see `search()` below.
_MAX_SEARCH_RESULTS = 100
_DEFAULT_SEARCH_FIELDS = ["summary", "status", "issuetype"]


def _make_client(conn: JiraConnection, allowed_hosts: set[str]) -> JiraClient:
    """Build a JiraClient for `conn` - NOT a context manager, unlike
    SonarClient/GitLabClient. Callers use `client = _make_client(...)` directly,
    never `async with`. base_url branches exactly like `JiraConnector.fetch()`."""
    if isinstance(conn.auth, JiraOAuthAuth):
        base_url = f"https://api.atlassian.com/ex/jira/{conn.auth.cloud_id}/rest/api/3"
    else:
        base_url = f"https://{conn.domain}/rest/api/3"
    return JiraClient(base_url, build_auth_header(conn), allowed_hosts)


async def _client_from_connection(conn: Any, config: Any) -> JiraClient:
    """Shared tail of connection resolution: normalize to JiraConnection,
    refresh OAuth if needed, then build a JiraClient. OAuth refresh always
    runs before the client is constructed. Both `_resolve_client` (resolves
    by issue_key) and `_resolve_client_by_domain` (resolves by explicit
    domain - no issue_key available, e.g. create_issue) end here."""
    conn = conn if isinstance(conn, JiraConnection) else JiraConnection.model_validate(conn.model_dump())

    conn = await refresh_oauth_if_needed(conn, config)

    if isinstance(conn.auth, JiraOAuthAuth):
        allowed_hosts = {"api.atlassian.com", "api.media.atlassian.com", conn.domain}
    else:
        allowed_hosts = {conn.domain, "api.media.atlassian.com"}
    return _make_client(conn, allowed_hosts)


async def _resolve_client(issue_key: str, config: Any) -> JiraClient:
    """Resolve the Jira connection for `issue_key`, refresh its OAuth token if
    needed, then build a JiraClient. OAuth refresh always runs before the
    client is constructed."""
    conn = resolve_connection(domain=None, config=config, raw_input=issue_key)
    if conn is None:
        raise NoConnectionError(_AMBIGUOUS_MSG.format(issue_key=issue_key))
    return await _client_from_connection(conn, config)


async def _resolve_client_by_domain(domain: str | None, config: Any) -> JiraClient:
    """Resolve the Jira connection by an explicit `domain` (no issue_key to
    narrow by - e.g. create_issue, which has no existing issue). Works
    unchanged for the common single-connection/default-connection case;
    `domain` is only required when genuinely ambiguous (multiple Jira
    connections, no default set)."""
    conn = resolve_connection(domain=domain, config=config, raw_input=None)
    if conn is None:
        raise NoConnectionError(_AMBIGUOUS_DOMAIN_MSG)
    return await _client_from_connection(conn, config)


def _text_to_adf(text: str) -> dict:
    """Wrap plain text in Jira's minimal ADF paragraph shape for a comment body."""
    return {
        "type": "doc",
        "version": 1,
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}],
    }


def _format_started_for_jira(started: datetime | str) -> str:
    """Format a worklog's `started` moment the way Jira's write API requires:
    ISO 8601 with milliseconds and a NUMERIC timezone offset, NO trailing 'Z'
    (e.g. "2026-07-28T10:00:00.000+0000") - so callers (MCP tool / CLI) can
    pass a plain `datetime.datetime` or an ISO string without needing to know
    this formatting quirk themselves. Naive input (no tzinfo, or a 'Z'-suffixed
    string) is treated as UTC."""
    if isinstance(started, str):
        text = started.strip()
        if text.endswith("Z"):
            dt = datetime.fromisoformat(text[:-1]).replace(tzinfo=timezone.utc)
        else:
            dt = datetime.fromisoformat(text)
    else:
        dt = started
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + dt.strftime("%z")


async def get_close_requirements(issue_key: str) -> dict:
    """Discover what `issue_key` needs to close out: available workflow
    transitions (with per-transition required fields) and the fields editable
    on the issue right now. Both calls run concurrently."""
    config = ConfigManager.load()
    client = await _resolve_client(issue_key, config)
    transitions, editable_fields = await asyncio.gather(
        client.get_transitions(issue_key),
        client.get_editmeta(issue_key),
    )
    return {
        "issue_key": issue_key,
        "transitions": transitions,
        "editable_fields": editable_fields,
    }


async def apply_update(
    issue_key: str,
    transition_id: str | None = None,
    fields: dict | None = None,
    comment: str | None = None,
) -> dict:
    """Submit a transition and/or field update to `issue_key`, optionally with
    a comment.

    - `transition_id` given: moves the issue through that transition via
      `transition_issue`, carrying `fields` and `comment` (wrapped as ADF)
      along in the same call.
    - `transition_id` is None and `fields` given: a field-only update via
      `update_fields` (Jira's PUT path, no workflow transition).
    - `comment` given with `transition_id=None` raises `ValueError` - Jira has
      no comment-only write endpoint; a comment can only ride along with a
      transition. Provide a `transition_id` to attach it.
    - Neither `transition_id` nor `fields` given: raises `ValueError` - nothing
      to update.

    On a 400 validation error (`JiraValidationError`), returns a structured
    "second round" response instead of raising:
    `{"ok": False, "needs_fields": {<fieldKey>: <message>}, "message": <summary>}`
    so the caller (MCP tool / CLI) can ask the human for exactly what's
    missing. On success: `{"ok": True, "issue_key": ..., "transition_id": ...,
    "fields": ..., "comment": ...}`.
    """
    if comment is not None and transition_id is None:
        raise ValueError(
            "Cannot add a comment without a transition_id - Jira has no comment-only "
            "write endpoint yet. Provide a transition_id to attach the comment, or "
            "omit the comment and update fields only."
        )
    if transition_id is None and not fields:
        raise ValueError("Nothing to update - provide a transition_id and/or fields.")

    config = ConfigManager.load()
    client = await _resolve_client(issue_key, config)
    comment_adf = _text_to_adf(comment) if comment else None

    try:
        if transition_id is not None:
            await client.transition_issue(
                issue_key, transition_id=transition_id, fields=fields, comment_adf=comment_adf,
            )
        else:
            await client.update_fields(issue_key, fields)
    except JiraValidationError as exc:
        return {"ok": False, "needs_fields": exc.errors, "message": str(exc)}

    return {
        "ok": True,
        "issue_key": issue_key,
        "transition_id": transition_id,
        "fields": fields or {},
        "comment": comment,
    }


async def list_issue_types(project: str, domain: str | None = None) -> list[dict]:
    """The issue types available for creation in `project` - a plain
    pass-through, used by the CLI's interactive `jira create` flow to prompt
    the human with real options instead of a free-text guess."""
    config = ConfigManager.load()
    client = await _resolve_client_by_domain(domain, config)
    return await client.list_issuetypes(project)


async def get_createmeta_fields(project: str, issuetype_id: str, domain: str | None = None) -> dict:
    """The create-time required/available fields for `issuetype_id` in
    `project` - a plain pass-through, the create-time analog of
    `get_close_requirements`'s editmeta half."""
    config = ConfigManager.load()
    client = await _resolve_client_by_domain(domain, config)
    return await client.get_createmeta_fields(project, issuetype_id)


async def create_issue(
    project: str, issuetype: str, summary: str, fields: dict | None = None, domain: str | None = None,
) -> dict:
    """Create a new Jira issue. A plain pass-through - GATED reasoning
    (confirming with the human before this runs) lives at the MCP/CLI layer,
    not here.

    There is no existing issue_key to resolve a connection by (unlike every
    other write in this module), so the connection is resolved by explicit
    `domain` instead via `_resolve_client_by_domain` - works unchanged for
    the common single-connection/default-connection case; pass `domain` only
    when genuinely ambiguous (multiple Jira connections, no default set).
    """
    config = ConfigManager.load()
    client = await _resolve_client_by_domain(domain, config)
    issue_key = await client.create_issue(project, issuetype, summary, extra_fields=fields)
    return {
        "ok": True,
        "issue_key": issue_key,
        "project": project,
        "issuetype": issuetype,
        "summary": summary,
        "fields": fields or {},
    }


async def delete_issue(issue_key: str, delete_subtasks: bool = False) -> dict:
    """Permanently delete `issue_key`. A plain pass-through via the existing
    `_resolve_client(issue_key, config)` - unlike create_issue, a delete
    always has an issue_key to resolve the connection by. GATED reasoning
    lives at the MCP/CLI layer, not here. Jira Cloud has no recycle bin for
    issues - this cannot be undone."""
    config = ConfigManager.load()
    client = await _resolve_client(issue_key, config)
    await client.delete_issue(issue_key, delete_subtasks=delete_subtasks)
    return {"ok": True, "issue_key": issue_key, "deleted": True, "delete_subtasks": delete_subtasks}


async def list_comments(issue_key: str) -> dict:
    """List all comments on `issue_key`. A plain pass-through via the
    existing `_resolve_client(issue_key, config)`."""
    config = ConfigManager.load()
    client = await _resolve_client(issue_key, config)
    comments = await client.list_comments(issue_key)
    return {"issue_key": issue_key, "comments": comments}


async def add_comment(issue_key: str, comment: str) -> dict:
    """Add a plain-text comment to `issue_key`, wrapped as ADF via
    `_text_to_adf` before the write - mirrors how `apply_update` wraps its
    own `comment` param, but goes through the standalone comment endpoint
    instead of riding along with a transition. UNGATED at the MCP/CLI layer
    (additive, not destructive)."""
    config = ConfigManager.load()
    client = await _resolve_client(issue_key, config)
    created = await client.add_comment(issue_key, _text_to_adf(comment))
    return {"ok": True, "issue_key": issue_key, "comment": created}


async def edit_comment(issue_key: str, comment_id: str, comment: str) -> dict:
    """Edit an existing comment's text on `issue_key`, wrapped as ADF via
    `_text_to_adf`. UNGATED at the MCP/CLI layer, matching `add_comment`."""
    config = ConfigManager.load()
    client = await _resolve_client(issue_key, config)
    updated = await client.edit_comment(issue_key, comment_id, _text_to_adf(comment))
    return {"ok": True, "issue_key": issue_key, "comment_id": comment_id, "comment": updated}


async def delete_comment(issue_key: str, comment_id: str) -> dict:
    """Permanently delete a comment from `issue_key`. Verified: Jira has no
    recovery mechanism for a deleted comment (no recycle bin, no undo) - the
    same permanence as `delete_issue`. GATED reasoning lives at the MCP/CLI
    layer, not here."""
    config = ConfigManager.load()
    client = await _resolve_client(issue_key, config)
    await client.delete_comment(issue_key, comment_id)
    return {"ok": True, "issue_key": issue_key, "comment_id": comment_id, "deleted": True}


async def search(
    jql: str, fields: list[str] | None = None, max_results: int = 50,
    page_token: str | None = None, domain: str | None = None,
) -> dict:
    """Search issues by JQL. UNGATED at the MCP/CLI layer (read-only).

    ICX-side hard caps - the actual point of this method, not optional:
    `max_results` is clamped to [1, _MAX_SEARCH_RESULTS] regardless of what
    is requested, and when `fields` is omitted/empty this defaults to a small
    explicit set (`_DEFAULT_SEARCH_FIELDS`) instead of letting Jira return its
    unbounded default field set - the same cost-safety discipline as sonar's
    `top_files`/`component_tree`.

    There is no existing issue_key to resolve a connection by (like
    `create_issue`), so the connection is resolved by explicit `domain` via
    `_resolve_client_by_domain`.
    """
    capped_max_results = min(max(1, max_results), _MAX_SEARCH_RESULTS)
    effective_fields = fields if fields else list(_DEFAULT_SEARCH_FIELDS)

    config = ConfigManager.load()
    client = await _resolve_client_by_domain(domain, config)
    result = await client.search_issues(
        jql, fields=effective_fields, max_results=capped_max_results, page_token=page_token,
    )
    return {
        "jql": jql,
        "issues": result.get("issues", []),
        "next_page_token": result.get("next_page_token"),
        "is_last": result.get("is_last", True),
    }


async def get_issue(issue_key: str, fields: list[str] | None = None) -> dict:
    """Lightweight raw fetch of `issue_key`'s current fields - NOT the
    RawIssueData shape the read pipeline (`JiraConnector.fetch()`) produces;
    useful for a cheap status check before deciding an action. A plain
    pass-through via the existing `_resolve_client(issue_key, config)`.
    UNGATED at the MCP/CLI layer (read-only)."""
    config = ConfigManager.load()
    client = await _resolve_client(issue_key, config)
    raw = await client.get_issue_raw(issue_key, fields=fields)
    return {"issue_key": issue_key, "raw": raw}


async def link_types(domain: str | None = None) -> dict:
    """List the link types available for creating an issue link (e.g.
    'Blocks', 'Relates to'). A GLOBAL, connection-level lookup with no
    issue_key at all - unlike every other method in this module apart from
    `create_issue`/`search`, it resolves its connection by explicit `domain`
    via `_resolve_client_by_domain`, the same pattern those two already use.
    UNGATED at the MCP/CLI layer (read-only)."""
    config = ConfigManager.load()
    client = await _resolve_client_by_domain(domain, config)
    return {"link_types": await client.list_link_types()}


async def create_link(link_type_name: str, inward_key: str, outward_key: str) -> dict:
    """Link `inward_key` and `outward_key` together as `link_type_name`.

    CONNECTION-RESOLUTION DECISION: resolves via `_resolve_client(inward_key,
    config)` - the inward issue, not the outward one. The plan does not
    mandate either side; `inward_key` is picked here for consistency with
    argument order (it appears first) and because Jira does not support
    cross-instance links at all, so both keys resolve to the same connection
    in the vast majority of real cases - this choice makes no practical
    difference. UNGATED at the MCP/CLI layer (additive, reversible via
    `delete_link`)."""
    config = ConfigManager.load()
    client = await _resolve_client(inward_key, config)
    await client.create_link(link_type_name, inward_key, outward_key)
    return {
        "ok": True, "link_type_name": link_type_name,
        "inward_key": inward_key, "outward_key": outward_key,
    }


async def delete_link(issue_key: str, link_id: str) -> dict:
    """Delete the link identified by `link_id`.

    CONNECTION-RESOLUTION DECISION: Jira's `DELETE .../issueLink/{id}`
    endpoint is global - it does not take an issue key in its URL path at
    all - but ICX's `_resolve_client` needs SOME issue_key to pick which
    Jira connection to call. `issue_key` here exists PURELY for that
    resolution purpose; it plays no role in the actual DELETE call. This is
    the pragmatic choice: in practice the caller already has an issue key
    handy, since `link_id` itself was obtained by listing or viewing that
    very issue's links in the first place - there is no realistic path to
    having a `link_id` with no issue_key at all. GATED at the MCP/CLI layer:
    not because this is irreversible (a link of the same type CAN be
    recreated afterward - Jira issue links are not permanent the way a
    deleted issue/comment is), but because removing a link can hide real
    dependency information between issues until someone notices it is
    missing and re-adds it."""
    config = ConfigManager.load()
    client = await _resolve_client(issue_key, config)
    await client.delete_link(link_id)
    return {"ok": True, "issue_key": issue_key, "link_id": link_id, "deleted": True}


async def set_assignee(issue_key: str, account_id: str | None = None) -> dict:
    """Set, clear, or reset the assignee of `issue_key`. A plain pass-through
    via the existing `_resolve_client(issue_key, config)`. `account_id=None`
    unassigns the issue; `"-1"` assigns Jira's project default assignee; any
    other string assigns that account. UNGATED at the MCP/CLI layer -
    reversible at any time by calling this again with a different value."""
    config = ConfigManager.load()
    client = await _resolve_client(issue_key, config)
    await client.set_assignee(issue_key, account_id)
    return {"ok": True, "issue_key": issue_key, "account_id": account_id}


async def upload_attachment(
    issue_key: str, filename: str, content_bytes: bytes, content_type: str | None = None,
) -> dict:
    """Upload a file attachment to `issue_key`. A plain pass-through via the
    existing `_resolve_client(issue_key, config)` - there is an issue_key to
    resolve by here, unlike create_issue/search/link_types. UNGATED at the
    MCP/CLI layer (additive, reversible via `delete_attachment`)."""
    config = ConfigManager.load()
    client = await _resolve_client(issue_key, config)
    created = await client.upload_attachment(issue_key, filename, content_bytes, content_type=content_type)
    return {"ok": True, "issue_key": issue_key, "filename": filename, "attachments": created}


async def delete_attachment(issue_key: str, attachment_id: str) -> dict:
    """Permanently delete an attachment identified by `attachment_id`.

    CONNECTION-RESOLUTION DECISION: mirrors `delete_link`'s precedent
    exactly. Jira's `DELETE .../attachment/{id}` endpoint is global - it
    does not take an issue key in its URL path at all - but ICX's
    `_resolve_client` needs SOME issue_key to pick which Jira connection to
    call. `issue_key` here exists PURELY for that resolution purpose; it
    plays no role in the actual DELETE call. Same pragmatic reasoning as
    `delete_link`: the caller already has an issue key handy, since
    `attachment_id` itself was obtained by viewing or listing that very
    issue's attachments in the first place.

    PERMANENCE (verified, not assumed - same due diligence as
    `delete_comment`): Jira Cloud has no recycle bin/trash for attachments,
    the same permanence class as a deleted issue or comment - a deleted
    attachment cannot be recovered by the user. GATED reasoning lives at the
    MCP/CLI layer, not here.
    """
    config = ConfigManager.load()
    client = await _resolve_client(issue_key, config)
    await client.delete_attachment(attachment_id)
    return {"ok": True, "issue_key": issue_key, "attachment_id": attachment_id, "deleted": True}


async def get_current_user(issue_key: str | None = None, domain: str | None = None) -> dict:
    """The authenticated user's own Jira identity (GET .../myself). A plain
    pass-through - UNGATED, used both as a standalone "who am I" lookup and,
    critically, by the MCP/CLI layer's self-vs-other gating decision for
    watcher/worklog mutations (see mcp_tools.py).

    CONNECTION-RESOLUTION DECISION: when `issue_key` is given, resolves via
    the existing `_resolve_client(issue_key, config)` - the SAME connection
    a watcher/worklog mutation on that issue will use. This matters:
    accountId is scoped per Jira Cloud site/connection, so a domain-resolved
    connection could silently differ from the mutation's actual target
    connection in a multi-connection setup and produce a wrong self-vs-other
    comparison. When `issue_key` is omitted (the standalone
    `jira_get_current_user` tool has no required arguments), falls back to
    `_resolve_client_by_domain`, the same pattern `create_issue`/`search`/
    `link_types` already use.
    """
    config = ConfigManager.load()
    if issue_key is not None:
        client = await _resolve_client(issue_key, config)
    else:
        client = await _resolve_client_by_domain(domain, config)
    return await client.get_current_user()


async def list_watchers(issue_key: str) -> dict:
    """List the watchers on `issue_key`. A plain pass-through via the
    existing `_resolve_client(issue_key, config)`. UNGATED (read-only)."""
    config = ConfigManager.load()
    client = await _resolve_client(issue_key, config)
    raw = await client.list_watchers(issue_key)
    return {
        "issue_key": issue_key,
        "watchers": raw.get("watchers", []),
        "watch_count": raw.get("watchCount", 0),
    }


async def add_watcher(issue_key: str, account_id: str) -> dict:
    """Add `account_id` as a watcher on `issue_key`. A plain pass-through -
    the self-vs-other GATING DECISION lives entirely at the MCP/CLI layer,
    not here; this function itself has no notion of who is calling."""
    config = ConfigManager.load()
    client = await _resolve_client(issue_key, config)
    await client.add_watcher(issue_key, account_id)
    return {"ok": True, "issue_key": issue_key, "account_id": account_id, "watching": True}


async def remove_watcher(issue_key: str, account_id: str) -> dict:
    """Remove `account_id` as a watcher on `issue_key`. A plain pass-through -
    same GATING note as `add_watcher`."""
    config = ConfigManager.load()
    client = await _resolve_client(issue_key, config)
    await client.remove_watcher(issue_key, account_id)
    return {"ok": True, "issue_key": issue_key, "account_id": account_id, "watching": False}


async def list_worklogs(issue_key: str) -> dict:
    """List the worklog entries on `issue_key`. A plain pass-through via the
    existing `_resolve_client(issue_key, config)`. UNGATED (read-only). Also
    used by the MCP/CLI layer to look up a worklog entry's `author.accountId`
    before deciding whether an edit/delete is self or other."""
    config = ConfigManager.load()
    client = await _resolve_client(issue_key, config)
    raw = await client.list_worklogs(issue_key)
    return {"issue_key": issue_key, "worklogs": raw.get("worklogs", [])}


async def add_worklog(
    issue_key: str, time_spent_seconds: int, started: datetime | str, comment: str | None = None,
) -> dict:
    """Add a worklog entry to `issue_key`. `started` is formatted for Jira via
    `_format_started_for_jira` - callers pass a plain `datetime.datetime` or
    ISO string, never Jira's exact wire format. `comment` is plain text,
    wrapped via `_text_to_adf`, mirroring every other comment-accepting
    method in this module. UNGATED at the MCP/CLI layer: Jira's worklog POST
    has no author-override field - a worklog is always attributed to the
    authenticated caller, so there is no "on behalf of someone else" case to
    gate here at all (see mcp_tools.py's jira_worklog_add for the full
    reasoning)."""
    config = ConfigManager.load()
    client = await _resolve_client(issue_key, config)
    comment_adf = _text_to_adf(comment) if comment else None
    created = await client.add_worklog(
        issue_key, time_spent_seconds, _format_started_for_jira(started), comment_adf=comment_adf,
    )
    return {"ok": True, "issue_key": issue_key, "worklog": created}


async def edit_worklog(
    issue_key: str, worklog_id: str, time_spent_seconds: int | None = None,
    started: datetime | str | None = None, comment: str | None = None,
) -> dict:
    """Edit an existing worklog entry on `issue_key`. A plain pass-through -
    the self-vs-other GATING DECISION (comparing the worklog's own
    `author.accountId` against the caller's) lives entirely at the MCP/CLI
    layer, not here; this function has no notion of who is calling or who
    logged the original entry."""
    config = ConfigManager.load()
    client = await _resolve_client(issue_key, config)
    comment_adf = _text_to_adf(comment) if comment else None
    formatted_started = _format_started_for_jira(started) if started is not None else None
    updated = await client.edit_worklog(
        issue_key, worklog_id, time_spent_seconds=time_spent_seconds,
        started=formatted_started, comment_adf=comment_adf,
    )
    return {"ok": True, "issue_key": issue_key, "worklog_id": worklog_id, "worklog": updated}


async def delete_worklog(issue_key: str, worklog_id: str) -> dict:
    """Delete a worklog entry from `issue_key`. A plain pass-through - same
    GATING note as `edit_worklog`."""
    config = ConfigManager.load()
    client = await _resolve_client(issue_key, config)
    await client.delete_worklog(issue_key, worklog_id)
    return {"ok": True, "issue_key": issue_key, "worklog_id": worklog_id, "deleted": True}
