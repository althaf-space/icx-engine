from __future__ import annotations
import asyncio
import shlex
import shutil
import subprocess
import sys
import typer
from typing import Annotated, Optional
from rich.console import Console

from icx_engine.services.connection_service import _connect_jira_token, _connect_jira_oauth
from icx_engine.error_display import render_icx_error

try:
    from importlib.metadata import version as _pkg_version
    _ICX_VERSION = _pkg_version("icx-engine")
except Exception:
    _ICX_VERSION = "dev"


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"icx {_ICX_VERSION}")
        raise typer.Exit()


def _author_callback(value: bool) -> None:
    if value:
        try:
            from importlib.metadata import metadata as _meta
            m = _meta("icx-engine")
            author_email = m.get("Author-email", "")
            if author_email:
                typer.echo(f"Author: {author_email}")
            else:
                typer.echo(f"Author: {m.get('Author', 'unknown')}")
        except Exception:
            typer.echo("Author: see https://github.com/althaf-space/icx-engine")
        raise typer.Exit()


def _check_for_update() -> None:
    """Check PyPI for a newer version in a daemon thread to avoid blocking CLI startup."""
    import importlib.metadata
    import threading

    def _worker() -> None:
        try:
            import httpx
            r = httpx.get("https://pypi.org/pypi/icx-engine/json", timeout=1.5)
            r.raise_for_status()
            latest = r.json()["info"]["version"]
        except Exception:
            return

        if not latest:
            return

        try:
            current = importlib.metadata.version("icx-engine")
        except Exception:
            return

        def _ver(v: str) -> tuple:
            try:
                return tuple(int(x) for x in v.split(".")[:3])
            except Exception:
                return (0, 0, 0)

        if _ver(latest) > _ver(current):
            print(
                f"\n  ⬆  Update available: icx-engine {current} → {latest}\n"
                "     Run: pipx upgrade icx-engine   or   pip install --upgrade icx-engine\n",
                file=sys.stderr,
            )

    threading.Thread(target=_worker, daemon=True).start()


def _trigger_memory_setup() -> None:
    """Check memory sentinel on every CLI startup. Download model if needed (first-run only)."""
    try:
        from icx_engine.memory.embeddings import _is_initialized, EmbeddingsManager
        if not _is_initialized():
            EmbeddingsManager().ensure_ready(console=err_console)
    except Exception:
        pass  # memory setup failure never prevents other commands from running


_FULL_HELP = """
[bold]ICX: Integrated Contextual X-ecution Engine[/bold]
AI-native intelligence layer for development teams. Deep context extraction, local memory, MCP-powered execution.

[bold]Quick start:[/bold]  [cyan]icx connection --add[/cyan]  ->  [cyan]icx model --add[/cyan]  ->  [cyan]icx analyze <KEY>[/cyan]

[bold]Analysis[/bold]
  [cyan]icx analyze <KEY>[/cyan]                                      Analyze an issue
  [cyan]icx analyze <KEY> --fast[/cyan]                               Text-only - skip image processing
  [cyan]icx analyze <KEY> --profile <NAME>[/cyan]                     Use a specific LLM profile for this run
  [cyan]icx analyze <KEY> --profile <NAME> --fast[/cyan]              Profile + text-only (skip image processing)
  [cyan]icx analyze <KEY> --debug[/cyan]                              Show step-by-step debug output
  [cyan]icx analyze <KEY> --traceback[/cyan]                          Show full error traceback on failure

[bold]Connections[/bold]
  [cyan]icx connection --add[/cyan]                                   Connect a new platform
  [cyan]icx connection --remove <DOMAIN>[/cyan]                       Remove by domain  (e.g. mycompany.atlassian.net)
  [cyan]icx connection --remove <INDEX>[/cyan]                        Remove by index number from icx status
  [cyan]icx connection --active <DOMAIN>[/cyan]                       Set default connection
  [cyan]icx connection --active <INDEX>[/cyan]                        Set default by index number

[bold]LLM Profiles[/bold]
  [cyan]icx model --add[/cyan]                                        Configure an AI provider
  [cyan]icx model --remove <PROFILE>[/cyan]                           Remove an entire profile (by name)
  [cyan]icx model --remove <INDEX>[/cyan]                             Remove by index from icx status
  [cyan]icx model --remove <PROFILE> --channel <CHANNEL>[/cyan]       Remove only the image/vision channel
  [cyan]icx model --active <PROFILE>[/cyan]                           Set default profile (name or index)

[bold]Memory[/bold]
  [cyan]icx memory save <KEY>[/cyan]                                  Save a resolved issue to local memory
  [cyan]icx memory save <KEY> --note "..."[/cyan]                     Save non-interactively
  [cyan]icx memory search "<query>"[/cyan]                            Search past resolutions
  [cyan]icx memory list[/cyan]                                        List all saved entries (newest first)
  [cyan]icx memory list --project <KEY>[/cyan]                        Filter by project key
  [cyan]icx memory list --source <TYPE>[/cyan]                        Filter by connector type
  [cyan]icx memory show <KEY>[/cyan]                                  Show full detail for one entry
  [cyan]icx memory delete <KEY>[/cyan]                                Delete one entry
  [cyan]icx memory export[/cyan]                                      Export memory to JSON
  [cyan]icx memory export --output <FILE>[/cyan]                      Export to specific path
  [cyan]icx memory import <FILE>[/cyan]                               Import from a JSON export
  [cyan]icx memory clear --confirm[/cyan]                             Delete all entries
  [cyan]icx memory status[/cyan]                                      Show stats: entries, size, model info

[bold]MCP Server[/bold]
  [cyan]icx mcp run[/cyan]                                            Start the MCP server (stdio)
  [cyan]icx mcp setup[/cyan]                                          Register ICX with detected AI editors
  [cyan]icx mcp setup --host <HOST>[/cyan]                            Register with a specific editor only
  [cyan]icx mcp remove[/cyan]                                         Remove ICX from all detected editors
  [cyan]icx mcp remove --host <HOST>[/cyan]                           Remove from a specific editor only
  [cyan]icx mcp config[/cyan]                                         Print config snippets for all editors
  [cyan]icx mcp list[/cyan]                                           List supported editors and detection status

[bold]General[/bold]
  [cyan]icx status[/cyan]                                             Show all connections and LLM profiles
  [cyan]icx logout[/cyan]                                             Remove all credentials from this machine
  [cyan]icx uninstall[/cyan]                                          Fully remove ICX - data, credentials, editor configs, package
  [cyan]icx uninstall --yes[/cyan]                                    Skip confirmation prompt
  [cyan]icx --version[/cyan]                                          Show installed version
  [cyan]icx --help[/cyan]                                             Show this help

[dim]Run icx --install-completion once to enable tab completion in your shell.[/dim]
"""


def _print_full_help() -> None:
    from rich.console import Console as _RichConsole
    _RichConsole().print(_FULL_HELP)


def _help_callback(ctx: typer.Context, param, value: bool) -> None:
    if value:
        _print_full_help()
        raise typer.Exit()


