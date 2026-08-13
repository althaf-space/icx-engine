"""MCP tool surface for Workstatus (time tracking/attendance). Owns its own
Tool() definitions and dispatch function - mcp_server.py's _list_tools()/
_call_tool() get a few additive lines only, no restructuring.

Every tool here maps 1:1 to a client.py method backed by a live-captured
request+response shape (see developer.md's Workstatus section for the
evidence trail). Endpoints whose request body was never captured are not
exposed here - see developer.md for that remaining backlog."""
from __future__ import annotations

import json

from mcp.types import TextContent, Tool

from icx_engine.config_manager import ConfigManager
from icx_engine.mcp_server import _ICX_FALLBACK
from icx_engine.workstatus import service
from icx_engine.workstatus.service import WorkstatusNotConfigured

_UNREAD_COUNT_TOOL = "workstatus_unread_notifications"
_MY_PROFILE_TOOL = "workstatus_my_profile"
_ADD_TIMESHEET_TOOL = "workstatus_add_timesheet"
_LIST_PROJECTS_TOOL = "workstatus_list_projects"
_GET_PROJECT_TOOL = "workstatus_get_project"
_PROJECT_BUDGET_TOOL = "workstatus_project_budget_analytics"
_LIST_TASKS_TOOL = "workstatus_list_tasks"
_LIST_TASK_STATUSES_TOOL = "workstatus_list_task_statuses"
_LIST_MILESTONES_TOOL = "workstatus_list_milestones"
_LIST_TASK_CHECKLIST_TOOL = "workstatus_list_task_checklist"
_LIST_MEMBERS_TOOL = "workstatus_list_members"
_LIST_TEAMS_TOOL = "workstatus_list_teams"
_ATTENDANCE_LIST_TOOL = "workstatus_attendance_list"
_ATTENDANCE_STATS_TOOL = "workstatus_attendance_stats"
_LIST_TIMESHEETS_TOOL = "workstatus_list_timesheets"
_LIST_TIMESHEET_CLIENTS_TOOL = "workstatus_list_timesheet_clients"
_WEEKLY_REPORT_TOOL = "workstatus_weekly_report"
_TIMESHEET_SUBMISSION_KPIS_TOOL = "workstatus_timesheet_submission_kpis"
_TIMESHEET_SUBMISSION_TABLE_TOOL = "workstatus_timesheet_submission_table"
_LIST_EXPENSES_TOOL = "workstatus_list_expenses"
_LIST_INVOICES_TOOL = "workstatus_list_invoices"
_PAYROLL_REPORT_TOOL = "workstatus_payroll_report"
_GET_TIMESHEET_TOOL = "workstatus_get_timesheet"
_EDIT_TIMESHEET_TOOL = "workstatus_edit_timesheet"
_RECENT_PROJECT_TASKS_TOOL = "workstatus_recent_project_tasks"

_NO_ARGS_SCHEMA = {"type": "object", "properties": {}, "required": []}


def _date_range_schema(*extra_required: str, **extra_props) -> dict:
    props = {"start_date": {"type": "string"}, "end_date": {"type": "string"}, **extra_props}
    return {"type": "object", "properties": props, "required": ["start_date", "end_date", *extra_required]}


