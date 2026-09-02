from __future__ import annotations
import json
import httpx
import pytest
import respx

from icx_engine.models.config import AppConfig, WorkstatusConnection
from icx_engine.workstatus.service import (
    WorkstatusNotConfigured,
    add_connection,
    status,
    unread_notifications_count,
    my_profile,
    add_timesheet,
    list_projects,
    get_project,
    project_budget_analytics,
    list_tasks,
    list_task_statuses,
    list_milestones,
    list_task_checklist,
    list_members,
    list_teams,
    attendance_list,
    attendance_stats,
    list_timesheets,
    list_timesheet_clients,
    weekly_report_all,
    timesheet_submission_kpis,
    timesheet_submission_table,
    list_expenses,
    list_invoices,
    payroll_report,
    get_timesheet,
    edit_timesheet,
    recent_project_tasks,
)


def _configured_cfg() -> AppConfig:
    cfg = AppConfig()
    cfg.workstatus_connections["default"] = WorkstatusConnection(
        name="default", user_id="175599", org_id="8570", device_type="web",
        authorization="Bearer x", sd_token="sd-x",
    )
    cfg.active_workstatus = "default"
    return cfg


@respx.mock
async def test_add_connection_saves_and_validates(monkeypatch):
    from icx_engine.config_manager import ConfigManager
    monkeypatch.setattr(ConfigManager, "save", staticmethod(lambda c: None))
    respx.get("https://web-api.workstatus.io/api/v5/notifications/unread-count").mock(
        return_value=httpx.Response(200, json={"code": 200, "message": "ok", "data": {"count": 2}})
    )
    cfg = AppConfig()
    out = await add_connection("default", "175599", "8570", "Bearer x", "sd-x", cfg=cfg)
    assert out["name"] == "default"
    assert out["active"] is True
    assert out["validation"]["valid"] is True
    assert out["validation"]["unread_notifications"] == 2
    assert cfg.workstatus_connections["default"].user_id == "175599"
    assert cfg.active_workstatus == "default"


async def test_add_connection_requires_all_fields(monkeypatch):
    from icx_engine.config_manager import ConfigManager
    monkeypatch.setattr(ConfigManager, "save", staticmethod(lambda c: None))
    cfg = AppConfig()
    with pytest.raises(ValueError):
        await add_connection("default", "", "8570", "Bearer x", "sd-x", cfg=cfg)


async def test_add_connection_requires_name(monkeypatch):
    from icx_engine.config_manager import ConfigManager
    monkeypatch.setattr(ConfigManager, "save", staticmethod(lambda c: None))
    cfg = AppConfig()
    with pytest.raises(ValueError):
        await add_connection("", "175599", "8570", "Bearer x", "sd-x", cfg=cfg)


@respx.mock
async def test_add_connection_reports_invalid_when_validation_fails(monkeypatch):
    from icx_engine.config_manager import ConfigManager
    monkeypatch.setattr(ConfigManager, "save", staticmethod(lambda c: None))
    respx.get("https://web-api.workstatus.io/api/v5/notifications/unread-count").mock(return_value=httpx.Response(401))
    cfg = AppConfig()
    out = await add_connection("default", "175599", "8570", "Bearer bad", "sd-x", cfg=cfg)
    assert out["name"] == "default"                  # saved regardless - user can fix creds and re-add
    assert out["validation"]["valid"] is False


async def test_status_reports_not_configured_when_empty():
    out = await status(cfg=AppConfig())
    assert out["configured"] is False
    assert out["connection"] is None


@respx.mock
async def test_status_validates_live_when_configured():
    respx.get("https://web-api.workstatus.io/api/v5/notifications/unread-count").mock(
        return_value=httpx.Response(200, json={"code": 200, "message": "ok", "data": {"count": 0}})
    )
    out = await status(cfg=_configured_cfg())
    assert out["configured"] is True
    assert out["connection"]["valid"] is True


