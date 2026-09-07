import json

import pytest
from mcp.types import Tool

from icx_engine.config_manager import ConfigManager
from icx_engine.mcp_gateway import mcp_tools, registry
from icx_engine.models.config import AppConfig, ExternalMcpServer


class _FakeClient:
    def __init__(self, tools=None, call_result=None):
        self._tools = tools if tools is not None else [Tool(name="navigate", description="go somewhere", inputSchema={"type": "object"})]
        self._call_result = call_result if call_result is not None else {"ok": True, "content": []}

    async def list_tools(self):
        return self._tools

    async def call_tool(self, name, arguments):
        return self._call_result


def _register(name, enabled=True, require_confirmation=False):
    cfg = AppConfig()
    cfg.external_mcp_servers[name] = ExternalMcpServer(
        name=name, command="npx", args=[], enabled=enabled, require_confirmation=require_confirmation,
    )
    ConfigManager.save(cfg)


@pytest.fixture(autouse=True)
def _reset(isolated_config):
    registry._CLIENTS.clear()
    yield
    registry._CLIENTS.clear()


async def test_external_tools_by_server_namespaces_and_marks_external(monkeypatch):
    _register("playwright")
    fake = _FakeClient()
    monkeypatch.setattr(registry, "get_client", lambda name: fake)

    by_server = await mcp_tools.external_tools_by_server()
    assert list(by_server.keys()) == ["playwright"]
    tool = by_server["playwright"][0]
    assert tool.name == "ext_playwright_navigate"
    assert "[EXTERNAL" in tool.description
    assert "playwright" in tool.description


async def test_external_tools_flattens_all_servers(monkeypatch):
    _register("playwright")
    fake = _FakeClient()
    monkeypatch.setattr(registry, "get_client", lambda name: fake)

    flat = await mcp_tools.external_tools()
    assert [t.name for t in flat] == ["ext_playwright_navigate"]


async def test_dispatch_returns_none_for_non_external_name():
    assert await mcp_tools.dispatch_external_tool("git_repo_status", {}) is None


async def test_dispatch_returns_none_for_unregistered_server():
    assert await mcp_tools.dispatch_external_tool("ext_nosuch_tool", {}) is None


async def test_dispatch_forwards_to_correct_client_longest_match_first(monkeypatch):
    _register("play")
    _register("playwright")
    fake_play = _FakeClient(call_result={"ok": True, "content": [{"text": "SHORT"}]})
    fake_playwright = _FakeClient(call_result={"ok": True, "content": [{"text": "LONG"}]})

    def _get(name):
        return {"play": fake_play, "playwright": fake_playwright}[name]
    monkeypatch.setattr(registry, "get_client", _get)

    result = await mcp_tools.dispatch_external_tool("ext_playwright_navigate", {"url": "x"})
    payload = json.loads(result[0].text)
    assert payload["content"][0]["text"] == "LONG"


async def test_dispatch_to_disabled_server_returns_clear_error():
    _register("playwright", enabled=False)
    result = await mcp_tools.dispatch_external_tool("ext_playwright_navigate", {})
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert "not enabled" in payload["error"]


async def test_dispatch_with_require_confirmation_gates_then_allows(monkeypatch):
    _register("playwright", require_confirmation=True)
    fake = _FakeClient(call_result={"ok": True, "content": [{"text": "did it"}]})
    monkeypatch.setattr(registry, "get_client", lambda name: fake)

    first = await mcp_tools.dispatch_external_tool("ext_playwright_click", {"selector": "#go"})
    first_payload = json.loads(first[0].text)
    assert first_payload["ok"] is False
    assert first_payload["pending_confirmation"] is True
    token = first_payload["confirm_token"]

    second = await mcp_tools.dispatch_external_tool(
        "ext_playwright_click", {"selector": "#go", "confirm_token": token},
    )
    second_payload = json.loads(second[0].text)
    assert second_payload["ok"] is True
    assert second_payload["content"][0]["text"] == "did it"


async def test_dispatch_with_wrong_confirm_token_is_rejected(monkeypatch):
    _register("playwright", require_confirmation=True)
    monkeypatch.setattr(registry, "get_client", lambda name: _FakeClient())

    result = await mcp_tools.dispatch_external_tool(
        "ext_playwright_click", {"selector": "#go", "confirm_token": "not-a-real-token"},
    )
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert "invalid" in payload["error"]