app = typer.Typer(
    name="icx",
    context_settings={"max_content_width": 9999},
    help=(
        "ICX: Integrated Contextual X-ecution Engine - AI-native intelligence layer "
        "for development teams.\n\n"
        "[bold]Quick start:[/bold]  "
        "[cyan]icx connection --add[/cyan]  ->  "
        "[cyan]icx model --add[/cyan]  ->  "
        "[cyan]icx analyze <KEY>[/cyan]"
    ),
    no_args_is_help=False,
    rich_markup_mode="rich",
    epilog=(
        "[bold]AI editor integration:[/bold] "
        "Run [bold cyan]icx mcp --help[/bold cyan] to see the [bold]setup[/bold], [bold]remove[/bold], "
        "[bold]config[/bold], and [bold]run[/bold] subcommands for wiring ICX into "
        "Claude Code, Cursor, Windsurf, or Codex.\n\n"
        "[dim]Run [bold]icx --install-completion[/bold] once to enable tab completion in your shell.[/dim]"
    ),
)
mcp_app = typer.Typer(
    help=(
        "Register ICX in your AI editor so it works as an MCP tool.\n\n"
        "[bold]Subcommands:[/bold]\n\n"
        "  [bold]setup[/bold]   Wire ICX into Claude Code, Cursor, Windsurf, or Codex\n"
        "  [bold]remove[/bold]  Remove ICX from your editor's config\n"
        "  [bold]config[/bold]  Print copy-paste config snippets for all supported editors\n"
        "  [bold]run[/bold]     Start the MCP server (called automatically by your editor)"
    ),
    rich_markup_mode="rich",
)
app.add_typer(mcp_app, name="mcp", rich_help_panel="MCP Server")

memory_app = typer.Typer(help="Manage local memory - save resolved issues, search past fixes.", rich_markup_mode="rich")
app.add_typer(memory_app, name="memory", rich_help_panel="Memory")

console = Console()
err_console = Console(stderr=True)

# Shared options
DebugOpt = Annotated[bool, typer.Option("--debug", help="Print step-by-step progress to stderr.")]
TracebackOpt = Annotated[bool, typer.Option("--traceback", help="Print the full Python traceback on errors.")]


# ---------------------------------------------------------------------------
# Memory subcommands
# ---------------------------------------------------------------------------

@memory_app.command("save")
def memory_save(
    key: Annotated[str, typer.Argument(help="Issue key, e.g. PROJ-456")],
    note: Annotated[Optional[str], typer.Option("--note", help="Resolution note (non-interactive)")] = None,
    files: Annotated[Optional[str], typer.Option("--files", help="Comma-separated file paths changed")] = None,
    tags: Annotated[Optional[str], typer.Option("--tags", help="Comma-separated tags")] = None,
    confirmed: Annotated[bool, typer.Option("--confirmed", help="Skip test confirmation prompt")] = False,
    debug: DebugOpt = False,
) -> None:
    """Save a resolved issue to local memory after it has been fixed and tested."""
    import asyncio as _asyncio
    from icx_engine.config_manager import ConfigManager
    from icx_engine.engine import extract_domain, resolve_connection
    from icx_engine.connectors.base import get_connector
    from icx_engine.memory.manager import MemoryManager
    from icx_engine.memory.schema import MemoryEntry
    import uuid
    from datetime import datetime, timezone

    try:
        config = ConfigManager.load()
        domain = extract_domain(key)
        conn = resolve_connection(domain, config, raw_input=key)
        if conn is None:
            err_console.print("[red]Multiple connections found. Use a full URL to specify which.[/red]")
            raise typer.Exit(1)

        connector = get_connector(conn)
        parsed = connector.parse_input(key)

        console.print(f"\n[bold]Saving resolution for[/bold] [cyan]{parsed.issue_key}[/cyan]")
        raw = _asyncio.run(connector.fetch(parsed.issue_key, config))
        console.print(f'  Issue found: "[dim]{raw.summary[:72]}[/dim]"')
        console.print(f"  Type: {raw.issue_type}  |  Source: {conn.connector_type} ({conn.domain})\n")

        if note is None:
            note = typer.prompt("  What was the fix?")
        if files is None:
            files_raw = typer.prompt("  Files changed? (comma-separated, or Enter to skip)", default="")
        else:
            files_raw = files
        if tags is None:
            tags_raw = typer.prompt("  Tags? (comma-separated, or Enter to skip)", default="")
        else:
            tags_raw = tags
        if not confirmed:
            confirmed = typer.confirm("  Tested and confirmed resolved?", default=False)
            if not confirmed:
                console.print("[yellow]Save cancelled - mark as confirmed when the fix is verified.[/yellow]")
                raise typer.Exit(0)

        files_list = [f.strip() for f in files_raw.split(",") if f.strip()]
        tags_list = [t.strip() for t in tags_raw.split(",") if t.strip()]

        entry = MemoryEntry(
            id=str(uuid.uuid4()),
            issue_key=parsed.issue_key,
            project_key=parsed.issue_key.split("-")[0] if "-" in parsed.issue_key else parsed.issue_key,
            source_type=conn.connector_type,
            issue_type=raw.issue_type,
            summary=raw.summary,
            problem_description=raw.description[:2000],
            resolution_note=note,
            files_changed=files_list,
            resolution_confirmed=confirmed,
            saved_at=datetime.now(timezone.utc).isoformat(),
            tags=tags_list,
        )

        mgr = MemoryManager()
        mgr.save(entry)
        console.print(f"\n[green]Saved.[/green] {parsed.issue_key} will surface as a past insight for similar issues.")

    except typer.Exit:
        raise
    except Exception as exc:
        render_icx_error(exc, err_console)
        raise typer.Exit(1)


@memory_app.command("search")
def memory_search(
    query: Annotated[str, typer.Argument(help="Search query, e.g. 'OAuth token expires'")],
    top_k: Annotated[int, typer.Option("--top", help="Max results")] = 5,
) -> None:
    """Search past resolutions using semantic and keyword matching."""
    from icx_engine.memory.manager import MemoryManager
    from icx_engine.memory.schema import MemoryQueryInput

    try:
        mgr = MemoryManager()
        q = MemoryQueryInput(
            issue_key="search",
            project_key="",
            source_type="",
            summary=query,
            description=query,
            issue_type="",
        )
        insights = mgr.query(q, top_k=top_k)
        if not insights:
            console.print(f"  No results for: {query}")
            return
        console.print(f"\n  Memory search results for: [bold]{query}[/bold]\n")
        for i, ins in enumerate(insights, 1):
            console.print(f"  [bold]{i}.[/bold] [cyan]{ins.issue_key}[/cyan]  {ins.summary[:60]}   [dim]score: {ins.similarity_score:.2f}[/dim]")
            console.print(f'     "{ins.resolution_note[:80]}"')
            if ins.files_changed:
                console.print(f"     [dim]{', '.join(ins.files_changed[:3])}[/dim]")
            console.print()
        console.print(f"  {len(insights)} result(s). Run [cyan]icx memory show <KEY>[/cyan] for full detail.")
    except Exception as exc:
        render_icx_error(exc, err_console)
        raise typer.Exit(1)


