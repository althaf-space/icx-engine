from __future__ import annotations
import json

import httpx
import pytest
import respx

from icx_engine.models.config import AppConfig, WorkstatusConnection
from icx_engine.workstatus.mcp_tools import (
    WORKSTATUS_TOOLS,
    dispatch_workstatus_tool,
)


def _configured_cfg() -> AppConfig:
    cfg = AppConfig()
    cfg.workstatus_connections["default"] = WorkstatusConnection(
        name="default", user_id="175599", org_id="8570", device_type="web",
        authorization="Bearer x", sd_token="sd-x",
    )
    cfg.active_workstatus = "default"
    return cfg


def _load_cfg(monkeypatch, cfg: AppConfig) -> None:
    from icx_engine.config_manager import ConfigManager
    monkeypatch.setattr(ConfigManager, "load", staticmethod(lambda: cfg))


def test_unknown_tool_returns_none():
    import asyncio
    result = asyncio.run(dispatch_workstatus_tool("not_a_workstatus_tool", {}))
    assert result is None


def test_all_tools_have_unique_names():
    names = [t.name for t in WORKSTATUS_TOOLS]
    assert len(names) == len(set(names))
    assert all(name.startswith("workstatus_") for name in names)


async def test_unread_notifications_no_connection_returns_fallback(monkeypatch):
    _load_cfg(monkeypatch, AppConfig())
    result = await dispatch_workstatus_tool("workstatus_unread_notifications", {})
    body = json.loads(result[0].text)
    assert body["ok"] is False
    assert "fallback" in body


@respx.mock
async def test_unread_notifications_happy_path(monkeypatch):
    _load_cfg(monkeypatch, _configured_cfg())
    respx.get("https://web-api.workstatus.io/api/v5/notifications/unread-count").mock(
        return_value=httpx.Response(200, json={"code": 200, "message": "ok", "data": {"count": 4}})
    )
    result = await dispatch_workstatus_tool("workstatus_unread_notifications", {})
    body = json.loads(result[0].text)
    assert body["ok"] is True
    assert body["unread_notifications"] == 4


@respx.mock
async def test_my_profile_happy_path(monkeypatch):
    _load_cfg(monkeypatch, _configured_cfg())
    respx.post("https://web-api.workstatus.io/api/v5/member/myprofile").mock(
        return_value=httpx.Response(200, json={"code": "200", "message": "ok", "data": {"primaryinfo": {"name": "A"}}})
    )
    result = await dispatch_workstatus_tool("workstatus_my_profile", {})
    body = json.loads(result[0].text)
    assert body["ok"] is True
    assert body["profile"]["primaryinfo"]["name"] == "A"


async def test_add_timesheet_missing_required_field_returns_error(monkeypatch):
    _load_cfg(monkeypatch, _configured_cfg())
    result = await dispatch_workstatus_tool("workstatus_add_timesheet", {"project_id": 1})
    body = json.loads(result[0].text)
    assert body["ok"] is False
    assert "required" in body["error"].lower()


@respx.mock
async def test_add_timesheet_happy_path(monkeypatch):
    _load_cfg(monkeypatch, _configured_cfg())
    respx.post("https://web-api.workstatus.io/api/v5/timesheet/add").mock(
        return_value=httpx.Response(200, json={"code": "200", "message": "ok", "data": [{"id": 1}]})
    )
    result = await dispatch_workstatus_tool("workstatus_add_timesheet", {
        "project_id": 1893, "todo_id": 3815, "date": "01-08-2026",
        "from_time": "10:00 am", "to_time": "11:00 am", "duration": "1h 0m",
        "reason": "Report development work",
    })
    body = json.loads(result[0].text)
    assert body["ok"] is True
    assert body["timesheet"] == [{"id": 1}]


