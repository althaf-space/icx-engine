from __future__ import annotations
import asyncio
import shlex
import shutil
import subprocess
import sys
import tempfile
import typer
from pathlib import Path
from typing import Annotated, Optional
from rich.console import Console

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


def _version_tuple(v: str) -> tuple:
    """Parse leading digits of each of the first 3 version segments so
    pre-release suffixes (e.g. "0.4.0rc1") compare without raising."""
    out: list[int] = []
    for seg in v.split(".")[:3]:
        digits = ""
        for ch in seg:
            if not ch.isdigit():
                break
            digits += ch
        out.append(int(digits) if digits else 0)
    return tuple(out)


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

        if _version_tuple(latest) > _version_tuple(current):
            print(
                f"\n  ^  Update available: icx-engine {current} -> {latest}\n"
                "     Run: pipx upgrade icx-engine   or   pip install --upgrade icx-engine\n",
                file=sys.stderr,
            )

    threading.Thread(target=_worker, daemon=True).start()



_FULL_HELP = """
[bold]ICX: Integrated Contextual X-ecution Engine[/bold]
AI-native intelligence layer for development teams. Connect your work tracker to your AI editor via MCP. Local memory, multi-modal analysis, and codebase knowledge graph.

[bold]Quick start:[/bold]  [cyan]icx setup[/cyan]  ->  [cyan]icx connection --add[/cyan]  ->  [cyan]icx model --add[/cyan]  ->  [cyan]icx analyze <KEY>[/cyan]

[bold]First-time Setup[/bold]
  [cyan]icx setup[/cyan]                                              Download AI model files (run once after install)
  [cyan]icx update[/cyan]                                             Apply config migrations after a package upgrade

[bold]Analysis[/bold]
  [cyan]icx analyze <KEY>[/cyan]                                      Fetch and analyze a work item (bug, story, task, feature)
  [cyan]icx analyze <KEY> --fast[/cyan]                               Skip all attachments - return raw text only
  [cyan]icx analyze <KEY> --profile <NAME>[/cyan]                     Use a specific LLM profile for this run
  [cyan]icx analyze <KEY> --profile <NAME> --fast[/cyan]              Specific profile + skip attachments
  [cyan]icx analyze <KEY> --path <PATH>[/cyan]                        Show graph status for a codebase path
  [cyan]icx analyze <KEY> --path <P1> --path <P2>[/cyan]              Show graph status for multiple paths
  [cyan]icx analyze <KEY> --path <P1> --path <P2> --fast[/cyan]       Graph status + skip attachments
  [cyan]icx analyze <KEY> --debug[/cyan]                              Show step-by-step debug output
  [cyan]icx analyze <KEY> --traceback[/cyan]                          Show full Python traceback on error

[bold]Connections[/bold]
  [cyan]icx connection --add[/cyan]                                   Connect a new platform (interactive)
  [cyan]icx connection --remove <DOMAIN>[/cyan]                       Remove connection by domain
  [cyan]icx connection --remove <INDEX>[/cyan]                        Remove connection by index (from icx status)
  [cyan]icx connection --active <DOMAIN>[/cyan]                       Set default connection
  [cyan]icx connection --active <INDEX>[/cyan]                        Set default by index

[bold]LLM Profiles[/bold]
  [cyan]icx model --add[/cyan]                                        Add an AI provider (interactive)
  [cyan]icx model --remove <PROFILE>[/cyan]                           Remove a profile by name
  [cyan]icx model --remove <INDEX>[/cyan]                             Remove a profile by index (from icx status)
  [cyan]icx model --remove <PROFILE> --channel text|image[/cyan]      Remove only one channel from a profile
  [cyan]icx model --active <PROFILE>[/cyan]                           Set default profile (name or index)

[bold]Memory[/bold]
  [cyan]icx memory save <KEY>[/cyan]                                  Save a resolved issue to local memory
  [cyan]icx memory save <KEY> --note "..."[/cyan]                     Save with a note (non-interactive)
  [cyan]icx memory search "<query>"[/cyan]                            Search past resolutions by description
  [cyan]icx memory list[/cyan]                                        List all saved entries (newest first)
  [cyan]icx memory list --project <KEY>[/cyan]                        Filter list by project key (e.g. PROJ)
  [cyan]icx memory list --source <TYPE>[/cyan]                        Filter list by source type (e.g. jira)
  [cyan]icx memory show <KEY>[/cyan]                                  Show full detail for one entry
  [cyan]icx memory delete <KEY>[/cyan]                                Delete one saved entry
  [cyan]icx memory export[/cyan]                                      Export all memory to a JSON file
  [cyan]icx memory export --output <FILE>[/cyan]                      Export to a specific path
  [cyan]icx memory import <FILE>[/cyan]                               Import from a JSON export file
  [cyan]icx memory clear --confirm[/cyan]                             Delete all saved entries
  [cyan]icx memory status[/cyan]                                      Show entry count, storage size, model info
  [cyan]icx memory migrate[/cyan]                                     Re-embed all saved work items after an embedding model upgrade
  [cyan]icx memory by-file <PATH>[/cyan]                              List all work items that touched a file path
  [cyan]icx memory by-file <PATH> --project <KEY>[/cyan]              Filter by project key
  [cyan]icx memory hotspots[/cyan]                                    Show files with most historical work items
  [cyan]icx memory hotspots --project <KEY> --top <N>[/cyan]          Filter by project, show top N files
  [cyan]icx memory related <KEY>[/cyan]                               Show work items related via shared file history
  [cyan]icx memory related <KEY> --project <KEY>[/cyan]               Filter related items to one project
  [cyan]icx memory patterns[/cyan]                                    Show auto-detected patterns across work items
  [cyan]icx memory patterns --project <KEY>[/cyan]                    Filter patterns by project key

[bold]Codebase Graph[/bold]
  [cyan]icx graph add --name <NAME> --path <PATH> --project <KEY>[/cyan]  Register a project for graph indexing
  [cyan]icx graph build <NAME>[/cyan]                                 Build the knowledge graph (shows live progress)
  [cyan]icx graph build --project <KEY>[/cyan]                        Build all graphs tagged with a Jira project key
  [cyan]icx graph build <NAME> --force[/cyan]                         Force full rebuild even if graph is current
  [cyan]icx graph build <NAME> --no-llm[/cyan]                        Build without LLM enrichment (faster, AST only)
  [cyan]icx graph build <NAME> --force --no-llm[/cyan]                Force rebuild, AST only
  [cyan]icx graph list[/cyan]                                         List all projects: name, status, file count, last built
  [cyan]icx graph status <NAME>[/cyan]                                Show detail: staleness, changed files, ETA
  [cyan]icx graph remove <NAME>[/cyan]                                Delete registration and graph files
  [cyan]icx graph remove <NAME> --keep-cache[/cyan]                   Delete registration only, keep cache on disk

[bold]Testing[/bold]
  [cyan]icx test health[/cyan]                                          Check Magik-AI Tester is reachable
  [cyan]icx test run <URL> --type ui|agent|api[/cyan]                   Direct test run (polls until done)
  [cyan]icx test status <ID>[/cyan]                                     Check run or session status
  [cyan]icx test sessions[/cyan]                                        List all active testing sessions
  [cyan]icx test cancel <SESSION_ID>[/cyan]                             Cancel an active testing session

[bold]Code Quality (SonarQube)[/bold]
  [cyan]icx sonar --add[/cyan]                                          Add a SonarQube server connection (name, URL, token)
  [cyan]icx sonar --list[/cyan]                                         List connections and which is active
  [cyan]icx sonar --active <NAME>[/cyan]                                Set the active connection
  [cyan]icx sonar --remove <NAME>[/cyan]                                Remove a connection
  [cyan]icx sonar status[/cyan]                                         Show the active connection status
  [cyan]icx sonar projects[/cyan]                                       List projects the token can access
  [cyan]icx sonar report --project <KEY> --branch <B>[/cyan]           Compact summary: quality gate + counts (MCP tools give full detail)

[bold]MCP Server[/bold]
  [cyan]icx mcp run[/cyan]                                            Start the MCP server (stdio transport)
  [cyan]icx mcp setup[/cyan]                                          Register ICX with all detected AI editors
  [cyan]icx mcp setup --host <HOST>[/cyan]                            Register with one specific editor
  [cyan]icx mcp remove[/cyan]                                         Remove ICX from all detected editors
  [cyan]icx mcp remove --host <HOST>[/cyan]                           Remove from one specific editor
  [cyan]icx mcp config[/cyan]                                         Print config JSON snippets for all editors
  [cyan]icx mcp list[/cyan]                                           List supported editors and detection status

[bold]General[/bold]
  [cyan]icx status[/cyan]                                             Show all connections and LLM profiles
  [cyan]icx logout[/cyan]                                             Remove all credentials from this machine
  [cyan]icx uninstall[/cyan]                                          Remove ICX completely (data, credentials, editor configs, package)
  [cyan]icx uninstall --yes[/cyan]                                    Uninstall without confirmation prompt
  [cyan]icx --version[/cyan]                                          Show installed version
  [cyan]icx --help[/cyan]                                             Show this help

[dim]Run [cyan]icx --install-completion[/cyan] once to enable tab completion in your shell.[/dim]
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
        "ICX: Integrated Contextual X-ecution Engine - AI-native intelligence layer for development teams. "
        "Deep context extraction, local-first RAG memory, multi-modal analysis, and codebase knowledge graph. "
        "Securely bridge your work tracker to your AI agents via MCP.\n\n"
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
        "[bold]Codebase graphs:[/bold] "
        "Run [bold cyan]icx graph --help[/bold cyan] to see [bold]add[/bold], [bold]build[/bold], "
        "[bold]list[/bold], [bold]status[/bold], and [bold]remove[/bold] subcommands.\n\n"
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

graph_app = typer.Typer(
    help=(
        "Build and query codebase knowledge graphs for AI-powered code navigation.\n\n"
        "[bold]Subcommands:[/bold]\n\n"
        "  [bold]add[/bold]     Register a project\n"
        "  [bold]build[/bold]   Build the knowledge graph (run this first)\n"
        "  [bold]list[/bold]    Show all registered projects and their graph status\n"
        "  [bold]status[/bold]  Detailed status for one project\n"
        "  [bold]remove[/bold]  Remove a project registration and graph files"
    ),
    rich_markup_mode="rich",
)
app.add_typer(graph_app, name="graph", rich_help_panel="Codebase Graph")

test_app = typer.Typer(
    help=(
        "Manage AI-driven testing sessions with Magik-AI Tester.\n\n"
        "[bold]Subcommands:[/bold]\n\n"
        "  [bold]health[/bold]    Check Magik-AI Tester is reachable\n"
        "  [bold]run[/bold]       Direct test run (no LangGraph, no gates)\n"
        "  [bold]status[/bold]    Check a run or session status\n"
        "  [bold]sessions[/bold]  List all active testing sessions\n"
        "  [bold]cancel[/bold]    Cancel an active testing session"
    ),
    rich_markup_mode="rich",
)
app.add_typer(test_app, name="test", rich_help_panel="Testing")

sonar_app = typer.Typer(help="SonarQube code-quality integration (distinct from testing).", rich_markup_mode="rich")
app.add_typer(sonar_app, name="sonar", rich_help_panel="Code Quality")

console = Console(highlight=False)
err_console = Console(stderr=True, highlight=False)
# Ensure UTF-8 output on Windows (cp1252 can't encode [x], ->, etc.)
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

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
    from icx_engine.connectors.base import get_connector, get_connector_class
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
            project_key=get_connector_class(conn.connector_type).extract_project_key(parsed.issue_key),
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
            console.print("\n  [bold]Files changed:[/bold]")
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


@memory_app.command("migrate")
def memory_migrate(
    debug: DebugOpt = False,
    traceback: TracebackOpt = False,
) -> None:
    """Re-embed all saved work items with the current embedding model.

    Run this after upgrading ICX when the embedding model version changes.
    All saved entries are preserved - only the internal vector representation
    is updated to match the new model.
    """
    from icx_engine.memory.manager import MemoryManager
    from icx_engine.memory.embeddings import EMBEDDING_MODEL, VECTOR_DIM

    try:
        mgr = MemoryManager()
        entry_count = len(mgr.list_entries())
        if entry_count == 0:
            console.print("  No saved work items found. Nothing to migrate.")
            return

        console.print(
            f"\n  [bold]Memory migration[/bold]\n"
            f"  Model:   [cyan]{EMBEDDING_MODEL}[/cyan] ({VECTOR_DIM}-dim)\n"
            f"  Entries: {entry_count} saved work items\n"
        )
        confirmed = typer.confirm("  Re-embed all entries with the new model?", default=True)
        if not confirmed:
            console.print("Cancelled.")
            return

        migrated = 0
        with console.status("[bold]Migrating...[/bold]", spinner="dots"):
            def _log(msg: str) -> None:
                pass  # suppress per-entry logs in spinner mode

            migrated = mgr.migrate(log=_log if not debug else console.print)

        console.print(f"[green]Migrated {migrated} work items.[/green] Memory is ready.")
    except typer.Exit:
        raise
    except Exception as exc:
        render_icx_error(exc, err_console, show_traceback=traceback)
        raise typer.Exit(1)


@memory_app.command("by-file")
def memory_by_file(
    path: Annotated[str, typer.Argument(help="File path to look up (substring match)")],
    project: Annotated[Optional[str], typer.Option("--project", help="Filter by project key")] = None,
) -> None:
    """List all saved work items that touched a given file path."""
    from icx_engine.memory.manager import MemoryManager
    from icx_engine.memory.bridge import find_work_items_by_file
    try:
        mgr = MemoryManager()
        entries = find_work_items_by_file(path, mgr, project_key=project)
        if not entries:
            console.print(f"  No saved work items found for [cyan]{path}[/cyan].")
            return
        console.print(f"\n  [bold]{len(entries)} work item(s)[/bold] touching [cyan]{path}[/cyan]\n")
        for e in entries:
            console.print(f"  [bold]{e.issue_key}[/bold]  [{e.work_item_type}]  {e.summary}")
            console.print(f"    {e.resolution_note[:120]}")
            console.print()
    except Exception as exc:
        render_icx_error(exc, err_console)
        raise typer.Exit(1)


@memory_app.command("hotspots")
def memory_hotspots(
    project: Annotated[Optional[str], typer.Option("--project", help="Filter by project key")] = None,
    top: Annotated[int, typer.Option("--top", help="Number of files to show")] = 20,
) -> None:
    """Show files with the most saved work items (churn hotspots)."""
    from icx_engine.memory.manager import MemoryManager
    from icx_engine.memory.bridge import get_work_item_density
    try:
        mgr = MemoryManager()
        rows = get_work_item_density(mgr, project_key=project, top_n=max(1, min(100, top)))
        if not rows:
            console.print("  No memory entries found.")
            return
        console.print(f"\n  [bold]Top {len(rows)} file(s) by work item count[/bold]\n")
        for row in rows:
            console.print(f"  [bold]{row['count']:>3}[/bold]  {row['file']}")
            console.print(f"       {', '.join(row['work_items'][:5])}")
            console.print()
    except Exception as exc:
        render_icx_error(exc, err_console)
        raise typer.Exit(1)


@memory_app.command("patterns")
def memory_patterns(
    project: Annotated[Optional[str], typer.Option("--project", help="Filter by project key")] = None,
) -> None:
    """Show auto-detected patterns across saved work items."""
    from icx_engine.memory.patterns import PatternManager
    try:
        pm = PatternManager()
        pats = pm.get_patterns(project_key=project)
        if not pats:
            console.print("  No patterns detected yet. Patterns are computed every 5 saved work items.")
            return
        console.print(f"\n  [bold]{len(pats)} pattern(s) detected[/bold]\n")
        for p in pats:
            console.print(f"  [bold]{p['pattern_type']}[/bold]  [dim]{p['project_key']}[/dim]")
            console.print(f"    {p['label']}")
            console.print()
    except Exception as exc:
        render_icx_error(exc, err_console)
        raise typer.Exit(1)


@memory_app.command("related")
def memory_related(
    key: Annotated[str, typer.Argument(help="Issue key, e.g. PROJ-456")],
    project: Annotated[Optional[str], typer.Option("--project", help="Filter results to a project key")] = None,
) -> None:
    """Show work items related to the given issue key via shared files."""
    from icx_engine.memory.manager import MemoryManager
    from icx_engine.memory.relations import RelationManager
    try:
        normalised = key.strip().upper()
        rel = RelationManager()
        related = rel.get_related(normalised)
        if project:
            mgr = MemoryManager()
            in_project = {e.issue_key for e in mgr.list_entries(project_key=project)}
            related = [r for r in related if r["issue_key"] in in_project]
        if not related:
            console.print(f"  No related work items found for [cyan]{normalised}[/cyan].")
            return
        console.print(f"\n  [bold]{len(related)} related work item(s)[/bold] for [cyan]{normalised}[/cyan]\n")
        for r in related:
            console.print(
                f"  [bold]{r['issue_key']}[/bold]  {r['relation_type']}  "
                f"[dim]strength={r['strength']:.2f}[/dim]"
            )
        console.print()
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
        in_path = Path(file).resolve()
        if not in_path.exists():
            err_console.print(
                f"\n[red]File not found:[/red] {in_path}\n"
                "  Pass the full path to the export file.\n"
                "  Example: [cyan]icx memory import /full/path/to/icx-memory-export.json[/cyan]"
            )
            raise typer.Exit(1)
        entries = import_from_json(in_path)
        console.print(f"  Found {len(entries)} entries in {in_path}.")
        confirmed = typer.confirm(f"Import all {len(entries)} entries?", default=True)
        if not confirmed:
            console.print("Cancelled.")
            return
        mgr = MemoryManager()
        for entry in entries:
            mgr.save(entry, restore=True)
        # Pattern refresh doesn't fire on small imports (every-10th trigger).
        # Rebuild explicitly per project after all entries are loaded.
        from collections import defaultdict
        by_project: dict[str, list] = defaultdict(list)
        for entry in entries:
            by_project[entry.project_key].append(entry)
        for proj_key, proj_entries in by_project.items():
            try:
                mgr._patterns.refresh(proj_entries, proj_key)
            except Exception:
                pass
        console.print(f"[green]Imported {len(entries)} entries.[/green]")
    except Exception as exc:
        render_icx_error(exc, err_console)
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# Entry point - no subcommand -> REPL
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
    _check_uninstall_result()
    if ctx.invoked_subcommand is None:
        _print_full_help()
        raise typer.Exit()


# ---------------------------------------------------------------------------
# setup - download optional AI model dependencies
# ---------------------------------------------------------------------------

@app.command(rich_help_panel="Setup")
def setup(
    debug: DebugOpt = False,
    traceback: TracebackOpt = False,
) -> None:
    """Download optional AI model dependencies (embedding model, Whisper audio model, token encoder).

    Run this once after install. Nothing else will trigger downloads.
    """
    import traceback as _tb_mod

    con = Console()
    any_failed = False

    # Step 1: ONNX embedding model (memory features)
    con.print("\n[bold]Step 1/3[/bold] Embedding model [dim](memory features, ~110 MB)[/dim]")
    try:
        from icx_engine.memory.embeddings import EmbeddingsManager, _is_initialized, MEMORY_DIR
        if _is_initialized():
            con.print("[green]OK[/green] Already downloaded.")
        else:
            EmbeddingsManager().ensure_ready(console=con)
            # Advisory: existing LanceDB data uses old vectors - needs re-embedding.
            try:
                _has_old_data = any(MEMORY_DIR.glob("*.lance")) if MEMORY_DIR.exists() else False
                if _has_old_data:
                    con.print(
                        "\n[yellow]Advisory:[/yellow] Existing memory data uses the old vector format.\n"
                        "Run [bold cyan]icx memory migrate[/bold cyan] to re-embed your saved work items."
                    )
            except Exception:
                pass
    except Exception as exc:
        any_failed = True
        con.print(f"[red]X[/red] Failed: {exc}")
        if debug or traceback:
            _tb_mod.print_exc()

    # Step 2: Whisper audio model (audio/video transcription)
    con.print("\n[bold]Step 2/3[/bold] Whisper model [dim](audio/video transcription, ~145 MB)[/dim]")
    try:
        from icx_engine.connectors.audio import WhisperManager, _is_whisper_ready
        if _is_whisper_ready():
            con.print("[green]OK[/green] Already downloaded.")
        else:
            WhisperManager().download()
    except Exception as exc:
        any_failed = True
        con.print(f"[red]X[/red] Failed: {exc}")
        if debug or traceback:
            _tb_mod.print_exc()

    # Step 3: tiktoken BPE encoding (graph token counting)
    con.print("\n[bold]Step 3/3[/bold] Token encoder [dim](graph context sizing, ~1 MB)[/dim]")
    try:
        import tiktoken
        _TIKTOKEN_ENC = "cl100k_base"
        enc = tiktoken.get_encoding(_TIKTOKEN_ENC)
        enc.encode("warmup")
        con.print("[green]OK[/green] Token encoder ready.")
    except ImportError:
        con.print("[dim]Skipped (tiktoken not installed).[/dim]")
    except Exception as exc:
        any_failed = True
        con.print(f"[red]X[/red] Failed: {exc}")
        if debug or traceback:
            _tb_mod.print_exc()

    if any_failed:
        con.print("\n[yellow]Setup completed with errors.[/yellow] Check output above.\n")
    else:
        con.print("\n[bold green]Setup complete.[/bold green] All models ready.\n")


# ---------------------------------------------------------------------------
# update - apply post-install migrations after a package upgrade
# ---------------------------------------------------------------------------

@app.command(rich_help_panel="Setup")
def update(
    debug: DebugOpt = False,
    traceback: TracebackOpt = False,
) -> None:
    """Apply migrations and verify your ICX installation after a package upgrade.

    Run this after upgrading ICX to apply any new config defaults, initialise
    new storage, and confirm all components are reachable.

    \b
    Example:
      pipx upgrade icx-engine
      icx update
    """
    import traceback as _tb_mod
    import json as _json

    con = Console()
    any_failed = False
    total_steps = 3

    # Step 1: Config migration - write any new default fields introduced in this version.
    con.print(f"\n[bold]Step 1/{total_steps}[/bold] Config migration [dim](apply new defaults to ~/.icx/config.json)[/dim]")
    try:
        from icx_engine.config_manager import ConfigManager, CONFIG_PATH

        cfg = ConfigManager.load()

        # Detect which new fields are not yet present in the raw JSON on disk.
        raw_on_disk: dict = {}
        if CONFIG_PATH.exists():
            try:
                raw_on_disk = _json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            except Exception:
                pass

        new_fields: list[str] = []
        for field in ("magik_base_url", "magik_max_iterations"):
            if field not in raw_on_disk:
                new_fields.append(field)

        ConfigManager.save(cfg)

        if new_fields:
            con.print(f"[green]OK[/green] Wrote new defaults: {', '.join(new_fields)}")
        else:
            con.print("[green]OK[/green] Config already current.")
    except Exception as exc:
        any_failed = True
        con.print(f"[red]X[/red] Config migration failed: {exc}")
        if debug or traceback:
            _tb_mod.print_exc()

    # Step 2: Testing sessions database - create and verify WAL mode.
    con.print(f"\n[bold]Step 2/{total_steps}[/bold] Testing sessions DB [dim](~/.icx/testing_sessions.db)[/dim]")
    try:
        from icx_engine.testing.graph import get_db_path, _make_checkpointer
        db_path = get_db_path()
        existed = db_path.exists()
        _make_checkpointer()
        if existed:
            con.print("[green]OK[/green] Testing sessions DB accessible.")
        else:
            con.print("[green]OK[/green] Testing sessions DB created.")
    except Exception as exc:
        any_failed = True
        con.print(f"[red]X[/red] Testing sessions DB failed: {exc}")
        if debug or traceback:
            _tb_mod.print_exc()

    # Step 3: Memory DB check (non-destructive - verify connection only).
    con.print(f"\n[bold]Step 3/{total_steps}[/bold] Memory DB [dim](~/.icx/memory/)[/dim]")
    try:
        from icx_engine.memory.manager import MemoryManager
        mgr = MemoryManager()
        s = mgr.status()
        count = s.get("entry_count", s.get("total_entries", "?"))
        con.print(f"[green]OK[/green] Memory DB accessible. [dim]({count} saved entries)[/dim]")
    except Exception as exc:
        any_failed = True
        con.print(f"[red]X[/red] Memory DB check failed: {exc}")
        if debug or traceback:
            _tb_mod.print_exc()

    if any_failed:
        con.print("\n[yellow]Update completed with errors.[/yellow] Check output above.\n")
    else:
        con.print(
            "\n[bold green]Update complete.[/bold green] All components current.\n"
            "  Run [cyan]icx test configure[/cyan] to set up Magik-AI Tester (if not done).\n"
        )


# ---------------------------------------------------------------------------
# connection - unified connection management command
# ---------------------------------------------------------------------------

@app.command(rich_help_panel="Setup")
def connection(
    add: Annotated[bool, typer.Option(
        "--add",
        help="Walk through connecting a new work tracker account (API token or OAuth).",
    )] = False,
    remove: Annotated[Optional[str], typer.Option(
        "--remove", metavar="DOMAIN/INDEX",
        help="Remove a connection. Pass domain or index number from 'icx status'.",
    )] = None,
    active: Annotated[Optional[str], typer.Option(
        "--active", metavar="DOMAIN/INDEX",
        help="Set the default connection. Pass domain or index number from 'icx status'.",
    )] = None,
    debug: DebugOpt = False,
    traceback: TracebackOpt = False,
) -> None:
    """Manage work tracker connections - add, switch, or remove.

    \b
    Examples:
      icx connection --add                              Connect a new work tracker account
      icx connection --remove mycompany.atlassian.net   Remove that connection
      icx connection --remove 2                         Remove connection #2 (from 'icx status')
      icx connection --active mycompany.atlassian.net   Make that connection the default
    """
    try:
        if add:
            _connect(debug=debug)
            return
        if remove is not None:
            from icx_engine.config_manager import ConfigManager
            from icx_engine import management
            config = ConfigManager.load()
            config = management.disconnect(config, remove)
            ConfigManager.save(config)
            console.print("[green]OK Connection removed.[/green]")
            return
        if active is not None:
            from icx_engine.config_manager import ConfigManager
            from icx_engine import management
            config = ConfigManager.load()
            config = management.set_default_connection(config, active)
            ConfigManager.save(config)
            console.print("[green]OK Active connection updated.[/green]")
            return
        console.print("Use --add, --remove, or --active. See [bold]icx connection --help[/bold].")
    except typer.Exit:
        raise
    except Exception as exc:
        render_icx_error(exc, err_console, show_traceback=traceback)
        raise typer.Exit(1)


# Registry of supported work tracker platforms.
# Each entry: (connector_type, display_label).
# Add new connectors here as they are implemented.
PLATFORMS: list[tuple[str, str]] = [
    ("jira", "Jira  (Jira Cloud - API Token or OAuth PKCE)"),
]


def _connect(debug: bool = False) -> None:
    """Generic connection entry point - dispatches to platform-specific connect flow.

    When only one platform is registered, skips the selection menu. When two or
    more platforms are registered, prompts the user to choose before delegating.
    """
    if len(PLATFORMS) == 1:
        platform_key = PLATFORMS[0][0]
    else:
        menu = "\n".join(f"  {i + 1}. {label}" for i, (_, label) in enumerate(PLATFORMS))
        choice = typer.prompt(
            f"\nChoose a work tracker platform\n{menu}\nEnter number",
            default="1",
        ).strip()
        try:
            idx = int(choice) - 1
            if idx < 0 or idx >= len(PLATFORMS):
                raise ValueError
        except ValueError:
            typer.echo("Invalid choice. Defaulting to first platform.")
            idx = 0
        platform_key = PLATFORMS[idx][0]

    _platform_dispatch = {
        "jira": _connect_jira,
    }
    fn = _platform_dispatch.get(platform_key)
    if fn is None:
        raise ValueError(f"No connect flow registered for platform '{platform_key}'.")
    fn(debug=debug)


def _connect_jira(debug: bool = False) -> None:
    from icx_engine.services.connection_service import _connect_jira_token, _connect_jira_oauth
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


# Provider menu + default models are derived from the single-source registry.
from icx_engine.llm.registry import PROVIDERS as _PROVIDER_SPECS

_PROVIDERS = [(name, spec.cli_label) for name, spec in _PROVIDER_SPECS.items()]

_DEFAULT_MODELS: dict[str, dict[str, str]] = {
    name: {"text": spec.default_text_model, "image": spec.default_image_model}
    for name, spec in _PROVIDER_SPECS.items()
}


def _prompt_channel_config(label: str, provider_key: str | None = None) -> "ChannelConfig":
    """Interactively prompt for one channel's provider/model/key/url."""
    from icx_engine.models.config import ChannelConfig

    typer.echo(f"\n-- {label} --")
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

    # -- Text model: hard fail -------------------------------------------------
    try:
        txt_label = new_llm.text_config.model
        if debug:
            typer.echo(f"  validating text model ({txt_label})...", err=True)
            asyncio.run(_run(new_llm.text_config))
        else:
            with console.status(f"[bold]Validating text model ({txt_label})...[/bold]", spinner="dots"):
                asyncio.run(_run(new_llm.text_config))
    except Exception as exc:
        render_icx_error(exc, err_console, show_traceback=traceback)
        raise typer.Exit(1)

    # -- Image model: soft fail - offer skip -----------------------------------
    active_llm = new_llm
    if new_llm.image_config is not None:
        img_model = new_llm.image_config.model
        try:
            if debug:
                typer.echo(f"  validating image model ({img_model})...", err=True)
                asyncio.run(_run(new_llm.image_config))
            else:
                with console.status(
                    f"[bold]Validating image model ({img_model})...[/bold]", spinner="dots"
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

    # -- Save + confirm ---------------------------------------------------------
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
        f"[green]OK Profile '{profile_name}' saved. "
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
        # -- --add --------------------------------------------------------------
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

        # -- --active -----------------------------------------------------------
        if active is not None:
            config = ConfigManager.load()
            config = management.use_ai_profile(config, active)
            ConfigManager.save(config)
            console.print("[green]OK Active AI profile updated.[/green]")
            return

        # -- --remove -----------------------------------------------------------
        if remove is not None:
            config = ConfigManager.load()
            if channel is not None:
                config = management.unset_llm_channel(config, remove, channel)
                if channel.lower() == "image":
                    console.print("[green]OK Image channel removed from profile.[/green]")
                else:
                    console.print("[green]OK AI profile removed.[/green]")
            else:
                config = management.unset_llm_profile(config, remove)
                console.print("[green]OK AI profile removed.[/green]")
            ConfigManager.save(config)
            return

        # -- bare target - set active -------------------------------------------
        if target is not None:
            config = ConfigManager.load()
            config = management.use_ai_profile(config, target)
            ConfigManager.save(config)
            console.print("[green]OK Active AI profile updated.[/green]")
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
            "Work item to analyze. Full URL or issue key (e.g. ABC-123). "
            "Works for bugs, stories, tasks, features - any tracked work item."
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
            help=(
                "Skip all attachment processing (images, audio, video, documents). "
                "Returns raw issue text only. "
                "Skipped files listed in pending_images, pending_audio, "
                "pending_documents, or pending_unsupported."
            ),
        ),
    ] = False,
    paths: Annotated[
        Optional[list[str]],
        typer.Option(
            "--path",
            help=(
                "Codebase path to show graph status for alongside the analysis. "
                "Repeatable for multiple repos: --path /svc --path /ui."
            ),
            metavar="PATH",
        ),
    ] = None,
) -> None:
    """Fetch and analyze a work item - prints structured JSON ready for your AI tools.

    \b
    Works with any connected platform. Analyzes bugs, stories, tasks, features
    - anything tracked in your work tracker.

    \b
    ICX reads the title, description, comments, and attachments, then asks your
    AI model to produce a structured summary with context and missing information.
    Images are written to ~/.icx/temp/<key>/ and paths returned in image_paths.

    \b
    Examples:
      icx analyze ABC-123
      icx analyze ABC-123 --fast
      icx analyze https://company.atlassian.net/browse/ABC-123
      icx analyze ABC-123 --profile work
      icx analyze ABC-123 --path /path/to/repo
      icx analyze ABC-123 --path /svc --path /ui --fast
      icx analyze ABC-123 --debug
    """
    import json as _json
    from icx_engine.config_manager import ConfigManager
    from icx_engine import engine
    from icx_engine.engine import extract_domain, resolve_connection, narrow_connections
    from icx_engine.models.output import IssueContext
    from icx_engine.exceptions import (
        ICXError,
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
            with console.status(f"[bold]Analyzing {url}...[/bold]", spinner="dots"):
                result = _run()

    except ICXError as exc:
        render_icx_error(exc, err_console, show_traceback=traceback)
        raise typer.Exit(1)
    except Exception as exc:
        render_icx_error(exc, err_console, show_traceback=traceback)
        raise typer.Exit(1)

    # Build output dict - write images to disk, never emit base64 in CLI output.
    result_dict = _json.loads(result.model_dump_json())
    image_paths: dict[str, str] = {}
    raw_images: dict[str, str] = result_dict.pop("images", None) or {}
    if raw_images:
        try:
            import base64 as _b64
            from icx_engine.graph.storage import temp_images_dir as _tid, sweep_stale_temp_dirs as _sweep
            _ALLOWED_EXTS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".tif"})
            _sweep()
            img_dir = _tid(url)
            img_dir.mkdir(parents=True, exist_ok=True)
            for fname, b64_data in raw_images.items():
                safe = Path(fname).name
                if safe and Path(safe).suffix.lower() in _ALLOWED_EXTS:
                    img_path = img_dir / safe
                    img_path.write_bytes(_b64.b64decode(b64_data))
                    image_paths[fname] = str(img_path)
        except Exception:
            pass
    if image_paths:
        result_dict["image_paths"] = image_paths

    typer.echo(_json.dumps(result_dict, indent=2))

    if isinstance(result, IssueContext) and result.past_insights:
        _render_past_insights(result.past_insights)

    if isinstance(result, IssueContext) and result.missing_information:
        err_console.print("\nMISSING REQUIREMENTS")
        for item in result.missing_information:
            err_console.print(f"  - {item}")

    if paths:
        from rich.table import Table
        from rich.panel import Panel as _Panel
        from icx_engine.graph.manager import graph_info_for_path as _gif

        graph_table = Table(show_header=True, header_style="bold cyan", box=None, padding=(0, 1))
        graph_table.add_column("Path", style="cyan", no_wrap=True)
        graph_table.add_column("Status", no_wrap=True)
        graph_table.add_column("Notes")

        for p in paths:
            info = _gif(p)
            s = info["status"]
            if s == "ready":
                status_cell = "[bold green]READY[/bold green]"
                notes = info.get("report_path") or ""
                if info.get("stale_note"):
                    notes = f"[yellow]STALE[/yellow]  {info['stale_note']}"
            elif s in ("building", "rebuilding"):
                eta = info.get("eta_seconds") or "?"
                status_cell = "[yellow]BUILDING[/yellow]"
                notes = f"ETA ~{eta}s"
            elif s == "not_built":
                status_cell = "[dim]NOT BUILT[/dim]"
                _rn = info.get("name") or p
                notes = f"run: icx graph build {_rn}"
            elif s == "not_registered":
                status_cell = "[dim]NOT REGISTERED[/dim]"
                notes = f"run: icx graph add --name <name> --path {p} --project <key>"
            else:
                status_cell = "[red]ERROR[/red]"
                notes = (info.get("report_inline") or "")[:120]
            graph_table.add_row(p, status_cell, notes)

        console.print(_Panel(graph_table, title="[bold]Codebase Graphs[/bold]"))