@memory_app.command("list")
def memory_list(
    project: Annotated[Optional[str], typer.Option("--project", help="Filter by project key, e.g. PROJ")] = None,
    source: Annotated[Optional[str], typer.Option("--source", help="Filter by source type, e.g. jira, github")] = None,
) -> None:
    """List all saved memory entries, newest first."""
    from icx_engine.memory.manager import MemoryManager
    try:
        mgr = MemoryManager()
        entries = mgr.list_entries(project_key=project, source_type=source)
        if not entries:
            console.print("  No memory entries saved yet. Use [cyan]icx memory save <KEY>[/cyan] after resolving an issue.")
            return
        console.print(f"\n  [bold]{len(entries)} saved entries[/bold]\n")
        for e in entries:
            console.print(f"  [cyan]{e.issue_key}[/cyan]  {e.summary[:60]}  [dim]{e.saved_at[:10]}[/dim]")
    except Exception as exc:
        render_icx_error(exc, err_console)
        raise typer.Exit(1)


@memory_app.command("show")
def memory_show(
    key: Annotated[str, typer.Argument(help="Issue key, e.g. PROJ-456")],
) -> None:
    """Show full detail for one saved memory entry."""
    from icx_engine.memory.manager import MemoryManager
    try:
        mgr = MemoryManager()
        entry = mgr.show(key)
        if entry is None:
            console.print(f"  No memory entry found for [cyan]{key}[/cyan].")
            return
        console.print(f"\n  [bold]{entry.issue_key}[/bold] - {entry.summary}")
        console.print(f"  Type: {entry.issue_type}  |  Source: {entry.source_type}  |  Confirmed: {entry.resolution_confirmed}")
        console.print(f"  Saved: {entry.saved_at[:10]}\n")
        console.print(f"  [bold]Problem:[/bold] {entry.problem_description[:200]}")
        console.print(f"\n  [bold]Resolution:[/bold] {entry.resolution_note}")
        if entry.files_changed:
            console.print(f"\n  [bold]Files changed:[/bold]")
            for f in entry.files_changed:
                console.print(f"    {f}")
        if entry.tags:
            console.print(f"\n  [bold]Tags:[/bold] {', '.join(entry.tags)}")
    except Exception as exc:
        render_icx_error(exc, err_console)
        raise typer.Exit(1)


@memory_app.command("delete")
def memory_delete(
    key: Annotated[str, typer.Argument(help="Issue key to delete, e.g. PROJ-456")],
) -> None:
    """Delete one saved memory entry."""
    from icx_engine.memory.manager import MemoryManager
    try:
        confirmed = typer.confirm(f"Delete memory entry for {key}?", default=False)
        if not confirmed:
            console.print("Cancelled.")
            return
        mgr = MemoryManager()
        mgr.delete(key)
        console.print(f"[green]Deleted memory entry for {key}.[/green]")
    except Exception as exc:
        render_icx_error(exc, err_console)
        raise typer.Exit(1)


@memory_app.command("clear")
def memory_clear(
    confirm: Annotated[bool, typer.Option("--confirm", help="Required to delete all entries.")] = False,
) -> None:
    """Delete all memory entries. Requires --confirm."""
    from icx_engine.memory.manager import MemoryManager
    try:
        if not confirm:
            console.print("Add [bold]--confirm[/bold] to delete all memory entries.")
            return
        final = typer.confirm("This will delete ALL memory entries and cannot be undone. Continue?", default=False)
        if not final:
            console.print("Cancelled.")
            return
        mgr = MemoryManager()
        mgr.clear()
        console.print("[green]All memory entries cleared.[/green]")
    except Exception as exc:
        render_icx_error(exc, err_console)
        raise typer.Exit(1)


@memory_app.command("status")
def memory_status() -> None:
    """Show memory engine stats: entry count, storage size, model info."""
    from icx_engine.memory.manager import MemoryManager
    from icx_engine.memory.embeddings import EMBEDDING_MODEL, SENTINEL_PATH
    try:
        mgr = MemoryManager()
        stats = mgr.status()
        initialized = SENTINEL_PATH.exists()
        size_mb = stats["db_size_bytes"] / (1024 * 1024)

        console.print("\n  [bold]ICX Memory Engine[/bold]")
        console.print(f"  - Entries:    {stats['entry_count']}")
        console.print(f"  - Storage:    {stats['db_path']}  ({size_mb:.1f} MB)")
        console.print(f"  - Model:      {EMBEDDING_MODEL} (cached)")
        console.print(f"  - Ready:      {'Yes' if initialized else 'No - run any icx command to initialize'}")
    except Exception as exc:
        render_icx_error(exc, err_console)
        raise typer.Exit(1)


@memory_app.command("export")
def memory_export(
    output: Annotated[Optional[str], typer.Option("--output", help="Output file path")] = None,
) -> None:
    """Export all memory entries to a JSON file for sharing or backup."""
    from icx_engine.memory.manager import MemoryManager
    from icx_engine.memory.export import export_to_json
    from pathlib import Path
    from datetime import date

    try:
        default_filename = f"icx-memory-export-{date.today()}.json"
        if output:
            out_path = Path(output)
            if out_path.is_dir() or str(output).endswith(("/", "\\")):
                out_path = out_path / default_filename
        else:
            out_path = Path(default_filename)
        console.print(
            f"\n  [yellow]Warning:[/yellow] The export file contains issue summaries, resolution notes,\n"
            f"  and file paths. Review before sharing.\n\n"
            f"  Export to: [cyan]{out_path}[/cyan]"
        )
        confirmed = typer.confirm("", default=False)
        if not confirmed:
            console.print("Cancelled.")
            return
        mgr = MemoryManager()
        entries = mgr.list_entries()
        export_to_json(entries, out_path)
        console.print(f"[green]Exported {len(entries)} entries to {out_path}[/green]")
    except Exception as exc:
        render_icx_error(exc, err_console)
        raise typer.Exit(1)


@memory_app.command("import")
def memory_import_cmd(
    file: Annotated[str, typer.Argument(help="Path to a JSON export file")],
) -> None:
    """Import memory entries from a JSON export file."""
    from icx_engine.memory.manager import MemoryManager
    from icx_engine.memory.export import import_from_json
    from pathlib import Path

    try:
        in_path = Path(file)
        entries = import_from_json(in_path)
        console.print(f"  Found {len(entries)} entries in {in_path}.")
        confirmed = typer.confirm(f"Import all {len(entries)} entries?", default=True)
        if not confirmed:
            console.print("Cancelled.")
            return
        mgr = MemoryManager()
        for entry in entries:
            mgr.save(entry)
        console.print(f"[green]Imported {len(entries)} entries.[/green]")
    except Exception as exc:
        render_icx_error(exc, err_console)
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# Entry point - no subcommand → REPL
# ---------------------------------------------------------------------------

@app.callback(invoke_without_command=True, add_help_option=False)
def main(
    ctx: typer.Context,
    debug: DebugOpt = False,
    version: Annotated[Optional[bool], typer.Option(
        "--version", "-v",
        callback=_version_callback,
        is_eager=True,
        expose_value=False,
        help="Print the installed ICX version number and exit.",
    )] = None,
    author: Annotated[Optional[bool], typer.Option(
        "--author",
        callback=_author_callback,
        is_eager=True,
        expose_value=False,
        hidden=True,
    )] = None,
    help: Annotated[Optional[bool], typer.Option(
        "--help", "-h",
        callback=_help_callback,
        is_eager=True,
        expose_value=False,
        help="Show this help message.",
    )] = None,
) -> None:
    _check_for_update()
    if ctx.invoked_subcommand == "memory":
        _trigger_memory_setup()
    if ctx.invoked_subcommand is None:
        _print_full_help()
        raise typer.Exit()


