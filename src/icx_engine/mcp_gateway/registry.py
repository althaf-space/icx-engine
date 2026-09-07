"""Registry of live ExternalMcpClient instances, one per registered+enabled external MCP server.
A client is constructed here but only actually started lazily, on first list_tools()/call_tool()
- registering a server never spawns a subprocess by itself, and neither does ICX's own startup;
only real use of that server's tools does."""
from __future__ import annotations

from icx_engine.mcp_gateway.client import ExternalMcpClient

_CLIENTS: dict[str, ExternalMcpClient] = {}

# Curated, pinned presets a user can add with `icx mcp-external --add --preset <name>`, following
# the same "pin an exact version, bump deliberately, never latest" discipline as
# testing/runners/install.py's RUNNER_SPECS. Empty by default - ICX ships with zero external MCP
# servers; each deployment adds its own curated entries here in code (never at runtime from user
# input). Example shape, if adding one:
#   "playwright": {
#       "command": "npx", "args": ["-y", "@playwright/mcp@0.0.29"], "env": {},
#       "description": "Microsoft's official Playwright MCP server - live browser automation tools.",
#   },
PRESETS: dict[str, dict] = {}


def _enabled_servers() -> dict:
    from icx_engine.config_manager import ConfigManager  # noqa: PLC0415
    config = ConfigManager.load()
    return {n: s for n, s in config.external_mcp_servers.items() if s.enabled}


def get_client(name: str) -> ExternalMcpClient | None:
    """Returns the cached client for a currently enabled server, constructing (but not starting)
    it on first request. None if the server isn't registered or isn't enabled - and drops any
    stale cached client for a server that was just disabled/removed, so a later re-enable starts
    fresh rather than reusing a client built from stale config."""
    servers = _enabled_servers()
    server = servers.get(name)
    if server is None:
        _CLIENTS.pop(name, None)
        return None
    client = _CLIENTS.get(name)
    if client is None:
        client = ExternalMcpClient(name, server.command, server.args, server.env or None)
        _CLIENTS[name] = client
    return client


def enabled_server_names() -> list[str]:
    return list(_enabled_servers().keys())


def all_server_names() -> list[str]:
    """Every registered server name, enabled or not - used for tool-name splitting so a disabled
    server's tool call gets a clear "not enabled" error instead of silently falling through to a
    generic unknown-tool error."""
    from icx_engine.config_manager import ConfigManager  # noqa: PLC0415
    return list(ConfigManager.load().external_mcp_servers.keys())


async def shutdown_all() -> None:
    """Closes every live client's subprocess. Called from mcp_server.py's _serve() shutdown path
    - guarded there, never fatal if this itself fails."""
    for client in list(_CLIENTS.values()):
        await client.close()
    _CLIENTS.clear()
