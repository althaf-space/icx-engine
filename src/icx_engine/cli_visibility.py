"""Single source of truth for the CLI-help-visibility flag. Code-level constant, NOT a user
config (not in AppConfig/config.json, deliberate) - hides the operational/feature-work CLI
surface from `--help` (an agent reaches that surface via MCP tools, never `--help`), keeping only
connection-setup and connection-testing commands visible to a human at the terminal.

`hidden=True` (Typer's own kwarg on `.command()`/`.add_typer()`) only removes a command from
`--help` listing - every hidden command still runs if invoked by exact name, nothing is actually
disabled. Flip to False locally to see the full CLI surface.

Lives in its own module, not `cli.py`, so per-module CLI command files (`jira/cli_commands.py`,
`git/cli_commands.py`, etc.) can import it without a circular import back into `cli.py`."""

AGENT_ONLY_CLI_HIDDEN = True