# ---------------------------------------------------------------------------
# connection - unified connection management command
# ---------------------------------------------------------------------------

@app.command(rich_help_panel="Setup")
def connection(
    add: Annotated[bool, typer.Option(
        "--add",
        help="Walk through connecting a new Jira account (API token or browser OAuth).",
    )] = False,
    remove: Annotated[Optional[str], typer.Option(
        "--remove", metavar="DOMAIN/INDEX",
        help="Disconnect a Jira account. Pass domain or number from 'icx status'.",
    )] = None,
    active: Annotated[Optional[str], typer.Option(
        "--active", metavar="DOMAIN/INDEX",
        help="Set the default Jira account. Pass domain or number from 'icx status'.",
    )] = None,
    debug: DebugOpt = False,
    traceback: TracebackOpt = False,
) -> None:
    """Connect to Jira - add a new account, switch the active one, or remove one.

    \b
    Examples:
      icx connection --add                              Walk through connecting a Jira account
      icx connection --remove mycompany.atlassian.net   Delete that connection
      icx connection --remove 2                         Delete connection #2 (from 'icx status')
      icx connection --active mycompany.atlassian.net   Make that connection the default
    """
    try:
        if add:
            _connect_jira(debug=debug)
            return
        if remove is not None:
            from icx_engine.config_manager import ConfigManager
            from icx_engine import management
            config = ConfigManager.load()
            config = management.disconnect(config, remove)
            ConfigManager.save(config)
            console.print("[green]✓ Connection removed.[/green]")
            return
        if active is not None:
            from icx_engine.config_manager import ConfigManager
            from icx_engine import management
            config = ConfigManager.load()
            config = management.set_default_connection(config, active)
            ConfigManager.save(config)
            console.print("[green]✓ Active connection updated.[/green]")
            return
        console.print("Use --add, --remove, or --active. See [bold]icx connection --help[/bold].")
    except typer.Exit:
        raise
    except Exception as exc:
        render_icx_error(exc, err_console, show_traceback=traceback)
        raise typer.Exit(1)


def _connect_jira(debug: bool = False) -> None:
    method = typer.prompt(
        "\nChoose Jira connection method\n"
        "  1. API Token   email + token from id.atlassian.com  (recommended)\n"
        "  2. OAuth PKCE  browser login, needs OAuth app at developer.atlassian.com\n"
        "Enter number",
        default="1",
    )
    if method.strip() == "2":
        _connect_jira_oauth(debug=debug)
    else:
        _connect_jira_token(debug=debug)


_PROVIDERS = [
    ("ollama",    "Ollama / LM Studio  (local, free, no API key needed)"),
    ("nim",       "Nvidia NIM          (cloud, free tier at build.nvidia.com)"),
    ("openai",    "OpenAI              (cloud, paid)"),
    ("anthropic", "Claude / Anthropic  (cloud, paid)"),
    ("google",    "Google Gemini       (cloud, free tier + paid)"),
    ("xai",       "xAI Grok            (cloud, paid)"),
]

_DEFAULT_MODELS: dict[str, dict[str, str]] = {
    "ollama":    {"text": "llama3",                         "image": "llava"},
    "nim":       {"text": "deepseek-ai/deepseek-v3",        "image": "meta/llama-3.2-11b-vision-instruct"},
    "openai":    {"text": "gpt-4o",                         "image": "gpt-4o"},
    "anthropic": {"text": "claude-opus-4-5",                "image": "claude-opus-4-5"},
    "google":    {"text": "gemini-1.5-pro",                 "image": "gemini-1.5-flash"},
    "xai":       {"text": "grok-beta",                      "image": "grok-vision-beta"},
}


def _prompt_channel_config(label: str, provider_key: str | None = None) -> "ChannelConfig":
    """Interactively prompt for one channel's provider/model/key/url."""
    from icx_engine.models.config import ChannelConfig

    typer.echo(f"\n── {label} ──")
    if provider_key is None:
        for i, (_, lbl) in enumerate(_PROVIDERS, 1):
            typer.echo(f"  {i}. {lbl}")
        choice = typer.prompt("Select provider", default="1")
        try:
            idx = int(choice.strip()) - 1
            provider_key, _ = _PROVIDERS[idx]
        except (ValueError, IndexError):
            err_console.print("Invalid choice.")
            raise typer.Exit(1)

    model_default = _DEFAULT_MODELS[provider_key]["text" if "Text" in label else "image"]
    model = typer.prompt("Model", default=model_default)

    api_key: str | None = None
    base_url: str | None = None

    if provider_key == "ollama":
        custom_url = typer.prompt("Ollama base URL", default="http://localhost:11434/v1")
        base_url = None if custom_url == "http://localhost:11434/v1" else custom_url
    elif provider_key == "nim":
        api_key = typer.prompt("Nvidia NIM API key", hide_input=True).strip()
        custom_url = typer.prompt("NIM base URL", default="https://integrate.api.nvidia.com/v1")
        if custom_url.startswith("http://"):
            render_icx_error(ValueError("NIM base URL must use HTTPS."), err_console)
            raise typer.Exit(1)
        base_url = None if custom_url == "https://integrate.api.nvidia.com/v1" else custom_url
    else:
        provider_label = next(lbl for k, lbl in _PROVIDERS if k == provider_key)
        api_key = typer.prompt(f"{provider_label} API key", hide_input=True).strip()

    return ChannelConfig(provider=provider_key, model=model, api_key=api_key, base_url=base_url)


def _prompt_vision_channel(text_config: "ChannelConfig") -> "ChannelConfig | None":
    """Prompt for vision channel choice: same provider / different / skip."""
    from icx_engine.models.config import ChannelConfig

    typer.echo(
        "\nConfigure vision channel?\n"
        "  1. Same provider as text  (reuses key & URL, just pick image model)\n"
        "  2. Different provider     (full setup for image channel)\n"
        "  3. Skip - OCR only\n"
    )
    choice = typer.prompt("Select", default="1").strip()
    if choice == "3":
        return None
    if choice == "2":
        return _prompt_channel_config("Visual Intelligence", provider_key=None)
    # Same provider
    image_model = typer.prompt(
        "Image model",
        default=_DEFAULT_MODELS[text_config.provider]["image"],
    )
    return ChannelConfig(
        provider=text_config.provider,
        model=image_model,
        api_key=text_config.api_key,
        base_url=text_config.base_url,
    )


