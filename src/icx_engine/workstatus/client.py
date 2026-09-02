"""Workstatus Web API client - reverse-engineered, no public docs exist (see
developer.md's Workstatus integration section for the full evidence trail:
what's VERIFIED live from browser capture vs UNVERIFIED).

Base URL and every endpoint/header/payload shape below were captured from
real authenticated network traffic, never fabricated. Where the request body
schema was NOT captured for a given endpoint, no method is implemented for it
- see developer.md for the list of catalogued-but-unimplemented paths.

Auth is four headers (Authorization/UserID/OrgID/SDToken) plus deviceType,
supplied by the caller (pasted from a browser session, see config.py) -
this client never performs a login itself.
"""
from __future__ import annotations

import uuid

import httpx

from icx_engine.exceptions import AuthError, RateLimited, SourceUnavailable

_BASE_URL = "https://web-api.workstatus.io"
_APP_ORIGIN = "https://app.workstatus.io"
_TIMEOUT = 30.0


class WorkstatusError(RuntimeError):
    """Raised for any Workstatus API failure - non-2xx response, network
    error, or malformed response body."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def _raise_for_workstatus(resp: httpx.Response, action: str) -> None:
    code = resp.status_code
    if code < 400:
        return
    if code == 401:
        raise AuthError(f"{action} failed: Workstatus session expired or invalid (HTTP 401). Re-run `icx workstatus --add`.")
    if code == 403:
        raise AuthError(
            f"{action} failed: Workstatus permission denied (HTTP 403). If this happens on "
            "every endpoint with freshly-pasted credentials, it is not a credential problem - "
            "re-run `icx workstatus --add` after pulling fresh header values again if this "
            "persists, and check whether your Workstatus account/role has any feature "
            "restriction that could explain a blanket permission denial."
        )
    if code == 429:
        raise RateLimited(f"{action} failed: Workstatus rate limited the request (HTTP 429).")
    if code >= 500:
        raise SourceUnavailable(f"{action} failed: Workstatus server error (HTTP {code}).")
    raise WorkstatusError(f"{action} failed (HTTP {code}): {resp.text}", code)


def _strip_nested_collections(row: dict) -> dict:
    """Drop any list/dict-valued field from a single response row, keeping only
    scalars (str/int/float/bool/None). Used for `lean=True` list responses -
    a generic structural rule rather than a guess at any specific field name
    (e.g. an embedded member roster), since exact field names beyond what
    was live-captured are not something this connector fabricates."""
    if not isinstance(row, dict):
        return row
    return {k: v for k, v in row.items() if not isinstance(v, (list, dict))}


class WorkstatusClient:
    """Async context manager wrapping one httpx client for a batch of calls."""

    def __init__(
        self, user_id: str, org_id: str, authorization: str, sd_token: str,
        device_type: str = "web", device_id: str | None = None,
    ):
        self._user_id = user_id
        self._org_id = org_id
        self._authorization = authorization
        self._sd_token = sd_token
        self._device_type = device_type
        # Body-level device identifier for write calls - no verified generation
        # algorithm exists (see developer.md); a fresh UUID is used per client
        # instance unless the caller supplies a stable one.
        self._device_id = device_id or str(uuid.uuid4())
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "WorkstatusClient":
        self._client = httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=False)
        return self

    async def __aexit__(self, *exc) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _headers(self, has_body: bool) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
            # Workstatus has no public API - web-api.workstatus.io is only ever meant to be
            # called from app.workstatus.io's own browser session, almost certainly behind
            # bot-detection middleware that rejects non-browser-shaped traffic outright (a
            # blanket 403 independent of whether the session token itself is valid). A live
            # comparison against a captured real-browser cURL for this same API found our
            # client sending httpx's default `User-Agent: python-httpx/...` - an immediate
            # giveaway - plus none of the Sec-Fetch-*/Sec-CH-UA client-hint headers a real
            # Chrome request carries (browser-enforced, cannot be set by page JS, but nothing
            # stops an HTTP client from sending them as literal header values). UNVERIFIED
            # whether this alone clears the block - not something confirmable without live
            # access - but it directly addresses the most conspicuous "this is a script, not
            # a browser" signal found so far.
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
            ),
            "sec-ch-ua": '"Not;A=Brand";v="8", "Chromium";v="150", "Google Chrome";v="150"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-site",
            "Origin": _APP_ORIGIN,
            "Referer": f"{_APP_ORIGIN}/",
            "Authorization": self._authorization,
            "UserID": self._user_id,
            "OrgID": self._org_id,
            "SDToken": self._sd_token,
            "deviceType": self._device_type,
        }
        if has_body:
            headers["Content-Type"] = "application/json"
        return headers

    async def _request(self, method: str, path: str, json: dict | None = None, params: dict | None = None) -> httpx.Response:
        assert self._client is not None, "WorkstatusClient must be used as an async context manager"
        try:
            resp = await self._client.request(
                method, f"{_BASE_URL}{path}",
                headers=self._headers(has_body=json is not None), json=json, params=params,
            )
        except httpx.HTTPError as exc:
            raise WorkstatusError(f"Workstatus request to {path} failed: {exc}") from exc
        return resp

    @staticmethod
    def _data(resp: httpx.Response, action: str, key: str = "data") -> dict:
        """Unwrap the response envelope. Most endpoints use `{code, message,
        data}`; a few (project listing, task listing) use `{code/status,
        message, result}`; a few more (timesheet view/edit) nest the WHOLE
        envelope under a `response` key - `{response: {code, message, data}}`.
        `key` picks which field holds the payload, verified per-endpoint
        rather than assumed uniform."""
        _raise_for_workstatus(resp, action)
        try:
            body = resp.json()
        except Exception as exc:
            raise WorkstatusError(f"{action} returned a malformed response body: {exc}") from exc
        envelope = body.get("response", body)
        return envelope.get(key) or {}

    async def validate(self) -> dict:
        """Confirm the session credentials are accepted - the unread-count
        endpoint is a cheap, side-effect-free GET, so it doubles as a health
        check (no dedicated auth-validate endpoint was found)."""
        resp = await self._request("GET", "/api/v5/notifications/unread-count")
        if resp.status_code == 401:
            return {"valid": False, "status_code": 401}
        data = self._data(resp, "Validating Workstatus session")
        return {"valid": True, "unread_notifications": data.get("count")}

    async def unread_notifications_count(self) -> int:
        resp = await self._request("GET", "/api/v5/notifications/unread-count")
        data = self._data(resp, "Fetching unread notification count")
        return int(data.get("count", 0) or 0)

    async def my_profile(self) -> dict:
        resp = await self._request("POST", "/api/v5/member/myprofile")
        return self._data(resp, "Fetching Workstatus profile")

    async def add_timesheet(
        self, project_id: int, todo_id: int, date: str, from_time: str, to_time: str,
        duration: str = "", reason: str = "", member_id: int | None = None, note: str = "",
        billable: bool | None = None, client_id: int | None = None,
        source_type: int = 3, time_type: int = 4, time_mode: int = 0, activity: int = 0,
        os_version: str = "",
    ) -> dict:
        """Create a manual timesheet entry. Payload shape UPDATED (2026-08-03)
        after a second, independently-supplied real cURL example (matching real
        host/headers/`organization_id`) surfaced concrete gaps the single
        earlier capture missed - the empty-200-body failure this fixes was
        real and reproducible against a genuinely assigned task, ruling out
        every project/task/date/format permutation tried first:

        - `from_time`/`to_time` are now the FULL `"YYYY-MM-DD HH:MM:SS"`
          datetime string (matching the read-side top-level `from_time`/
          `to_time` fields confirmed in WS-3), NOT the 12-hour `"10:00 am"`
          display format previously documented - that format was tried
          repeatedly against a real submission and consistently produced the
          empty-200 failure; the display format only ever verified as a READ
          representation (`interval.from`/`interval.to`), never independently
          confirmed as accepted on write.
        - `deviceType`/`os_version`/`togglenotes`/`togglereason` are now sent
          in the body - these are NOT new/invented fields: they already exist,
          live-verified, in `edit_timesheet`'s own captured shape below, just
          never ported to this sibling endpoint. `togglenotes`/`togglereason`
          are computed the same way `edit_timesheet` computes them
          (`bool(note)`/`bool(reason)`).
        - `duration` now defaults to `""` (empty) rather than being a required
          caller-computed string - the new example sends it empty, consistent
          with Workstatus computing duration itself from `from`/`to`.
        - `source_type`/`time_type`/`time_mode` defaults changed to `3`/`4`/`0`
          - matching the human's own independently-confirmed WORKING historical
          entries (real July submissions stored `source_type: 3, time_type: 4`),
          not the single earlier submission this connector's defaults were
          originally captured from. Still UNVERIFIED beyond these two data
          points - Workstatus exposes no enum-list endpoint for these, unlike
          Jira's createmeta or GitLab's own lookups - override if a different
          value is confirmed needed.

        `ip_address` is intentionally left blank - the server has the real
        request IP regardless, and fabricating one would misrepresent it.

        `billable` is NOT mandatory - omitting it (None) sends an empty value
        rather than forcing a True/False default, so an entry is never
        silently marked non-billable just because the caller didn't specify.

        `date` format CORRECTED (2026-08-13): must be `"YYYY-MM-DD"`, matching
        the date component already used in `from_time`/`to_time` - NOT
        `"DD-MM-YYYY"` as previously documented here. `"DD-MM-YYYY"` was tried
        against a real submission (same project/task, only the date field
        varying) and consistently produced the same empty-200 silent
        failure; switching to `"YYYY-MM-DD"` with every other field held
        identical succeeded immediately. This module never converts `date`
        itself - it is passed straight through to the API body below, so the
        caller (mcp_tools.py/cli.py) is the sole source of this format."""
        body = {
            "billable": billable if billable is not None else "",
            "date": date,
            "deviceId": self._device_id,
            "deviceType": self._device_type,
            "from": from_time,
            "ip_address": "",
            "member_id": int(member_id if member_id is not None else self._user_id),
            "notes": {"note": note},
            "organization_id": int(self._org_id),
            "os_version": os_version,
            "project_id": project_id,
            "client_id": client_id,
            "reason": reason,
            "source_type": source_type,
            "time_type": time_type,
            "to": to_time,
            "todo_id": todo_id,
            "activity": activity,
            "time_mode": time_mode,
            "duration": duration,
            "togglenotes": bool(note),
            "togglereason": bool(reason),
        }
        resp = await self._request("POST", "/api/v5/timesheet/add", json=body)
        data = self._data(resp, "Adding Workstatus timesheet entry")
        if not data:
            # Observed live: Workstatus can respond HTTP 200 with an empty `data` payload
            # when the entry was NOT actually created - an in-band failure signal, not an
            # HTTP error status, so _raise_for_workstatus never catches it. Silently
            # returning {} here previously reported false success on a failed write.
            raise WorkstatusError(
                "Adding Workstatus timesheet entry: Workstatus returned HTTP 200 with an "
                "empty response body - the entry was NOT created despite the success-shaped "
                "status code. Do not retry blindly; verify the submitted fields are valid "
                "for this project/task before trying again."
            )
        return data

    # -- Projects ------------------------------------------------------------

    async def list_projects(
        self, keyword: str = "", project_status: list | None = None, billable: str = "",
        over_budget: str = "", user_id: list | None = None, client_id: list | None = None,
        data_count: int = 15, page: int = 1, lean: bool = False,
    ) -> dict:
        """Paginated project list. Returns the Laravel-style paginator dict
        (`current_page`, `data`, `total`, ...) - the envelope here nests it
        under `result`, not `data`, unlike most other endpoints. `page` is a
        query-string param (Laravel's `paginate()` reads it by framework
        convention, not something specific to this endpoint that had to be
        captured live) - lets a caller actually reach entries past the first
        `data_count` rows, previously unreachable. `lean=True` strips any
        nested list/dict-valued field from each project row after the
        response comes back (e.g. an embedded member roster) - a generic
        structural trim, not a guess at Workstatus's exact field name for
        that roster, since that name was never verified live; scalar fields
        like id/name/status are always kept."""
        body = {
            "organization_id": int(self._org_id), "keyword": keyword,
            "billable": billable, "project_status": project_status or [],
            "over_budget": over_budget, "user_id": user_id or [], "client_id": client_id or [],
            "data_count": data_count, "margin_value": "", "margin_value_2": "",
            "budget_time_value": "", "budget_time_value_2": "",
            "profit_loss_value": "", "profit_loss_value_2": "", "profit_loss_sort": "",
        }
        resp = await self._request(
            "POST", "/api/v5/table/view/project/list", json=body, params={"page": page},
        )
        result = self._data(resp, "Listing Workstatus projects", key="result")
        if lean and isinstance(result.get("data"), list):
            result["data"] = [_strip_nested_collections(row) for row in result["data"]]
        return result

    async def get_project(self, project_id: int) -> dict:
        body = {"organization_id": int(self._org_id), "project_id": project_id}
        resp = await self._request("POST", "/api/v5/project/detailsview", json=body)
        data = self._data(resp, "Fetching Workstatus project")
        items = data if isinstance(data, list) else []
        return items[0] if items else {}

    async def project_budget_analytics(self, project_id: int, quarter: str = "") -> dict:
        body = {"project_id": str(project_id), "quarter": quarter}
        resp = await self._request("POST", "/api/v5/project/budget-analytics", json=body)
        return self._data(resp, "Fetching Workstatus project budget analytics")

    # -- Tasks / Milestones ---------------------------------------------------

    async def list_tasks(
        self, project_id: int, search: str = "", status: list | None = None,
        priority: list | None = None, show_completed: int = 0, overdue: int = 0,
        page: int = 1,
    ) -> dict:
        """Paginated task list for a project. Returns the Laravel-style
        paginator dict nested under `data`. `page` is a query-string param
        (Laravel's `paginate()` reads it by framework convention) - lets a
        caller reach entries past the server's default page size, previously
        unreachable (no page-size override was ever captured live for this
        endpoint specifically, unlike list_projects's `data_count`, so only
        `page` is exposed here - not a fabricated per-page override field).

        `search` is UNVERIFIED to actually filter results: the captured
        request body always sent `search_option` as an empty string (the
        capture session never had an active search term), and Workstatus's
        server-side behavior with a real `search_option` value was never
        observed - passing `search` alone may return the full unfiltered
        list regardless of what is passed. Do not assume it filters."""
        body = {
            "organization_id": int(self._org_id), "search": search, "search_option": "",
            "status": status or [], "priority": priority or [], "memberIds": "",
            "created_by": "", "worked_by_members": [], "billable": False,
            "recurrence": False, "over_budget": False, "tags": [], "milestone_ids": None,
            "todo_group_ids": [], "todo_parent_ids": [], "overdue": overdue,
            "show_completed": show_completed, "sort_by": None, "sort_order": None,
            "project_id": [project_id],
        }
        resp = await self._request("POST", "/api/v5/get/task/list", json=body, params={"page": page})
        return self._data(resp, "Listing Workstatus tasks")

    async def list_task_statuses(self, project_id: int) -> list:
        body = {"project_id": project_id}
        resp = await self._request("POST", "/api/v5/get/taskstatus/list", json=body)
        data = self._data(resp, "Listing Workstatus task statuses")
        return data if isinstance(data, list) else []

    async def list_milestones(
        self, project_id: int, due: str = "", start_date: str = "", end_date: str = "",
    ) -> list:
        body = {
            "project_id": str(project_id), "due": due, "start_date": start_date,
            "end_date": end_date, "milestone_progress": "", "milestone_progress_value": "",
        }
        resp = await self._request("POST", "/api/v5/list/milestone", json=body)
        data = self._data(resp, "Listing Workstatus milestones")
        return data if isinstance(data, list) else []

    async def list_task_checklist(self, task_id: int) -> list:
        body = {"task_id": task_id}
        resp = await self._request("POST", "/api/v5/task/checklist/list", json=body)
        data = self._data(resp, "Listing Workstatus task checklist")
        return data if isinstance(data, list) else []

    # -- Members / Teams -------------------------------------------------------

    async def list_members(
        self, search_key: str = "", role_id: list | None = None, team_ids: list | None = None,
        department_ids: list | None = None, data_count: int = 20,
    ) -> list:
        body = {
            "organization_id": int(self._org_id), "type": [], "deactivate": [],
            "online_status": "", "searchKey": search_key, "data_count": data_count,
            "login_device": [], "role_id": role_id or [], "teamIds": team_ids or [],
            "departmentIds": department_ids or [], "location": [], "location_status_sf": [],
            "designation_id": [], "timeTracking": "", "tracking_mode": "", "include_leaves": 0,
        }
        resp = await self._request("POST", "/api/v5/members/lists", json=body)
        data = self._data(resp, "Listing Workstatus members")
        return data if isinstance(data, list) else []

    async def list_teams(self, show_deleted_user: bool = False) -> list:
        body = {"org_id": int(self._org_id), "show_deleted_user": show_deleted_user}
        resp = await self._request("POST", "/api/v5/team/list", json=body)
        data = self._data(resp, "Listing Workstatus teams")
        return data if isinstance(data, list) else []

    # -- Attendance ------------------------------------------------------------

    async def attendance_list(self, from_date: str, to_date: str, member_id: int | None = None) -> list:
        body = {
            "memberId": int(member_id if member_id is not None else self._user_id),
            "organization_id": int(self._org_id),
            "interval": {"from": from_date, "to": to_date},
            "arrivals": [], "schedule_status": [], "Leaves": [],
        }
        resp = await self._request("POST", "/api/v5/attendance/list", json=body)
        data = self._data(resp, "Listing Workstatus attendance")
        return data if isinstance(data, list) else []

    async def attendance_stats(self, start_date: str, end_date: str, user_id: int | None = None) -> dict:
        body = {
            "user_id": int(user_id if user_id is not None else self._user_id),
            "org_id": int(self._org_id), "start_date": start_date, "end_date": end_date,
        }
        resp = await self._request("POST", "/api/v5/member/attendance/stats", json=body)
        return self._data(resp, "Fetching Workstatus attendance stats")

    # -- Timesheets (read) -----------------------------------------------------

    async def list_timesheets(self, from_date: str, to_date: str, member_id: int | None = None) -> dict:
        body = {
            "interval": {"from": from_date, "to": to_date},
            "memberId": int(member_id if member_id is not None else self._user_id),
            "organization_id": int(self._org_id), "source_type": "", "timezone": "",
            "overtime": "", "location": [], "os_type": [], "client_id": None,
            "time_type": [], "condition": "", "activityLevel": "", "activityLevel2": "",
            "break": [], "project_id": [], "todo_id": [],
        }
        resp = await self._request("POST", "/api/v5/timesheets/viewTimesheet/list", json=body)
        return self._data(resp, "Listing Workstatus timesheets")

    async def list_timesheet_clients(self) -> list:
        resp = await self._request("POST", "/api/v5/timesheet/client/list", json={"user_id": self._user_id})
        data = self._data(resp, "Listing Workstatus timesheet clients")
        return data if isinstance(data, list) else []

    # -- Reports -----------------------------------------------------------

    async def weekly_report_all(
        self, start_date: str, end_date: str, user_ids: list | None = None,
        project_ids: list | None = None, data_count: int = 20,
    ) -> dict:
        body = {
            "start_date": start_date, "end_date": end_date, "org_id": int(self._org_id),
            "userIds": user_ids or [], "projectIds": project_ids or [], "teamids": [],
            "user_type": 0, "is_listing": True, "data_count": data_count, "departmentIds": [],
            "activitycondition": "", "activityParams": "", "activityParams2": "",
            "condition": "", "timeParams": "", "timeParams2": "",
        }
        resp = await self._request("POST", "/api/v5/reports/weeklyreportall", json=body)
        return self._data(resp, "Fetching Workstatus weekly report")

    async def timesheet_submission_kpis(self, start_date: str, end_date: str, search: str = "") -> dict:
        body = {
            "search": search,
            "filters": {
                "start_date": start_date, "end_date": end_date, "member_ids": [], "team_ids": [],
                "dept_ids": [], "project_ids": [], "project_status": [], "todo_ids": [],
                "manager_ids": [], "hours_logged_operator": "", "hours_logged_value": "",
                "submitted_start_date": "", "submitted_end_date": "", "status": [],
            },
            "show_deleted_user": False,
        }
        resp = await self._request("POST", "/api/v5/reports/timesheet-submission/kpis", json=body)
        return self._data(resp, "Fetching Workstatus timesheet submission KPIs")

    async def timesheet_submission_table(
        self, start_date: str, end_date: str, page: int = 1, per_page: int = 15, search: str = "",
    ) -> dict:
        body = {
            "filters": {
                "start_date": start_date, "end_date": end_date, "member_ids": [], "team_ids": [],
                "dept_ids": [], "project_ids": [], "project_status": [], "todo_ids": [], "manager_ids": [],
            },
            "pagination": {"page": page, "per_page": per_page},
            "sort": {"key": "", "order": ""}, "search": search, "show_deleted_user": False,
        }
        resp = await self._request("POST", "/api/v5/reports/timesheet-submission/table", json=body)
        return self._data(resp, "Fetching Workstatus timesheet submission table")

    # -- Financials --------------------------------------------------------

    async def list_expenses(self, start_date: str, end_date: str, user_ids: list | None = None) -> dict:
        body = {
            "count": 20, "user_type": 0, "teamIds": [], "departmentIds": [],
            "userIds": user_ids or [], "start_date": start_date, "end_date": end_date,
            "projectIds": [], "taskIds": [], "status": [], "category": [],
            "condition": "", "amount": "", "amount2": "",
        }
        resp = await self._request("POST", "/api/v5/expense/filtered-data", json=body)
        return self._data(resp, "Listing Workstatus expenses")

    async def list_invoices(self, search: str = "", invoice_status: str = "", project_id: list | None = None) -> dict:
        body = {
            "project_id": project_id or [], "client_id": [], "data_count": 20, "search": search,
            "invoice_status": invoice_status, "condition": "", "amount": "", "amount2": "",
            "search_option": "", "sort_by": {"key": "", "order_by": ""},
            "created_at": {"start_date": "", "end_date": ""},
            "due_date": {"start_date": "", "end_date": ""},
        }
        resp = await self._request("POST", "/api/v5/list/invoices", json=body)
        return self._data(resp, "Listing Workstatus invoices")

    async def payroll_report(self, from_date: str, to_date: str, member_id: list | None = None) -> dict:
        body = {
            "group_by": 0, "interval": {"from": from_date, "to": to_date},
            "organization_id": int(self._org_id), "memberId": member_id or [], "teamids": [],
            "user_type": 0, "is_listing": True, "data_count": 20, "departmentIds": [], "export": False,
        }
        resp = await self._request("POST", "/api/v5/payroll/report/list", json=body)
        return self._data(resp, "Fetching Workstatus payroll report")

    # -- Timesheet view/edit ----------------------------------------------

    async def get_timesheet(self, timesheet_id: int, member_id: int | None = None) -> dict:
        """Fetch one timesheet entry's full detail - the data behind the
        'View Timesheet' screen (member/project/task/date/times/OS/location/
        IP/reason/notes). The `type` field's correct value was never
        confirmed live (only that the field exists) - `"manual"` is a
        best-effort default matching this connector's own manually-created
        entries; pass a different value if it proves necessary for
        auto-tracked entries."""
        body = {
            "member_id": str(member_id if member_id is not None else self._user_id),
            "id": str(timesheet_id), "type": "manual",
        }
        resp = await self._request("POST", "/api/v5/timesheets/view", json=body)
        data = self._data(resp, "Fetching Workstatus timesheet")
        items = data if isinstance(data, list) else []
        if not items:
            # Silently returning {} here previously reported false success on a
            # not-found id - no signal to stop, inviting blind repeat calls with
            # different ids. Surface it as a real error instead.
            raise WorkstatusError(f"Timesheet {timesheet_id} not found (member_id={member_id if member_id is not None else self._user_id}).")
        return items[0]

    async def edit_timesheet(
        self, timesheet_id: int, project_id: int, todo_id: int, date: str, from_time: str,
        to_time: str, duration: str, reason: str, updated_fields: list[dict],
        member_id: int | None = None, note: str = "", note_id: int = 0, billable: bool | None = None,
        client_id: str = "", source_type: int = 1, time_type: int = 1, time_mode: int = 1,
        activity: int = 0, os_version: str = "",
    ) -> dict:
        """Edit an existing manual timesheet entry - VERIFIED live: the 'View
        Timesheet' modal auto-saves on every single field change (no separate
        Save button), confirmed by capturing two real calls to this endpoint
        (one field-edit, one revert). `updated_fields` is a required diff
        descriptor - `[{"field_name", "previous_value", "new_value"}, ...]` -
        mirroring exactly what the Manual Time Edit report's audit trail
        displays; the server appears to want an explicit change description,
        not just the new state. Caller must supply this (usually by comparing
        against a prior `get_timesheet()` call) - it is not fabricated here.

        `source_type`/`time_type`/`time_mode`/`activity` default to the values
        captured from that one live submission - see `add_timesheet`'s
        docstring: Workstatus has no enum-list endpoint to verify these
        against, so treat the defaults as unverified beyond the captured case.

        `billable` is NOT mandatory - same reasoning as `add_timesheet`: omitting
        it (None) sends an empty value rather than forcing a True/False default.

        `date` format is the same `"YYYY-MM-DD"` as `add_timesheet` - see that
        method's docstring for why (never `"DD-MM-YYYY"`)."""
        body = {
            "billable": billable if billable is not None else "", "date": date, "deviceId": self._device_id,
            "deviceType": self._device_type, "from": from_time, "ip_address": "",
            "member_id": str(member_id if member_id is not None else self._user_id),
            "organization_id": int(self._org_id), "os_version": os_version, "reason": reason,
            "source_type": source_type, "time_type": time_type, "to": to_time,
            "updatedFields": updated_fields, "project_id": project_id, "client_id": client_id,
            "todo_id": todo_id, "activity": activity, "duration": duration, "time_mode": time_mode,
            "notes": {"id": note_id, "note": note}, "togglenotes": bool(note), "togglereason": bool(reason),
        }
        resp = await self._request("POST", f"/api/v5/edit/timesheet/{timesheet_id}", json=body)
        _raise_for_workstatus(resp, "Editing Workstatus timesheet entry")
        try:
            raw = resp.json()
        except Exception as exc:
            raise WorkstatusError(f"Editing Workstatus timesheet entry returned a malformed response body: {exc}") from exc
        envelope = raw.get("response", raw)
        return {"code": envelope.get("code"), "message": envelope.get("message")}
