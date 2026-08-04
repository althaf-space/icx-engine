# src/icx_engine/workstatus/service.py
"""Workstatus business logic. Multiple named connections with one active,
mirroring gitlab/service.py's shape exactly - add/list/remove/set_active,
same as GitLab/Sonar. `AppConfig.integrations["workstatus"]` (the original
single-instance storage) is migrated automatically into
`workstatus_connections["default"]` on load (see models/config.py's
`_migrate_legacy_workstatus`) - nothing here reads `integrations["workstatus"]`
directly."""
from __future__ import annotations

from typing import Any

from icx_engine.config_manager import ConfigManager
from icx_engine.models.config import WorkstatusConnection
from icx_engine.workstatus.client import WorkstatusClient


class WorkstatusNotConfigured(Exception):
    pass


_NOT_CONFIGURED_MSG = "No Workstatus connection configured. Run `icx workstatus --add`."


def _make_client(cfg: Any) -> WorkstatusClient:
    conn = cfg.active_workstatus_connection()
    if conn is None:
        raise WorkstatusNotConfigured(_NOT_CONFIGURED_MSG)
    return WorkstatusClient(
        user_id=conn.user_id, org_id=conn.org_id,
        authorization=conn.authorization, sd_token=conn.sd_token,
        device_type=conn.device_type,
    )


async def add_connection(
    name: str, user_id: str, org_id: str, authorization: str, sd_token: str,
    device_type: str = "web", make_active: bool = False, cfg: Any | None = None,
) -> dict:
    """Add or update a named Workstatus connection. The first connection added
    (or make_active=True) becomes the active one. Validates the connection live."""
    name = (name or "").strip()
    if not name:
        raise ValueError("Connection name is required.")
    user_id = (user_id or "").strip()
    org_id = (org_id or "").strip()
    authorization = (authorization or "").strip()
    sd_token = (sd_token or "").strip()
    if not (user_id and org_id and authorization and sd_token):
        raise ValueError("user_id, org_id, authorization, and sd_token are all required.")

    cfg = cfg or ConfigManager.load()
    conn = WorkstatusConnection(
        name=name, user_id=user_id, org_id=org_id, authorization=authorization,
        sd_token=sd_token, device_type=device_type or "web",
    )
    cfg.workstatus_connections[name] = conn
    if make_active or cfg.active_workstatus is None:
        cfg.active_workstatus = name
    ConfigManager.save(cfg)

    validation: dict = {"valid": None}
    try:
        async with WorkstatusClient(
            user_id=conn.user_id, org_id=conn.org_id,
            authorization=conn.authorization, sd_token=conn.sd_token,
            device_type=conn.device_type,
        ) as client:
            validation = await client.validate()
    except Exception as exc:
        validation = {"valid": False, "error": str(exc)}
    return {
        "name": name,
        "user_id": conn.user_id,
        "org_id": conn.org_id,
        "active": cfg.active_workstatus == name,
        "validation": validation,
    }


def list_connections(cfg: Any | None = None) -> dict:
    cfg = cfg or ConfigManager.load()
    return {
        "active": cfg.active_workstatus,
        "connections": [
            {
                "name": c.name, "user_id": c.user_id, "org_id": c.org_id,
                "active": c.name == cfg.active_workstatus,
                "has_authorization": bool(c.authorization), "has_sd_token": bool(c.sd_token),
            }
            for c in cfg.workstatus_connections.values()
        ],
    }


def remove_connection(name: str, cfg: Any | None = None) -> dict:
    cfg = cfg or ConfigManager.load()
    if name not in cfg.workstatus_connections:
        raise KeyError(f"No Workstatus connection named '{name}'.")
    cfg.workstatus_connections.pop(name, None)
    ConfigManager.delete_workstatus_connection_secret(name)
    if cfg.active_workstatus == name:
        cfg.active_workstatus = next(iter(cfg.workstatus_connections), None)
    ConfigManager.save(cfg)
    return {"removed": name, "active": cfg.active_workstatus}


def set_active(name: str, cfg: Any | None = None) -> dict:
    cfg = cfg or ConfigManager.load()
    if name not in cfg.workstatus_connections:
        raise KeyError(f"No Workstatus connection named '{name}'.")
    cfg.active_workstatus = name
    ConfigManager.save(cfg)
    return {"active": cfg.active_workstatus}


async def status(cfg: Any | None = None) -> dict:
    cfg = cfg or ConfigManager.load()
    conn = cfg.active_workstatus_connection()
    out: dict = {"configured": conn is not None, "active": cfg.active_workstatus, "connection": None}
    if conn is not None:
        try:
            async with _make_client(cfg) as client:
                out["connection"] = await client.validate()
        except Exception as exc:
            out["connection"] = {"valid": False, "error": str(exc)}
    return out


async def unread_notifications_count(cfg: Any | None = None) -> int:
    cfg = cfg or ConfigManager.load()
    async with _make_client(cfg) as client:
        return await client.unread_notifications_count()


async def my_profile(cfg: Any | None = None) -> dict:
    cfg = cfg or ConfigManager.load()
    async with _make_client(cfg) as client:
        return await client.my_profile()


async def add_timesheet(
    project_id: int, todo_id: int, date: str, from_time: str, to_time: str,
    duration: str = "", reason: str = "", note: str = "", billable: bool | None = None,
    cfg: Any | None = None,
) -> dict:
    cfg = cfg or ConfigManager.load()
    async with _make_client(cfg) as client:
        return await client.add_timesheet(
            project_id=project_id, todo_id=todo_id, date=date, from_time=from_time,
            to_time=to_time, duration=duration, reason=reason, note=note, billable=billable,
        )


# -- Projects ----------------------------------------------------------------