def _validate_and_save_model(
    profile_name: str,
    new_llm: "LLMConfig",
    config: "AppConfig",
    debug: bool,
    traceback: bool,
) -> None:
    """Validate text then image channel separately; offer skip on image failure."""
    import asyncio
    from icx_engine.llm.base import get_provider
    from icx_engine.models.output import RawIssueData
    from icx_engine.config_manager import ConfigManager
    from icx_engine.models.config import LLMConfig

    test_raw = RawIssueData(
        issue_key="TEST-1", issue_type="Bug", summary="test", description="test",
        comments=[], attachments=[], priority="High", status="Open", metadata={},
    )

    async def _run(channel_config):
        await get_provider(channel_config).analyze(test_raw)

    # ── Text model: hard fail ─────────────────────────────────────────────────
    try:
        txt_label = new_llm.text_config.model
        if debug:
            typer.echo(f"  validating text model ({txt_label})...", err=True)
            asyncio.run(_run(new_llm.text_config))
        else:
            with console.status(f"[bold]Validating text model ({txt_label})…[/bold]", spinner="dots"):
                asyncio.run(_run(new_llm.text_config))
    except Exception as exc:
        render_icx_error(exc, err_console, show_traceback=traceback)
        raise typer.Exit(1)

    # ── Image model: soft fail - offer skip ───────────────────────────────────
    active_llm = new_llm
    if new_llm.image_config is not None:
        img_model = new_llm.image_config.model
        try:
            if debug:
                typer.echo(f"  validating image model ({img_model})...", err=True)
                asyncio.run(_run(new_llm.image_config))
            else:
                with console.status(
                    f"[bold]Validating image model ({img_model})…[/bold]", spinner="dots"
                ):
                    asyncio.run(_run(new_llm.image_config))
        except Exception as exc:
            err_console.print(f"\n[yellow]Image model validation failed:[/yellow] {exc}")
            skip = typer.confirm(
                f"  {img_model} is not yet available. Skip vision and use OCR only?",
                default=True,
            )
            if not skip:
                raise typer.Exit(1)
            active_llm = LLMConfig(
                text_config=new_llm.text_config,
                image_config=None,
            )

    # ── Save + confirm ─────────────────────────────────────────────────────────
    is_first = not config.llm_profiles
    new_profiles = dict(config.llm_profiles)
    new_profiles[profile_name] = active_llm
    updated = config.model_copy(update={
        "llm_profiles": new_profiles,
        "current_llm_profile": profile_name if is_first else config.current_llm_profile,
    })
    ConfigManager.save(updated)
    ConfigManager.warn_if_plaintext()

    tc = active_llm.text_config
    ic = active_llm.image_config
    key_display = (
        (tc.api_key[:4] + "..." + tc.api_key[-4:]) if tc.api_key and len(tc.api_key) > 8 else "(local)"
    )
    img_display = f" + {ic.provider}/{ic.model}" if ic else " (OCR only)"
    console.print(
        f"[green]✓ Profile '{profile_name}' saved. "
        f"Text: {tc.provider}/{tc.model} key={key_display}{img_display}[/green]"
    )


@app.command(rich_help_panel="Setup")
def model(
    target: Annotated[Optional[str], typer.Argument(
        help="Profile name or number from 'icx status'."
    )] = None,
    add: Annotated[bool, typer.Option(
        "--add",
        help="Add a new AI provider profile.",
    )] = False,
    active: Annotated[Optional[str], typer.Option(
        "--active", metavar="PROFILE",
        help="Set the active AI profile. Pass name or number from 'icx status'.",
    )] = None,
    remove: Annotated[Optional[str], typer.Option(
        "--remove", metavar="PROFILE",
        help="Delete an AI profile. Add --channel image to remove only the image channel.",
    )] = None,
    channel: Annotated[Optional[str], typer.Option(
        "--channel", metavar="text|image",
        help="With --remove: channel to remove. 'image' removes only the vision channel; 'text' removes the entire profile.",
    )] = None,
    debug: DebugOpt = False,
    traceback: TracebackOpt = False,
) -> None:
    """Configure which AI model ICX uses - add providers, switch profiles, or remove them.

    \b
    Each profile has two optional channels:
      TEXT   - the main LLM that reads the issue and writes the structured summary
      IMAGE  - a separate vision model for screenshots and image attachments (optional)

    \b
    Examples:
      icx model --add                          Set up a new AI profile from scratch
      icx model work --add                     Add the missing vision channel to 'work'
      icx model --active work                  Make 'work' the default profile
      icx model --active 2                     Make profile #2 the default
      icx model --remove work                  Delete the entire 'work' profile
      icx model --remove 2                     Delete profile #2 (from 'icx status')
      icx model --remove work --channel image   Remove only the image channel from 'work'
    """
    from icx_engine.config_manager import ConfigManager
    from icx_engine.models.config import LLMConfig
    from icx_engine import management

    try:
        # ── --add ──────────────────────────────────────────────────────────────
        if add:
            config = ConfigManager.load()
            profile_name = target

            # Resolve index to name if given
            if profile_name and profile_name.isdigit():
                names = list(config.llm_profiles.keys())
                idx = int(profile_name) - 1
                if 0 <= idx < len(names):
                    profile_name = names[idx]
                else:
                    render_icx_error(ValueError(f"Profile index {target} is out of range."), err_console)
                    raise typer.Exit(1)

            existing = config.llm_profiles.get(profile_name) if profile_name else None

            if profile_name and profile_name not in config.llm_profiles:
                from rich.panel import Panel
                err_console.print(Panel(
                    f"[bold]What:[/bold] Profile '{profile_name}' not found.\n"
                    "[bold]How:[/bold]  Run `icx status` to see existing profiles, "
                    "or `icx model --add` (no name) to create a new one.",
                    title="[bold red]ICX Error[/bold red]", border_style="red",
                ))
                raise typer.Exit(1)

            needs_text = existing is None or existing.text_config is None
            needs_image = existing is None or existing.image_config is None

            if existing and existing.text_config and existing.image_config:
                overwrite = typer.confirm(
                    f"Profile '{profile_name}' is already fully configured. Overwrite?", default=False
                )
                if not overwrite:
                    typer.echo("Cancelled.")
                    return
                needs_text = needs_image = True

            if not profile_name:
                profile_name = typer.prompt("Profile name (e.g. work, personal)").strip()
                if not profile_name:
                    err_console.print("Profile name cannot be empty.")
                    raise typer.Exit(1)

            text_config = _prompt_channel_config("Text Intelligence (mandatory)") if needs_text else existing.text_config
            image_config = _prompt_vision_channel(text_config) if needs_image else existing.image_config

            new_llm = LLMConfig(text_config=text_config, image_config=image_config)
            _validate_and_save_model(profile_name, new_llm, config, debug, traceback)
            return

        # ── --active ───────────────────────────────────────────────────────────
        if active is not None:
            config = ConfigManager.load()
            config = management.use_ai_profile(config, active)
            ConfigManager.save(config)
            console.print("[green]✓ Active AI profile updated.[/green]")
            return

        # ── --remove ───────────────────────────────────────────────────────────
        if remove is not None:
            config = ConfigManager.load()
            if channel is not None:
                config = management.unset_llm_channel(config, remove, channel)
                if channel.lower() == "image":
                    console.print("[green]✓ Image channel removed from profile.[/green]")
                else:
                    console.print("[green]✓ AI profile removed.[/green]")
            else:
                config = management.unset_llm_profile(config, remove)
                console.print("[green]✓ AI profile removed.[/green]")
            ConfigManager.save(config)
            return

        # ── bare target - set active ───────────────────────────────────────────
        if target is not None:
            config = ConfigManager.load()
            config = management.use_ai_profile(config, target)
            ConfigManager.save(config)
            console.print("[green]✓ Active AI profile updated.[/green]")
            return

        console.print("Use --add, --active, or --remove. See [bold]icx model --help[/bold].")

    except typer.Exit:
        raise
    except Exception as exc:
        render_icx_error(exc, err_console, show_traceback=traceback)
        raise typer.Exit(1)


