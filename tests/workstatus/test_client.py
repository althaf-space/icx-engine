from __future__ import annotations
import json

import httpx
import pytest
import respx

from icx_engine.exceptions import AuthError, RateLimited, SourceUnavailable
from icx_engine.workstatus.client import WorkstatusClient, WorkstatusError


def _client(**overrides) -> WorkstatusClient:
    kwargs = dict(user_id="175599", org_id="8570", authorization="Bearer x", sd_token="sd-x")
    kwargs.update(overrides)
    return WorkstatusClient(**kwargs)


@respx.mock
async def test_unread_notifications_count_sends_expected_headers(workstatus_base_url):
    route = respx.get(f"{workstatus_base_url}/api/v5/notifications/unread-count").mock(
        return_value=httpx.Response(200, json={"code": 200, "message": "ok", "data": {"count": 3}})
    )
    async with _client() as client:
        count = await client.unread_notifications_count()
    assert count == 3
    sent = route.calls[0].request.headers
    assert sent["Authorization"] == "Bearer x"
    assert sent["UserID"] == "175599"
    assert sent["OrgID"] == "8570"
    assert sent["SDToken"] == "sd-x"
    assert sent["deviceType"] == "web"
    assert sent["Origin"] == "https://app.workstatus.io"
    assert sent["Referer"] == "https://app.workstatus.io/"
    assert "Chrome" in sent["User-Agent"]
    assert sent["Sec-Fetch-Site"] == "same-site"
    assert sent["Sec-Fetch-Mode"] == "cors"
    assert sent["Sec-Fetch-Dest"] == "empty"


@respx.mock
async def test_unread_notifications_count_defaults_to_zero_when_missing(workstatus_base_url):
    respx.get(f"{workstatus_base_url}/api/v5/notifications/unread-count").mock(
        return_value=httpx.Response(200, json={"code": 200, "message": "ok", "data": {}})
    )
    async with _client() as client:
        count = await client.unread_notifications_count()
    assert count == 0


@respx.mock
async def test_my_profile_returns_data_payload(workstatus_base_url):
    respx.post(f"{workstatus_base_url}/api/v5/member/myprofile").mock(
        return_value=httpx.Response(200, json={"code": "200", "message": "ok", "data": {"primaryinfo": {"name": "A"}}})
    )
    async with _client() as client:
        profile = await client.my_profile()
    assert profile["primaryinfo"]["name"] == "A"


@respx.mock
async def test_validate_reports_invalid_on_401(workstatus_base_url):
    respx.get(f"{workstatus_base_url}/api/v5/notifications/unread-count").mock(return_value=httpx.Response(401))
    async with _client() as client:
        result = await client.validate()
    assert result["valid"] is False
    assert result["status_code"] == 401


@respx.mock
async def test_validate_reports_valid_with_count(workstatus_base_url):
    respx.get(f"{workstatus_base_url}/api/v5/notifications/unread-count").mock(
        return_value=httpx.Response(200, json={"code": 200, "message": "ok", "data": {"count": 5}})
    )
    async with _client() as client:
        result = await client.validate()
    assert result["valid"] is True
    assert result["unread_notifications"] == 5


@respx.mock
async def test_unread_count_401_raises_auth_error(workstatus_base_url):
    respx.get(f"{workstatus_base_url}/api/v5/notifications/unread-count").mock(return_value=httpx.Response(401))
    async with _client() as client:
        with pytest.raises(AuthError):
            await client.unread_notifications_count()


@respx.mock
async def test_unread_count_403_raises_auth_error_with_workstatus_in_message(workstatus_base_url):
    """The 403 message must contain 'Workstatus' so error_display.py's keyword
    routing picks the workstatus-specific remediation hint, not the generic one."""
    respx.get(f"{workstatus_base_url}/api/v5/notifications/unread-count").mock(return_value=httpx.Response(403))
    async with _client() as client:
        with pytest.raises(AuthError, match="Workstatus"):
            await client.unread_notifications_count()


