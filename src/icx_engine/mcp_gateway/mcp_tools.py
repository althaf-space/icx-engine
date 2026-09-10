"""Dynamic tool aggregation/dispatch for registered external MCP servers - see registry.py and
client.py. Unlike every other module's `<MODULE>_TOOLS`, this module's tool list is NOT a static,
import-time constant - it depends on which servers are registered+enabled in the live config, and
is queried fresh on every icx_find_tools/tools-list call. This is ICX's first dynamic module,
called out explicitly in developer.md's MCP tool architecture section.

Every proxied tool's name is namespaced `ext_<server>_<tool>` and its description gets an
EXTERNAL provenance prefix so the agent never mistakes third-party subprocess code for an
ICX-authored tool. Confirmation gating is opt-in per server (`require_confirmation`), not
automatic - ICX cannot itself know which of an arbitrary external server's tools are destructive.
"""
from __future__ import annotations

import json

from mcp.types import TextContent, Tool

from icx_engine.mcp_gateway import registry

_PREFIX = "ext_"


def _namespaced(server: str, tool_name: str) -> str:
    return f"{_PREFIX}{server}_{tool_name}"


async def external_tools_by_server() -> dict[str, list[Tool]]:
    """One entry per enabled server, each a list of namespaced Tool copies - never the server's
    raw Tool objects, since the name must carry the ext_ prefix before mcp_server.py ever sees
    it, and the description must carry the EXTERNAL provenance note (plus a confirmation-required
    note when that server's require_confirmation is set)."""
    from icx_engine.config_manager import ConfigManager  # noqa: PLC0415

    config = ConfigManager.load()
    out: dict[str, list[Tool]] = {}
    for name in registry.enabled_server_names():
        client = registry.get_client(name)
        if client is None:
            continue
        server_cfg = config.external_mcp_servers.get(name)
        confirm_note = (
            " REQUIRES CONFIRMATION: the first call returns pending_confirmation plus a token; "
            "call again with confirm_token to actually execute."
            if server_cfg is not None and server_cfg.require_confirmation else ""
        )
        raw_tools = await client.list_tools()
        namespaced = [
            Tool(
                name=_namespaced(name, t.name),
                description=(
                    f"[EXTERNAL - server {name!r}, unverified by ICX]{confirm_note} "
                    f"{t.description or ''}"
                ).strip(),
                inputSchema=t.inputSchema,
                annotations=t.annotations,
            )
            for t in raw_tools
        ]
        out[name] = namespaced
    return out


async def external_tools() -> list[Tool]:
    by_server = await external_tools_by_server()
    return [t for tools in by_server.values() for t in tools]


async def dispatch_external_tool(name: str, arguments: dict) -> list[TextContent] | None:
    """Returns None when `name` doesn't match ext_<enabled-server>_<tool> for any currently
    enabled server, so mcp_server.py's existing fallthrough dispatch chain keeps working
    unmodified for every other module."""
    if not name.startswith(_PREFIX):
        return None
    rest = name[len(_PREFIX):]

    # Matched against every REGISTERED server (enabled or not) so a disabled server's tool call
    # gets a clear "not enabled" error from _dispatch_one below, instead of silently falling
    # through to a generic unknown-tool error. Longest server-name-first, so a server whose name
    # is a prefix of another registered server's name (e.g. "play" vs "playwright") is never
    # mis-split.
    for server_name in sorted(registry.all_server_names(), key=len, reverse=True):
        server_prefix = f"{server_name}_"
        if not rest.startswith(server_prefix):
            continue
        tool_name = rest[len(server_prefix):]
        return await _dispatch_one(server_name, tool_name, arguments)
    return None


async def _dispatch_one(server_name: str, tool_name: str, arguments: dict) -> list[TextContent]:
    from icx_engine.config_manager import ConfigManager  # noqa: PLC0415
    from icx_engine.confirm import issue_token, verify_token  # noqa: PLC0415

    config = ConfigManager.load()
    server_cfg = config.external_mcp_servers.get(server_name)
    if server_cfg is None or not server_cfg.enabled:
        return [TextContent(type="text", text=json.dumps(
            {"ok": False, "error": f"external MCP server {server_name!r} is not enabled."}
        ))]

    if server_cfg.require_confirmation:
        action = f"external_mcp:{server_name}:{tool_name}"
        confirm_token = arguments.get("confirm_token")
        inner_args = {k: v for k, v in arguments.items() if k != "confirm_token"}
        if not confirm_token:
            token = issue_token(action, inner_args)
            return [TextContent(type="text", text=json.dumps({
                "ok": False, "pending_confirmation": True, "confirm_token": token,
                "server": server_name, "tool": tool_name, "arguments": inner_args,
                "message": (
                    f"External MCP server {server_name!r} requires confirmation for every call. "
                    "Show the human the tool name and arguments above, get explicit agreement, "
                    "then call again with this confirm_token."
                ),
            }))]
        payload = verify_token(confirm_token, action)
        if payload is None:
            return [TextContent(type="text", text=json.dumps(
                {"ok": False, "error": "invalid or reused confirm_token for this external tool call."}
            ))]
        arguments = payload

    client = registry.get_client(server_name)
    if client is None:
        return [TextContent(type="text", text=json.dumps(
            {"ok": False, "error": f"external MCP server {server_name!r} is not enabled."}
        ))]
    result = await client.call_tool(tool_name, arguments)
    return [TextContent(type="text", text=json.dumps(result))]
