"""Workstatus integration - registered via icx_engine.integrations, not the
connectors/ registry (Workstatus is a time-tracking/attendance SaaS, not an
issue tracker - see developer.md's Workstatus section for why)."""
from __future__ import annotations

from icx_engine.integrations import register_integration
from icx_engine.workstatus.config import WorkstatusConfig

register_integration("workstatus", WorkstatusConfig)
