"""External MCP server gateway - lets a user register a curated, preset-only stdio-launched MCP
server (e.g. Microsoft's Playwright MCP), have ICX own its subprocess lifecycle, and reach its
tools through ICX's own icx_find_tools/icx_call_tool discovery pair, namespaced
`ext_<server>_<tool>`. Ships with zero presets by default (registry.PRESETS is empty) - a
deployment adds its own curated entries in code; a custom/arbitrary command can never be
registered from user input. See client.py (one subprocess+session), registry.py (which servers
are live, presets), mcp_tools.py (dynamic tool aggregation/dispatch - ICX's first non-static
module), service.py (CLI-facing config CRUD + smoke test)."""
