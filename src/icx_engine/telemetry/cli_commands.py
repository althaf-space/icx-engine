"""CLI front door for tool-call usage telemetry - own Typer app, mounted into cli.py with two
lines, same pattern as git/cli_commands.py."""
from __future__ import annotations

from datetime import date, datetime

import typer
from rich.console import Console
from rich.table import Table

from icx_engine.telemetry.logger import ToolCallLogger
from icx_engine.telemetry.report import build_report

logs_app = typer.Typer(help="Local MCP tool-call usage logs (~/.icx/logs/) - counts, durations, token estimates.",
                        rich_markup_mode="rich")
# width=200: a narrow/non-TTY terminal (CliRunner in tests, some CI shells) would otherwise
# make rich wrap tool names mid-word inside the table's "Tool" column.
console = Console(width=200)

# See git/cli_commands.py's identical import-order note - avoids a circular ImportError
# from cli.py importing this module before cli.py itself finishes defining _guarded.
import icx_engine.cli as _cli


def _guarded(fn):
    import functools

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return _cli._guarded(fn)(*args, **kwargs)
    return wrapper


def _parse_date(value: str | None) -> date:
    if not value:
        return datetime.now().date()
    return date.fromisoformat(value)


@logs_app.callback()
def _logs_app_callback() -> None:
    """Local MCP tool-call usage logs. Forces multi-command group mode even with one subcommand."""


@logs_app.command("report")
@_guarded
def logs_report(
    when: str = typer.Option(None, "--date", help="YYYY-MM-DD, defaults to today"),
    tool: str = typer.Option(None, "--tool", help="Scope to one tool name"),
    debug: _cli.DebugOpt = False,
    traceback: _cli.TracebackOpt = False,
) -> None:
    """Per-tool call count, error count, average duration, and estimated token cost for one day."""
    day = _parse_date(when)
    logger = ToolCallLogger()
    report = build_report(logger.root, day, tool_filter=tool)

    if not report.tools:
        scope = f" for '{tool}'" if tool else ""
        console.print(f"No logged tool calls{scope} on {report.day}.")
        return

    table = Table(title=f"Tool calls - {report.day}")
    table.add_column("Tool")
    table.add_column("Calls", justify="right")
    table.add_column("Errors", justify="right")
    table.add_column("Avg ms", justify="right")
    table.add_column("Input tok (est)", justify="right")
    table.add_column("Output tok (est)", justify="right")
    for stats in sorted(report.tools.values(), key=lambda s: s.calls, reverse=True):
        table.add_row(
            stats.tool, str(stats.calls), str(stats.errors),
            f"{stats.avg_duration_ms:.1f}",
            str(stats.total_input_tokens_est), str(stats.total_output_tokens_est),
        )
    console.print(table)
    console.print(f"Total: {report.total_calls} calls, {report.total_errors} errors.")