@app.command(rich_help_panel="Setup")
def status(
    debug: DebugOpt = False,
    traceback: TracebackOpt = False,
) -> None:
    """Show all connected platforms and configured AI profiles at a glance."""
    from icx_engine.config_manager import ConfigManager
    from rich.table import Table

    try:
        config = ConfigManager.load()

        # -- Connections --------------------------------------------------------
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

        # -- AI Profiles --------------------------------------------------------
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

        # -- Sonar connections --------------------------------------------------
        console.print()
        sonar_table = Table(title="Sonar Connections", show_header=True, header_style="bold cyan")
        sonar_table.add_column("#", style="dim", width=3)
        sonar_table.add_column("Name")
        sonar_table.add_column("URL")
        sonar_table.add_column("TLS", width=5)
        sonar_table.add_column("Token", width=8)
        sonar_table.add_column("Active")
        for i, (name, sc) in enumerate(config.sonar_connections.items(), 1):
            active_cell = "[bold green][ACTIVE][/bold green]" if name == config.active_sonar else ""
            token_cell = "[dim]set[/dim]" if sc.token else "[red]missing[/red]"
            sonar_table.add_row(
                str(i), name, sc.url, "yes" if sc.verify_tls else "no", token_cell, active_cell)
        if not config.sonar_connections:
            sonar_table.caption = "Run [bold]icx sonar --add[/bold] to add one."
        console.print(sonar_table)

    except Exception as exc:
        render_icx_error(exc, err_console, show_traceback=traceback)
        raise typer.Exit(1)


