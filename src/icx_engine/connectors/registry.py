from __future__ import annotations
from icx_engine.connectors.base import _CONNECTION_CLASSES, _connector_registry

_connector_registry()

CONNECTION_REGISTRY: dict = _CONNECTION_CLASSES