@respx.mock
async def test_unread_count_429_raises_rate_limited(workstatus_base_url):
    respx.get(f"{workstatus_base_url}/api/v5/notifications/unread-count").mock(return_value=httpx.Response(429))
    async with _client() as client:
        with pytest.raises(RateLimited):
            await client.unread_notifications_count()


@respx.mock
async def test_unread_count_5xx_raises_source_unavailable(workstatus_base_url):
    respx.get(f"{workstatus_base_url}/api/v5/notifications/unread-count").mock(return_value=httpx.Response(503))
    async with _client() as client:
        with pytest.raises(SourceUnavailable):
            await client.unread_notifications_count()


@respx.mock
async def test_malformed_json_response_raises_workstatus_error(workstatus_base_url):
    respx.get(f"{workstatus_base_url}/api/v5/notifications/unread-count").mock(
        return_value=httpx.Response(200, text="not json")
    )
    async with _client() as client:
        with pytest.raises(WorkstatusError):
            await client.unread_notifications_count()


@respx.mock
async def test_add_timesheet_posts_expected_payload(workstatus_base_url):
    route = respx.post(f"{workstatus_base_url}/api/v5/timesheet/add").mock(
        return_value=httpx.Response(200, json={"code": "200", "message": "ok", "data": [{"id": 99}]})
    )
    async with _client() as client:
        result = await client.add_timesheet(
            project_id=1893, todo_id=3815, date="01-08-2026", from_time="10:00 am",
            to_time="11:00 am", duration="1h 0m", reason="Report development work",
        )
    assert result == [{"id": 99}]
    body = json.loads(route.calls[0].request.content)
    assert body["project_id"] == 1893
    assert body["todo_id"] == 3815
    assert body["date"] == "01-08-2026"
    assert body["from"] == "10:00 am"
    assert body["to"] == "11:00 am"
    assert body["duration"] == "1h 0m"
    assert body["reason"] == "Report development work"
    assert body["organization_id"] == 8570
    assert body["member_id"] == 175599
    assert body["notes"] == {"note": ""}
    assert body["billable"] == ""
    assert "deviceId" in body


@respx.mock
async def test_add_timesheet_defaults_match_confirmed_working_values(workstatus_base_url):
    """2026-08-03: source_type/time_type/time_mode defaults changed to match the
    human's own confirmed-working historical entries (3/4/0), and deviceType/
    os_version/togglenotes/togglereason - already verified in edit_timesheet's
    own captured shape - are now sent here too."""
    route = respx.post(f"{workstatus_base_url}/api/v5/timesheet/add").mock(
        return_value=httpx.Response(200, json={"code": "200", "message": "ok", "data": [{"id": 99}]})
    )
    async with _client() as client:
        await client.add_timesheet(
            project_id=1893, todo_id=3815, date="01-08-2026",
            from_time="2026-08-01 10:00:00", to_time="2026-08-01 11:00:00",
            reason="Report development work",
        )
    body = json.loads(route.calls[0].request.content)
    assert body["from"] == "2026-08-01 10:00:00"
    assert body["to"] == "2026-08-01 11:00:00"
    assert body["duration"] == ""
    assert body["source_type"] == 3
    assert body["time_type"] == 4
    assert body["time_mode"] == 0
    assert body["deviceType"] == "web"
    assert body["os_version"] == ""
    assert body["togglenotes"] is False
    assert body["togglereason"] is True


@respx.mock
async def test_add_timesheet_billable_explicit_false_is_sent_as_false(workstatus_base_url):
    route = respx.post(f"{workstatus_base_url}/api/v5/timesheet/add").mock(
        return_value=httpx.Response(200, json={"code": "200", "message": "ok", "data": [{"id": 99}]})
    )
    async with _client() as client:
        await client.add_timesheet(
            project_id=1893, todo_id=3815, date="01-08-2026", from_time="10:00 am",
            to_time="11:00 am", duration="1h 0m", reason="x", billable=False,
        )
    body = json.loads(route.calls[0].request.content)
    assert body["billable"] is False