@app.command(rich_help_panel="Setup")
def logout(
    debug: DebugOpt = False,
    traceback: TracebackOpt = False,
) -> None:
    """Wipe all saved credentials - removes every work tracker connection and AI API key from this machine."""
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
        console.print("[green]OK All credentials removed.[/green]")
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


_UNINSTALL_LOG = Path(tempfile.gettempdir()) / "icx_uninstall_result.txt"


def _check_uninstall_result() -> None:
    """Warn if a previous background uninstall silently failed."""
    if not _UNINSTALL_LOG.exists():
        return
    try:
        result = _UNINSTALL_LOG.read_text(encoding="utf-8").strip()
        _UNINSTALL_LOG.unlink(missing_ok=True)
    except Exception:
        return
    if result.startswith("FAILED"):
        err_console.print(
            "\n[yellow]Previous uninstall failed.[/yellow] Remove manually:\n"
            "  [cyan]pip uninstall -y icx-engine[/cyan]  "
            "or  [cyan]pipx uninstall icx-engine[/cyan]\n"
        )


def _uninstall_package(console: Console) -> None:
    """Uninstall icx-engine, working around the Windows running-exe lock.

    On Windows, pip cannot delete icx.exe while it is the current process.
    Fix: launch a hidden PowerShell script that waits for THIS process to
    actually exit (via Wait-Process), then runs the uninstall. On Unix the
    exe lock does not exist so we run the uninstall directly.
    """
    import os

    cmd = _build_uninstall_cmd()

    if sys.platform != "win32":
        subprocess.run(cmd, check=True)
        console.print("[bold green]OK ICX fully removed. Goodbye![/bold green]\n")
        return

    def _ps_quote(arg: str) -> str:
        arg = arg.replace('`', '``')   # escape PS escape-char first
        arg = arg.replace('$', '`$')   # prevent variable/subexpression expansion
        arg = arg.replace('"', '`"')   # escape double-quote
        return '"' + arg + '"'

    cmd_str = " ".join(_ps_quote(c) for c in cmd)
    # Assign log path via single-quoted PS variable so the path is never
    # subject to double-quote PS expansion regardless of its contents.
    log_ps_safe = str(_UNINSTALL_LOG).replace("'", "''")  # '' is the only escape in PS single-quoted strings
    pid = os.getpid()

    ps_script = (
        f"$logPath = '{log_ps_safe}'\n"
        f"Wait-Process -Id {pid} -ErrorAction SilentlyContinue\n"
        f"Stop-Process -Name 'icx' -Force -ErrorAction SilentlyContinue\n"
        f"Get-CimInstance Win32_Process -Filter \"Name='python.exe' OR Name='pythonw.exe'\""
        f" | Where-Object {{ $_.CommandLine -like '*icx_engine*' -or $_.CommandLine -like '*icx-engine*' }}"
        f" | ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }}\n"
        f"Start-Sleep -Seconds 1\n"
        f"& {cmd_str}\n"
        f"if ($LASTEXITCODE -eq 0) {{\n"
        f'    "OK" | Out-File -FilePath $logPath -Encoding utf8\n'
        f"}} else {{\n"
        f'    "FAILED:exit $LASTEXITCODE" | Out-File -FilePath $logPath -Encoding utf8\n'
        f"}}\n"
        f"Remove-Item $PSCommandPath -Force -ErrorAction SilentlyContinue\n"
    )
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".ps1", delete=False, encoding="utf-8"
    ) as tf:
        tf.write(ps_script)
        script_path = tf.name

    fallback_cmd = " ".join(cmd)
    console.print(
        f"[dim]If background uninstall fails, run manually: [bold]{fallback_cmd}[/bold][/dim]\n"
    )
    subprocess.Popen(
        [
            "powershell",
            "-ExecutionPolicy", "Bypass",
            "-NonInteractive",
            "-WindowStyle", "Hidden",
            "-File", script_path,
        ],
        creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW,
    )
    console.print(
        "[bold green]OK Data and configs removed.[/bold green]\n"
        "[dim]Package uninstall running in background.[/dim]\n"
        "[dim]Open a new terminal in ~5 seconds and run "
        "[bold]icx --version[/bold] - 'not recognized' means success.[/dim]\n"
    )