async def test_unread_notifications_count_raises_when_not_configured():
    with pytest.raises(WorkstatusNotConfigured):
        await unread_notifications_count(cfg=AppConfig())


@respx.mock
async def test_unread_notifications_count_returns_value_when_configured():
    respx.get("https://web-api.workstatus.io/api/v5/notifications/unread-count").mock(
        return_value=httpx.Response(200, json={"code": 200, "message": "ok", "data": {"count": 7}})
    )
    count = await unread_notifications_count(cfg=_configured_cfg())
    assert count == 7


async def test_my_profile_raises_when_not_configured():
    with pytest.raises(WorkstatusNotConfigured):
        await my_profile(cfg=AppConfig())


@respx.mock
async def test_my_profile_returns_data_when_configured():
    respx.post("https://web-api.workstatus.io/api/v5/member/myprofile").mock(
        return_value=httpx.Response(200, json={"code": "200", "message": "ok", "data": {"primaryinfo": {}}})
    )
    profile = await my_profile(cfg=_configured_cfg())
    assert "primaryinfo" in profile


async def test_add_timesheet_raises_when_not_configured():
    with pytest.raises(WorkstatusNotConfigured):
        await add_timesheet(
            project_id=1, todo_id=2, date="01-08-2026", from_time="10:00 am",
            to_time="11:00 am", duration="1h 0m", reason="x", cfg=AppConfig(),
        )


@respx.mock
async def test_add_timesheet_returns_data_when_configured():
    respx.post("https://web-api.workstatus.io/api/v5/timesheet/add").mock(
        return_value=httpx.Response(200, json={"code": "200", "message": "ok", "data": [{"id": 1}]})
    )
    result = await add_timesheet(
        project_id=1893, todo_id=3815, date="01-08-2026", from_time="10:00 am",
        to_time="11:00 am", duration="1h 0m", reason="Report development work", cfg=_configured_cfg(),
    )
    assert result == [{"id": 1}]


@respx.mock
async def test_add_timesheet_empty_response_raises_not_false_success():
    from icx_engine.workstatus.client import WorkstatusError
    respx.post("https://web-api.workstatus.io/api/v5/timesheet/add").mock(
        return_value=httpx.Response(200, json={"code": "200", "message": "ok", "data": {}})
    )
    with pytest.raises(WorkstatusError):
        await add_timesheet(
            project_id=1893, todo_id=3815, date="01-08-2026", from_time="10:00 am",
            to_time="11:00 am", duration="1h 0m", reason="x", cfg=_configured_cfg(),
        )


@respx.mock
async def test_list_projects_passes_data_count_page_lean_through():
    route = respx.post("https://web-api.workstatus.io/api/v5/table/view/project/list").mock(
        return_value=httpx.Response(200, json={"code": "200", "message": "ok", "result": {
            "total": 1, "data": [{"id": 1, "members": [{"id": 9}]}],
        }})
    )
    result = await list_projects(data_count=30, page=2, lean=True, cfg=_configured_cfg())
    assert route.calls[0].request.url.params["page"] == "2"
    body = json.loads(route.calls[0].request.content)
    assert body["data_count"] == 30
    assert "members" not in result["data"][0]


@respx.mock
async def test_list_tasks_passes_page_through():
    route = respx.post("https://web-api.workstatus.io/api/v5/get/task/list").mock(
        return_value=httpx.Response(200, json={"status": "200", "message": "ok", "data": {"total": 574}})
    )
    await list_tasks(1893, page=5, cfg=_configured_cfg())
    assert route.calls[0].request.url.params["page"] == "5"


BASE = "https://web-api.workstatus.io/api/v5"