WORKSTATUS_TOOLS: list[Tool] = [
    Tool(
        name=_UNREAD_COUNT_TOOL,
        description=(
            "USE WHEN the user asks how many unread Workstatus notifications they have: MUST "
            "call workstatus_unread_notifications with no arguments. Requires an active "
            "Workstatus connection."
        ),
        inputSchema=_NO_ARGS_SCHEMA,
    ),
    Tool(
        name=_MY_PROFILE_TOOL,
        description=(
            "USE WHEN the user asks about their own Workstatus profile (job details, contact "
            "info, etc): MUST call workstatus_my_profile with no arguments. Requires an active "
            "Workstatus connection."
        ),
        inputSchema=_NO_ARGS_SCHEMA,
    ),
    Tool(
        name=_ADD_TIMESHEET_TOOL,
        description=(
            "USE WHEN the user wants to log time against a Workstatus project/task: BEFORE "
            "browsing projects/tasks, call workstatus_recent_project_tasks first (one cheap call) "
            "and ask the user whether they mean one of their recently-logged project/task pairs "
            "or want to browse the full list - only fall back to workstatus_list_projects / "
            "workstatus_list_tasks (which can require paging through hundreds of tasks - see that "
            "tool's own description) once the human says they want the full list or the recent "
            "list doesn't contain what they mean. Once project_id/todo_id are known, MUST call "
            "workstatus_add_timesheet with project_id, todo_id (the task id), date (DD-MM-YYYY), "
            "reason, and from_time/to_time as a FULL datetime string 'YYYY-MM-DD HH:MM:SS' "
            "(NOT the 12-hour '10:00 am' display format - that was tried repeatedly against a real "
            "assigned task and consistently produced an HTTP-200-with-empty-body silent failure; "
            "the full-datetime form matches the read-side top-level from_time/to_time fields and "
            "is what a second, independently-supplied real submission example used). duration is "
            "optional - leave it empty unless the human gives one; Workstatus appears to compute it "
            "from from_time/to_time itself. note and billable are optional - billable is NOT "
            "mandatory: if omitted, an empty value is sent rather than forcing a False default. "
            "Internally this fills source_type/time_type/time_mode/activity with defaults matching "
            "the human's own confirmed-working historical entries - Workstatus has no discoverable "
            "enum-list endpoint for these, unlike Jira's createmeta, so a rejection tied to one of "
            "them cannot be resolved by a lookup call and should be surfaced to the human as-is. "
            "This creates a REAL timesheet entry - confirm the details with the user before calling. "
            "Requires an active Workstatus connection."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project_id": {"type": "integer"},
                "todo_id": {"type": "integer"},
                "date": {"type": "string"},
                "from_time": {"type": "string"},
                "to_time": {"type": "string"},
                "duration": {"type": "string"},
                "reason": {"type": "string"},
                "note": {"type": "string"},
                "billable": {"type": "boolean"},
            },
            "required": ["project_id", "todo_id", "date", "from_time", "to_time", "reason"],
        },
    ),
    Tool(
        name=_LIST_PROJECTS_TOOL,
        description=(
            "USE WHEN the user wants to see Workstatus projects: MUST call workstatus_list_projects "
            "with an optional keyword to filter by name. Returns a paginated project list - pass "
            "`page` (1-based) to reach entries past the first `data_count` rows, and raise "
            "`data_count` (default 15) to fetch more per page. SET `lean=true` UNLESS the caller "
            "specifically needs a project's nested detail (e.g. its member roster) - each raw row "
            "can be very large (~50KB+) with lean=false; lean=true keeps only scalar fields "
            "(id/name/status/etc.) and strips nested list/dict fields. Requires an active "
            "Workstatus connection."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "keyword": {"type": "string"},
                "data_count": {"type": "integer"},
                "page": {"type": "integer"},
                "lean": {"type": "boolean"},
            },
            "required": [],
        },
    ),
    Tool(
        name=_GET_PROJECT_TOOL,
        description=(
            "USE WHEN the user wants details for one specific Workstatus project by id: MUST call "
            "workstatus_get_project with project_id. Requires an active Workstatus connection."
        ),
        inputSchema={"type": "object", "properties": {"project_id": {"type": "integer"}}, "required": ["project_id"]},
    ),
    Tool(
        name=_PROJECT_BUDGET_TOOL,
        description=(
            "USE WHEN the user asks about a Workstatus project's budget/margin/profit-loss: MUST "
            "call workstatus_project_budget_analytics with project_id, optional quarter. Requires "
            "an active Workstatus connection."
        ),
        inputSchema={
            "type": "object",
            "properties": {"project_id": {"type": "integer"}, "quarter": {"type": "string"}},
            "required": ["project_id"],
        },
    ),
    Tool(
        name=_LIST_TASKS_TOOL,
        description=(
            "USE WHEN the user wants tasks for a Workstatus project: MUST call workstatus_list_tasks "
            "with project_id, optional search keyword, optional page (1-based) to page through a "
            "large project's task list - a project can have hundreds of tasks and only one page "
            "is returned per call, so DO NOT assume the first page is the complete list. CAVEAT: "
            "`search` is UNVERIFIED to actually filter server-side (the exact trigger parameter "
            "Workstatus needs was never confirmed live) - if the returned count looks like the "
            "full unfiltered list rather than a filtered one, treat search as not applied and use "
            "page to browse instead of trusting the filter. A project can have hundreds of tasks "
            "spread across dozens of pages, so paging through all of them to find one task by name "
            "is expensive - try workstatus_recent_project_tasks first if the goal is finding a "
            "task the human has logged time against before. Requires an active Workstatus connection."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project_id": {"type": "integer"},
                "search": {"type": "string"},
                "page": {"type": "integer"},
            },
            "required": ["project_id"],
        },
    ),
    Tool(
        name=_LIST_TASK_STATUSES_TOOL,
        description=(
            "USE WHEN the user asks what task statuses exist for a Workstatus project: MUST call "
            "workstatus_list_task_statuses with project_id. Requires an active Workstatus connection."
        ),
        inputSchema={"type": "object", "properties": {"project_id": {"type": "integer"}}, "required": ["project_id"]},
    ),
    Tool(
        name=_LIST_MILESTONES_TOOL,
        description=(
            "USE WHEN the user wants milestones for a Workstatus project: MUST call "
            "workstatus_list_milestones with project_id. Requires an active Workstatus connection."
        ),
        inputSchema={"type": "object", "properties": {"project_id": {"type": "integer"}}, "required": ["project_id"]},
    ),
    Tool(
        name=_LIST_TASK_CHECKLIST_TOOL,
        description=(
            "USE WHEN the user wants the checklist items for one Workstatus task: MUST call "
            "workstatus_list_task_checklist with task_id. Requires an active Workstatus connection."
        ),
        inputSchema={"type": "object", "properties": {"task_id": {"type": "integer"}}, "required": ["task_id"]},
    ),
    Tool(
        name=_LIST_MEMBERS_TOOL,
        description=(
            "USE WHEN the user wants the Workstatus member/employee list: MUST call "
            "workstatus_list_members with an optional search_key. Requires an active Workstatus "
            "connection."
        ),
        inputSchema={"type": "object", "properties": {"search_key": {"type": "string"}}, "required": []},
    ),
    Tool(
        name=_LIST_TEAMS_TOOL,
        description=(
            "USE WHEN the user wants the Workstatus team list: MUST call workstatus_list_teams with "
            "no arguments. Requires an active Workstatus connection."
        ),
        inputSchema=_NO_ARGS_SCHEMA,
    ),
    Tool(
        name=_ATTENDANCE_LIST_TOOL,
        description=(
            "USE WHEN the user asks for their own day-by-day Workstatus attendance/check-in-out "
            "history: MUST call workstatus_attendance_list with start_date and end_date "
            "(YYYY-MM-DD). Requires an active Workstatus connection."
        ),
        inputSchema=_date_range_schema(),
    ),
    Tool(
        name=_ATTENDANCE_STATS_TOOL,
        description=(
            "USE WHEN the user wants summary attendance stats (days present/absent, avg hours) "
            "rather than the day-by-day list: MUST call workstatus_attendance_stats with "
            "start_date and end_date (YYYY-MM-DD). Requires an active Workstatus connection."
        ),
        inputSchema=_date_range_schema(),
    ),
    Tool(
        name=_LIST_TIMESHEETS_TOOL,
        description=(
            "USE WHEN the user wants their logged timesheet entries for a date range: MUST call "
            "workstatus_list_timesheets with start_date and end_date (YYYY-MM-DD). Requires an "
            "active Workstatus connection."
        ),
        inputSchema=_date_range_schema(),
    ),
    Tool(
        name=_LIST_TIMESHEET_CLIENTS_TOOL,
        description=(
            "USE WHEN the user wants the list of clients billable via Workstatus timesheets: MUST "
            "call workstatus_list_timesheet_clients with no arguments. Requires an active "
            "Workstatus connection."
        ),
        inputSchema=_NO_ARGS_SCHEMA,
    ),
    Tool(
        name=_WEEKLY_REPORT_TOOL,
        description=(
            "USE WHEN the user wants a weekly hours/activity/earnings report: MUST call "
            "workstatus_weekly_report with start_date and end_date (YYYY-MM-DD). Requires an "
            "active Workstatus connection."
        ),
        inputSchema=_date_range_schema(),
    ),
    Tool(
        name=_TIMESHEET_SUBMISSION_KPIS_TOOL,
        description=(
            "USE WHEN the user wants summary KPIs on timesheet submission/approval status (missing, "
            "pending, approved counts): MUST call workstatus_timesheet_submission_kpis with "
            "start_date and end_date (YYYY-MM-DD). Requires an active Workstatus connection."
        ),
        inputSchema=_date_range_schema(),
    ),
    Tool(
        name=_TIMESHEET_SUBMISSION_TABLE_TOOL,
        description=(
            "USE WHEN the user wants the per-member timesheet submission/approval table (not just "
            "the KPI summary): MUST call workstatus_timesheet_submission_table with start_date and "
            "end_date (YYYY-MM-DD); page/per_page optional for pagination. Requires an active "
            "Workstatus connection."
        ),
        inputSchema=_date_range_schema(page={"type": "integer"}, per_page={"type": "integer"}),
    ),
    Tool(
        name=_LIST_EXPENSES_TOOL,
        description=(
            "USE WHEN the user wants recorded Workstatus expenses for a date range: MUST call "
            "workstatus_list_expenses with start_date and end_date (YYYY-MM-DD). Requires an "
            "active Workstatus connection."
        ),
        inputSchema=_date_range_schema(),
    ),
    Tool(
        name=_LIST_INVOICES_TOOL,
        description=(
            "USE WHEN the user wants Workstatus invoices: MUST call workstatus_list_invoices with "
            "an optional search keyword. Requires an active Workstatus connection."
        ),
        inputSchema={"type": "object", "properties": {"search": {"type": "string"}}, "required": []},
    ),
    Tool(
        name=_PAYROLL_REPORT_TOOL,
        description=(
            "USE WHEN the user wants a Workstatus payroll report: MUST call workstatus_payroll_report "
            "with start_date and end_date (YYYY-MM-DD). Requires an active Workstatus connection."
        ),
        inputSchema=_date_range_schema(),
    ),
    Tool(
        name=_GET_TIMESHEET_TOOL,
        description=(
            "USE WHEN the user wants full detail on one specific timesheet entry (e.g. before "
            "editing it): MUST call workstatus_get_timesheet with timesheet_id. Requires an active "
            "Workstatus connection."
        ),
        inputSchema={"type": "object", "properties": {"timesheet_id": {"type": "integer"}}, "required": ["timesheet_id"]},
    ),
    Tool(
        name=_EDIT_TIMESHEET_TOOL,
        description=(
            "USE WHEN the user wants to correct/change an EXISTING timesheet entry (not create a "
            "new one - use workstatus_add_timesheet for that): MUST first call "
            "workstatus_get_timesheet to see the current values, THEN call workstatus_edit_timesheet "
            "with timesheet_id, project_id, todo_id, date, from_time, to_time, duration, reason (all "
            "required - re-send the unchanged ones as-is), plus updated_fields: a list of "
            "{field_name, previous_value, new_value} describing exactly what changed (this is the "
            "audit trail the server expects, matching the Manual Time Edit report). Like "
            "workstatus_add_timesheet, source_type/time_type/time_mode/activity default to "
            "unverified values captured from one live submission - no lookup endpoint exists for "
            "them. billable is NOT mandatory - omitted sends an empty value, never forced False. "
            "This mutates a "
            "REAL entry - confirm the exact change with the user before calling. Requires an active "
            "Workstatus connection."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "timesheet_id": {"type": "integer"},
                "project_id": {"type": "integer"},
                "todo_id": {"type": "integer"},
                "date": {"type": "string"},
                "from_time": {"type": "string"},
                "to_time": {"type": "string"},
                "duration": {"type": "string"},
                "reason": {"type": "string"},
                "updated_fields": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "field_name": {"type": "string"},
                            "previous_value": {"type": "string"},
                            "new_value": {"type": "string"},
                        },
                        "required": ["field_name", "previous_value", "new_value"],
                    },
                },
                "note": {"type": "string"},
                "billable": {"type": "boolean"},
            },
            "required": [
                "timesheet_id", "project_id", "todo_id", "date", "from_time", "to_time",
                "duration", "reason", "updated_fields",
            ],
        },
    ),
    Tool(
        name=_RECENT_PROJECT_TASKS_TOOL,
        description=(
            "USE WHEN the user wants to log time and you need to identify a project/task: MUST "
            "call workstatus_recent_project_tasks FIRST, before workstatus_list_projects or "
            "workstatus_list_tasks. One cheap call (derived from recent timesheet history, default "
            "90-day lookback) returns the user's distinct project/task pairs actually logged "
            "against recently, most-recent-first - present these to the user as a quick-pick before "
            "falling back to a full project/task browse, which can require paging through hundreds "
            "of tasks. Optional lookback_days overrides the 90-day window. Requires an active "
            "Workstatus connection."
        ),
        inputSchema={"type": "object", "properties": {"lookback_days": {"type": "integer"}}, "required": []},
    ),
]