@respx.mock
async def test_add_timesheet_billable_explicit_true_is_sent_as_true(workstatus_base_url):
    route = respx.post(f"{workstatus_base_url}/api/v5/timesheet/add").mock(
        return_value=httpx.Response(200, json={"code": "200", "message": "ok", "data": [{"id": 99}]})
    )
    async with _client() as client:
        await client.add_timesheet(
            project_id=1893, todo_id=3815, date="01-08-2026", from_time="10:00 am",
            to_time="11:00 am", duration="1h 0m", reason="x", billable=True,
        )
    body = json.loads(route.calls[0].request.content)
    assert body["billable"] is True


@respx.mock
async def test_add_timesheet_sends_content_type_header(workstatus_base_url):
    route = respx.post(f"{workstatus_base_url}/api/v5/timesheet/add").mock(
        return_value=httpx.Response(200, json={"code": "200", "message": "ok", "data": {"id": 1}})
    )
    async with _client() as client:
        await client.add_timesheet(
            project_id=1, todo_id=2, date="01-08-2026", from_time="10:00 am",
            to_time="11:00 am", duration="1h 0m", reason="x",
        )
    assert route.calls[0].request.headers["Content-Type"] == "application/json"


@respx.mock
async def test_add_timesheet_400_raises_workstatus_error(workstatus_base_url):
    respx.post(f"{workstatus_base_url}/api/v5/timesheet/add").mock(
        return_value=httpx.Response(400, json={"message": "invalid project_id"})
    )
    async with _client() as client:
        with pytest.raises(WorkstatusError):
            await client.add_timesheet(
                project_id=1, todo_id=2, date="01-08-2026", from_time="10:00 am",
                to_time="11:00 am", duration="1h 0m", reason="x",
            )


@respx.mock
async def test_add_timesheet_empty_data_on_200_raises_instead_of_false_success(workstatus_base_url):
    """The actual reported bug: Workstatus can respond HTTP 200 with an empty data
    payload when the write silently failed server-side - this must never be
    reported back as a successful create."""
    respx.post(f"{workstatus_base_url}/api/v5/timesheet/add").mock(
        return_value=httpx.Response(200, json={"code": "200", "message": "ok", "data": {}})
    )
    async with _client() as client:
        with pytest.raises(WorkstatusError, match="empty response body"):
            await client.add_timesheet(
                project_id=1, todo_id=2, date="01-08-2026", from_time="10:00 am",
                to_time="11:00 am", duration="1h 0m", reason="x",
            )


@respx.mock
async def test_add_timesheet_empty_list_data_on_200_also_raises(workstatus_base_url):
    respx.post(f"{workstatus_base_url}/api/v5/timesheet/add").mock(
        return_value=httpx.Response(200, json={"code": "200", "message": "ok", "data": []})
    )
    async with _client() as client:
        with pytest.raises(WorkstatusError, match="empty response body"):
            await client.add_timesheet(
                project_id=1, todo_id=2, date="01-08-2026", from_time="10:00 am",
                to_time="11:00 am", duration="1h 0m", reason="x",
            )


# -- Projects ------------------------------------------------------------

@respx.mock
async def test_list_projects_posts_expected_body_and_unwraps_result(workstatus_base_url):
    route = respx.post(f"{workstatus_base_url}/api/v5/table/view/project/list").mock(
        return_value=httpx.Response(200, json={"code": "200", "message": "ok", "result": {"total": 2, "data": []}})
    )
    async with _client() as client:
        result = await client.list_projects(keyword="Vodafone")
    assert result["total"] == 2
    body = json.loads(route.calls[0].request.content)
    assert body["keyword"] == "Vodafone"
    assert body["organization_id"] == 8570