@pytest.mark.parametrize(
    "fn, args, path",
    [
        (list_projects, {}, "table/view/project/list"),
        (get_project, {"project_id": 1893}, "project/detailsview"),
        (project_budget_analytics, {"project_id": 1893}, "project/budget-analytics"),
        (list_tasks, {"project_id": 1893}, "get/task/list"),
        (list_task_statuses, {"project_id": 1893}, "get/taskstatus/list"),
        (list_milestones, {"project_id": 1893}, "list/milestone"),
        (list_task_checklist, {"task_id": 5407}, "task/checklist/list"),
        (list_members, {}, "members/lists"),
        (list_teams, {}, "team/list"),
        (attendance_list, {"from_date": "2026-07-01", "to_date": "2026-08-01"}, "attendance/list"),
        (attendance_stats, {"start_date": "2026-07-01", "end_date": "2026-08-01"}, "member/attendance/stats"),
        (list_timesheets, {"from_date": "2026-07-01", "to_date": "2026-08-01"}, "timesheets/viewTimesheet/list"),
        (list_timesheet_clients, {}, "timesheet/client/list"),
        (weekly_report_all, {"start_date": "2026-07-01", "end_date": "2026-08-01"}, "reports/weeklyreportall"),
        (timesheet_submission_kpis, {"start_date": "2026-07-01", "end_date": "2026-08-01"}, "reports/timesheet-submission/kpis"),
        (timesheet_submission_table, {"start_date": "2026-07-01", "end_date": "2026-08-01"}, "reports/timesheet-submission/table"),
        (list_expenses, {"start_date": "2026-07-01", "end_date": "2026-08-01"}, "expense/filtered-data"),
        (list_invoices, {}, "list/invoices"),
        (payroll_report, {"from_date": "2026-07-01", "to_date": "2026-08-01"}, "payroll/report/list"),
        (get_timesheet, {"timesheet_id": 329559}, "timesheets/view"),
    ],
)
async def test_each_new_service_function_raises_when_not_configured(fn, args, path):
    with pytest.raises(WorkstatusNotConfigured):
        await fn(**args, cfg=AppConfig())


@pytest.mark.parametrize(
    "fn, args, path",
    [
        (list_projects, {}, "table/view/project/list"),
        (get_project, {"project_id": 1893}, "project/detailsview"),
        (project_budget_analytics, {"project_id": 1893}, "project/budget-analytics"),
        (list_tasks, {"project_id": 1893}, "get/task/list"),
        (list_task_statuses, {"project_id": 1893}, "get/taskstatus/list"),
        (list_milestones, {"project_id": 1893}, "list/milestone"),
        (list_task_checklist, {"task_id": 5407}, "task/checklist/list"),
        (list_members, {}, "members/lists"),
        (list_teams, {}, "team/list"),
        (attendance_list, {"from_date": "2026-07-01", "to_date": "2026-08-01"}, "attendance/list"),
        (attendance_stats, {"start_date": "2026-07-01", "end_date": "2026-08-01"}, "member/attendance/stats"),
        (list_timesheets, {"from_date": "2026-07-01", "to_date": "2026-08-01"}, "timesheets/viewTimesheet/list"),
        (list_timesheet_clients, {}, "timesheet/client/list"),
        (weekly_report_all, {"start_date": "2026-07-01", "end_date": "2026-08-01"}, "reports/weeklyreportall"),
        (timesheet_submission_kpis, {"start_date": "2026-07-01", "end_date": "2026-08-01"}, "reports/timesheet-submission/kpis"),
        (timesheet_submission_table, {"start_date": "2026-07-01", "end_date": "2026-08-01"}, "reports/timesheet-submission/table"),
        (list_expenses, {"start_date": "2026-07-01", "end_date": "2026-08-01"}, "expense/filtered-data"),
        (list_invoices, {}, "list/invoices"),
        (payroll_report, {"from_date": "2026-07-01", "to_date": "2026-08-01"}, "payroll/report/list"),
    ],
)
@respx.mock
async def test_each_new_service_function_calls_expected_endpoint_when_configured(fn, args, path):
    route = respx.post(f"{BASE}/{path}").mock(
        return_value=httpx.Response(200, json={"code": "200", "message": "ok", "data": {}, "result": {}})
    )
    await fn(**args, cfg=_configured_cfg())
    assert route.called