@app.command(rich_help_panel="Setup")
def uninstall(
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation prompt.")] = False,
    traceback: TracebackOpt = False,
) -> None:
    """Fully remove ICX - wipes all data, credentials, editor configs, then uninstalls the package.

    \b
    Removes:
      - ~/.icx/             (config, memory database, embedding model)
      - Keyring secrets     (all stored API keys and work tracker tokens)
      - MCP editor configs  (Claude Code, Cursor, Windsurf, Codex)
      - icx-engine package  (via pipx or pip)

    Run this instead of bare 'pip uninstall' to leave nothing behind.
    """
    from pathlib import Path
    from icx_engine.config_manager import ConfigManager
    from icx_engine.mcp_hosts import detect_installed_hosts, remove_icx_entry

    icx_dir = Path.home() / ".icx"

    console.print("\n[bold red]ICX Uninstall[/bold red]\n")
    console.print("This will permanently remove:\n")
    console.print(f"  [dim]-[/dim] [cyan]{icx_dir}[/cyan]  (config, memory, embedding model)")
    console.print("  [dim]-[/dim] All stored API keys and tokens from system keyring")
    console.print("  [dim]-[/dim] ICX entry from all detected AI editor configs")
    console.print("  [dim]-[/dim] [bold]icx-engine[/bold] package\n")

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
        console.print("[green]OK[/green] Keyring secrets removed.")
    except Exception as exc:
        errors.append(f"Keyring: {exc}")
        console.print(f"[yellow]Keyring cleanup failed:[/yellow] {exc}")

    # 2. Remove ICX from MCP editor configs
    try:
        hosts = detect_installed_hosts()
        removed_hosts = [h for h in hosts if remove_icx_entry(h)]
        if removed_hosts:
            for h in removed_hosts:
                console.print(f"[green]OK[/green] Removed from {h.config_path}")
        else:
            console.print("[dim]  No editor MCP configs found to clean.[/dim]")
    except Exception as exc:
        errors.append(f"MCP hosts: {exc}")
        console.print(f"[yellow]Editor config cleanup failed:[/yellow] {exc}")

    # 3. Delete ~/.icx/ entirely
    try:
        if icx_dir.exists():
            shutil.rmtree(icx_dir)
            console.print(f"[green]OK[/green] Deleted {icx_dir}")
        else:
            console.print(f"[dim]  {icx_dir} not found - nothing to delete.[/dim]")
    except Exception as exc:
        errors.append(f"~/.icx/: {exc}")
        console.print(f"[yellow]Failed to delete {icx_dir}:[/yellow] {exc}")

    # 4. Uninstall the package - detect pipx vs pip, handle Windows exe-lock
    console.print("\nUninstalling [bold]icx-engine[/bold] package...")
    try:
        _uninstall_package(console)
    except Exception as exc:
        console.print(
            "[yellow]Package uninstall failed.[/yellow] Run manually:\n"
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

def _json_snippet() -> str:
    from icx_engine.mcp_hosts import _resolve_icx_command
    cmd = _resolve_icx_command()
    return (
        '{\n'
        '  "mcpServers": {\n'
        '    "icx": {\n'
        f'      "command": "{cmd}",\n'
        '      "args": ["mcp", "run"]\n'
        '    }\n'
        '  }\n'
        '}'
    )


def _toml_snippet() -> str:
    from icx_engine.mcp_hosts import _resolve_icx_command
    cmd = _resolve_icx_command()
    return f'[mcp_servers.icx]\ncommand = "{cmd}"\nargs = ["mcp", "run"]'


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
        typer.echo(_json_snippet())
        typer.echo("\n--- Codex (TOML) ---\n")
        typer.echo(_toml_snippet())
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
                typer.echo("Specify one with --host claude | cursor | windsurf | codex | antigravity")
                raise typer.Exit(1)

        for target in targets:
            result = write_icx_entry(target)
            if result.fallback:
                typer.echo(
                    f"  {target.label}: not detected - wrote fallback config to {result.path}"
                )
            else:
                console.print(f"[green]OK ICX entry written to {result.path}[/green]")
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
                console.print(f"[green]OK ICX entry removed from {target.config_path}[/green]")
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
# Graph subcommands
# ---------------------------------------------------------------------------

@graph_app.command("add")
def graph_add(
    name: Annotated[str, typer.Option("--name", help="Project name (used to reference it later).")],
    path: Annotated[str, typer.Option("--path", help="Absolute or relative path to the project root.")],
    project: Annotated[str, typer.Option("--project", help="Tracker project key (e.g. a Jira project key like PROJ, or your tracker's project identifier). Case-insensitive. Required.")],
) -> None:
    """Register a project for codebase graph indexing.

    \b
    Example:
      icx graph add --name proj-svc --path ./proj-svc --project PROJ
    """
    from icx_engine.graph.manager import GraphManager
    from icx_engine.exceptions import GraphError

    try:
        mgr = GraphManager()
        project_id = mgr.register(name, path, tracker_project_key=project)
        console.print(
            f"[green]OK Project '[bold]{name.lower()}[/bold]' registered "
            f"(project: [bold]{project.upper()}[/bold], id: {project_id}).[/green]\n"
            f"  Run [cyan]icx graph build {name.lower()}[/cyan] to build the knowledge graph."
        )
    except GraphError as exc:
        render_icx_error(exc, err_console)
        raise typer.Exit(1)
    except Exception as exc:
        render_icx_error(exc, err_console)
        raise typer.Exit(1)


def _run_build_with_progress(mgr, project_id: str, force: bool, skip_llm: bool = False) -> dict:
    """Run a blocking graph build while rendering per-stage Rich Progress."""
    import io
    import sys
    import time as _time
    import threading
    from rich.progress import (
        Progress, SpinnerColumn, TextColumn, BarColumn,
        MofNCompleteColumn, TimeElapsedColumn,
    )
    from icx_engine.graph.progress import (
        new_progress_path, tail_events, safe_unlink, STAGES, STAGE_LABELS,
    )

    def _fmt_elapsed(secs: float) -> str:
        m, s = divmod(int(secs), 60)
        h, m = divmod(m, 60)
        return f"{h}:{m:02d}:{s:02d}"

    progress_path = new_progress_path()
    result_holder: dict[str, dict] = {}
    error_holder: dict[str, BaseException] = {}
    done = threading.Event()
    _stderr_capture = io.StringIO()
    _real_stderr = sys.stderr
    sys.stderr = _stderr_capture

    def worker() -> None:
        try:
            result_holder["value"] = mgr.build(
                project_id, force=force, progress_path=progress_path, skip_llm=skip_llm,
            )
        except BaseException as exc:
            error_holder["exc"] = exc
        finally:
            done.set()

    progress = Progress(
        SpinnerColumn(),
        TextColumn("[bold]{task.description:<20}"),
        BarColumn(bar_width=30),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        TextColumn("[dim]{task.fields[message]}"),
        console=console,
        transient=True,
    )

    task_ids: dict[str, int] = {}
    _task_start: dict[str, float] = {}
    # stage -> (current, total, msg, elapsed_str): updated on every completion
    # event so the LAST message wins (builder emits two completion events for
    # AST: one from on_progress callback with "parsing", one final with "N nodes")
    _done: dict[str, tuple] = {}
    _prev_stage: list[str | None] = [None]

    def _flush_prev() -> None:
        """Print the previous stage's final summary line now that we know it's done."""
        prev = _prev_stage[0]
        if prev is not None and prev in _done:
            cur, tot, msg, el = _done.pop(prev)
            console.print(
                f"  [bold]{STAGE_LABELS[prev]:<20}[/bold]"
                f"  [dim]{cur}/{tot}[/dim]"
                f"  {el}"
                f"  [dim]{msg}[/dim]"
            )

    with progress:
        for stage in STAGES:
            task_ids[stage] = progress.add_task(
                STAGE_LABELS[stage], total=None, message="", start=False, visible=False,
            )

        def on_event(event: dict) -> None:
            stage = event.get("stage")
            if stage not in task_ids:
                return
            task_id = task_ids[stage]
            total = event.get("total") or 0
            current = event.get("current") or 0
            msg = event.get("message") or ""

            # New stage starting: flush the previous stage's summary line
            if stage != _prev_stage[0]:
                _flush_prev()
                _prev_stage[0] = stage

            if total == 0:
                skip_msg = f"skipped - {msg}" if msg else "skipped"
                _done[stage] = (1, 1, skip_msg, "0:00:00")
            else:
                if stage not in _task_start:
                    _task_start[stage] = _time.monotonic()
                    progress.update(task_id, visible=True)
                    progress.start_task(task_id)
                progress.update(task_id, total=total, completed=current, message=msg)
                if current >= total:
                    elapsed = _fmt_elapsed(_time.monotonic() - _task_start[stage])
                    # Update _done so later events with better messages overwrite
                    _done[stage] = (current, total, msg, elapsed)
                    progress.stop_task(task_id)
                    progress.update(task_id, visible=False)

        worker_thread = threading.Thread(target=worker, daemon=True)
        worker_thread.start()

        try:
            tail_events(progress_path, on_event, done.is_set)
        except KeyboardInterrupt:
            done.set()
            sys.stderr = _real_stderr
            safe_unlink(progress_path)
            raise
        worker_thread.join()

    # Progress live display cleared (transient=True). Restore stderr first so
    # any subsequent prints go to the real terminal.
    sys.stderr = _real_stderr
    captured_warnings = _stderr_capture.getvalue().strip()

    _flush_prev()
    safe_unlink(progress_path)

    if captured_warnings:
        console.print(f"[yellow]Build warnings:[/yellow]\n{captured_warnings}")

    if "exc" in error_holder:
        raise error_holder["exc"]
    return result_holder.get("value", {})


@graph_app.command("build")
def graph_build(
    name: Annotated[Optional[str], typer.Argument(help="Registered project name.")] = None,
    project: Annotated[Optional[str], typer.Option("--project", help="Tracker project key - builds all graphs tagged with this project (case-insensitive).")] = None,
    force: Annotated[bool, typer.Option("--force", help="Force full rebuild even if graph is current.")] = False,
    no_llm: Annotated[bool, typer.Option("--no-llm", help="Skip LLM semantic enrichment. Faster but fewer cross-file edges.")] = False,
) -> None:
    """Build the codebase knowledge graph for a project.

    \b
    Run this before using graph tools in your AI editor.
    Building from the CLI shows a progress bar and avoids blocking your editor.

    \b
    Examples:
      icx graph build myapp
      icx graph build myapp --force
      icx graph build myapp --no-llm
      icx graph build --project PROJ
    """
    from icx_engine.graph.manager import GraphManager
    from icx_engine.graph.builder import estimate_build_eta
    from icx_engine.exceptions import GraphError

    try:
        mgr = GraphManager()

        # Resolve project_ids - name arg > --project key
        project_ids: list[str] = []

        if name is not None:
            project_id = mgr.resolve_project(project_name=name)
            project_ids = [project_id]
        elif project is not None:
            from icx_engine.graph.storage import lookup_by_tracker_project_key as _lookup_jp
            matches = _lookup_jp(project)
            if not matches:
                err_console.print(
                    f"No graphs registered under project [bold]{project.upper()}[/bold]. "
                    f"Register with [cyan]icx graph add --name <NAME> --path <PATH> --project {project.upper()}[/cyan]."
                )
                raise typer.Exit(1)
            project_ids = [m.project_id for m in matches]
        else:
            err_console.print(
                "Provide a project name or [bold]--project[/bold]. "
                "See [bold]icx graph build --help[/bold]."
            )
            raise typer.Exit(1)

        from icx_engine.graph.storage import read_meta as _read_meta
        any_failed = False

        for pid in project_ids:
            meta = _read_meta(pid)
            if meta is None:
                err_console.print(f"Project {pid} not found. Register it first with [cyan]icx graph add[/cyan].")
                any_failed = True
                continue

            if not force and meta.build_status in ("building", "rebuilding"):
                eta = estimate_build_eta(meta.file_count)
                console.print(f"  Graph is already building for [cyan]{meta.name}[/cyan]. ETA: ~{eta}s.")
                continue

            display_name = meta.name or pid
            console.print(f"\n  Building codebase graph: [bold]{display_name}[/bold]")

            result = _run_build_with_progress(mgr, pid, force, skip_llm=no_llm)

            if result.get("error"):
                err_console.print(f"[red]Build failed:[/red] {result['error']}")
                any_failed = True
                continue

            file_count = result.get("file_count", 0)
            node_count = result.get("node_count", 0)
            edge_count = result.get("edge_count", 0)
            community_count = result.get("community_count", 0)

            console.print(
                f"\n  [green]Graph ready.[/green] "
                f"{file_count} files | {node_count} nodes | {edge_count} edges | {community_count} communities\n"
                f"  [dim]Tip: add an LLM profile ([cyan]icx model --add[/cyan]) for richer query results.[/dim]"
            )

        if any_failed:
            raise typer.Exit(1)

    except typer.Exit:
        raise
    except GraphError as exc:
        render_icx_error(exc, err_console)
        raise typer.Exit(1)
    except Exception as exc:
        render_icx_error(exc, err_console)
        raise typer.Exit(1)


@graph_app.command("list")
def graph_list() -> None:
    """Show all registered projects and their graph status."""
    from icx_engine.graph.manager import GraphManager
    from rich.table import Table
    from icx_engine.exceptions import GraphError

    try:
        mgr = GraphManager()
        projects = mgr.list_projects()

        if not projects:
            console.print(
                "  No projects registered. "
                "Run [cyan]icx graph add --name <NAME> --path <PATH>[/cyan] to register one."
            )
            return

        has_tracker_key = any(p.tracker_project_key for p in projects)

        table = Table(show_header=True, header_style="bold cyan")
        table.add_column("Name", style="cyan")
        table.add_column("Path")
        table.add_column("Status", width=12)
        table.add_column("Last Built", width=16)
        table.add_column("Files", width=7, justify="right")
        if has_tracker_key:
            table.add_column("Project", width=10)

        _STATUS_STYLE = {
            "ready": "[green]ready[/green]",
            "stale": "[yellow]stale[/yellow]",
            "building": "[blue]building[/blue]",
            "rebuilding": "[blue]rebuilding[/blue]",
            "not_built": "[dim]not built[/dim]",
        }

        for p in projects:
            status_cell = _STATUS_STYLE.get(p.build_status, p.build_status)
            last_built = p.last_built[:16].replace("T", " ") if p.last_built else "[dim]-[/dim]"
            file_count = str(p.file_count) if p.file_count else "[dim]-[/dim]"
            row = [p.name, p.path, status_cell, last_built, file_count]
            if has_tracker_key:
                row.append(p.tracker_project_key or "[dim]-[/dim]")
            table.add_row(*row)

        console.print()
        console.print(table)
        console.print()

    except GraphError as exc:
        render_icx_error(exc, err_console)
        raise typer.Exit(1)
    except Exception as exc:
        render_icx_error(exc, err_console)
        raise typer.Exit(1)


@graph_app.command("status")
def graph_status(
    name: Annotated[str, typer.Argument(help="Registered project name.")],
) -> None:
    """Show detailed status for a project: staleness, changed files, ETA."""
    from icx_engine.graph.manager import GraphManager
    from icx_engine.graph.change import check_staleness
    from icx_engine.graph.builder import estimate_build_eta
    from icx_engine.graph.storage import read_meta
    from pathlib import Path
    from icx_engine.exceptions import GraphError

    try:
        mgr = GraphManager()
        project_id = mgr.resolve_project(project_name=name)
        meta = read_meta(project_id)
        if meta is None:
            err_console.print(f"Project '{name}' not found.")
            raise typer.Exit(1)

        console.print(f"\n  [bold]{meta.name}[/bold]  [dim]({meta.project_id})[/dim]")
        console.print(f"  Path:        {meta.path}")
        console.print(f"  Status:      {meta.build_status}")
        console.print(f"  Last built:  {meta.last_built or 'never'}")
        console.print(f"  Files:       {meta.file_count}")
        console.print(f"  Git commit:  {meta.git_commit or 'unknown'}")

        if meta.build_status in ("ready", "stale") and meta.git_commit:
            change = check_staleness(meta.git_commit, meta.file_count, Path(meta.path))
            if change.is_stale:
                console.print(f"\n  [yellow]Graph is stale.[/yellow] {len(change.changed_files)} file(s) changed:")
                for f in change.changed_files[:10]:
                    console.print(f"    [dim]{f}[/dim]")
                eta = estimate_build_eta(meta.file_count)
                console.print(f"\n  Rebuild ETA: ~{eta}s  |  Run: [cyan]icx graph build {meta.name}[/cyan]")
            else:
                console.print("\n  [green]Graph is up to date.[/green]")
        elif meta.build_status == "not_built":
            console.print(f"\n  Run [cyan]icx graph build {meta.name}[/cyan] to build the graph.")
        elif meta.build_status in ("building", "rebuilding"):
            eta = estimate_build_eta(meta.file_count)
            console.print(f"\n  Build in progress. ETA: ~{eta}s")
        console.print()

    except typer.Exit:
        raise
    except GraphError as exc:
        render_icx_error(exc, err_console)
        raise typer.Exit(1)
    except Exception as exc:
        render_icx_error(exc, err_console)
        raise typer.Exit(1)


@graph_app.command("remove")
def graph_remove(
    name: Annotated[str, typer.Argument(help="Registered project name.")],
    keep_cache: Annotated[bool, typer.Option("--keep-cache", help="Keep cache files; remove registration only.")] = False,
) -> None:
    """Remove a project registration and delete its graph files.

    \b
    Examples:
      icx graph remove myapp               Remove registration and all graph files
      icx graph remove myapp --keep-cache  Remove registration only, keep cache for faster future rebuild
    """
    from icx_engine.graph.manager import GraphManager
    from icx_engine.exceptions import GraphError

    try:
        mgr = GraphManager()
        project_id = mgr.resolve_project(project_name=name)

        if keep_cache:
            action_desc = f"remove registration for '[bold]{name}[/bold]' (keep cache)"
        else:
            action_desc = f"remove '[bold]{name}[/bold]' and delete all graph files"

        confirmed = typer.confirm(f"  This will {action_desc}. Continue?", default=False)
        if not confirmed:
            console.print("Cancelled.")
            return

        mgr.remove(project_id, keep_cache=keep_cache)
        if keep_cache:
            console.print(f"[green]OK Registration removed. Cache kept at ~/.icx/graphs/{project_id}/cache/[/green]")
        else:
            console.print(f"[green]OK Project '{name}' removed.[/green]")

    except typer.Exit:
        raise
    except GraphError as exc:
        render_icx_error(exc, err_console)
        raise typer.Exit(1)
    except Exception as exc:
        render_icx_error(exc, err_console)
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# Test subcommands
# ---------------------------------------------------------------------------

@test_app.command("health")
def test_health() -> None:
    """Check Magik-AI Tester connectivity and print status.

    \b
    Example:
      icx test health
    """
    import asyncio as _asyncio
    from icx_engine.testing.client import MagikClient, MagikUnreachable
    from icx_engine.config_manager import ConfigManager

    cfg = ConfigManager.load()

    async def _check() -> dict:
        client = MagikClient(base_url=cfg.magik_base_url, api_key=cfg.magik_api_key)
        try:
            return await client.health_check()
        finally:
            await client.aclose()

    try:
        data = _asyncio.run(_check())
        console.print(f"[green]Magik-AI Tester is up[/green] - {cfg.magik_base_url}")
        console.print(f"  status: {data.get('status')}  uptime: {data.get('uptimeSec')}s")
    except MagikUnreachable as exc:
        err_console.print(f"[red]Magik-AI unreachable:[/red] {exc}")
        err_console.print(f"  Configured URL: {cfg.magik_base_url}")
        err_console.print("  Edit ~/.icx/config.json to update magik_base_url.")
        raise typer.Exit(code=1)


@test_app.command("run")
def test_run(
    url: Annotated[str, typer.Argument(help="Target URL to test")],
    test_type: Annotated[str, typer.Option("--type", help="Test type: ui, agent, api")] = "ui",
) -> None:
    """Submit a direct Magik-AI test run without the session loop. Polls until done, prints result.

    \b
    Examples:
      icx test run http://localhost:3000/login --type ui
      icx test run http://localhost:3000/login --type agent
      icx test run http://localhost:8080/api/login --type api
    """
    import asyncio as _asyncio
    from icx_engine.testing.client import MagikClient, MagikUnreachable
    from icx_engine.config_manager import ConfigManager

    if test_type not in ("ui", "agent", "api"):
        err_console.print("[red]--type must be ui, agent, or api[/red]")
        raise typer.Exit(code=1)

    cfg = ConfigManager.load()
    client = MagikClient(base_url=cfg.magik_base_url, api_key=cfg.magik_api_key)

    async def _run() -> None:
        try:
            await client.health_check()
        except MagikUnreachable as exc:
            err_console.print(f"[red]Magik-AI unreachable:[/red] {exc}")
            raise typer.Exit(code=1)

        console.print(f"Submitting {test_type} test to {url} ...")
        if test_type == "ui":
            data = await client.submit_ui_test(url=url)
        elif test_type == "agent":
            goal = typer.prompt("Goal for agent run")
            data = await client.submit_agent_run(url=url, goal=goal)
        else:
            endpoint = typer.prompt("API endpoint URL")
            method = typer.prompt("HTTP method", default="POST")
            payload = typer.prompt("Payload (JSON string)")
            data = await client.submit_api_test(endpoint=endpoint, method=method, payload=payload, payload_type="json")

        run_id = data["runId"]
        console.print(f"Run started: [cyan]{run_id}[/cyan]  (polling every 30s ...)")

        while True:
            await _asyncio.sleep(30)
            snap = await client.get_run_status(run_id)
            c = snap.get("counters", {})
            console.print(
                f"  state={snap['state']}  pass={c.get('pass', 0)}  fail={c.get('fail', 0)}",
                end="\r",
            )
            if snap["state"] in ("completed", "failed", "cancelled"):
                console.print()
                break

        console.print(f"\n[bold]Done[/bold] - state: [cyan]{snap['state']}[/cyan]")
        console.print(f"  pass={c.get('pass', 0)}  fail={c.get('fail', 0)}  warn={c.get('warn', 0)}")
        await client.aclose()

    _asyncio.run(_run())


@test_app.command("status")
def test_status(
    id_: Annotated[str, typer.Argument(help="run_id or session_id to check")],
) -> None:
    """Check the status of a Magik-AI run or testing session.

    \b
    Examples:
      icx test status ui-1748602800-abc123
      icx test status agent-1748602800-abc1
    """
    import asyncio as _asyncio
    from icx_engine.testing.client import MagikClient, MagikUnreachable, MagikRunLost
    from icx_engine.config_manager import ConfigManager

    cfg = ConfigManager.load()

    async def _fetch() -> dict:
        client = MagikClient(base_url=cfg.magik_base_url, api_key=cfg.magik_api_key)
        try:
            return await client.get_run_status(id_)
        finally:
            await client.aclose()

    try:
        data = _asyncio.run(_fetch())
        console.print(f"run_id:  {data.get('runId', id_)}")
        console.print(f"state:   {data.get('state')}")
        c = data.get("counters", {})
        if c:
            console.print(f"counters: pass={c.get('pass', 0)} fail={c.get('fail', 0)} total={c.get('total', 0)}")
        if data.get("reportUrl"):
            console.print(f"report:  {data['reportUrl']}")
    except MagikRunLost:
        err_console.print(f"[yellow]Run {id_!r} not found - Magik may have restarted.[/yellow]")
        raise typer.Exit(code=1)
    except MagikUnreachable as exc:
        err_console.print(f"[red]Magik-AI unreachable:[/red] {exc}")
        raise typer.Exit(code=1)


@test_app.command("sessions")
def test_sessions() -> None:
    """List all active testing sessions with their status and file counts.

    \b
    Example:
      icx test sessions
    """
    from icx_engine.testing.graph import get_db_path
    from icx_engine.testing.session_store import list_active_sessions

    db_path = get_db_path()
    sessions = list_active_sessions(db_path)
    if not sessions:
        console.print("No active testing sessions.")
        return
    console.print(f"[bold]{len(sessions)} session(s):[/bold]")
    for s in sessions:
        files = s.get("file_paths", [])
        console.print(
            f"  [cyan]{s['session_id']}[/cyan]  "
            f"status={s.get('status', '?')}  "
            f"iteration={s.get('iteration', 0)}  "
            f"files={len(files)}"
        )
        if s.get("run_id"):
            console.print(f"    run_id: {s['run_id']}")


@test_app.command("cancel")
def test_cancel(
    session_id: Annotated[str, typer.Argument(help="Session UUID to cancel")],
) -> None:
    """Cancel an active testing session and remove it from the session store.

    \b
    Example:
      icx test cancel 3f7a1c2d-8e4b-4f9a-b2d1-0c5e6f7a8b9c
    """
    from icx_engine.testing.graph import get_db_path
    from icx_engine.testing.session_store import cancel_session

    db_path = get_db_path()
    if cancel_session(session_id, db_path):
        console.print(f"Session [cyan]{session_id}[/cyan] cancelled.")
    else:
        err_console.print(f"[yellow]Session {session_id!r} not found.[/yellow]")
        raise typer.Exit(code=1)


@test_app.command("configure")
def test_configure() -> None:
    """Configure Magik-AI Tester settings: base URL, optional API key, and agent step limits.

    \b
    Prompts for:
      - Magik-AI base URL
      - Magik-AI API key (optional)
      - Max fix iterations (re-test loops before the limit gate)
      - Agent max steps   (default per-run step budget for agent test runs)
      - Agent step cap     (hard ceiling enforced at the config gate)

    \b
    Example:
      icx test configure
      icx test health
    """
    from icx_engine.config_manager import ConfigManager

    cfg = ConfigManager.load()

    base_url = typer.prompt(
        "Magik-AI base URL",
        default=cfg.magik_base_url,
    )
    api_key_input = typer.prompt(
        "Magik-AI API key (leave blank if not configured)",
        default="",
        hide_input=True,
        show_default=False,
    )
    max_iterations = typer.prompt(
        "Max fix iterations (re-test loops before the limit gate)",
        default=cfg.magik_max_iterations,
        type=int,
    )
    agent_max_steps = typer.prompt(
        "Agent max steps (default step budget for agent runs)",
        default=cfg.magik_agent_max_steps,
        type=int,
    )
    agent_step_cap = typer.prompt(
        "Agent step cap (hard ceiling)",
        default=cfg.magik_agent_step_cap,
        type=int,
    )

    cfg.magik_base_url = base_url.strip()
    cfg.magik_api_key = api_key_input.strip() or None
    cfg.magik_max_iterations = max(1, int(max_iterations))
    cfg.magik_agent_max_steps = max(1, int(agent_max_steps))
    cfg.magik_agent_step_cap = max(1, int(agent_step_cap))
    if cfg.magik_agent_step_cap < cfg.magik_agent_max_steps:
        console.print(
            f"[yellow]Note: step cap ({cfg.magik_agent_step_cap}) is below max steps "
            f"({cfg.magik_agent_max_steps}); runs will be clamped to the cap.[/yellow]"
        )
    ConfigManager.save(cfg)

    console.print("[green]Magik-AI settings saved.[/green]")
    console.print(f"  base_url:        {cfg.magik_base_url}")
    console.print(f"  api_key:         {'[set]' if cfg.magik_api_key else '[not configured]'}")
    console.print(f"  max_iters:       {cfg.magik_max_iterations}")
    console.print(f"  agent_max_steps: {cfg.magik_agent_max_steps}")
    console.print(f"  agent_step_cap:  {cfg.magik_agent_step_cap}")


@test_app.command("rules")
def test_rules(reset: bool = typer.Option(False, "--reset", help="Re-seed any missing default rule files.")) -> None:
    """Show the testing rulebook - the mandatory per-gate rules the agent must follow.

    Rules live as editable Markdown in ~/.icx/testing_rules/ (seeded from bundled
    defaults on first use, never overwriting your edits). ICX loads the relevant
    <gate>.md and injects its text into every gate, so editing a file changes agent
    behavior on the next gate - no code change, and it applies in every session.

    \b
    Example:
      icx test rules
      icx test rules --reset
    """
    from icx_engine.testing import rules as _rules

    _rules.ensure_seeded()
    d = _rules.rules_dir()
    console.print(f"Rulebook directory: [cyan]{d}[/cyan]")
    files = sorted(d.glob("*.md")) if d.exists() else []
    if not files:
        console.print("[yellow]No rule files found.[/yellow]")
        return
    for f in files:
        gate = f.stem
        req = _rules.required_sections(gate)
        line = f"  {f.name}"
        if req:
            line += f"   [dim](enforced sections: {', '.join(req)})[/dim]"
        console.print(line)
    console.print("\nEdit any file to change the mandatory rules for that gate. "
                  "Delete a file and run [cyan]icx test rules --reset[/cyan] to restore its default.")


# ---------------------------------------------------------------------------
# Sonar subcommands
# ---------------------------------------------------------------------------

def _sonar_add_flow(default_name: str = "default") -> None:
    from icx_engine.sonar import service
    name = typer.prompt("Connection name", default=default_name)
    url = typer.prompt("SonarQube server URL")
    token = typer.prompt("SonarQube token (blank to keep existing)", default="", hide_input=True, show_default=False)
    verify_tls = typer.confirm("Verify TLS certificates?", default=True)
    make_active = typer.confirm("Make this the active connection?", default=True)
    out = asyncio.run(service.add_connection(
        name.strip(), url.strip(), token.strip() or None,
        verify_tls=verify_tls, make_active=make_active,
    ))
    console.print(f"[green]Sonar connection '{out['name']}' saved.[/green]")
    console.print(f"  url:        {out['url']}")
    console.print(f"  verify_tls: {out['verify_tls']}")
    console.print(f"  active:     {out['active']}")
    v = out.get("validation") or {}
    if v.get("valid") is True:
        console.print(f"  connection: [green]ok[/green] (SonarQube {v.get('version', '')})")
    elif v.get("valid") is False:
        console.print(f"  connection: [red]failed[/red] {v.get('error', '')}")


def _sonar_resolve_name(value: str) -> str:
    """Map a 1-based index (from `icx status` / `icx sonar --list`) to a connection
    name; pass a name through unchanged."""
    if value and value.isdigit():
        from icx_engine.config_manager import ConfigManager
        names = list(ConfigManager.load().sonar_connections.keys())
        idx = int(value) - 1
        if 0 <= idx < len(names):
            return names[idx]
    return value


def _sonar_list() -> None:
    from icx_engine.sonar import service
    out = service.list_connections()
    if not out["connections"]:
        console.print("[yellow]No Sonar connections. Run `icx sonar --add`.[/yellow]")
        return
    for c in out["connections"]:
        mark = "[bold green][ACTIVE][/bold green]" if c["active"] else ""
        console.print(f"  {c['name']:<16} {c['url']:<40} verify_tls={c['verify_tls']} {mark}")


@sonar_app.callback(invoke_without_command=True)
def sonar_main(
    ctx: typer.Context,
    add: Annotated[bool, typer.Option("--add", help="Add a SonarQube server connection (interactive).")] = False,
    active: Annotated[Optional[str], typer.Option("--active", metavar="NAME", help="Set the active connection.")] = None,
    remove: Annotated[Optional[str], typer.Option("--remove", metavar="NAME", help="Remove a connection (clears its keyring token).")] = None,
    list_conns: Annotated[bool, typer.Option("--list", help="List connections and which is active.")] = False,
) -> None:
    """Manage SonarQube server connections - add, list, switch active, remove (same flag form as `icx model`).

    \b
    Examples:
      icx sonar --add                 Add a server connection (name, URL, token)
      icx sonar --list                List connections (bare `icx sonar` also lists)
      icx sonar --active prod         Make 'prod' the active connection
      icx sonar --active 2            Make connection #2 (from `icx status`) active
      icx sonar --remove prod         Remove 'prod'
      icx sonar --remove 2            Remove connection #2 (from `icx status`)
    """
    if ctx.invoked_subcommand is not None:
        return
    from icx_engine.sonar import service
    if add:
        _sonar_add_flow()
        return
    if active:
        try:
            out = service.set_active(_sonar_resolve_name(active))
        except KeyError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1)
        console.print(f"[green]Active Sonar connection: {out['active']}[/green]")
        return
    if remove:
        try:
            out = service.remove_connection(_sonar_resolve_name(remove))
        except KeyError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1)
        console.print(f"[green]Removed '{out['removed']}'.[/green] Active: {out['active'] or '(none)'}")
        return
    _sonar_list()


