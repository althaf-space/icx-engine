import pytest

from icx_engine.config_manager import ConfigManager
from icx_engine.mcp_gateway import registry
from icx_engine.models.config import AppConfig, ExternalMcpServer


@pytest.fixture(autouse=True)
def _reset_registry():
    """_CLIENTS is a module-level cache - reset it between tests so one test's registered
    server/client never leaks into another."""
    registry._CLIENTS.clear()
    yield
    registry._CLIENTS.clear()


def _save_server(name, enabled):
    cfg = AppConfig()
    cfg.external_mcp_servers[name] = ExternalMcpServer(
        name=name, command="npx", args=["-y", "@playwright/mcp@0.0.29"], enabled=enabled,
    )
    ConfigManager.save(cfg)


def test_presets_ship_empty_by_default():
    assert registry.PRESETS == {}


def test_disabled_server_never_appears_enabled(isolated_config):
    _save_server("s1", enabled=False)
    assert "s1" not in registry.enabled_server_names()
    assert registry.get_client("s1") is None


def test_enabled_server_lazily_gets_a_client_not_started(isolated_config):
    _save_server("s1", enabled=True)
    assert "s1" in registry.enabled_server_names()
    client = registry.get_client("s1")
    assert client is not None
    assert client.started is False  # constructed, not started - lazy


def test_get_client_caches_the_same_instance(isolated_config):
    _save_server("s1", enabled=True)
    a = registry.get_client("s1")
    b = registry.get_client("s1")
    assert a is b


async def test_shutdown_all_clears_the_registry(isolated_config):
    _save_server("s1", enabled=True)
    registry.get_client("s1")
    await registry.shutdown_all()
    assert registry.get_client("s1") is not None  # a fresh client is fine post-shutdown
