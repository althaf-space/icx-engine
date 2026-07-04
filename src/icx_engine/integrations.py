"""Pluggable third-party integration registry.

Lets a new integration store its settings under `AppConfig.integrations[name]`
and register a Pydantic config model, WITHOUT adding fields to the core
`AppConfig`. Secret fields (declared `Field(..., exclude=True)` on the model)
are routed through the OS keyring by `config_manager`, exactly like other ICX
secrets, under the account name `integration_secret:<name>:<field>`.

This mirrors the connector registry pattern (`connectors/base.register_connector`).

Note: the existing Magik-AI and Sonar settings remain inline on `AppConfig` for
backward compatibility (existing config files + stored secrets). New integrations
should use this registry instead of extending `AppConfig`.
"""
from __future__ import annotations

from pydantic import BaseModel

_INTEGRATION_MODELS: dict[str, type[BaseModel]] = {}


def register_integration(name: str, model_cls: type[BaseModel]) -> None:
    """Register a config model for the integration `name`."""
    _INTEGRATION_MODELS[name] = model_cls


def get_integration_model(name: str) -> type[BaseModel] | None:
    return _INTEGRATION_MODELS.get(name)


def registered_integrations() -> dict[str, type[BaseModel]]:
    return dict(_INTEGRATION_MODELS)


def integration_secret_fields(name: str) -> list[str]:
    """Return the names of `Field(..., exclude=True)` fields on the registered
    model - these are stored in the OS keyring, never serialized to disk."""
    model_cls = _INTEGRATION_MODELS.get(name)
    if model_cls is None:
        return []
    return [
        fname
        for fname, field in model_cls.model_fields.items()
        if getattr(field, "exclude", None) is True
    ]