def _render_past_insights(insights: list) -> None:
    """Render the Past Insights Rich panel after analyze output."""
    from rich.panel import Panel
    from rich.text import Text

    body = Text()
    for ins in insights:
        body.append(f"\n  {ins.issue_key}  ", style="bold cyan")
        body.append(f"{ins.summary[:55]}  ", style="white")
        body.append(f"score: {ins.similarity_score:.2f}\n", style="dim")
        note_preview = ins.resolution_note[:120]
        body.append(f'  "{note_preview}"\n', style="white")
        if ins.files_changed:
            for f in ins.files_changed[:3]:
                body.append(f"  {f}\n", style="dim")
        body.append(f"  Saved: {ins.saved_at[:10]}\n", style="dim")

    console.print(
        Panel(
            body,
            title=f"[bold green]Past Insights  {len(insights)} similar issues found[/bold green]",
        )
    )


@app.command(rich_help_panel="Analysis")
def analyze(
    url: Annotated[str, typer.Argument(
        help=(
            "The Jira issue to analyze. Can be a full URL "
            "(https://company.atlassian.net/browse/ABC-123) or just the issue key (ABC-123)."
        )
    )],
    debug: DebugOpt = False,
    traceback: TracebackOpt = False,
    profile: Annotated[
        Optional[str],
        typer.Option(
            "--profile",
            help="Use this AI profile for this request (does not change your default).",
            metavar="NAME",
        ),
    ] = None,
    fast: Annotated[
        bool,
        typer.Option(
            "--fast",
            help="Skip image processing for speed. Images are listed in pending_images.",
        ),
    ] = False,
) -> None:
    """Fetch a Jira issue and analyze it - prints structured JSON ready for your AI tools.

    \b
    ICX reads the issue title, description, comments, and attachments, then asks your
    configured AI model to produce a structured summary with requirements, context,
    and any missing information it detected.

    \b
    If you have a vision model configured, ICX also reads screenshots and image
    attachments when it needs more confidence in its analysis.

    \b
    Examples:
      icx analyze ABC-123
      icx analyze ABC-123 --fast
      icx analyze https://company.atlassian.net/browse/ABC-123
      icx analyze ABC-123 --profile work
      icx analyze ABC-123 --debug           (show step-by-step what ICX is doing)
    """
    from icx_engine.config_manager import ConfigManager
    from icx_engine import engine
    from icx_engine.engine import extract_domain, resolve_connection, narrow_connections
    from icx_engine.models.output import IssueContext
    from icx_engine.exceptions import (
        NoConnectionError, InvalidInput, ICXError,
    )

    config = ConfigManager.load()

    if profile is not None and profile not in config.llm_profiles:
        from icx_engine.exceptions import ManagementError
        available = ", ".join(sorted(config.llm_profiles)) or "none"
        render_icx_error(ManagementError(f"Profile '{profile}' not found. Available: {available}. Use --profile with a configured profile name."), err_console, show_traceback=traceback)
        raise typer.Exit(1)

    debug_console = None
    if debug:
        from rich.console import Console as _RichConsole
        debug_console = _RichConsole(stderr=True)

        def _log(msg: str) -> None:
            debug_console.print(msg)

        _log(f"[debug] analyzing {url}")
    else:
        _log = None

    try:
        domain = extract_domain(url)
        conn = resolve_connection(domain, config, raw_input=url)

        if conn is None:
            pool = narrow_connections(config.connections, url)
            choices = "\n".join(
                f"  {i + 1}. [{c.connector_type}] {c.domain}"
                for i, c in enumerate(pool)
            )
            raw_choice = typer.prompt(f"Which connection?\n{choices}\nEnter number")
            try:
                conn = pool[int(raw_choice.strip()) - 1]
            except (ValueError, IndexError):
                err_console.print("Invalid choice.")
                raise typer.Exit(1)

        def _run() -> object:
            kwargs: dict = dict(log=_log, profile_override=profile, skip_vision=fast)
            if debug_console is not None:
                kwargs["debug_console"] = debug_console
            return asyncio.run(engine.run(url, config, connection=conn, **kwargs))

        if debug:
            result = _run()
        else:
            with console.status(f"[bold]Analyzing {url}…[/bold]", spinner="dots"):
                result = _run()

    except ICXError as exc:
        render_icx_error(exc, err_console, show_traceback=traceback)
        raise typer.Exit(1)
    except Exception as exc:
        render_icx_error(exc, err_console, show_traceback=traceback)
        raise typer.Exit(1)

    typer.echo(result.model_dump_json(indent=2))

    if isinstance(result, IssueContext) and result.past_insights:
        _render_past_insights(result.past_insights)

    if isinstance(result, IssueContext) and result.missing_information:
        err_console.print("\n⚠ MISSING REQUIREMENTS")
        for item in result.missing_information:
            err_console.print(f"  • {item}")


@app.command(rich_help_panel="Setup")
def status(
    debug: DebugOpt = False,
    traceback: TracebackOpt = False,
) -> None:
    """Show all connected Jira accounts and configured AI profiles at a glance."""
    from icx_engine.config_manager import ConfigManager
    from rich.table import Table

    try:
        config = ConfigManager.load()

        # ── Connections ────────────────────────────────────────────────────────
        conn_table = Table(title="Connections", show_header=True, header_style="bold cyan")
        conn_table.add_column("#", style="dim", width=3)
        conn_table.add_column("Platform")
        conn_table.add_column("Domain")
        conn_table.add_column("Auth")
        conn_table.add_column("Identity")
        conn_table.add_column("Active")
        for i, conn in enumerate(config.connections, 1):
            conn_key = f"{conn.connector_type}:{conn.domain}"
            auth = getattr(conn, "auth", None)
            auth_type = getattr(auth, "auth_type", "") if auth else ""
            auth_display = auth_type.title() if auth_type else conn.connector_type.title()
            identity = ""
            if hasattr(auth, "email"):
                identity = auth.email
            elif auth_type == "oauth":
                identity = "OAuth PKCE"
            active_cell = "[bold green][ACTIVE][/bold green]" if conn_key == config.default_connection else ""
            conn_table.add_row(str(i), conn.connector_type.title(), conn.domain, auth_display, identity, active_cell)
        if not config.connections:
            conn_table.caption = "Run [bold]icx connection --add[/bold] to add one."
        console.print(conn_table)

        console.print()

        # ── AI Profiles ────────────────────────────────────────────────────────
        profile_table = Table(title="AI Profiles", show_header=True, header_style="bold cyan")
        profile_table.add_column("#", style="dim", width=3)
        profile_table.add_column("Profile")
        profile_table.add_column("Channel", width=7)
        profile_table.add_column("Provider")
        profile_table.add_column("Model")
        profile_table.add_column("Key", width=14)
        profile_table.add_column("Base URL")
        profile_table.add_column("Active")

        def _mask(key: str | None) -> str:
            if not key:
                return "[dim](local)[/dim]"
            return f"{key[:4]}...{key[-4:]}" if len(key) > 8 else "****"

        def _url(u: str | None) -> str:
            return u if u else "[dim](default)[/dim]"

        for i, (name, profile) in enumerate(config.llm_profiles.items(), 1):
            is_active = name == config.current_llm_profile
            active_cell = "[bold green][ACTIVE][/bold green]" if is_active else ""
            tc = profile.text_config
            profile_table.add_row(
                str(i), name, "[bold]TEXT[/bold]",
                tc.provider.title(), tc.model, _mask(tc.api_key), _url(tc.base_url), active_cell,
            )
            if profile.image_config:
                ic = profile.image_config
                profile_table.add_row(
                    "", "", "[bold]IMAGE[/bold]",
                    ic.provider.title(), ic.model, _mask(ic.api_key), _url(ic.base_url), "",
                )
            else:
                profile_table.add_row("", "", "[dim]IMAGE[/dim]", "[dim]-[/dim]", "[dim]OCR only[/dim]", "", "", "")

        if not config.llm_profiles:
            profile_table.caption = "Run [bold]icx model --add[/bold] to configure one."
        console.print(profile_table)

    except Exception as exc:
        render_icx_error(exc, err_console, show_traceback=traceback)
        raise typer.Exit(1)


