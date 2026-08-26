from __future__ import annotations
import httpx
import pytest
import respx


def test_workstatus_help(cli_runner):
    from icx_engine.cli import app
    result = cli_runner.invoke(app, ["workstatus", "--help"])
    assert result.exit_code == 0
    for cmd in (
        "status", "profile", "unread", "add-time", "projects", "project",
        "project-budget", "tasks", "task-statuses", "milestones", "task-checklist",
        "members", "teams", "attendance", "attendance-stats", "timesheets",
        "timesheet-clients", "weekly-report", "submission-kpis", "submission-table",
        "expenses", "invoices", "payroll", "timesheet", "edit-time",
    ):
        assert cmd in result.output


@pytest.mark.parametrize("args", [
    ["projects"],
    ["project", "--project-id", "1"],
    ["project-budget", "--project-id", "1"],
    ["tasks", "--project-id", "1"],
    ["task-statuses", "--project-id", "1"],
    ["milestones", "--project-id", "1"],
    ["task-checklist", "--task-id", "1"],
    ["members"],
    ["teams"],
    ["attendance", "--start-date", "2026-07-01", "--end-date", "2026-08-01"],
    ["attendance-stats", "--start-date", "2026-07-01", "--end-date", "2026-08-01"],
    ["timesheets", "--start-date", "2026-07-01", "--end-date", "2026-08-01"],
    ["timesheet-clients"],
    ["weekly-report", "--start-date", "2026-07-01", "--end-date", "2026-08-01"],
    ["submission-kpis", "--start-date", "2026-07-01", "--end-date", "2026-08-01"],
    ["submission-table", "--start-date", "2026-07-01", "--end-date", "2026-08-01"],
    ["expenses", "--start-date", "2026-07-01", "--end-date", "2026-08-01"],
    ["invoices"],
    ["payroll", "--start-date", "2026-07-01", "--end-date", "2026-08-01"],
    ["timesheet", "--timesheet-id", "1"],
    ["edit-time", "--timesheet-id", "1", "--project-id", "1", "--todo-id", "1",
     "--date", "01-08-2026", "--from", "10:00 am", "--to", "11:00 am", "--duration", "1h 0m",
     "--reason", "x", "--field-name", "to", "--previous-value", "11:00 am", "--new-value", "11:30 am"],
])
def test_new_workstatus_command_reports_not_configured(cli_runner, isolated_config, args):
    from icx_engine.cli import app
    result = cli_runner.invoke(app, ["workstatus", *args])
    assert result.exit_code == 1
    assert "no workstatus connection" in result.output.lower()


def test_workstatus_status_reports_not_configured(cli_runner, isolated_config):
    from icx_engine.cli import app
    result = cli_runner.invoke(app, ["workstatus", "status"])
    assert result.exit_code == 0
    assert "no workstatus connection" in result.output.lower()


@respx.mock
@pytest.mark.xdist_group(name="workstatus_default_keyring")
def test_workstatus_connect_command_saves_connection(cli_runner, isolated_config, monkeypatch):
    """xdist_group: see test_smoke.py's identically-named test for why - both write real
    keyring secrets under connection name 'default' and must not run on different xdist
    workers concurrently."""
    from icx_engine.cli import app
    respx.get("https://web-api.workstatus.io/api/v5/notifications/unread-count").mock(
        return_value=httpx.Response(200, json={"code": 200, "message": "ok", "data": {"count": 0}})
    )
    monkeypatch.setattr("typer.prompt", lambda *a, **k: {
        "UserID header value": "175599",
        "OrgID header value": "8570",
        "Authorization header value": "Bearer x",
        "SDToken header value": "sd-x",
        "deviceType header value": "web",
    }.get(a[0], k.get("default", "")))
    monkeypatch.setattr("typer.confirm", lambda *a, **k: True)
    result = cli_runner.invoke(app, ["workstatus", "--add"])
    assert result.exit_code == 0
    from icx_engine.config_manager import ConfigManager
    config = ConfigManager.load()
    assert config.workstatus_connections["default"].user_id == "175599"
    assert config.workstatus_connections["default"].org_id == "8570"
    assert config.active_workstatus == "default"


@respx.mock
@pytest.mark.xdist_group(name="workstatus_default_keyring")
def test_workstatus_status_reports_connected_after_add(cli_runner, isolated_config, monkeypatch):
    from icx_engine.cli import app
    respx.get("https://web-api.workstatus.io/api/v5/notifications/unread-count").mock(
        return_value=httpx.Response(200, json={"code": 200, "message": "ok", "data": {"count": 1}})
    )
    monkeypatch.setattr("typer.prompt", lambda *a, **k: {
        "UserID header value": "175599",
        "OrgID header value": "8570",
        "Authorization header value": "Bearer x",
        "SDToken header value": "sd-x",
        "deviceType header value": "web",
    }.get(a[0], k.get("default", "")))
    monkeypatch.setattr("typer.confirm", lambda *a, **k: True)
    cli_runner.invoke(app, ["workstatus", "--add"])
    result = cli_runner.invoke(app, ["workstatus", "status"])
    assert result.exit_code == 0
    assert "connected" in result.output.lower()
