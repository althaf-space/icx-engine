"""Pure aggregation over ToolCallLogger's JSONL output - no CLI/printing concerns here, see
cli_commands.py for presentation."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path


@dataclass
class ToolStats:
    tool: str
    calls: int = 0
    errors: int = 0
    total_duration_ms: float = 0.0
    total_input_tokens_est: int = 0
    total_output_tokens_est: int = 0

    @property
    def avg_duration_ms(self) -> float:
        return self.total_duration_ms / self.calls if self.calls else 0.0


@dataclass
class DailyReport:
    day: str
    tools: dict[str, ToolStats] = field(default_factory=dict)

    @property
    def total_calls(self) -> int:
        return sum(t.calls for t in self.tools.values())

    @property
    def total_errors(self) -> int:
        return sum(t.errors for t in self.tools.values())


def _read_records(path: Path):
    if not path.exists():
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def build_report(logs_root: Path, day: date, tool_filter: str | None = None) -> DailyReport:
    """Aggregates one day's tool_calls.jsonl into per-tool stats. Returns an empty (zero-tool)
    report - never raises - when the day's log file doesn't exist yet."""
    day_str = day.isoformat()
    path = logs_root / day_str / "tool_calls.jsonl"
    report = DailyReport(day=day_str)
    for record in _read_records(path):
        tool = record.get("tool")
        if not isinstance(tool, str):
            continue
        if tool_filter is not None and tool != tool_filter:
            continue
        stats = report.tools.setdefault(tool, ToolStats(tool=tool))
        stats.calls += 1
        if not record.get("ok", True):
            stats.errors += 1
        stats.total_duration_ms += float(record.get("duration_ms", 0) or 0)
        stats.total_input_tokens_est += int(record.get("input_tokens_est", 0) or 0)
        stats.total_output_tokens_est += int(record.get("output_tokens_est", 0) or 0)
    return report