@respx.mock
async def test_list_projects_sends_page_as_query_param(workstatus_base_url):
    route = respx.post(f"{workstatus_base_url}/api/v5/table/view/project/list").mock(
        return_value=httpx.Response(200, json={"code": "200", "message": "ok", "result": {"total": 28, "data": []}})
    )
    async with _client() as client:
        await client.list_projects(page=2, data_count=25)
    assert route.calls[0].request.url.params["page"] == "2"
    body = json.loads(route.calls[0].request.content)
    assert body["data_count"] == 25


@respx.mock
async def test_list_projects_lean_strips_nested_fields_keeps_scalars(workstatus_base_url):
    respx.post(f"{workstatus_base_url}/api/v5/table/view/project/list").mock(
        return_value=httpx.Response(200, json={"code": "200", "message": "ok", "result": {
            "total": 1,
            "data": [{
                "id": 1893, "name": "Vodafone", "status": "active",
                "members": [{"id": 1, "email": "a@x.com"}, {"id": 2, "email": "b@x.com"}],
                "client": {"id": 5, "name": "Client A"},
            }],
        }})
    )
    async with _client() as client:
        result = await client.list_projects(lean=True)
    row = result["data"][0]
    assert row == {"id": 1893, "name": "Vodafone", "status": "active"}
    assert "members" not in row
    assert "client" not in row


@respx.mock
async def test_list_projects_not_lean_keeps_nested_fields(workstatus_base_url):
    respx.post(f"{workstatus_base_url}/api/v5/table/view/project/list").mock(
        return_value=httpx.Response(200, json={"code": "200", "message": "ok", "result": {
            "total": 1, "data": [{"id": 1893, "members": [{"id": 1}]}],
        }})
    )
    async with _client() as client:
        result = await client.list_projects(lean=False)
    assert result["data"][0]["members"] == [{"id": 1}]


@respx.mock
async def test_get_project_returns_first_item(workstatus_base_url):
    route = respx.post(f"{workstatus_base_url}/api/v5/project/detailsview").mock(
        return_value=httpx.Response(200, json={"code": "200", "message": "ok", "data": [{"id": 1893, "name": "Vodafone"}]})
    )
    async with _client() as client:
        project = await client.get_project(1893)
    assert project["name"] == "Vodafone"
    body = json.loads(route.calls[0].request.content)
    assert body["project_id"] == 1893


@respx.mock
async def test_project_budget_analytics_posts_project_id_as_string(workstatus_base_url):
    route = respx.post(f"{workstatus_base_url}/api/v5/project/budget-analytics").mock(
        return_value=httpx.Response(200, json={"code": "200", "message": "ok", "data": {"margin": 10}})
    )
    async with _client() as client:
        result = await client.project_budget_analytics(1893, quarter="Q1")
    assert result["margin"] == 10
    body = json.loads(route.calls[0].request.content)
    assert body["project_id"] == "1893"
    assert body["quarter"] == "Q1"


# -- Tasks / Milestones ---------------------------------------------------

@respx.mock
async def test_list_tasks_posts_project_id_as_list(workstatus_base_url):
    route = respx.post(f"{workstatus_base_url}/api/v5/get/task/list").mock(
        return_value=httpx.Response(200, json={"status": "200", "message": "ok", "data": {"total": 1}})
    )
    async with _client() as client:
        result = await client.list_tasks(1893, search="report")
    assert result["total"] == 1
    body = json.loads(route.calls[0].request.content)
    assert body["project_id"] == [1893]
    assert body["search"] == "report"


@respx.mock
async def test_list_tasks_sends_page_as_query_param(workstatus_base_url):
    route = respx.post(f"{workstatus_base_url}/api/v5/get/task/list").mock(
        return_value=httpx.Response(200, json={"status": "200", "message": "ok", "data": {"total": 574}})
    )
    async with _client() as client:
        await client.list_tasks(1893, page=3)
    assert route.calls[0].request.url.params["page"] == "3"