@respx.mock
async def test_add_timesheet_empty_response_surfaces_as_ok_false_not_false_success(monkeypatch):
    _load_cfg(monkeypatch, _configured_cfg())
    respx.post("https://web-api.workstatus.io/api/v5/timesheet/add").mock(
        return_value=httpx.Response(200, json={"code": "200", "message": "ok", "data": {}})
    )
    result = await dispatch_workstatus_tool("workstatus_add_timesheet", {
        "project_id": 1893, "todo_id": 3815, "date": "01-08-2026",
        "from_time": "10:00 am", "to_time": "11:00 am", "duration": "1h 0m",
        "reason": "Report development work",
    })
    body = json.loads(result[0].text)
    assert body["ok"] is False
    assert "empty response body" in body["error"]


@respx.mock
async def test_add_timesheet_billable_omitted_sends_empty_not_false(monkeypatch):
    _load_cfg(monkeypatch, _configured_cfg())
    route = respx.post("https://web-api.workstatus.io/api/v5/timesheet/add").mock(
        return_value=httpx.Response(200, json={"code": "200", "message": "ok", "data": [{"id": 1}]})
    )
    await dispatch_workstatus_tool("workstatus_add_timesheet", {
        "project_id": 1893, "todo_id": 3815, "date": "01-08-2026",
        "from_time": "10:00 am", "to_time": "11:00 am", "duration": "1h 0m",
        "reason": "Report development work",
    })
    body = json.loads(route.calls[0].request.content)
    assert body["billable"] == ""


@respx.mock
async def test_add_timesheet_billable_explicit_false_sent_as_false(monkeypatch):
    _load_cfg(monkeypatch, _configured_cfg())
    route = respx.post("https://web-api.workstatus.io/api/v5/timesheet/add").mock(
        return_value=httpx.Response(200, json={"code": "200", "message": "ok", "data": [{"id": 1}]})
    )
    await dispatch_workstatus_tool("workstatus_add_timesheet", {
        "project_id": 1893, "todo_id": 3815, "date": "01-08-2026",
        "from_time": "10:00 am", "to_time": "11:00 am", "duration": "1h 0m",
        "reason": "Report development work", "billable": False,
    })
    body = json.loads(route.calls[0].request.content)
    assert body["billable"] is False


@respx.mock
async def test_list_projects_passes_page_data_count_lean_through(monkeypatch):
    _load_cfg(monkeypatch, _configured_cfg())
    route = respx.post("https://web-api.workstatus.io/api/v5/table/view/project/list").mock(
        return_value=httpx.Response(200, json={"code": "200", "message": "ok", "result": {
            "total": 1, "data": [{"id": 1, "members": [{"id": 9}]}],
        }})
    )
    result = await dispatch_workstatus_tool("workstatus_list_projects", {
        "page": 2, "data_count": 30, "lean": True,
    })
    body = json.loads(result[0].text)
    assert body["ok"] is True
    assert "members" not in body["projects"]["data"][0]
    assert route.calls[0].request.url.params["page"] == "2"


@respx.mock
async def test_list_tasks_passes_page_through(monkeypatch):
    _load_cfg(monkeypatch, _configured_cfg())
    route = respx.post("https://web-api.workstatus.io/api/v5/get/task/list").mock(
        return_value=httpx.Response(200, json={"status": "200", "message": "ok", "data": {"total": 574}})
    )
    result = await dispatch_workstatus_tool("workstatus_list_tasks", {"project_id": 1893, "page": 4})
    body = json.loads(result[0].text)
    assert body["ok"] is True
    assert route.calls[0].request.url.params["page"] == "4"


async def test_add_timesheet_no_connection_returns_fallback(monkeypatch):
    _load_cfg(monkeypatch, AppConfig())
    result = await dispatch_workstatus_tool("workstatus_add_timesheet", {
        "project_id": 1, "todo_id": 2, "date": "01-08-2026",
        "from_time": "10:00 am", "to_time": "11:00 am", "duration": "1h 0m", "reason": "x",
    })
    body = json.loads(result[0].text)
    assert body["ok"] is False
    assert "fallback" in body


