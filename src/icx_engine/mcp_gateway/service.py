"""CLI-facing operations for external MCP server registration - synchronous config CRUD (mirrors
gitlab/service.py's shape) plus one async smoke-test that spawns a server, lists its tools, and
shuts it down again without leaving anything in the live registry."""
from __future__ import annotations

from typing import Any

from icx_engine.config_manager import ConfigManager
from icx_engine.mcp_gateway.registry import PRESETS


def list_servers(cfg: Any | None = None) -> list[dict]:
    cfg = cfg or ConfigManager.load()
    return [
        {
            "name": s.name, "command": s.command, "args": s.args, "enabled": s.enabled,
            "require_confirmation": s.require_confirmation, "preset": s.preset,
            "env_keys": sorted(s.env.keys()),
        }
        for s in cfg.external_mcp_servers.values()
    ]


def resolve_preset(preset: str) -> dict:
    if preset not in PRESETS:
        raise KeyError(f"No preset named '{preset}'. Available: {sorted(PRESETS.keys())}")
    return PRESETS[preset]


def add_server(
    name: str, command: str, args: list[str] | None = None, env: dict[str, str] | None = None,
    enabled: bool = True, require_confirmation: bool = False, preset: str | None = None,
    cfg: Any | None = None,
) -> dict:
    from icx_engine.models.config import ExternalMcpServer  # noqa: PLC0415

    cfg = cfg or ConfigManager.load()
    cfg.external_mcp_servers[name] = ExternalMcpServer(
        name=name, command=command, args=list(args or []), env=dict(env or {}),
        enabled=enabled, require_confirmation=require_confirmation, preset=preset,
    )
    ConfigManager.save(cfg)
    ConfigManager.warn_if_plaintext()
    return {"added": name, "enabled": enabled}


def remove_server(name: str, cfg: Any | None = None) -> dict:
    cfg = cfg or ConfigManager.load()
    server = cfg.external_mcp_servers.get(name)
    if server is None:
        raise KeyError(f"No external MCP server named '{name}'.")
    env_keys = list(server.env.keys())
    del cfg.external_mcp_servers[name]
    ConfigManager.save(cfg)
    ConfigManager.delete_external_mcp_server_secret(name, env_keys)
    return {"removed": name}


def set_enabled(name: str, enabled: bool, cfg: Any | None = None) -> dict:
    cfg = cfg or ConfigManager.load()
    server = cfg.external_mcp_servers.get(name)
    if server is None:
        raise KeyError(f"No external MCP server named '{name}'.")
    server.enabled = enabled
    ConfigManager.save(cfg)
    return {"name": name, "enabled": enabled}


async def test_server(name: str, cfg: Any | None = None) -> dict:
    """Spawns a FRESH client (never the cached registry one - a test run must never leave a
    lingering process behind), lists its tools, then closes it immediately."""
    from icx_engine.mcp_gateway.client import ExternalMcpClient  # noqa: PLC0415

    cfg = cfg or ConfigManager.load()
    server = cfg.external_mcp_servers.get(name)
    if server is None:
        raise KeyError(f"No external MCP server named '{name}'.")
    client = ExternalMcpClient(name, server.command, server.args, server.env or None)
    try:
        tools = await client.list_tools()
        if not tools and client.start_error:
            return {"ok": False, "name": name, "error": client.start_error}
        return {"ok": True, "name": name, "tool_count": len(tools), "tools": [t.name for t in tools]}
    finally:
        await client.close()
