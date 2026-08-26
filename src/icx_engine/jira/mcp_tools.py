"""MCP tool surface for Jira write-back (close-out). Mirrors
git/mcp_tools.py's module shape: JIRA_TOOLS + dispatch_jira_tool - the
dispatcher returns None for an unrecognized tool name so mcp_server.py's own
dispatcher chain falls through unmodified."""
from __future__ import annotations

import base64
import binascii
import json
from pathlib import Path

from mcp.types import TextContent, Tool

from icx_engine.confirm import issue_token, verify_token
from icx_engine.jira import service

_GET_CLOSE_REQUIREMENTS_TOOL = "jira_get_close_requirements"
_APPLY_UPDATE_TOOL = "jira_apply_update"
_LIST_ISSUE_TYPES_TOOL = "jira_list_issue_types"
_GET_CREATEMETA_FIELDS_TOOL = "jira_get_createmeta_fields"
_CREATE_ISSUE_TOOL = "jira_create_issue"
_DELETE_ISSUE_TOOL = "jira_delete_issue"
_COMMENT_LIST_TOOL = "jira_comment_list"
_COMMENT_ADD_TOOL = "jira_comment_add"
_COMMENT_EDIT_TOOL = "jira_comment_edit"
_COMMENT_DELETE_TOOL = "jira_comment_delete"
_SEARCH_TOOL = "jira_search"
_GET_ISSUE_TOOL = "jira_get_issue"
_LINK_TYPES_TOOL = "jira_link_types"
_LINK_CREATE_TOOL = "jira_link_create"
_LINK_DELETE_TOOL = "jira_link_delete"
_SET_ASSIGNEE_TOOL = "jira_set_assignee"
_SEARCH_ASSIGNABLE_USERS_TOOL = "jira_search_assignable_users"
_ATTACHMENT_UPLOAD_TOOL = "jira_attachment_upload"
_ATTACHMENT_DELETE_TOOL = "jira_attachment_delete"
_GET_CURRENT_USER_TOOL = "jira_get_current_user"
_LIST_WATCHERS_TOOL = "jira_list_watchers"
_LIST_WORKLOGS_TOOL = "jira_list_worklogs"
_SET_WATCHER_TOOL = "jira_set_watcher"
_WORKLOG_ADD_TOOL = "jira_worklog_add"
_WORKLOG_EDIT_TOOL = "jira_worklog_edit"
_WORKLOG_DELETE_TOOL = "jira_worklog_delete"