@respx.mock
async def test_get_timesheet_calls_expected_endpoint_when_configured():
    """Separate from the generic empty-data check above: an empty `data` list is get_timesheet's
    own not-found signal (see client.get_timesheet), so it needs a real item in the envelope."""
    route = respx.post(f"{BASE}/timesheets/view").mock(
        return_value=httpx.Response(200, json={"code": "200", "message": "ok", "data": [{"id": 329559}]})
    )
    await get_timesheet(timesheet_id=329559, cfg=_configured_cfg())
    assert route.called


async def test_edit_timesheet_raises_when_not_configured():
    with pytest.raises(WorkstatusNotConfigured):
        await edit_timesheet(
            timesheet_id=1, project_id=1, todo_id=1, date="01-08-2026", from_time="10:00 am",
            to_time="11:00 am", duration="1h 0m", reason="x", updated_fields=[], cfg=AppConfig(),
        )


@respx.mock
async def test_edit_timesheet_returns_data_when_configured():
    respx.post(f"{BASE}/edit/timesheet/329559").mock(
        return_value=httpx.Response(200, json={"response": {"code": "200", "message": "ok"}})
    )
    result = await edit_timesheet(
        timesheet_id=329559, project_id=1893, todo_id=3815, date="01-08-2026", from_time="10:00 am",
        to_time="11:30 am", duration="1h 30m", reason="Report development work",
        updated_fields=[{"field_name": "to", "previous_value": "11:00 am", "new_value": "11:30 am"}],
        cfg=_configured_cfg(),
    )
    assert result == {"code": "200", "message": "ok"}


async def test_recent_project_tasks_raises_when_not_configured():
    with pytest.raises(WorkstatusNotConfigured):
        await recent_project_tasks(cfg=AppConfig())


@respx.mock
async def test_recent_project_tasks_dedupes_sorts_and_returns_empty_on_no_history():
    respx.post(f"{BASE}/timesheets/viewTimesheet/list").mock(
        return_value=httpx.Response(200, json={"code": "200", "message": "ok", "data": {"timeSheetList": [
            {"date": "2026-08-04", "project": {"id": 1814, "name": "VIL Ads"}, "todo": {"id": 44967, "name": "VILAds_Development_Dev"}},
            {"date": "2026-08-06", "project": {"id": 1814, "name": "VIL Ads"}, "todo": {"id": 44967, "name": "VILAds_Development_Dev"}},
            {"date": "2026-08-05", "project": {"id": 2105, "name": "6D_IN_CVM_Int"}, "todo": {"id": 76822, "name": "R&D_Dashboards"}},
        ]}})
    )
    rows = await recent_project_tasks(cfg=_configured_cfg())
    assert len(rows) == 2
    assert rows[0] == {
        "project_id": 1814, "project_name": "VIL Ads", "todo_id": 44967,
        "todo_name": "VILAds_Development_Dev", "last_used": "2026-08-06",
    }
    assert rows[1]["project_id"] == 2105


@respx.mock
async def test_recent_project_tasks_empty_history_returns_empty_list():
    respx.post(f"{BASE}/timesheets/viewTimesheet/list").mock(
        return_value=httpx.Response(200, json={"code": "200", "message": "ok", "data": {"timeSheetList": []}})
    )
    rows = await recent_project_tasks(cfg=_configured_cfg())
    assert rows == []


@respx.mock
async def test_recent_project_tasks_passes_lookback_days_as_interval():
    route = respx.post(f"{BASE}/timesheets/viewTimesheet/list").mock(
        return_value=httpx.Response(200, json={"code": "200", "message": "ok", "data": {"timeSheetList": []}})
    )
    await recent_project_tasks(lookback_days=30, cfg=_configured_cfg())
    body = json.loads(route.calls[0].request.content)
    from datetime import date, timedelta
    assert body["interval"]["to"] == date.today().isoformat()
    assert body["interval"]["from"] == (date.today() - timedelta(days=30)).isoformat()