@app.command(rich_help_panel="Setup")
def logout(
    debug: DebugOpt = False,
    traceback: TracebackOpt = False,
) -> None:
    """Wipe all saved credentials - removes every Jira account and AI API key from this machine."""
    from icx_engine.config_manager import ConfigManager
    from icx_engine.models.config import AppConfig
    try:
        confirmed = typer.confirm("Remove all connections and AI profiles?")
        if not confirmed:
            typer.echo("Cancelled.")
            return
        current = ConfigManager.load()
        ConfigManager.delete_all_secrets(current)
        ConfigManager.save(AppConfig())
        console.print("[green]✓ All credentials removed.[/green]")
    except Exception as exc:
        render_icx_error(exc, err_console, show_traceback=traceback)
        raise typer.Exit(1)



def _build_uninstall_cmd() -> list[str]:
    """Return the correct pip/pipx command to uninstall icx-engine on this machine."""
    if shutil.which("pipx") and "pipx" in sys.executable.lower():
        return ["pipx", "uninstall", "icx-engine"]
    if shutil.which("pipx"):
        result = subprocess.run(
            ["pipx", "list", "--short"], capture_output=True, text=True
        )
        if "icx-engine" in result.stdout:
            return ["pipx", "uninstall", "icx-engine"]
    return [sys.executable, "-m", "pip", "uninstall", "-y", "icx-engine"]


def _uninstall_package(console: Console) -> None:
    """Uninstall icx-engine, working around the Windows running-exe lock.

    On Windows, pip cannot move icx.exe while it is the current process.
    Fix: write a hidden PowerShell script that waits 3 s for this process to
    exit, then runs the uninstall, then deletes itself. On Unix the exe lock
    does not exist so we run the uninstall directly.
    """
    import tempfile

    cmd = _build_uninstall_cmd()

    if sys.platform != "win32":
        subprocess.run(cmd, check=True)
        console.print("[bold green]✓ ICX fully removed. Goodbye![/bold green]\n")
        return

    def _ps_quote(arg: str) -> str:
        return '"' + arg.replace('"', '`"') + '"'

    cmd_str = " ".join(_ps_quote(c) for c in cmd)
    ps_script = (
        "Start-Sleep -Seconds 3\n"
        f"& {cmd_str}\n"
        "Remove-Item $PSCommandPath -Force -ErrorAction SilentlyContinue\n"
    )
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".ps1", delete=False, encoding="utf-8"
    ) as tf:
        tf.write(ps_script)
        script_path = tf.name

    subprocess.Popen(
        ["powershell", "-NonInteractive", "-WindowStyle", "Hidden", "-File", script_path],
        creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW,
        close_fds=True,
    )
    console.print(
        "[bold green]✓ Data and configs removed.[/bold green]\n"
        "[dim]Package uninstall running in background — completes in ~3 seconds.[/dim]\n"
        "[dim]You can close this window.[/dim]\n"
    )


@app.command(rich_help_panel="Setup")
def uninstall(
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation prompt.")] = False,
    traceback: TracebackOpt = False,
) -> None:
    """Fully remove ICX — wipes all data, credentials, editor configs, then uninstalls the package.

    \b
    Removes:
      • ~/.icx/             (config, memory database, embedding model)
      • Keyring secrets     (all stored API keys and Jira tokens)
      • MCP editor configs  (Claude Code, Cursor, Windsurf, Codex)
      • icx-engine package  (via pipx or pip)

    Run this instead of bare 'pip uninstall' to leave nothing behind.
    """
    from pathlib import Path
    from icx_engine.config_manager import ConfigManager
    from icx_engine.mcp_hosts import detect_installed_hosts, remove_icx_entry

    icx_dir = Path.home() / ".icx"

    console.print("\n[bold red]ICX Uninstall[/bold red]\n")
    console.print("This will permanently remove:\n")
    console.print(f"  [dim]•[/dim] [cyan]{icx_dir}[/cyan]  (config, memory, embedding model)")
    console.print("  [dim]•[/dim] All stored API keys and tokens from system keyring")
    console.print("  [dim]•[/dim] ICX entry from all detected AI editor configs")
    console.print("  [dim]•[/dim] [bold]icx-engine[/bold] package\n")

    if not yes:
        confirmed = typer.confirm("Proceed with full uninstall?")
        if not confirmed:
            typer.echo("Cancelled.")
            return

    errors: list[str] = []

    # 1. Wipe credentials from keyring
    try:
        current = ConfigManager.load()
        ConfigManager.delete_all_secrets(current)
        console.print("[green]✓[/green] Keyring secrets removed.")
    except Exception as exc:
        errors.append(f"Keyring: {exc}")
        console.print(f"[yellow]⚠[/yellow] Keyring cleanup failed: {exc}")

    # 2. Remove ICX from MCP editor configs
    try:
        hosts = detect_installed_hosts()
        removed_hosts = [h for h in hosts if remove_icx_entry(h)]
        if removed_hosts:
            for h in removed_hosts:
                console.print(f"[green]✓[/green] Removed from {h.config_path}")
        else:
            console.print("[dim]  No editor MCP configs found to clean.[/dim]")
    except Exception as exc:
        errors.append(f"MCP hosts: {exc}")
        console.print(f"[yellow]⚠[/yellow] Editor config cleanup failed: {exc}")

    # 3. Delete ~/.icx/ entirely
    try:
        if icx_dir.exists():
            shutil.rmtree(icx_dir)
            console.print(f"[green]✓[/green] Deleted {icx_dir}")
        else:
            console.print(f"[dim]  {icx_dir} not found — nothing to delete.[/dim]")
    except Exception as exc:
        errors.append(f"~/.icx/: {exc}")
        console.print(f"[yellow]⚠[/yellow] Failed to delete {icx_dir}: {exc}")

    # 4. Uninstall the package — detect pipx vs pip, handle Windows exe-lock
    console.print("\nUninstalling [bold]icx-engine[/bold] package…")
    try:
        _uninstall_package(console)
    except Exception as exc:
        console.print(
            "[yellow]⚠[/yellow] Package uninstall failed. Run manually:\n"
            "  [cyan]pipx uninstall icx-engine[/cyan]  or  "
            "[cyan]pip uninstall -y icx-engine[/cyan]\n"
        )
        if traceback:
            raise typer.Exit(1) from exc