@respx.mock
async def test_list_task_statuses_returns_list(workstatus_base_url):
    respx.post(f"{workstatus_base_url}/api/v5/get/taskstatus/list").mock(
        return_value=httpx.Response(200, json={"code": "200", "message": "ok", "data": [{"id": 1, "name": "Yet to start"}]})
    )
    async with _client() as client:
        statuses = await client.list_task_statuses(1893)
    assert statuses == [{"id": 1, "name": "Yet to start"}]


@respx.mock
async def test_list_milestones_returns_list(workstatus_base_url):
    route = respx.post(f"{workstatus_base_url}/api/v5/list/milestone").mock(
        return_value=httpx.Response(200, json={"code": "200", "message": "ok", "data": [{"id": 1}]})
    )
    async with _client() as client:
        milestones = await client.list_milestones(1893)
    assert milestones == [{"id": 1}]
    body = json.loads(route.calls[0].request.content)
    assert body["project_id"] == "1893"


@respx.mock
async def test_list_task_checklist_returns_list(workstatus_base_url):
    route = respx.post(f"{workstatus_base_url}/api/v5/task/checklist/list").mock(
        return_value=httpx.Response(200, json={"code": "200", "message": "ok", "data": [{"id": 1, "text": "step 1"}]})
    )
    async with _client() as client:
        checklist = await client.list_task_checklist(5407)
    assert checklist == [{"id": 1, "text": "step 1"}]
    body = json.loads(route.calls[0].request.content)
    assert body["task_id"] == 5407


# -- Members / Teams -------------------------------------------------------

@respx.mock
async def test_list_members_posts_search_key(workstatus_base_url):
    route = respx.post(f"{workstatus_base_url}/api/v5/members/lists").mock(
        return_value=httpx.Response(200, json={"code": "200", "message": "ok", "data": [{"id": 1}]})
    )
    async with _client() as client:
        members = await client.list_members(search_key="althaf")
    assert members == [{"id": 1}]
    body = json.loads(route.calls[0].request.content)
    assert body["searchKey"] == "althaf"
    assert body["organization_id"] == 8570


@respx.mock
async def test_list_teams_returns_list(workstatus_base_url):
    respx.post(f"{workstatus_base_url}/api/v5/team/list").mock(
        return_value=httpx.Response(200, json={"code": "200", "message": "ok", "data": [{"id": 1, "name": "Team Nipun"}]})
    )
    async with _client() as client:
        teams = await client.list_teams()
    assert teams == [{"id": 1, "name": "Team Nipun"}]


# -- Attendance ------------------------------------------------------------

@respx.mock
async def test_attendance_list_posts_interval(workstatus_base_url):
    route = respx.post(f"{workstatus_base_url}/api/v5/attendance/list").mock(
        return_value=httpx.Response(200, json={"code": "200", "message": "ok", "data": [{"date": "2026-08-01"}]})
    )
    async with _client() as client:
        entries = await client.attendance_list("2026-07-01", "2026-08-01")
    assert entries == [{"date": "2026-08-01"}]
    body = json.loads(route.calls[0].request.content)
    assert body["interval"] == {"from": "2026-07-01", "to": "2026-08-01"}
    assert body["memberId"] == 175599


@respx.mock
async def test_attendance_stats_posts_expected_body(workstatus_base_url):
    route = respx.post(f"{workstatus_base_url}/api/v5/member/attendance/stats").mock(
        return_value=httpx.Response(200, json={"code": "200", "message": "ok", "data": {"days_present": 20}})
    )
    async with _client() as client:
        stats = await client.attendance_stats("2026-07-01", "2026-08-01")
    assert stats["days_present"] == 20
    body = json.loads(route.calls[0].request.content)
    assert body["user_id"] == 175599
    assert body["org_id"] == 8570


# -- Timesheets (read) -----------------------------------------------------

