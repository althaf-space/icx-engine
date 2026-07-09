"""Pluggable integration registry + generic secret handling (finding A1 / Phase C-4)."""
import json

import pytest
from pydantic import BaseModel, Field

from icx_engine import integrations as I
from icx_engine.config_manager import ConfigManager, _SENTINEL
from icx_engine.models.config import AppConfig


class _DummyIntegration(BaseModel):
    base_url: str = "https://default"
    token: str | None = Field(default=None, exclude=True)


@pytest.fixture
def dummy_registered():
    I.register_integration("dummy", _DummyIntegration)
    try:
        yield
    finally:
        I._INTEGRATION_MODELS.pop("dummy", None)


def test_register_and_get_model(dummy_registered):
    assert I.get_integration_model("dummy") is _DummyIntegration
    assert "dummy" in I.registered_integrations()
    assert I.get_integration_model("nope") is None


def test_secret_fields_introspection(dummy_registered):
    assert I.integration_secret_fields("dummy") == ["token"]      # exclude=True field
    assert I.integration_secret_fields("unregistered") == []


def test_appconfig_integration_accessor(dummy_registered):
    cfg = AppConfig(integrations={"dummy": {"base_url": "https://y", "token": "t"}})
    model = cfg.integration("dummy")
    assert model.base_url == "https://y"
    assert model.token == "t"
    assert AppConfig().integration("dummy") is None      # no stored data
    assert cfg.integration("unregistered") is None       # not registered


def test_backward_compat_config_without_integrations_key():
    # Existing config files have no "integrations" key - must load unchanged.
    cfg = AppConfig.model_validate({"connections": [], "llm_profiles": {}})
    assert cfg.integrations == {}
    assert cfg.integration("dummy") is None


def test_integration_secret_round_trips_via_keyring(dummy_registered, isolated_config, monkeypatch):
    store: dict[str, str] = {}
    monkeypatch.setattr("icx_engine.config_manager._check_keychain", lambda: True)
    monkeypatch.setattr(
        "icx_engine.config_manager._kset",
        lambda a, v: (store.__setitem__(a, v) or True),
    )
    monkeypatch.setattr("icx_engine.config_manager._kget", lambda a: store.get(a))

    cfg = AppConfig(integrations={"dummy": {"base_url": "https://y", "token": "secret123"}})
    ConfigManager.save(cfg)

    disk = isolated_config.read_text(encoding="utf-8")
    assert "secret123" not in disk                        # secret never written plaintext
    raw = json.loads(disk)
    assert raw["integrations"]["dummy"]["token"] == _SENTINEL
    assert raw["integrations"]["dummy"]["base_url"] == "https://y"

    loaded = ConfigManager.load()
    assert loaded.integration("dummy").token == "secret123"   # resolved from keyring
    assert loaded.integration("dummy").base_url == "https://y"