HostOpt = Annotated[
    str | None,
    typer.Option(
        "--host",
        help="Editor to target: claude, cursor, windsurf, codex, antigravity.",
    ),
]

_JSON_SNIPPET = """\
{
  "mcpServers": {
    "icx": {
      "command": "icx",
      "args": ["mcp", "run"]
    }
  }
}"""

_TOML_SNIPPET = """\
[mcp_servers.icx]
command = "icx"
args = ["mcp", "run"]"""


@mcp_app.command("list")
def mcp_list(
    debug: DebugOpt = False,
    traceback: TracebackOpt = False,
) -> None:
    """List all supported MCP hosts and whether they are detected on this machine."""
    from icx_engine.mcp_hosts import list_hosts, detect_installed_hosts
    from rich.table import Table

    try:
        all_hosts = list_hosts()
        installed_names = {h.name for h in detect_installed_hosts()}

        table = Table(show_header=True, header_style="bold cyan")
        table.add_column("Host", style="cyan", width=14)
        table.add_column("Label")
        table.add_column("Detected", width=10)
        table.add_column("Config path")

        for h in all_hosts:
            detected = h.name in installed_names
            detected_cell = "[green]yes[/green]" if detected else "[dim]no[/dim]"
            table.add_row(h.name, h.label, detected_cell, str(h.config_path))

        console.print()
        console.print(table)
        console.print("\n  Use [cyan]icx mcp setup --host <HOST>[/cyan] to register with a specific editor.")
    except Exception as exc:
        render_icx_error(exc, err_console, show_traceback=traceback)
        raise typer.Exit(1)


@mcp_app.command("config")
def mcp_config(
    debug: DebugOpt = False,
    traceback: TracebackOpt = False,
) -> None:
    """Print the JSON/TOML config snippets you need to paste into your AI editor manually.

    Also shows the exact config file path for each supported editor so you know where to paste it.
    If you'd rather have ICX do it automatically, use [bold]icx mcp setup[/bold] instead.
    """
    from icx_engine.mcp_hosts import list_hosts

    try:
        typer.echo("\n--- Standard (Claude Code / Cursor / Windsurf / Antigravity) ---\n")
        typer.echo(_JSON_SNIPPET)
        typer.echo("\n--- Codex (TOML) ---\n")
        typer.echo(_TOML_SNIPPET)
        typer.echo("\nConfig file locations:")
        for h in list_hosts():
            typer.echo(f"  {h.label:<14} {h.config_path}")
    except Exception as exc:
        render_icx_error(exc, err_console, show_traceback=traceback)
        raise typer.Exit(1)


@mcp_app.command("setup")
def mcp_setup(host: HostOpt = None, debug: DebugOpt = False, traceback: TracebackOpt = False) -> None:
    """Wire ICX into your AI editor so it appears as an available MCP tool.

    \b
    ICX detects which AI editors are installed on your machine and adds itself to
    each one automatically. After running this, restart your editor and ICX will
    appear in its list of available tools.

    \b
    Examples:
      icx mcp setup                  Auto-detect and configure all installed editors
      icx mcp setup --host claude    Configure Claude Code only
      icx mcp setup --host cursor    Configure Cursor only
    """
    from icx_engine.mcp_hosts import get_host, detect_installed_hosts, write_icx_entry

    try:
        if host is not None:
            target = get_host(host)
            if target is None:
                err_console.print(
                    f"Unknown host '{host}'. Valid options: claude, cursor, windsurf, codex, antigravity"
                )
                raise typer.Exit(1)
            targets = [target]
        else:
            targets = detect_installed_hosts()
            if not targets:
                typer.echo("No known MCP host config directories detected.")
                typer.echo("Specify one with --host claude | cursor | windsurf | codex | antigravity | cline")
                raise typer.Exit(1)

        for target in targets:
            result = write_icx_entry(target)
            if result.fallback:
                typer.echo(
                    f"  {target.label}: not detected - wrote fallback config to {result.path}"
                )
            else:
                console.print(f"[green]✓ ICX entry written to {result.path}[/green]")
    except typer.Exit:
        raise
    except Exception as exc:
        render_icx_error(exc, err_console, show_traceback=traceback)
        raise typer.Exit(1)


@mcp_app.command("remove")
def mcp_remove(host: HostOpt = None, debug: DebugOpt = False, traceback: TracebackOpt = False) -> None:
    """Remove ICX from your AI editor's config - undoes what [bold]icx mcp setup[/bold] did.

    \b
    Examples:
      icx mcp remove                 Remove ICX from all detected editors
      icx mcp remove --host claude   Remove from Claude Code only
    """
    from icx_engine.mcp_hosts import get_host, detect_installed_hosts, remove_icx_entry

    try:
        if host is not None:
            target = get_host(host)
            if target is None:
                err_console.print(
                    f"Unknown host '{host}'. Valid options: claude, cursor, windsurf, codex, antigravity"
                )
                raise typer.Exit(1)
            targets = [target]
        else:
            targets = detect_installed_hosts()
            if not targets:
                typer.echo("No known MCP host config directories detected.")
                return

        for target in targets:
            removed = remove_icx_entry(target)
            if removed:
                console.print(f"[green]✓ ICX entry removed from {target.config_path}[/green]")
            else:
                typer.echo(f"No ICX entry found in {target.config_path}")
    except typer.Exit:
        raise
    except Exception as exc:
        render_icx_error(exc, err_console, show_traceback=traceback)
        raise typer.Exit(1)


@mcp_app.command("run")
def mcp_run(debug: DebugOpt = False) -> None:
    """Start the ICX MCP server over stdio.

    Your AI editor calls this automatically in the background - you do not need to run it yourself.
    It is listed here for transparency and for advanced debugging.
    """
    from icx_engine.mcp_server import run_mcp_server
    run_mcp_server()


# ---------------------------------------------------------------------------
# REPL
# ---------------------------------------------------------------------------

def _start_repl(debug: bool = False) -> None:
    """Interactive REPL - re-enters Typer for each command line."""
    console.print("[bold]ICX Interactive Mode[/bold] - type [italic]exit[/italic] to quit.")
    while True:
        try:
            line = input("icx> ").strip()
        except (EOFError, KeyboardInterrupt):
            typer.echo("")
            break
        if not line:
            continue
        if line in ("exit", "quit"):
            break
        try:
            args = shlex.split(line)
        except ValueError as exc:
            err_console.print(f"Parse error: {exc}")
            continue
        try:
            app(args, standalone_mode=False)
        except SystemExit:
            pass
        except Exception as exc:
            if debug:
                raise
            err_console.print(f"Error: {exc}")
