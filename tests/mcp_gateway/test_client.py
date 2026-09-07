import asyncio
from contextlib import asynccontextmanager

import pytest
from mcp.types import CallToolResult, ListToolsResult, TextContent as MCPTextContent, Tool

import icx_engine.mcp_gateway.client as client_module
from icx_engine.mcp_gateway.client import ExternalMcpClient


class _FakeSession:
    fail_start = False
    fail_call = False

    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def initialize(self):
        if self.fail_start:
            raise RuntimeError("boom on initialize")
        return None

    async def list_tools(self):
        return ListToolsResult(tools=[Tool(name="foo", description="d", inputSchema={"type": "object"})])

    async def call_tool(self, name, arguments):
        if self.fail_call:
            raise RuntimeError("boom on call")
        return CallToolResult(content=[MCPTextContent(type="text", text="ok")], isError=False)


@asynccontextmanager
async def _fake_stdio_client(params):
    yield (None, None)


@asynccontextmanager
async def _failing_stdio_client(params):
    raise RuntimeError("spawn failed")
    yield  # pragma: no cover


@pytest.fixture
def patched(monkeypatch):
    monkeypatch.setattr(client_module, "stdio_client", _fake_stdio_client)
    monkeypatch.setattr(client_module, "ClientSession", _FakeSession)
    _FakeSession.fail_start = False
    _FakeSession.fail_call = False
    yield


async def test_list_tools_returns_expected_set(patched):
    c = ExternalMcpClient("test", "fake-cmd", [])
    tools = await c.list_tools()
    assert [t.name for t in tools] == ["foo"]
    await c.close()


async def test_call_tool_forwards_and_returns_result(patched):
    c = ExternalMcpClient("test", "fake-cmd", [])
    result = await c.call_tool("foo", {"x": 1})
    assert result["ok"] is True
    assert result["content"][0]["text"] == "ok"
    await c.close()


async def test_start_failure_never_raises(monkeypatch, patched):
    monkeypatch.setattr(client_module, "stdio_client", _failing_stdio_client)
    c = ExternalMcpClient("test", "fake-cmd", [])
    ok = await c.ensure_started()
    assert ok is False
    assert c.start_error is not None
    result = await c.call_tool("foo", {})
    assert result["ok"] is False
    assert "not reachable" in result["error"]
    await c.close()


async def test_call_failure_after_successful_start_never_raises(patched):
    _FakeSession.fail_call = True
    c = ExternalMcpClient("test", "fake-cmd", [])
    result = await c.call_tool("foo", {})
    assert result["ok"] is False
    assert "call failed" in result["error"]
    await c.close()


async def test_close_terminates_cleanly_even_if_never_started(patched):
    c = ExternalMcpClient("test", "fake-cmd", [])
    await c.close()  # must not raise
    assert c.started is False