def _ok(payload: dict) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps({"ok": True, **payload}))]


def _err(message: str) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps({"ok": False, "error": message}))]


def _no_workstatus_connection_err() -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps({
        "ok": False,
        "error": "No Workstatus connection configured. Run `icx workstatus --add`.",
        "fallback": _ICX_FALLBACK("Workstatus", "icx workstatus --add"),
    }))]


async def _guarded(payload_key: str, awaitable) -> list[TextContent]:
    """Shared try/except for every read/write call below - not-configured and
    any other failure both surface as a structured error, never raise."""
    try:
        result = await awaitable
        return _ok({payload_key: result})
    except WorkstatusNotConfigured:
        return _no_workstatus_connection_err()
    except Exception as exc:
        return _err(str(exc))


def _require(arguments: dict, *fields: str) -> str | None:
    """Returns an error message for the first missing required field, or None."""
    for field in fields:
        if not arguments.get(field) and arguments.get(field) != 0:
            return f"{field} is required."
    return None


_TOOL_NAMES = {t.name for t in WORKSTATUS_TOOLS}


async def dispatch_workstatus_tool(name: str, arguments: dict) -> list[TextContent] | None:
    """Returns None when `name` is not a workstatus tool, so mcp_server.py's
    existing dispatcher can fall through to its own chain unmodified. Checked
    BEFORE loading config - this dispatcher runs for every tool call in the
    chain (git/jira/gitlab/...), not just workstatus ones, so ConfigManager.load()
    must not fire on every single call."""
    if name not in _TOOL_NAMES:
        return None
    cfg = ConfigManager.load()

    if name == _UNREAD_COUNT_TOOL:
        return await _guarded("unread_notifications", service.unread_notifications_count(cfg=cfg))

    if name == _MY_PROFILE_TOOL:
        return await _guarded("profile", service.my_profile(cfg=cfg))

    if name == _ADD_TIMESHEET_TOOL:
        err = _require(arguments, "project_id", "todo_id", "date", "from_time", "to_time", "reason")
        if err:
            return _err(err)
        raw_billable = arguments.get("billable")
        return await _guarded("timesheet", service.add_timesheet(
            project_id=int(arguments["project_id"]), todo_id=int(arguments["todo_id"]),
            date=arguments["date"], from_time=arguments["from_time"], to_time=arguments["to_time"],
            duration=arguments.get("duration", ""), reason=arguments["reason"],
            note=arguments.get("note", ""),
            billable=bool(raw_billable) if raw_billable is not None else None, cfg=cfg,
        ))

    if name == _LIST_PROJECTS_TOOL:
        return await _guarded("projects", service.list_projects(
            keyword=arguments.get("keyword", ""),
            data_count=int(arguments.get("data_count", 15)),
            page=int(arguments.get("page", 1)),
            lean=bool(arguments.get("lean", False)),
            cfg=cfg,
        ))

    if name == _GET_PROJECT_TOOL:
        err = _require(arguments, "project_id")
        if err:
            return _err(err)
        return await _guarded("project", service.get_project(int(arguments["project_id"]), cfg=cfg))

    if name == _PROJECT_BUDGET_TOOL:
        err = _require(arguments, "project_id")
        if err:
            return _err(err)
        return await _guarded("budget", service.project_budget_analytics(
            int(arguments["project_id"]), quarter=arguments.get("quarter", ""), cfg=cfg,
        ))

    if name == _LIST_TASKS_TOOL:
        err = _require(arguments, "project_id")
        if err:
            return _err(err)
        return await _guarded("tasks", service.list_tasks(
            int(arguments["project_id"]), search=arguments.get("search", ""),
            page=int(arguments.get("page", 1)), cfg=cfg,
        ))

    if name == _LIST_TASK_STATUSES_TOOL:
        err = _require(arguments, "project_id")
        if err:
            return _err(err)
        return await _guarded("task_statuses", service.list_task_statuses(int(arguments["project_id"]), cfg=cfg))

    if name == _LIST_MILESTONES_TOOL:
        err = _require(arguments, "project_id")
        if err:
            return _err(err)
        return await _guarded("milestones", service.list_milestones(int(arguments["project_id"]), cfg=cfg))

    if name == _LIST_TASK_CHECKLIST_TOOL:
        err = _require(arguments, "task_id")
        if err:
            return _err(err)
        return await _guarded("checklist", service.list_task_checklist(int(arguments["task_id"]), cfg=cfg))

    if name == _LIST_MEMBERS_TOOL:
        return await _guarded("members", service.list_members(search_key=arguments.get("search_key", ""), cfg=cfg))

    if name == _LIST_TEAMS_TOOL:
        return await _guarded("teams", service.list_teams(cfg=cfg))

    if name == _ATTENDANCE_LIST_TOOL:
        err = _require(arguments, "start_date", "end_date")
        if err:
            return _err(err)
        return await _guarded("attendance", service.attendance_list(arguments["start_date"], arguments["end_date"], cfg=cfg))

    if name == _ATTENDANCE_STATS_TOOL:
        err = _require(arguments, "start_date", "end_date")
        if err:
            return _err(err)
        return await _guarded("stats", service.attendance_stats(arguments["start_date"], arguments["end_date"], cfg=cfg))

    if name == _LIST_TIMESHEETS_TOOL:
        err = _require(arguments, "start_date", "end_date")
        if err:
            return _err(err)
        return await _guarded("timesheets", service.list_timesheets(arguments["start_date"], arguments["end_date"], cfg=cfg))

    if name == _LIST_TIMESHEET_CLIENTS_TOOL:
        return await _guarded("clients", service.list_timesheet_clients(cfg=cfg))

    if name == _WEEKLY_REPORT_TOOL:
        err = _require(arguments, "start_date", "end_date")
        if err:
            return _err(err)
        return await _guarded("report", service.weekly_report_all(arguments["start_date"], arguments["end_date"], cfg=cfg))

    if name == _TIMESHEET_SUBMISSION_KPIS_TOOL:
        err = _require(arguments, "start_date", "end_date")
        if err:
            return _err(err)
        return await _guarded("kpis", service.timesheet_submission_kpis(arguments["start_date"], arguments["end_date"], cfg=cfg))

    if name == _TIMESHEET_SUBMISSION_TABLE_TOOL:
        err = _require(arguments, "start_date", "end_date")
        if err:
            return _err(err)
        return await _guarded("table", service.timesheet_submission_table(
            arguments["start_date"], arguments["end_date"],
            page=int(arguments.get("page", 1)), per_page=int(arguments.get("per_page", 15)), cfg=cfg,
        ))

    if name == _LIST_EXPENSES_TOOL:
        err = _require(arguments, "start_date", "end_date")
        if err:
            return _err(err)
        return await _guarded("expenses", service.list_expenses(arguments["start_date"], arguments["end_date"], cfg=cfg))

    if name == _LIST_INVOICES_TOOL:
        return await _guarded("invoices", service.list_invoices(search=arguments.get("search", ""), cfg=cfg))

    if name == _PAYROLL_REPORT_TOOL:
        err = _require(arguments, "start_date", "end_date")
        if err:
            return _err(err)
        return await _guarded("payroll", service.payroll_report(arguments["start_date"], arguments["end_date"], cfg=cfg))

    if name == _GET_TIMESHEET_TOOL:
        err = _require(arguments, "timesheet_id")
        if err:
            return _err(err)
        return await _guarded("timesheet", service.get_timesheet(int(arguments["timesheet_id"]), cfg=cfg))

    if name == _EDIT_TIMESHEET_TOOL:
        err = _require(
            arguments, "timesheet_id", "project_id", "todo_id", "date", "from_time",
            "to_time", "duration", "reason",
        )
        if err:
            return _err(err)
        if not arguments.get("updated_fields"):
            return _err("updated_fields is required.")
        raw_billable = arguments.get("billable")
        return await _guarded("timesheet", service.edit_timesheet(
            timesheet_id=int(arguments["timesheet_id"]), project_id=int(arguments["project_id"]),
            todo_id=int(arguments["todo_id"]), date=arguments["date"], from_time=arguments["from_time"],
            to_time=arguments["to_time"], duration=arguments["duration"], reason=arguments["reason"],
            updated_fields=arguments["updated_fields"], note=arguments.get("note", ""),
            billable=bool(raw_billable) if raw_billable is not None else None, cfg=cfg,
        ))

    if name == _RECENT_PROJECT_TASKS_TOOL:
        return await _guarded("recent_project_tasks", service.recent_project_tasks(
            lookback_days=int(arguments.get("lookback_days", 90)), cfg=cfg,
        ))

    return None