async def list_projects(
    keyword: str = "", data_count: int = 15, page: int = 1, lean: bool = False,
    cfg: Any | None = None,
) -> dict:
    cfg = cfg or ConfigManager.load()
    async with _make_client(cfg) as client:
        return await client.list_projects(keyword=keyword, data_count=data_count, page=page, lean=lean)


async def get_project(project_id: int, cfg: Any | None = None) -> dict:
    cfg = cfg or ConfigManager.load()
    async with _make_client(cfg) as client:
        return await client.get_project(project_id)


async def project_budget_analytics(project_id: int, quarter: str = "", cfg: Any | None = None) -> dict:
    cfg = cfg or ConfigManager.load()
    async with _make_client(cfg) as client:
        return await client.project_budget_analytics(project_id, quarter=quarter)


# -- Tasks / Milestones -------------------------------------------------------

async def list_tasks(project_id: int, search: str = "", page: int = 1, cfg: Any | None = None) -> dict:
    cfg = cfg or ConfigManager.load()
    async with _make_client(cfg) as client:
        return await client.list_tasks(project_id, search=search, page=page)


async def list_task_statuses(project_id: int, cfg: Any | None = None) -> list:
    cfg = cfg or ConfigManager.load()
    async with _make_client(cfg) as client:
        return await client.list_task_statuses(project_id)


async def list_milestones(project_id: int, cfg: Any | None = None) -> list:
    cfg = cfg or ConfigManager.load()
    async with _make_client(cfg) as client:
        return await client.list_milestones(project_id)


async def list_task_checklist(task_id: int, cfg: Any | None = None) -> list:
    cfg = cfg or ConfigManager.load()
    async with _make_client(cfg) as client:
        return await client.list_task_checklist(task_id)


# -- Members / Teams -----------------------------------------------------------

async def list_members(search_key: str = "", cfg: Any | None = None) -> list:
    cfg = cfg or ConfigManager.load()
    async with _make_client(cfg) as client:
        return await client.list_members(search_key=search_key)


async def list_teams(cfg: Any | None = None) -> list:
    cfg = cfg or ConfigManager.load()
    async with _make_client(cfg) as client:
        return await client.list_teams()


# -- Attendance ----------------------------------------------------------------

async def attendance_list(from_date: str, to_date: str, cfg: Any | None = None) -> list:
    cfg = cfg or ConfigManager.load()
    async with _make_client(cfg) as client:
        return await client.attendance_list(from_date, to_date)


async def attendance_stats(start_date: str, end_date: str, cfg: Any | None = None) -> dict:
    cfg = cfg or ConfigManager.load()
    async with _make_client(cfg) as client:
        return await client.attendance_stats(start_date, end_date)


# -- Timesheets (read) ---------------------------------------------------------

async def list_timesheets(from_date: str, to_date: str, cfg: Any | None = None) -> dict:
    cfg = cfg or ConfigManager.load()
    async with _make_client(cfg) as client:
        return await client.list_timesheets(from_date, to_date)


async def list_timesheet_clients(cfg: Any | None = None) -> list:
    cfg = cfg or ConfigManager.load()
    async with _make_client(cfg) as client:
        return await client.list_timesheet_clients()


# -- Reports ------------------------------------------------------------------

async def weekly_report_all(start_date: str, end_date: str, cfg: Any | None = None) -> dict:
    cfg = cfg or ConfigManager.load()
    async with _make_client(cfg) as client:
        return await client.weekly_report_all(start_date, end_date)


async def timesheet_submission_kpis(start_date: str, end_date: str, cfg: Any | None = None) -> dict:
    cfg = cfg or ConfigManager.load()
    async with _make_client(cfg) as client:
        return await client.timesheet_submission_kpis(start_date, end_date)


async def timesheet_submission_table(
    start_date: str, end_date: str, page: int = 1, per_page: int = 15, cfg: Any | None = None,
) -> dict:
    cfg = cfg or ConfigManager.load()
    async with _make_client(cfg) as client:
        return await client.timesheet_submission_table(start_date, end_date, page=page, per_page=per_page)


# -- Financials -----------------------------------------------------------------

async def list_expenses(start_date: str, end_date: str, cfg: Any | None = None) -> dict:
    cfg = cfg or ConfigManager.load()
    async with _make_client(cfg) as client:
        return await client.list_expenses(start_date, end_date)


async def list_invoices(search: str = "", cfg: Any | None = None) -> dict:
    cfg = cfg or ConfigManager.load()
    async with _make_client(cfg) as client:
        return await client.list_invoices(search=search)


async def payroll_report(from_date: str, to_date: str, cfg: Any | None = None) -> dict:
    cfg = cfg or ConfigManager.load()
    async with _make_client(cfg) as client:
        return await client.payroll_report(from_date, to_date)


# -- Timesheet view/edit -------------------------------------------------------

async def get_timesheet(timesheet_id: int, cfg: Any | None = None) -> dict:
    cfg = cfg or ConfigManager.load()
    async with _make_client(cfg) as client:
        return await client.get_timesheet(timesheet_id)


async def edit_timesheet(
    timesheet_id: int, project_id: int, todo_id: int, date: str, from_time: str, to_time: str,
    duration: str, reason: str, updated_fields: list[dict], note: str = "", billable: bool | None = None,
    cfg: Any | None = None,
) -> dict:
    cfg = cfg or ConfigManager.load()
    async with _make_client(cfg) as client:
        return await client.edit_timesheet(
            timesheet_id=timesheet_id, project_id=project_id, todo_id=todo_id, date=date,
            from_time=from_time, to_time=to_time, duration=duration, reason=reason,
            updated_fields=updated_fields, note=note, billable=billable,
        )
