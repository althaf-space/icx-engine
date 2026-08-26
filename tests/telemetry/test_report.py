from __future__ import annotations
import json
from datetime import date
from pathlib import Path

from icx_engine.telemetry.report import build_report


def _write_day(root: Path, day: str, records: list[dict]) -> None:
    day_dir = root / day
    day_dir.mkdir(parents=True, exist_ok=True)
    with open(day_dir / "tool_calls.jsonl", "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def test_build_report_aggregates_per_tool(tmp_path):
    _write_day(tmp_path, "2026-08-25", [
        {"tool": "git_repo_status", "ok": True, "duration_ms": 10.0, "input_tokens_est": 5, "output_tokens_est": 20},
        {"tool": "git_repo_status", "ok": True, "duration_ms": 20.0, "input_tokens_est": 5, "output_tokens_est": 25},
        {"tool": "sonar_status", "ok": False, "duration_ms": 5.0, "input_tokens_est": 2, "output_tokens_est": 3},
    ])
    report = build_report(tmp_path, date(2026, 8, 25))
    assert report.total_calls == 3
    assert report.total_errors == 1
    git_stats = report.tools["git_repo_status"]
    assert git_stats.calls == 2
    assert git_stats.errors == 0
    assert git_stats.avg_duration_ms == 15.0
    assert git_stats.total_input_tokens_est == 10
    assert git_stats.total_output_tokens_est == 45
    sonar_stats = report.tools["sonar_status"]
    assert sonar_stats.errors == 1


def test_build_report_missing_day_returns_empty_report(tmp_path):
    report = build_report(tmp_path, date(2026, 1, 1))
    assert report.tools == {}
    assert report.total_calls == 0


def test_build_report_filters_by_tool(tmp_path):
    _write_day(tmp_path, "2026-08-25", [
        {"tool": "git_repo_status", "ok": True, "duration_ms": 10.0, "input_tokens_est": 1, "output_tokens_est": 1},
        {"tool": "sonar_status", "ok": True, "duration_ms": 10.0, "input_tokens_est": 1, "output_tokens_est": 1},
    ])
    report = build_report(tmp_path, date(2026, 8, 25), tool_filter="sonar_status")
    assert list(report.tools.keys()) == ["sonar_status"]


def test_build_report_skips_malformed_lines(tmp_path):
    day_dir = tmp_path / "2026-08-25"
    day_dir.mkdir(parents=True)
    (day_dir / "tool_calls.jsonl").write_text(
        '{"tool": "git_repo_status", "ok": true, "duration_ms": 1.0}\n'
        "not json at all\n"
        "\n",
        encoding="utf-8",
    )
    report = build_report(tmp_path, date(2026, 8, 25))
    assert report.total_calls == 1


def test_build_report_skips_records_missing_tool_field(tmp_path):
    day_dir = tmp_path / "2026-08-25"
    day_dir.mkdir(parents=True)
    (day_dir / "tool_calls.jsonl").write_text('{"ok": true}\n', encoding="utf-8")
    report = build_report(tmp_path, date(2026, 8, 25))
    assert report.tools == {}
