import pytest

from icx_engine.config_manager import ConfigManager
from icx_engine.mcp_gateway import service


def test_presets_empty_by_default():
    assert service.PRESETS == {}


def test_resolve_preset_known(monkeypatch):
    monkeypatch.setitem(service.PRESETS, "fake", {"command": "npx", "args": ["-y", "fake-mcp@1.0.0"]})
    preset = service.resolve_preset("fake")
    assert preset["command"] == "npx"


def test_resolve_preset_unknown_raises():
    with pytest.raises(KeyError):
        service.resolve_preset("nosuchpreset")


def test_add_list_server(isolated_config):
    service.add_server("playwright", "npx", args=["-y", "@playwright/mcp@0.0.29"], enabled=True, preset="playwright")
    servers = service.list_servers()
    assert len(servers) == 1
    assert servers[0]["name"] == "playwright"
    assert servers[0]["enabled"] is True
    assert servers[0]["preset"] == "playwright"


def test_set_enabled_toggles(isolated_config):
    service.add_server("s", "npx", enabled=True)
    out = service.set_enabled("s", False)
    assert out["enabled"] is False
    servers = service.list_servers()
    assert servers[0]["enabled"] is False


def test_set_enabled_unknown_raises(isolated_config):
    with pytest.raises(KeyError):
        service.set_enabled("nosuch", True)


def test_remove_server_deletes_config_and_secrets(isolated_config, monkeypatch):
    deleted = []
    import icx_engine.config_manager as cm
    monkeypatch.setattr(cm, "_check_keychain", lambda: True)
    monkeypatch.setattr(cm, "_kset", lambda a, v: True)
    monkeypatch.setattr(cm, "_kdel", lambda account: deleted.append(account))

    service.add_server("s", "npx", env={"K": "secret"})
    service.remove_server("s")

    assert service.list_servers() == []
    assert "external_mcp_env:s:K" in deleted


def test_remove_unknown_server_raises(isolated_config):
    with pytest.raises(KeyError):
        service.remove_server("nosuch")


async def test_test_server_reports_tools(isolated_config, monkeypatch):
    from mcp.types import Tool
    from icx_engine.mcp_gateway.client import ExternalMcpClient

    async def fake_list_tools(self):
        return [Tool(name="navigate", description="d", inputSchema={"type": "object"})]

    async def fake_close(self):
        return None

    monkeypatch.setattr(ExternalMcpClient, "list_tools", fake_list_tools)
    monkeypatch.setattr(ExternalMcpClient, "close", fake_close)

    service.add_server("playwright", "npx", args=["-y", "@playwright/mcp@0.0.29"])
    out = await service.test_server("playwright")
    assert out["ok"] is True
    assert out["tool_count"] == 1
    assert out["tools"] == ["navigate"]


async def test_test_server_unknown_raises(isolated_config):
    with pytest.raises(KeyError):
        await service.test_server("nosuch")
