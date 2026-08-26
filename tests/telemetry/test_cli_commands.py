from __future__ import annotations
import json
import typer.testing


def test_logs_report_no_data_prints_clean_message(tmp_path, monkeypatch):
    from icx_engine.telemetry.cli_commands import logs_app
    monkeypatch.setattr("icx_engine.telemetry.cli_commands.ToolCallLogger",
                         lambda: type("L", (), {"root": tmp_path})())
    runner = typer.testing.CliRunner()
    result = runner.invoke(logs_app, ["report", "--date", "2026-01-01"])
    assert result.exit_code == 0
    assert "no logged tool calls" in result.stdout.lower()


def test_logs_report_prints_table_with_data(tmp_path, monkeypatch):
    from icx_engine.telemetry.cli_commands import logs_app
    day_dir = tmp_path / "2026-08-25"
    day_dir.mkdir(parents=True)
    (day_dir / "tool_calls.jsonl").write_text(
        json.dumps({"tool": "git_repo_status", "ok": True, "duration_ms": 10.0,
                    "input_tokens_est": 5, "output_tokens_est": 20}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("icx_engine.telemetry.cli_commands.ToolCallLogger",
                         lambda: type("L", (), {"root": tmp_path})())
    runner = typer.testing.CliRunner()
    result = runner.invoke(logs_app, ["report", "--date", "2026-08-25"])
    assert result.exit_code == 0
    assert "git_repo_status" in result.stdout
    assert "Total: 1 calls, 0 errors." in result.stdout


def test_logs_report_tool_filter(tmp_path, monkeypatch):
    from icx_engine.telemetry.cli_commands import logs_app
    day_dir = tmp_path / "2026-08-25"
    day_dir.mkdir(parents=True)
    (day_dir / "tool_calls.jsonl").write_text(
        json.dumps({"tool": "git_repo_status", "ok": True, "duration_ms": 1.0,
                    "input_tokens_est": 1, "output_tokens_est": 1}) + "\n"
        + json.dumps({"tool": "sonar_status", "ok": True, "duration_ms": 1.0,
                      "input_tokens_est": 1, "output_tokens_est": 1}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("icx_engine.telemetry.cli_commands.ToolCallLogger",
                         lambda: type("L", (), {"root": tmp_path})())
    runner = typer.testing.CliRunner()
    result = runner.invoke(logs_app, ["report", "--date", "2026-08-25", "--tool", "sonar_status"])
    assert result.exit_code == 0
    assert "sonar_status" in result.stdout
    assert "git_repo_status" not in result.stdout


def test_logs_report_defaults_to_today_without_date_flag(tmp_path, monkeypatch):
    from icx_engine.telemetry.cli_commands import logs_app
    monkeypatch.setattr("icx_engine.telemetry.cli_commands.ToolCallLogger",
                         lambda: type("L", (), {"root": tmp_path})())
    runner = typer.testing.CliRunner()
    result = runner.invoke(logs_app, ["report"])
    assert result.exit_code == 0