@sonar_app.command("status")
def sonar_status_cmd() -> None:
    """Show the active Sonar connection status."""
    from icx_engine.sonar import service
    out = asyncio.run(service.status())
    console.print(f"  active:     {out['active'] or '(none)'}")
    console.print(f"  connections:{out['count']}")
    console.print(f"  url:        {out['url']}")
    console.print(f"  configured: {out['configured']}")
    console.print(f"  verify_tls: {out['verify_tls']}")
    conn = out.get("connection")
    if conn is not None:
        console.print(f"  connection: {conn}")


@sonar_app.command("projects")
def sonar_projects_cmd(
    query: Annotated[Optional[str], typer.Option("--query", "-q", help="Filter projects by key/name substring")] = None,
) -> None:
    """List SonarQube projects the token can access (pick a key for `report`)."""
    from icx_engine.sonar import service
    try:
        out = asyncio.run(service.projects(query=query))
    except service.SonarDisabled as exc:
        console.print(f"[yellow]{exc}[/yellow]")
        raise typer.Exit(1)
    for p in out["projects"]:
        console.print(f"  {p['key']}  {p['name']}")
    if out["truncated"]:
        console.print(
            f"[yellow]{out['total']} projects total - list withheld or narrowed. "
            f"Filter with `icx sonar projects --query <term>`, or if you already know the "
            f"key run `icx sonar report --project <key> --branch <branch>` directly.[/yellow]")