JIRA_TOOLS: list[Tool] = [
    Tool(
        name=_GET_CLOSE_REQUIREMENTS_TOOL,
        description=(
            "USE WHEN the human wants to close out or update a Jira issue: MUST call this "
            "first, before jira_apply_update, to discover what the issue actually needs - "
            "available workflow transitions (with any per-transition required fields) and the "
            "fields currently editable on the issue. Transitions and required fields vary per "
            "project/workflow - never guess them. `include_allowed_values` (default true) "
            "controls whether each field's full option catalogue (`allowedValues` - every "
            "Functionality/Component/Issue Category value, sometimes 50-70+ entries) is "
            "included - PASS `include_allowed_values=false` on repeat calls for the SAME issue "
            "within a multi-hop workflow walk (e.g. New -> Assigned -> Fixed -> Retest) once the "
            "catalogue is already known from an earlier call - it does not change between hops "
            "moments apart, and re-sending it every time is pure repeated payload. `required`/ "
            "`schema` are still returned either way. The response always includes `status` (the "
            "issue's current workflow status) - PASS that exact value back as `since_status` on "
            "your NEXT call for the same issue if the intervening jira_apply_update call only "
            "changed a field, not the status: if status is unchanged, this returns a compact "
            "`{status, unchanged: true}` instead of the full transitions/editable_fields bundle "
            "(both are purely a function of current status, so they cannot have changed either). "
            "Omit `since_status` (or if the status DID change) to get the full bundle. Requires a "
            "resolvable Jira connection for the issue's key."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "issue_key": {"type": "string"},
                "include_allowed_values": {"type": "boolean"},
                "since_status": {"type": "string"},
            },
            "required": ["issue_key"],
        },
            annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True},
    ),
    Tool(
        name=_APPLY_UPDATE_TOOL,
        description=(
            "USE WHEN the human has decided what to submit to a Jira issue after calling "
            "jira_get_close_requirements (a transition and/or field update, optionally with a "
            "comment attached to a transition): MUST submit exactly that. This is a "
            "CONFIRMATION-GATED tool: the first call (no confirm_token) returns "
            "pending_confirmation plus a one-time token after showing the human the exact "
            "issue_key/transition_id/fields/comment that will be submitted - you MUST show that "
            "to the human and get an explicit yes before calling again with confirm_token set. "
            "Calling with a wrong or reused token fails. A 400 validation response comes back as "
            "needs_fields (not a bare failure) - relay those field messages to the human and "
            "retry with them filled in by calling this tool AGAIN FROM SCRATCH (no confirm_token) - "
            "the consumed token cannot be reused, so a fresh pending_confirmation + human agreement "
            "is required for the retry too, the same as any other call. Requires a resolvable Jira "
            "connection for the issue's key."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "issue_key": {"type": "string"},
                "transition_id": {"type": "string"},
                "fields": {"type": "object"},
                "comment": {"type": "string"},
                "confirm_token": {"type": "string"},
            },
            "required": ["issue_key"],
        },
            annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True},
    ),
    Tool(
        name=_LIST_ISSUE_TYPES_TOOL,
        description=(
            "USE WHEN the human wants to create a Jira issue and the exact issuetype name/id for "
            "the target project isn't already known: call this first. Issue types are configured "
            "per-project - never guess a name. Read-only, UNGATED. Optional limit/offset to page "
            "results. Pass `domain` only when "
            "multiple Jira connections are configured and none is set as default."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project": {"type": "string"}, "domain": {"type": "string"},
                "limit": {"type": "integer"}, "offset": {"type": "integer"},
            },
            "required": ["project"],
        },
            annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
    ),
    Tool(
        name=_GET_CREATEMETA_FIELDS_TOOL,
        description=(
            "BEST-EFFORT ONLY - call jira_list_issue_types + this tool as a first, cheap attempt "
            "to learn create-time fields for a project+issuetype (keyed by real field id, e.g. "
            "`customfield_10050`, each with `required`/`schema`/`allowedValues`) before creating a "
            "Jira issue whose `fields` carries anything beyond a plain summary. UNRELIABLE ON SOME "
            "JIRA PROJECTS: Jira's createmeta endpoint is documented to return an EMPTY or "
            "incomplete field list on certain project configurations (observed live: team-managed "
            "projects), independent of the field genuinely existing and being settable - this is a "
            "known Jira Cloud API gap, not something a retry or different parameters fixes. "
            "If this returns empty, or is missing a field the human named, do NOT fall back to "
            "guessing its key or value shape: the RELIABLE fallback is jira_get_close_requirements "
            "called on an EXISTING issue of the SAME project+issuetype (find one with jira_search, "
            "e.g. `project = X AND issuetype = Y ORDER BY created DESC`, if none is already known) "
            "- its `editable_fields` reliably includes the real field id and `allowedValues` (e.g. "
            "`customfield_10045` for Severity with Critical/Major/Minor/Trivial) even when this tool "
            "returns nothing. A field's DISPLAY NAME (e.g. 'Severity') is NEVER the correct JSON "
            "key either way - select-list fields take `{\"value\": ...}` while a plain few system "
            "fields like `priority` take `{\"name\": ...}`. Read-only, UNGATED. Pass `domain` only "
            "when multiple Jira connections are configured and none is set as default."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "issuetype_id": {"type": "string"},
                "domain": {"type": "string"},
            },
            "required": ["project", "issuetype_id"],
        },
            annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
    ),
    Tool(
        name=_CREATE_ISSUE_TOOL,
        description=(
            "USE WHEN the human wants to create a new Jira issue. If `fields` will carry anything "
            "beyond a plain summary (severity, components, any custom field), its real field id and "
            "value shape MUST be confirmed first - NEVER send a guessed field key (e.g. a literal "
            "\"Severity\" key) or guessed value shape, Jira rejects it with a generic validation "
            "error that does not say what was wrong. jira_get_createmeta_fields is a cheap "
            "best-effort first try, but it is UNRELIABLE on some Jira projects and can return "
            "completely empty; when it does (or is missing the field), call jira_get_close_requirements "
            "on an EXISTING issue of the SAME project+issuetype instead (use jira_search to find one "
            "if none is already known) - its `editable_fields` reliably has the real field id and "
            "`allowedValues`, unlike createmeta. `fields.description` may be passed as a PLAIN "
            "STRING - it is auto-wrapped into Jira's required ADF format before submission, no "
            "manual conversion needed. Creates a persistent artifact in Jira, so "
            "this is a CONFIRMATION-GATED tool: the first call (no confirm_token) returns "
            "pending_confirmation plus a one-time token after showing the human the exact "
            "project/issuetype/summary/fields that will be submitted - you MUST show that to the "
            "human and get an explicit yes before calling again with confirm_token set. Calling "
            "with a wrong or reused token fails. Pass `domain` only when multiple Jira connections "
            "are configured and none is set as default - otherwise omit it."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "issuetype": {"type": "string"},
                "summary": {"type": "string"},
                "fields": {"type": "object"},
                "domain": {"type": "string"},
                "confirm_token": {"type": "string"},
            },
            "required": ["project", "issuetype", "summary"],
        },
            annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True},
    ),
    Tool(
        name=_DELETE_ISSUE_TOOL,
        description=(
            "USE WHEN the human wants to delete a Jira issue. WARNING: this is PERMANENT - no "
            "undo, no trash - Jira Cloud has no recycle bin for issues, so a deleted issue cannot "
            "be recovered. This is a CONFIRMATION-GATED tool: the first call (no confirm_token) "
            "returns pending_confirmation plus a one-time token after showing the human the exact "
            "issue_key (and whether subtasks are also being deleted) that will be permanently "
            "removed - you MUST show that to the human, including this permanent/no-undo/no-trash "
            "warning, and get an explicit yes before calling again with confirm_token set. Calling "
            "with a wrong or reused token fails."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "issue_key": {"type": "string"},
                "delete_subtasks": {"type": "boolean"},
                "confirm_token": {"type": "string"},
            },
            "required": ["issue_key"],
        },
            annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True},
    ),
    Tool(
        name=_COMMENT_LIST_TOOL,
        description=(
            "USE WHEN the human wants to see the comments on a Jira issue. Read-only, UNGATED. "
            "Optional limit/offset to page results. Requires a resolvable Jira connection for "
            "the issue's key."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "issue_key": {"type": "string"},
                "limit": {"type": "integer"}, "offset": {"type": "integer"},
            },
            "required": ["issue_key"],
        },
            annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
    ),
    Tool(
        name=_COMMENT_ADD_TOOL,
        description=(
            "USE WHEN the human wants to add a new comment to a Jira issue. Additive and "
            "reversible via jira_comment_delete, so this tool is UNGATED - it executes "
            "immediately, no confirm_token round-trip. `comment` is plain text; it is wrapped "
            "into Jira's ADF comment body format automatically. Requires a resolvable Jira "
            "connection for the issue's key."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "issue_key": {"type": "string"},
                "comment": {"type": "string"},
            },
            "required": ["issue_key", "comment"],
        },
            annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
    ),
    Tool(
        name=_COMMENT_EDIT_TOOL,
        description=(
            "USE WHEN the human wants to change the text of an existing comment on a Jira "
            "issue. UNGATED - it executes immediately, no confirm_token round-trip. `comment` "
            "is plain text; it is wrapped into Jira's ADF comment body format automatically. "
            "Requires a resolvable Jira connection for the issue's key."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "issue_key": {"type": "string"},
                "comment_id": {"type": "string"},
                "comment": {"type": "string"},
            },
            "required": ["issue_key", "comment_id", "comment"],
        },
            annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
    ),
    Tool(
        name=_COMMENT_DELETE_TOOL,
        description=(
            "USE WHEN the human wants to delete a comment from a Jira issue. WARNING: this is "
            "PERMANENT - deleted comments cannot be recovered; Jira has no undo for a deleted "
            "comment (verified: the same permanence as deleting an issue, there is simply no "
            "comment-level trash to begin with). This is a CONFIRMATION-GATED tool: the first "
            "call (no confirm_token) returns pending_confirmation plus a one-time token after "
            "showing the human the exact issue_key and comment_id that will be permanently "
            "removed - you MUST show that to the human, including this permanent/no-undo "
            "warning, and get an explicit yes before calling again with confirm_token set. "
            "Calling with a wrong or reused token fails."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "issue_key": {"type": "string"},
                "comment_id": {"type": "string"},
                "confirm_token": {"type": "string"},
            },
            "required": ["issue_key", "comment_id"],
        },
            annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True},
    ),
    Tool(
        name=_SEARCH_TOOL,
        description=(
            "USE WHEN the human wants to find Jira issues matching a JQL query. This is a "
            "LIGHTWEIGHT, RAW search - it returns bare Jira issue fields with NO LLM analysis, "
            "distinct from jira_analyze_issue_fast/jira_analyze_issue (which run full LLM analysis on a "
            "single already-known issue). Use jira_search to discover candidate issue keys "
            "first, then call jira_analyze_issue_fast/jira_analyze_issue on a specific key afterwards if "
            "deep analysis is needed - this tool does not replace those. UNGATED, read-only, "
            "executes immediately. Cost-capped server-side: max_results is clamped to 100 "
            "regardless of what is requested, and fields defaults to a small set "
            "(summary/status/issuetype) when omitted rather than Jira's unbounded default "
            "field set. Pagination is token-based - pass the previous response's "
            "next_page_token back as page_token to continue. Pass `domain` only when multiple "
            "Jira connections are configured and none is set as default."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "jql": {"type": "string"},
                "fields": {"type": "array", "items": {"type": "string"}},
                "max_results": {"type": "integer"},
                "page_token": {"type": "string"},
                "domain": {"type": "string"},
            },
            "required": ["jql"],
        },
            annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
    ),
    Tool(
        name=_GET_ISSUE_TOOL,
        description=(
            "USE WHEN the human wants a cheap, raw look at a single Jira issue's current "
            "fields - e.g. a quick status check before deciding an action. This is a "
            "LIGHTWEIGHT, RAW fetch - it returns Jira's bare field JSON with NO LLM analysis. "
            "It is NOT a replacement for jira_analyze_issue_fast/jira_analyze_issue, which run full LLM "
            "analysis and produce ICX's structured issue context - use those instead when real "
            "analysis is needed, use jira_get_issue only for a quick raw peek. UNGATED, "
            "read-only, executes immediately. Pass `fields` to limit which fields come back; "
            "omit it to get Jira's own default field set for this issue."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "issue_key": {"type": "string"},
                "fields": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["issue_key"],
        },
            annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
    ),
    Tool(
        name=_LINK_TYPES_TOOL,
        description=(
            "USE WHEN the human wants to see what link types are available for linking Jira "
            "issues together (e.g. 'Blocks', 'Relates to', 'Duplicates'), typically before "
            "calling jira_link_create so the exact link_type_name is known rather than "
            "guessed. This is a GLOBAL, connection-level lookup with no issue_key - it does "
            "not depend on any particular issue. UNGATED, read-only, executes immediately. "
            "Pass `domain` only when multiple Jira connections are configured and none is set "
            "as default - otherwise omit it."
        ),
        inputSchema={
            "type": "object",
            "properties": {"domain": {"type": "string"}},
            "required": [],
        },
            annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
    ),
    Tool(
        name=_LINK_CREATE_TOOL,
        description=(
            "USE WHEN the human wants to link two Jira issues together (e.g. 'ABC-1 blocks "
            "ABC-2'). Call jira_link_types first if the exact link_type_name isn't already "
            "known - Jira rejects an unrecognized name. Additive and reversible via "
            "jira_link_delete, so this tool is UNGATED - it executes immediately, no "
            "confirm_token round-trip. Requires a resolvable Jira connection for the "
            "inward_key."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "link_type_name": {"type": "string"},
                "inward_key": {"type": "string"},
                "outward_key": {"type": "string"},
            },
            "required": ["link_type_name", "inward_key", "outward_key"],
        },
            annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
    ),
    Tool(
        name=_LINK_DELETE_TOOL,
        description=(
            "USE WHEN the human wants to remove a link between two Jira issues. This is a "
            "CONFIRMATION-GATED tool: removing a link can hide real dependency information "
            "between issues - a link of the same type CAN be recreated afterward if the "
            "relationship still applies (this is not a permanent, no-undo action like deleting "
            "an issue or comment), but until someone notices it is missing and re-adds it, "
            "anyone relying on that link sees an incomplete picture of how the issues relate. "
            "The first call (no confirm_token) returns pending_confirmation plus a one-time "
            "token after showing the human the exact issue_key and link_id that will be "
            "removed - you MUST show that to the human, including this dependency-visibility "
            "risk, and get an explicit yes before calling again with confirm_token set. Calling "
            "with a wrong or reused token fails. `issue_key` is used only to resolve which "
            "Jira connection to call - Jira's link-delete endpoint itself is global and does "
            "not take an issue key in its URL."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "issue_key": {"type": "string"},
                "link_id": {"type": "string"},
                "confirm_token": {"type": "string"},
            },
            "required": ["issue_key", "link_id"],
        },
            annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True},
    ),
    Tool(
        name=_SET_ASSIGNEE_TOOL,
        description=(
            "USE WHEN the human wants to assign, unassign, or reset the assignee of a Jira "
            "issue. If assigning to anyone other than the caller and their real account_id "
            "isn't already known, call jira_search_assignable_users first - do not guess an "
            "account_id. Reversible at any time (assigning again changes it back), so this tool "
            "is UNGATED - it executes immediately, no confirm_token round-trip. Pass "
            "`account_id` to assign that account; omit it (or pass null) to unassign the issue; "
            "pass the literal string \"-1\" to assign the project's default assignee. Requires a "
            "resolvable Jira connection for the issue_key."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "issue_key": {"type": "string"},
                "account_id": {"type": "string"},
            },
            "required": ["issue_key"],
        },
            annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
    ),
    Tool(
        name=_SEARCH_ASSIGNABLE_USERS_TOOL,
        description=(
            "USE WHEN the human wants to assign a Jira issue to someone other than themselves "
            "and the real account_id isn't already known - call this before jira_set_assignee. "
            "Returns users assignable to `issue_key`, each with a real `accountId`, optionally "
            "narrowed by `query` (name/email substring). get_current_user only resolves the "
            "caller's own accountId, never someone else's - this is the discovery path for "
            "anyone else, so account_id is never guessed. Read-only, UNGATED. Optional "
            "limit/offset to page results. Requires a resolvable Jira connection for the issue_key."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "issue_key": {"type": "string"},
                "query": {"type": "string"},
                "limit": {"type": "integer"}, "offset": {"type": "integer"},
            },
            "required": ["issue_key"],
        },
            annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
    ),
    Tool(
        name=_ATTACHMENT_UPLOAD_TOOL,
        description=(
            "USE WHEN the human wants to upload a file attachment to a Jira issue. Additive and "
            "reversible via jira_attachment_delete, so this tool is UNGATED - it executes "
            "immediately, no confirm_token round-trip. Pass EXACTLY ONE of: `file_path` (an "
            "absolute local path - ICX reads the file directly off disk, the same way `icx jira "
            "attach add` does; `filename` is derived from the path unless you also pass an explicit "
            "`filename`; this is the reliable option for binary files like Excel/PDF/images, since "
            "no encoding round-trip through the calling agent is involved) OR `content_base64` "
            "(the file's contents base64-encoded, e.g. Python's base64.b64encode(data).decode() - "
            "for when the content only exists in-memory / no local file path applies; `filename` is "
            "then REQUIRED since there is no path to derive it from). An invalid base64 string, or a "
            "file_path that does not exist or is not a file, is rejected with a named error. Pass "
            "`content_type` (a MIME type, e.g. 'image/png') if known; when omitted it defaults to "
            "'application/octet-stream'. Requires a resolvable Jira connection for the issue's key."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "issue_key": {"type": "string"},
                "filename": {"type": "string"},
                "file_path": {"type": "string"},
                "content_base64": {"type": "string"},
                "content_type": {"type": "string"},
            },
            "required": ["issue_key"],
        },
            annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
    ),
    Tool(
        name=_ATTACHMENT_DELETE_TOOL,
        description=(
            "USE WHEN the human wants to delete an attachment from a Jira issue. WARNING: this is "
            "PERMANENT - no undo, no trash - Jira Cloud has no recycle bin for attachments, so a "
            "deleted attachment cannot be recovered (verified: the same permanence as deleting an "
            "issue or comment). This is a CONFIRMATION-GATED tool: the first call (no confirm_token) "
            "returns pending_confirmation plus a one-time token after showing the human the exact "
            "issue_key and attachment_id that will be permanently removed - you MUST show that to "
            "the human, including this permanent/no-undo/no-trash warning, and get an explicit yes "
            "before calling again with confirm_token set. Calling with a wrong or reused token "
            "fails. `issue_key` is used only to resolve which Jira connection to call - Jira's "
            "attachment-delete endpoint itself is global and does not take an issue key in its URL."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "issue_key": {"type": "string"},
                "attachment_id": {"type": "string"},
                "confirm_token": {"type": "string"},
            },
            "required": ["issue_key", "attachment_id"],
        },
            annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True},
    ),
    Tool(
        name=_GET_CURRENT_USER_TOOL,
        description=(
            "USE WHEN the human/agent needs to know the caller's own Jira identity "
            "(accountId, displayName) - e.g. as context before deciding whether a "
            "watcher/worklog action targets yourself or a different user. Read-only, "
            "UNGATED, executes immediately. No required arguments. Pass `issue_key` to "
            "resolve the SAME Jira connection a subsequent watcher/worklog call on that "
            "issue would use (matters in multi-connection setups, since accountId is "
            "scoped per Jira site/connection); omit it (optionally passing `domain` "
            "instead) for a standalone 'who am I' lookup against the default/single "
            "connection."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "issue_key": {"type": "string"},
                "domain": {"type": "string"},
            },
            "required": [],
        },
            annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
    ),
    Tool(
        name=_LIST_WATCHERS_TOOL,
        description=(
            "USE WHEN the human wants to see who is watching a Jira issue. Read-only, "
            "UNGATED, executes immediately. Optional limit/offset to page results. Requires a "
            "resolvable Jira connection for the issue's key."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "issue_key": {"type": "string"},
                "limit": {"type": "integer"}, "offset": {"type": "integer"},
            },
            "required": ["issue_key"],
        },
            annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
    ),
    Tool(
        name=_LIST_WORKLOGS_TOOL,
        description=(
            "USE WHEN the human wants to see the work log entries on a Jira issue - "
            "who logged time, how much, and when. Read-only, UNGATED, executes "
            "immediately. Call this before jira_worklog_edit/jira_worklog_delete to "
            "find a worklog_id and see its author. Optional limit/offset to page results. "
            "Requires a resolvable Jira connection for the issue's key."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "issue_key": {"type": "string"},
                "limit": {"type": "integer"}, "offset": {"type": "integer"},
            },
            "required": ["issue_key"],
        },
            annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
    ),
    Tool(
        name=_SET_WATCHER_TOOL,
        description=(
            "USE WHEN the human wants to add or remove a watcher on a Jira issue - one "
            "tool for both directions, controlled by `watching` (true=add, false=remove). "
            "SELF-VS-OTHER GATING (the real point of this tool): this call first looks "
            "up the caller's own accountId via jira_get_current_user. If `account_id` is "
            "omitted, it DEFAULTS to the caller's own identity and this executes "
            "IMMEDIATELY, UNGATED - watching/unwatching an issue yourself is not "
            "destructive. If `account_id` is given and MATCHES the caller's own "
            "identity, this is still treated as self and stays UNGATED. If `account_id` "
            "is given and DIFFERS from the caller's own identity, this becomes "
            "CONFIRMATION-GATED exactly like any other destructive tool in this codebase: "
            "the first call (no confirm_token) returns pending_confirmation plus a "
            "one-time token after showing the human the exact issue_key/account_id/"
            "watching value that will be changed FOR SOMEONE ELSE - you MUST show that to "
            "the human and get an explicit yes before calling again with confirm_token "
            "set. Calling with a wrong or reused token fails. Requires a resolvable Jira "
            "connection for the issue's key."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "issue_key": {"type": "string"},
                "account_id": {"type": "string"},
                "watching": {"type": "boolean"},
                "confirm_token": {"type": "string"},
            },
            "required": ["issue_key", "watching"],
        },
            annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True},
    ),
    Tool(
        name=_WORKLOG_ADD_TOOL,
        description=(
            "USE WHEN the human wants to log time against a Jira issue. UNGATED, "
            "executes immediately - Jira's worklog creation endpoint has NO author-"
            "override field: a new worklog entry is always attributed to the "
            "authenticated caller, so there is no 'log time on someone else's behalf' "
            "case that could ever need gating here. `started` accepts a plain ISO 8601 "
            "string (e.g. '2026-07-28T10:00:00' or with an explicit offset); it is "
            "reformatted to Jira's required wire format automatically. `comment` is "
            "plain text, wrapped into ADF automatically. Requires a resolvable Jira "
            "connection for the issue's key."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "issue_key": {"type": "string"},
                "time_spent_seconds": {"type": "integer"},
                "started": {"type": "string"},
                "comment": {"type": "string"},
            },
            "required": ["issue_key", "time_spent_seconds", "started"],
        },
            annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
    ),
    Tool(
        name=_WORKLOG_EDIT_TOOL,
        description=(
            "USE WHEN the human wants to change an existing worklog entry on a Jira "
            "issue (time spent, start time, and/or comment - only the fields given are "
            "changed). SELF-VS-OTHER GATING (the real point of this tool): this call "
            "first fetches the worklog via jira_list_worklogs to find its "
            "author.accountId, then compares it to the caller's own accountId via "
            "jira_get_current_user. Editing YOUR OWN worklog entry executes IMMEDIATELY, "
            "UNGATED. Editing a DIFFERENT user's worklog entry becomes CONFIRMATION-"
            "GATED exactly like any other destructive tool: the first call (no "
            "confirm_token) returns pending_confirmation plus a one-time token after "
            "showing the human the exact issue_key/worklog_id/changes that will be made "
            "to SOMEONE ELSE'S time entry - you MUST show that to the human and get an "
            "explicit yes before calling again with confirm_token set. Calling with a "
            "wrong or reused token fails. `started`, if given, accepts a plain ISO 8601 "
            "string, reformatted automatically."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "issue_key": {"type": "string"},
                "worklog_id": {"type": "string"},
                "time_spent_seconds": {"type": "integer"},
                "started": {"type": "string"},
                "comment": {"type": "string"},
                "confirm_token": {"type": "string"},
            },
            "required": ["issue_key", "worklog_id"],
        },
            annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True},
    ),
    Tool(
        name=_WORKLOG_DELETE_TOOL,
        description=(
            "USE WHEN the human wants to delete a worklog entry from a Jira issue. "
            "SELF-VS-OTHER GATING (the real point of this tool): this call first fetches "
            "the worklog via jira_list_worklogs to find its author.accountId, then "
            "compares it to the caller's own accountId via jira_get_current_user. "
            "Deleting YOUR OWN worklog entry executes IMMEDIATELY, UNGATED. Deleting a "
            "DIFFERENT user's worklog entry becomes CONFIRMATION-GATED exactly like any "
            "other destructive tool: the first call (no confirm_token) returns "
            "pending_confirmation plus a one-time token after showing the human the exact "
            "issue_key/worklog_id that will be removed from SOMEONE ELSE'S time log - you "
            "MUST show that to the human and get an explicit yes before calling again "
            "with confirm_token set. Calling with a wrong or reused token fails."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "issue_key": {"type": "string"},
                "worklog_id": {"type": "string"},
                "confirm_token": {"type": "string"},
            },
            "required": ["issue_key", "worklog_id"],
        },
            annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True},
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