BASE = "https://web-api.workstatus.io/api/v5"

# (tool name, arguments, endpoint path, response envelope key ("data"/"result"), payload key in _ok(), returns_list)
_READ_TOOLS = [
    ("workstatus_list_projects", {}, "table/view/project/list", "result", "projects", False),
    ("workstatus_get_project", {"project_id": 1893}, "project/detailsview", "data", "project", "single"),
    ("workstatus_project_budget_analytics", {"project_id": 1893}, "project/budget-analytics", "data", "budget", False),
    ("workstatus_list_tasks", {"project_id": 1893}, "get/task/list", "data", "tasks", False),
    ("workstatus_list_task_statuses", {"project_id": 1893}, "get/taskstatus/list", "data", "task_statuses", True),
    ("workstatus_list_milestones", {"project_id": 1893}, "list/milestone", "data", "milestones", True),
    ("workstatus_list_task_checklist", {"task_id": 5407}, "task/checklist/list", "data", "checklist", True),
    ("workstatus_list_members", {}, "members/lists", "data", "members", True),
    ("workstatus_list_teams", {}, "team/list", "data", "teams", True),
    ("workstatus_attendance_list", {"start_date": "2026-07-01", "end_date": "2026-08-01"}, "attendance/list", "data", "attendance", True),
    ("workstatus_attendance_stats", {"start_date": "2026-07-01", "end_date": "2026-08-01"}, "member/attendance/stats", "data", "stats", False),
    ("workstatus_list_timesheets", {"start_date": "2026-07-01", "end_date": "2026-08-01"}, "timesheets/viewTimesheet/list", "data", "timesheets", False),
    ("workstatus_list_timesheet_clients", {}, "timesheet/client/list", "data", "clients", True),
    ("workstatus_weekly_report", {"start_date": "2026-07-01", "end_date": "2026-08-01"}, "reports/weeklyreportall", "data", "report", False),
    ("workstatus_timesheet_submission_kpis", {"start_date": "2026-07-01", "end_date": "2026-08-01"}, "reports/timesheet-submission/kpis", "data", "kpis", False),
    ("workstatus_timesheet_submission_table", {"start_date": "2026-07-01", "end_date": "2026-08-01"}, "reports/timesheet-submission/table", "data", "table", False),
    ("workstatus_list_expenses", {"start_date": "2026-07-01", "end_date": "2026-08-01"}, "expense/filtered-data", "data", "expenses", False),
    ("workstatus_list_invoices", {}, "list/invoices", "data", "invoices", False),
    ("workstatus_payroll_report", {"start_date": "2026-07-01", "end_date": "2026-08-01"}, "payroll/report/list", "data", "payroll", False),
    ("workstatus_get_timesheet", {"timesheet_id": 329559}, "timesheets/view", "data", "timesheet", "single"),
]


@pytest.mark.parametrize("tool_name, args, path, env_key, payload_key, returns_list", _READ_TOOLS)
async def test_new_tool_no_connection_returns_fallback(monkeypatch, tool_name, args, path, env_key, payload_key, returns_list):
    _load_cfg(monkeypatch, AppConfig())
    result = await dispatch_workstatus_tool(tool_name, args)
    body = json.loads(result[0].text)
    assert body["ok"] is False
    assert "fallback" in body