@sonar_app.command("report")
def sonar_report_cmd(
    project: Annotated[str, typer.Option("--project", "-p", help="SonarQube project key")],
    branch: Annotated[Optional[str], typer.Option("--branch", "-b", help="Branch name")] = None,
    files: Annotated[Optional[list[str]], typer.Option("--file", "-f", help="Restrict to a file path (repeatable)")] = None,
    new_code: Annotated[bool, typer.Option("--new-code", help="Only findings in new code")] = False,
) -> None:
    """Print a compact Sonar summary (gate + counts). Use the MCP tools for full detail."""
    from icx_engine.sonar import service
    try:
        out = asyncio.run(service.report(
            project, branch=branch, files=list(files or []), new_code_only=new_code,
        ))
    except service.SonarDisabled as exc:
        console.print(f"[yellow]{exc}[/yellow]")
        raise typer.Exit(1)
    gate = out.get("quality_gate", {})
    m = out.get("measures", {})
    summary = out.get("summary", {})
    console.print(f"  quality gate: {gate.get('status', '')}")
    console.print(f"  bugs={m.get('bugs')} vulnerabilities={m.get('vulnerabilities')} "
                  f"code_smells={m.get('code_smells')} security_hotspots={m.get('security_hotspots')}")
    console.print(f"  coverage={m.get('coverage')} duplication={m.get('duplicated_lines_density')} "
                  f"debt={m.get('technical_debt')}")
    console.print(f"  findings: {summary.get('total', 0)} (truncated={out.get('truncated')})")
    by_sev = summary.get("by_severity", {})
    if by_sev:
        console.print(f"  by severity: {by_sev}")


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


def main() -> None:
    try:
        from icx_engine.logging_setup import configure_logging
        configure_logging()
        app()
    except KeyboardInterrupt:
        err_console.print("\nCancelled.")
        raise SystemExit(130)