@respx.mock
async def test_list_timesheets_posts_interval(workstatus_base_url):
    route = respx.post(f"{workstatus_base_url}/api/v5/timesheets/viewTimesheet/list").mock(
        return_value=httpx.Response(200, json={"code": "200", "message": "ok", "data": {"timeSheetList": []}})
    )
    async with _client() as client:
        result = await client.list_timesheets("2026-07-01", "2026-08-01")
    assert result == {"timeSheetList": []}
    body = json.loads(route.calls[0].request.content)
    assert body["interval"] == {"from": "2026-07-01", "to": "2026-08-01"}


@respx.mock
async def test_list_timesheet_clients_returns_list(workstatus_base_url):
    respx.post(f"{workstatus_base_url}/api/v5/timesheet/client/list").mock(
        return_value=httpx.Response(200, json={"code": "200", "message": "ok", "data": [{"id": 1, "name": "Client A"}]})
    )
    async with _client() as client:
        clients = await client.list_timesheet_clients()
    assert clients == [{"id": 1, "name": "Client A"}]


# -- Reports ------------------------------------------------------------

@respx.mock
async def test_weekly_report_all_posts_expected_body(workstatus_base_url):
    route = respx.post(f"{workstatus_base_url}/api/v5/reports/weeklyreportall").mock(
        return_value=httpx.Response(200, json={"code": "200", "message": "ok", "data": {"total_hours": 40}})
    )
    async with _client() as client:
        result = await client.weekly_report_all("2026-07-26", "2026-08-01")
    assert result["total_hours"] == 40
    body = json.loads(route.calls[0].request.content)
    assert body["start_date"] == "2026-07-26"
    assert body["org_id"] == 8570


@respx.mock
async def test_timesheet_submission_kpis_posts_filters(workstatus_base_url):
    route = respx.post(f"{workstatus_base_url}/api/v5/reports/timesheet-submission/kpis").mock(
        return_value=httpx.Response(200, json={"code": "200", "message": "ok", "data": {"missing": 0}})
    )
    async with _client() as client:
        result = await client.timesheet_submission_kpis("2026-07-26", "2026-08-01")
    assert result["missing"] == 0
    body = json.loads(route.calls[0].request.content)
    assert body["filters"]["start_date"] == "2026-07-26"


@respx.mock
async def test_timesheet_submission_table_posts_pagination(workstatus_base_url):
    route = respx.post(f"{workstatus_base_url}/api/v5/reports/timesheet-submission/table").mock(
        return_value=httpx.Response(200, json={"code": "200", "message": "ok", "data": {"total": 1}})
    )
    async with _client() as client:
        result = await client.timesheet_submission_table("2026-07-26", "2026-08-01", page=2, per_page=10)
    assert result["total"] == 1
    body = json.loads(route.calls[0].request.content)
    assert body["pagination"] == {"page": 2, "per_page": 10}


# -- Financials --------------------------------------------------------

@respx.mock
async def test_list_expenses_posts_date_range(workstatus_base_url):
    route = respx.post(f"{workstatus_base_url}/api/v5/expense/filtered-data").mock(
        return_value=httpx.Response(200, json={"code": 200, "message": "ok", "data": {"total": 0}})
    )
    async with _client() as client:
        result = await client.list_expenses("2026-07-26", "2026-08-01")
    assert result["total"] == 0
    body = json.loads(route.calls[0].request.content)
    assert body["start_date"] == "2026-07-26"


@respx.mock
async def test_list_invoices_posts_search(workstatus_base_url):
    route = respx.post(f"{workstatus_base_url}/api/v5/list/invoices").mock(
        return_value=httpx.Response(200, json={"code": "200", "message": "ok", "data": {"total": 0}})
    )
    async with _client() as client:
        result = await client.list_invoices(search="INV-1")
    assert result["total"] == 0
    body = json.loads(route.calls[0].request.content)
    assert body["search"] == "INV-1"