@pytest.mark.parametrize("tool_name, args, path, env_key, payload_key, returns_list", _READ_TOOLS)
@respx.mock
async def test_new_tool_happy_path(monkeypatch, tool_name, args, path, env_key, payload_key, returns_list):
    _load_cfg(monkeypatch, _configured_cfg())
    # returns_list == "single": endpoint returns a one-item list, client unwraps to that item.
    if returns_list == "single":
        envelope_value = [{"marker": True}]
        expected = {"marker": True}
    elif returns_list:
        envelope_value = [{"marker": True}]
        expected = [{"marker": True}]
    else:
        envelope_value = {"marker": True}
        expected = {"marker": True}
    respx.post(f"{BASE}/{path}").mock(
        return_value=httpx.Response(200, json={"code": "200", "message": "ok", env_key: envelope_value})
    )
    result = await dispatch_workstatus_tool(tool_name, args)
    body = json.loads(result[0].text)
    assert body["ok"] is True, body
    assert body[payload_key] == expected


@pytest.mark.parametrize("tool_name", [
    "workstatus_get_project", "workstatus_project_budget_analytics", "workstatus_list_tasks",
    "workstatus_list_task_statuses", "workstatus_list_milestones",
    "workstatus_attendance_list", "workstatus_attendance_stats", "workstatus_list_timesheets",
    "workstatus_weekly_report", "workstatus_timesheet_submission_kpis",
    "workstatus_timesheet_submission_table", "workstatus_list_expenses", "workstatus_payroll_report",
    "workstatus_get_timesheet",
])
async def test_new_tool_missing_required_field_returns_error(monkeypatch, tool_name):
    _load_cfg(monkeypatch, _configured_cfg())
    result = await dispatch_workstatus_tool(tool_name, {})
    body = json.loads(result[0].text)
    assert body["ok"] is False
    assert "required" in body["error"].lower()


async def test_list_task_checklist_missing_task_id_returns_error(monkeypatch):
    _load_cfg(monkeypatch, _configured_cfg())
    result = await dispatch_workstatus_tool("workstatus_list_task_checklist", {})
    body = json.loads(result[0].text)
    assert body["ok"] is False
    assert "required" in body["error"].lower()


_EDIT_TIMESHEET_ARGS = {
    "timesheet_id": 329559, "project_id": 1893, "todo_id": 3815, "date": "01-08-2026",
    "from_time": "10:00 am", "to_time": "11:30 am", "duration": "1h 30m",
    "reason": "Report development work",
    "updated_fields": [{"field_name": "to", "previous_value": "11:00 am", "new_value": "11:30 am"}],
}


async def test_edit_timesheet_missing_updated_fields_returns_error(monkeypatch):
    _load_cfg(monkeypatch, _configured_cfg())
    args = {k: v for k, v in _EDIT_TIMESHEET_ARGS.items() if k != "updated_fields"}
    result = await dispatch_workstatus_tool("workstatus_edit_timesheet", args)
    body = json.loads(result[0].text)
    assert body["ok"] is False
    assert "updated_fields" in body["error"]


async def test_edit_timesheet_missing_required_field_returns_error(monkeypatch):
    _load_cfg(monkeypatch, _configured_cfg())
    result = await dispatch_workstatus_tool("workstatus_edit_timesheet", {"timesheet_id": 1})
    body = json.loads(result[0].text)
    assert body["ok"] is False
    assert "required" in body["error"].lower()


async def test_edit_timesheet_no_connection_returns_fallback(monkeypatch):
    _load_cfg(monkeypatch, AppConfig())
    result = await dispatch_workstatus_tool("workstatus_edit_timesheet", _EDIT_TIMESHEET_ARGS)
    body = json.loads(result[0].text)
    assert body["ok"] is False
    assert "fallback" in body


@respx.mock
async def test_edit_timesheet_happy_path(monkeypatch):
    _load_cfg(monkeypatch, _configured_cfg())
    respx.post(f"{BASE}/edit/timesheet/329559").mock(
        return_value=httpx.Response(200, json={"response": {"code": "200", "message": "ok"}})
    )
    result = await dispatch_workstatus_tool("workstatus_edit_timesheet", _EDIT_TIMESHEET_ARGS)
    body = json.loads(result[0].text)
    assert body["ok"] is True
    assert body["timesheet"] == {"code": "200", "message": "ok"}