async def _resolve_worklog_author(issue_key: str, worklog_id: str) -> str | None:
    """Look up a worklog entry's author.accountId via list_worklogs - the
    lookup half of jira_worklog_edit/jira_worklog_delete's self-vs-other
    gating decision. Returns None if the worklog is not found (an unknown
    author is treated as OTHER, not self - fail-safe toward gating)."""
    worklogs_result = await service.list_worklogs(issue_key)
    for wl in worklogs_result.get("worklogs", []):
        if str(wl.get("id")) == str(worklog_id):
            return (wl.get("author") or {}).get("accountId")
    return None


async def dispatch_jira_tool(name: str, arguments: dict) -> list[TextContent] | None:
    """Returns None when `name` is not a jira tool, so mcp_server.py's existing
    dispatcher can fall through to its own chain unmodified."""
    if name == _GET_CLOSE_REQUIREMENTS_TOOL:
        issue_key = arguments.get("issue_key")
        if not issue_key or not isinstance(issue_key, str):
            return _err("issue_key is required and must be a non-empty string.")
        try:
            include_allowed_values = bool(arguments.get("include_allowed_values", True))
            result = await service.get_close_requirements(
                issue_key, include_allowed_values=include_allowed_values,
                since_status=arguments.get("since_status"),
            )
            return _ok(result)
        except Exception as exc:
            return _err(str(exc))

    if name == _APPLY_UPDATE_TOOL:
        try:
            confirm_token = arguments.get("confirm_token")
            if not confirm_token:
                issue_key = arguments.get("issue_key")
                if not issue_key or not isinstance(issue_key, str):
                    return _err("issue_key is required and must be a non-empty string.")
                transition_id = arguments.get("transition_id")
                fields = arguments.get("fields")
                comment = arguments.get("comment")
                token = issue_token(_APPLY_UPDATE_TOOL, {
                    "issue_key": issue_key, "transition_id": transition_id,
                    "fields": fields, "comment": comment,
                })
                return [TextContent(type="text", text=json.dumps({
                    "status": "pending_confirmation",
                    "token": token,
                    "issue_key": issue_key,
                    "transition_id": transition_id,
                    "fields": fields,
                    "comment": comment,
                    "instruction": "Show this exact issue_key, transition_id, fields, and comment "
                                   "to the human. Only call this tool again with confirm_token set "
                                   "once they explicitly agree.",
                }))]
            payload = verify_token(confirm_token, _APPLY_UPDATE_TOOL)
            if payload is None:
                return _err("Invalid or already-used confirm_token. Call again without a token to get a fresh one.")
            result = await service.apply_update(
                payload["issue_key"],
                transition_id=payload.get("transition_id"),
                fields=payload.get("fields"),
                comment=payload.get("comment"),
            )
            return [TextContent(type="text", text=json.dumps(result))]
        except Exception as exc:
            return _err(str(exc))

    if name == _LIST_ISSUE_TYPES_TOOL:
        project = arguments.get("project")
        if not project or not isinstance(project, str):
            return _err("project is required and must be a non-empty string.")
        try:
            result = await service.list_issue_types(project, domain=arguments.get("domain"))
            result, extra = _paginate(result, arguments.get("limit"), arguments.get("offset"))
            return _ok({"issue_types": result, **extra})
        except Exception as exc:
            return _err(str(exc))

    if name == _GET_CREATEMETA_FIELDS_TOOL:
        project = arguments.get("project")
        if not project or not isinstance(project, str):
            return _err("project is required and must be a non-empty string.")
        issuetype_id = arguments.get("issuetype_id")
        if not issuetype_id or not isinstance(issuetype_id, str):
            return _err("issuetype_id is required and must be a non-empty string.")
        try:
            result = await service.get_createmeta_fields(project, issuetype_id, domain=arguments.get("domain"))
            return _ok({"fields": result})
        except Exception as exc:
            return _err(str(exc))

    if name == _CREATE_ISSUE_TOOL:
        try:
            confirm_token = arguments.get("confirm_token")
            if not confirm_token:
                project = arguments.get("project")
                if not project or not isinstance(project, str):
                    return _err("project is required and must be a non-empty string.")
                issuetype = arguments.get("issuetype")
                if not issuetype or not isinstance(issuetype, str):
                    return _err("issuetype is required and must be a non-empty string.")
                summary = arguments.get("summary")
                if not summary or not isinstance(summary, str):
                    return _err("summary is required and must be a non-empty string.")
                fields = arguments.get("fields")
                domain = arguments.get("domain")
                token = issue_token(_CREATE_ISSUE_TOOL, {
                    "project": project, "issuetype": issuetype, "summary": summary,
                    "fields": fields, "domain": domain,
                })
                return [TextContent(type="text", text=json.dumps({
                    "status": "pending_confirmation",
                    "token": token,
                    "project": project,
                    "issuetype": issuetype,
                    "summary": summary,
                    "fields": fields,
                    "domain": domain,
                    "instruction": "Show this exact project, issuetype, summary, and fields to "
                                   "the human. Only call this tool again with confirm_token set "
                                   "once they explicitly agree.",
                }))]
            payload = verify_token(confirm_token, _CREATE_ISSUE_TOOL)
            if payload is None:
                return _err("Invalid or already-used confirm_token. Call again without a token to get a fresh one.")
            result = await service.create_issue(
                payload["project"], payload["issuetype"], payload["summary"],
                fields=payload.get("fields"), domain=payload.get("domain"),
            )
            return [TextContent(type="text", text=json.dumps(result))]
        except Exception as exc:
            return _err(str(exc))

    if name == _DELETE_ISSUE_TOOL:
        try:
            confirm_token = arguments.get("confirm_token")
            if not confirm_token:
                issue_key = arguments.get("issue_key")
                if not issue_key or not isinstance(issue_key, str):
                    return _err("issue_key is required and must be a non-empty string.")
                delete_subtasks = bool(arguments.get("delete_subtasks", False))
                token = issue_token(_DELETE_ISSUE_TOOL, {
                    "issue_key": issue_key, "delete_subtasks": delete_subtasks,
                })
                return [TextContent(type="text", text=json.dumps({
                    "status": "pending_confirmation",
                    "token": token,
                    "issue_key": issue_key,
                    "delete_subtasks": delete_subtasks,
                    "instruction": "PERMANENT: no undo, no trash - Jira Cloud has no recycle bin "
                                   "for issues. Show this exact issue_key (and whether subtasks "
                                   "are also being deleted) to the human and get an explicit yes "
                                   "before calling again with confirm_token set.",
                }))]
            payload = verify_token(confirm_token, _DELETE_ISSUE_TOOL)
            if payload is None:
                return _err("Invalid or already-used confirm_token. Call again without a token to get a fresh one.")
            result = await service.delete_issue(
                payload["issue_key"], delete_subtasks=payload.get("delete_subtasks", False),
            )
            return [TextContent(type="text", text=json.dumps(result))]
        except Exception as exc:
            return _err(str(exc))

    if name == _COMMENT_LIST_TOOL:
        issue_key = arguments.get("issue_key")
        if not issue_key or not isinstance(issue_key, str):
            return _err("issue_key is required and must be a non-empty string.")
        try:
            result = await service.list_comments(issue_key)
            comments, extra = _paginate(result["comments"], arguments.get("limit"), arguments.get("offset"))
            return _ok({**result, "comments": comments, **extra})
        except Exception as exc:
            return _err(str(exc))

    if name == _COMMENT_ADD_TOOL:
        issue_key = arguments.get("issue_key")
        if not issue_key or not isinstance(issue_key, str):
            return _err("issue_key is required and must be a non-empty string.")
        comment = arguments.get("comment")
        if not comment or not isinstance(comment, str):
            return _err("comment is required and must be a non-empty string.")
        try:
            result = await service.add_comment(issue_key, comment)
            return [TextContent(type="text", text=json.dumps(result))]
        except Exception as exc:
            return _err(str(exc))

    if name == _COMMENT_EDIT_TOOL:
        issue_key = arguments.get("issue_key")
        if not issue_key or not isinstance(issue_key, str):
            return _err("issue_key is required and must be a non-empty string.")
        comment_id = arguments.get("comment_id")
        if not comment_id or not isinstance(comment_id, str):
            return _err("comment_id is required and must be a non-empty string.")
        comment = arguments.get("comment")
        if not comment or not isinstance(comment, str):
            return _err("comment is required and must be a non-empty string.")
        try:
            result = await service.edit_comment(issue_key, comment_id, comment)
            return [TextContent(type="text", text=json.dumps(result))]
        except Exception as exc:
            return _err(str(exc))

    if name == _COMMENT_DELETE_TOOL:
        try:
            confirm_token = arguments.get("confirm_token")
            if not confirm_token:
                issue_key = arguments.get("issue_key")
                if not issue_key or not isinstance(issue_key, str):
                    return _err("issue_key is required and must be a non-empty string.")
                comment_id = arguments.get("comment_id")
                if not comment_id or not isinstance(comment_id, str):
                    return _err("comment_id is required and must be a non-empty string.")
                token = issue_token(_COMMENT_DELETE_TOOL, {
                    "issue_key": issue_key, "comment_id": comment_id,
                })
                return [TextContent(type="text", text=json.dumps({
                    "status": "pending_confirmation",
                    "token": token,
                    "issue_key": issue_key,
                    "comment_id": comment_id,
                    "instruction": "PERMANENT: deleted comments cannot be recovered - no undo. "
                                   "Show this exact issue_key and comment_id to the human and "
                                   "get an explicit yes before calling again with confirm_token set.",
                }))]
            payload = verify_token(confirm_token, _COMMENT_DELETE_TOOL)
            if payload is None:
                return _err("Invalid or already-used confirm_token. Call again without a token to get a fresh one.")
            result = await service.delete_comment(payload["issue_key"], payload["comment_id"])
            return [TextContent(type="text", text=json.dumps(result))]
        except Exception as exc:
            return _err(str(exc))

    if name == _SEARCH_TOOL:
        jql = arguments.get("jql")
        if not jql or not isinstance(jql, str):
            return _err("jql is required and must be a non-empty string.")
        try:
            result = await service.search(
                jql,
                fields=arguments.get("fields"),
                max_results=arguments.get("max_results", 50),
                page_token=arguments.get("page_token"),
                domain=arguments.get("domain"),
            )
            return _ok(result)
        except Exception as exc:
            return _err(str(exc))

    if name == _GET_ISSUE_TOOL:
        issue_key = arguments.get("issue_key")
        if not issue_key or not isinstance(issue_key, str):
            return _err("issue_key is required and must be a non-empty string.")
        try:
            result = await service.get_issue(issue_key, fields=arguments.get("fields"))
            return _ok(result)
        except Exception as exc:
            return _err(str(exc))

    if name == _LINK_TYPES_TOOL:
        try:
            result = await service.link_types(domain=arguments.get("domain"))
            return _ok(result)
        except Exception as exc:
            return _err(str(exc))

    if name == _LINK_CREATE_TOOL:
        link_type_name = arguments.get("link_type_name")
        if not link_type_name or not isinstance(link_type_name, str):
            return _err("link_type_name is required and must be a non-empty string.")
        inward_key = arguments.get("inward_key")
        if not inward_key or not isinstance(inward_key, str):
            return _err("inward_key is required and must be a non-empty string.")
        outward_key = arguments.get("outward_key")
        if not outward_key or not isinstance(outward_key, str):
            return _err("outward_key is required and must be a non-empty string.")
        try:
            result = await service.create_link(link_type_name, inward_key, outward_key)
            return [TextContent(type="text", text=json.dumps(result))]
        except Exception as exc:
            return _err(str(exc))

    if name == _LINK_DELETE_TOOL:
        try:
            confirm_token = arguments.get("confirm_token")
            if not confirm_token:
                issue_key = arguments.get("issue_key")
                if not issue_key or not isinstance(issue_key, str):
                    return _err("issue_key is required and must be a non-empty string.")
                link_id = arguments.get("link_id")
                if not link_id or not isinstance(link_id, str):
                    return _err("link_id is required and must be a non-empty string.")
                token = issue_token(_LINK_DELETE_TOOL, {"issue_key": issue_key, "link_id": link_id})
                return [TextContent(type="text", text=json.dumps({
                    "status": "pending_confirmation",
                    "token": token,
                    "issue_key": issue_key,
                    "link_id": link_id,
                    "instruction": "Removing a link can hide real dependency information "
                                   "between issues - it can be recreated afterward if the "
                                   "relationship still applies, but anyone relying on it in the "
                                   "meantime sees an incomplete picture. Show this exact "
                                   "issue_key and link_id to the human and get an explicit yes "
                                   "before calling again with confirm_token set.",
                }))]
            payload = verify_token(confirm_token, _LINK_DELETE_TOOL)
            if payload is None:
                return _err("Invalid or already-used confirm_token. Call again without a token to get a fresh one.")
            result = await service.delete_link(payload["issue_key"], payload["link_id"])
            return [TextContent(type="text", text=json.dumps(result))]
        except Exception as exc:
            return _err(str(exc))

    if name == _SET_ASSIGNEE_TOOL:
        issue_key = arguments.get("issue_key")
        if not issue_key or not isinstance(issue_key, str):
            return _err("issue_key is required and must be a non-empty string.")
        try:
            result = await service.set_assignee(issue_key, account_id=arguments.get("account_id"))
            return _ok(result)
        except Exception as exc:
            return _err(str(exc))

    if name == _SEARCH_ASSIGNABLE_USERS_TOOL:
        issue_key = arguments.get("issue_key")
        if not issue_key or not isinstance(issue_key, str):
            return _err("issue_key is required and must be a non-empty string.")
        try:
            result = await service.search_assignable_users(issue_key, query=arguments.get("query") or "")
            result, extra = _paginate(result, arguments.get("limit"), arguments.get("offset"))
            return _ok({"users": result, **extra})
        except Exception as exc:
            return _err(str(exc))

    if name == _ATTACHMENT_UPLOAD_TOOL:
        issue_key = arguments.get("issue_key")
        if not issue_key or not isinstance(issue_key, str):
            return _err("issue_key is required and must be a non-empty string.")
        file_path = arguments.get("file_path")
        content_base64 = arguments.get("content_base64")
        if file_path and content_base64:
            return _err("Pass exactly one of file_path or content_base64, not both.")
        if not file_path and not content_base64:
            return _err("One of file_path or content_base64 is required.")

        if file_path:
            if not isinstance(file_path, str):
                return _err("file_path must be a string.")
            path = Path(file_path)
            if not path.is_file():
                return _err(f"file_path does not exist or is not a file: {file_path}")
            try:
                content_bytes = path.read_bytes()
            except OSError as exc:
                return _err(f"Could not read file_path: {exc}")
            filename = arguments.get("filename")
            if not filename or not isinstance(filename, str):
                filename = path.name
        else:
            filename = arguments.get("filename")
            if not filename or not isinstance(filename, str):
                return _err("filename is required and must be a non-empty string when content_base64 is used.")
            if not isinstance(content_base64, str):
                return _err("content_base64 must be a non-empty string.")
            try:
                content_bytes = base64.b64decode(content_base64, validate=True)
            except (binascii.Error, ValueError):
                return _err("content_base64 is not valid base64.")

        try:
            result = await service.upload_attachment(
                issue_key, filename, content_bytes, content_type=arguments.get("content_type"),
            )
            return [TextContent(type="text", text=json.dumps(result))]
        except Exception as exc:
            return _err(str(exc))

    if name == _ATTACHMENT_DELETE_TOOL:
        try:
            confirm_token = arguments.get("confirm_token")
            if not confirm_token:
                issue_key = arguments.get("issue_key")
                if not issue_key or not isinstance(issue_key, str):
                    return _err("issue_key is required and must be a non-empty string.")
                attachment_id = arguments.get("attachment_id")
                if not attachment_id or not isinstance(attachment_id, str):
                    return _err("attachment_id is required and must be a non-empty string.")
                token = issue_token(_ATTACHMENT_DELETE_TOOL, {
                    "issue_key": issue_key, "attachment_id": attachment_id,
                })
                return [TextContent(type="text", text=json.dumps({
                    "status": "pending_confirmation",
                    "token": token,
                    "issue_key": issue_key,
                    "attachment_id": attachment_id,
                    "instruction": "PERMANENT: no undo, no trash - Jira Cloud has no recycle bin "
                                   "for attachments. Show this exact issue_key and attachment_id "
                                   "to the human and get an explicit yes before calling again with "
                                   "confirm_token set.",
                }))]
            payload = verify_token(confirm_token, _ATTACHMENT_DELETE_TOOL)
            if payload is None:
                return _err("Invalid or already-used confirm_token. Call again without a token to get a fresh one.")
            result = await service.delete_attachment(payload["issue_key"], payload["attachment_id"])
            return [TextContent(type="text", text=json.dumps(result))]
        except Exception as exc:
            return _err(str(exc))

    if name == _GET_CURRENT_USER_TOOL:
        try:
            result = await service.get_current_user(
                issue_key=arguments.get("issue_key"), domain=arguments.get("domain"),
            )
            return _ok(result)
        except Exception as exc:
            return _err(str(exc))

    if name == _LIST_WATCHERS_TOOL:
        issue_key = arguments.get("issue_key")
        if not issue_key or not isinstance(issue_key, str):
            return _err("issue_key is required and must be a non-empty string.")
        try:
            result = await service.list_watchers(issue_key)
            watchers, extra = _paginate(result["watchers"], arguments.get("limit"), arguments.get("offset"))
            return _ok({**result, "watchers": watchers, **extra})
        except Exception as exc:
            return _err(str(exc))

    if name == _LIST_WORKLOGS_TOOL:
        issue_key = arguments.get("issue_key")
        if not issue_key or not isinstance(issue_key, str):
            return _err("issue_key is required and must be a non-empty string.")
        try:
            result = await service.list_worklogs(issue_key)
            worklogs, extra = _paginate(result["worklogs"], arguments.get("limit"), arguments.get("offset"))
            return _ok({**result, "worklogs": worklogs, **extra})
        except Exception as exc:
            return _err(str(exc))

    if name == _SET_WATCHER_TOOL:
        try:
            confirm_token = arguments.get("confirm_token")
            if not confirm_token:
                issue_key = arguments.get("issue_key")
                if not issue_key or not isinstance(issue_key, str):
                    return _err("issue_key is required and must be a non-empty string.")
                watching = arguments.get("watching")
                if not isinstance(watching, bool):
                    return _err("watching is required and must be a boolean.")
                account_id = arguments.get("account_id")
                if account_id is not None and not isinstance(account_id, str):
                    return _err("account_id must be a string when given.")

                me = await service.get_current_user(issue_key=issue_key)
                own_account_id = me.get("accountId")
                target_account_id = account_id if account_id is not None else own_account_id
                is_self = target_account_id == own_account_id

                if is_self:
                    result = (
                        await service.add_watcher(issue_key, target_account_id) if watching
                        else await service.remove_watcher(issue_key, target_account_id)
                    )
                    return [TextContent(type="text", text=json.dumps(result))]

                token = issue_token(_SET_WATCHER_TOOL, {
                    "issue_key": issue_key, "account_id": target_account_id, "watching": watching,
                })
                return [TextContent(type="text", text=json.dumps({
                    "status": "pending_confirmation",
                    "token": token,
                    "issue_key": issue_key,
                    "account_id": target_account_id,
                    "watching": watching,
                    "instruction": (
                        "This targets a DIFFERENT Jira user's watcher status, not your "
                        f"own (your accountId is {own_account_id}). Show this exact "
                        "issue_key, account_id, and watching value to the human and get "
                        "an explicit yes before calling again with confirm_token set."
                    ),
                }))]
            payload = verify_token(confirm_token, _SET_WATCHER_TOOL)
            if payload is None:
                return _err("Invalid or already-used confirm_token. Call again without a token to get a fresh one.")
            result = (
                await service.add_watcher(payload["issue_key"], payload["account_id"]) if payload["watching"]
                else await service.remove_watcher(payload["issue_key"], payload["account_id"])
            )
            return [TextContent(type="text", text=json.dumps(result))]
        except Exception as exc:
            return _err(str(exc))

    if name == _WORKLOG_ADD_TOOL:
        issue_key = arguments.get("issue_key")
        if not issue_key or not isinstance(issue_key, str):
            return _err("issue_key is required and must be a non-empty string.")
        time_spent_seconds = arguments.get("time_spent_seconds")
        if not isinstance(time_spent_seconds, int) or isinstance(time_spent_seconds, bool):
            return _err("time_spent_seconds is required and must be an integer.")
        started = arguments.get("started")
        if not started or not isinstance(started, str):
            return _err("started is required and must be a non-empty ISO 8601 string.")
        try:
            result = await service.add_worklog(
                issue_key, time_spent_seconds, started, comment=arguments.get("comment"),
            )
            return [TextContent(type="text", text=json.dumps(result))]
        except Exception as exc:
            return _err(str(exc))

    if name == _WORKLOG_EDIT_TOOL:
        try:
            confirm_token = arguments.get("confirm_token")
            if not confirm_token:
                issue_key = arguments.get("issue_key")
                if not issue_key or not isinstance(issue_key, str):
                    return _err("issue_key is required and must be a non-empty string.")
                worklog_id = arguments.get("worklog_id")
                if not worklog_id or not isinstance(worklog_id, str):
                    return _err("worklog_id is required and must be a non-empty string.")
                time_spent_seconds = arguments.get("time_spent_seconds")
                started = arguments.get("started")
                comment = arguments.get("comment")
                if time_spent_seconds is None and started is None and comment is None:
                    return _err("At least one of time_spent_seconds, started, or comment is required.")

                author_account_id = await _resolve_worklog_author(issue_key, worklog_id)
                me = await service.get_current_user(issue_key=issue_key)
                own_account_id = me.get("accountId")
                is_self = author_account_id is not None and author_account_id == own_account_id

                edit_payload = {
                    "issue_key": issue_key, "worklog_id": worklog_id,
                    "time_spent_seconds": time_spent_seconds, "started": started, "comment": comment,
                }
                if is_self:
                    result = await service.edit_worklog(
                        issue_key, worklog_id, time_spent_seconds=time_spent_seconds,
                        started=started, comment=comment,
                    )
                    return [TextContent(type="text", text=json.dumps(result))]

                token = issue_token(_WORKLOG_EDIT_TOOL, edit_payload)
                return [TextContent(type="text", text=json.dumps({
                    "status": "pending_confirmation",
                    "token": token,
                    **edit_payload,
                    "instruction": (
                        "This edits a worklog entry belonging to a DIFFERENT Jira user, "
                        f"not your own (your accountId is {own_account_id}). Show this "
                        "exact issue_key, worklog_id, and the changes being made to the "
                        "human and get an explicit yes before calling again with "
                        "confirm_token set."
                    ),
                }))]
            payload = verify_token(confirm_token, _WORKLOG_EDIT_TOOL)
            if payload is None:
                return _err("Invalid or already-used confirm_token. Call again without a token to get a fresh one.")
            result = await service.edit_worklog(
                payload["issue_key"], payload["worklog_id"],
                time_spent_seconds=payload.get("time_spent_seconds"),
                started=payload.get("started"), comment=payload.get("comment"),
            )
            return [TextContent(type="text", text=json.dumps(result))]
        except Exception as exc:
            return _err(str(exc))

    if name == _WORKLOG_DELETE_TOOL:
        try:
            confirm_token = arguments.get("confirm_token")
            if not confirm_token:
                issue_key = arguments.get("issue_key")
                if not issue_key or not isinstance(issue_key, str):
                    return _err("issue_key is required and must be a non-empty string.")
                worklog_id = arguments.get("worklog_id")
                if not worklog_id or not isinstance(worklog_id, str):
                    return _err("worklog_id is required and must be a non-empty string.")

                author_account_id = await _resolve_worklog_author(issue_key, worklog_id)
                me = await service.get_current_user(issue_key=issue_key)
                own_account_id = me.get("accountId")
                is_self = author_account_id is not None and author_account_id == own_account_id

                if is_self:
                    result = await service.delete_worklog(issue_key, worklog_id)
                    return [TextContent(type="text", text=json.dumps(result))]

                token = issue_token(_WORKLOG_DELETE_TOOL, {"issue_key": issue_key, "worklog_id": worklog_id})
                return [TextContent(type="text", text=json.dumps({
                    "status": "pending_confirmation",
                    "token": token,
                    "issue_key": issue_key,
                    "worklog_id": worklog_id,
                    "instruction": (
                        "This deletes a worklog entry belonging to a DIFFERENT Jira "
                        f"user, not your own (your accountId is {own_account_id}). Show "
                        "this exact issue_key and worklog_id to the human and get an "
                        "explicit yes before calling again with confirm_token set."
                    ),
                }))]
            payload = verify_token(confirm_token, _WORKLOG_DELETE_TOOL)
            if payload is None:
                return _err("Invalid or already-used confirm_token. Call again without a token to get a fresh one.")
            result = await service.delete_worklog(payload["issue_key"], payload["worklog_id"])
            return [TextContent(type="text", text=json.dumps(result))]
        except Exception as exc:
            return _err(str(exc))

    return None