@respx.mock
async def test_payroll_report_posts_interval(workstatus_base_url):
    route = respx.post(f"{workstatus_base_url}/api/v5/payroll/report/list").mock(
        return_value=httpx.Response(200, json={"code": "200", "message": "ok", "data": {"total": 0}})
    )
    async with _client() as client:
        result = await client.payroll_report("2026-07-26", "2026-08-01")
    assert result["total"] == 0
    body = json.loads(route.calls[0].request.content)
    assert body["interval"] == {"from": "2026-07-26", "to": "2026-08-01"}
    assert body["organization_id"] == 8570


# -- Timesheet view/edit -----------------------------------------------------

@respx.mock
async def test_get_timesheet_posts_expected_body_and_unwraps_response_envelope(workstatus_base_url):
    route = respx.post(f"{workstatus_base_url}/api/v5/timesheets/view").mock(
        return_value=httpx.Response(200, json={"response": {"code": "200", "message": "ok", "data": [{"id": 329559, "reason": "Report development work"}]}})
    )
    async with _client() as client:
        entry = await client.get_timesheet(329559)
    assert entry["id"] == 329559
    body = json.loads(route.calls[0].request.content)
    assert body["id"] == "329559"
    assert body["member_id"] == "175599"
    assert body["type"] == "manual"


@respx.mock
async def test_get_timesheet_returns_empty_dict_when_no_items(workstatus_base_url):
    respx.post(f"{workstatus_base_url}/api/v5/timesheets/view").mock(
        return_value=httpx.Response(200, json={"response": {"code": "200", "message": "ok", "data": []}})
    )
    async with _client() as client:
        entry = await client.get_timesheet(1)
    assert entry == {}


@respx.mock
async def test_edit_timesheet_posts_expected_body_and_envelope(workstatus_base_url):
    route = respx.post(f"{workstatus_base_url}/api/v5/edit/timesheet/329559").mock(
        return_value=httpx.Response(200, json={"response": {"code": "200", "message": "Timesheet updated successfully"}})
    )
    async with _client() as client:
        result = await client.edit_timesheet(
            timesheet_id=329559, project_id=1893, todo_id=3815, date="01-08-2026",
            from_time="10:00 am", to_time="11:30 am", duration="1h 30m",
            reason="Report development work", note="",
            updated_fields=[{"field_name": "to", "previous_value": "11:00 am", "new_value": "11:30 am"}],
        )
    assert result == {"code": "200", "message": "Timesheet updated successfully"}
    body = json.loads(route.calls[0].request.content)
    assert body["to"] == "11:30 am"
    assert body["duration"] == "1h 30m"
    assert body["updatedFields"] == [{"field_name": "to", "previous_value": "11:00 am", "new_value": "11:30 am"}]
    assert body["organization_id"] == 8570
    assert "deviceId" in body
    assert body["billable"] == ""


@respx.mock
async def test_edit_timesheet_billable_explicit_value_is_sent_verbatim(workstatus_base_url):
    route = respx.post(f"{workstatus_base_url}/api/v5/edit/timesheet/329559").mock(
        return_value=httpx.Response(200, json={"response": {"code": "200", "message": "ok"}})
    )
    async with _client() as client:
        await client.edit_timesheet(
            timesheet_id=329559, project_id=1893, todo_id=3815, date="01-08-2026",
            from_time="10:00 am", to_time="11:30 am", duration="1h 30m",
            reason="x", updated_fields=[{"field_name": "to", "previous_value": "a", "new_value": "b"}],
            billable=True,
        )
    body = json.loads(route.calls[0].request.content)
    assert body["billable"] is True


@respx.mock
async def test_edit_timesheet_400_raises_workstatus_error(workstatus_base_url):
    respx.post(f"{workstatus_base_url}/api/v5/edit/timesheet/1").mock(
        return_value=httpx.Response(400, json={"message": "invalid field"})
    )
    async with _client() as client:
        with pytest.raises(WorkstatusError):
            await client.edit_timesheet(
                timesheet_id=1, project_id=1, todo_id=1, date="01-08-2026", from_time="10:00 am",
                to_time="11:00 am", duration="1h 0m", reason="x", updated_fields=[],
            )
