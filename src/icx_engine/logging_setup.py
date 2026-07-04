"""Opt-in logging bootstrap.

The library uses module-level `logging.getLogger(__name__)` throughout, but no
handler is configured, so `_log.debug(...)` calls are invisible by default (only
WARNING+ leaks via Python's lastResort handler). This wires a stderr handler for
the `icx_engine` logger namespace ONLY when `ICX_LOG_LEVEL` is set - an unset
value is a no-op, preserving the default behavior exactly.
"""
from __future__ import annotations

import logging
import os
import sys

_HANDLER_NAME = "icx_stderr"


def configure_logging() -> None:
    """Attach a stderr handler to the `icx_engine` logger when `ICX_LOG_LEVEL`
    is set (e.g. DEBUG, INFO, WARNING). No-op when unset or invalid. Idempotent:
    a second call updates the level rather than adding a duplicate handler."""
    level_name = os.environ.get("ICX_LOG_LEVEL", "").strip().upper()
    if not level_name:
        return
    level = getattr(logging, level_name, None)
    if not isinstance(level, int):
        return

    logger = logging.getLogger("icx_engine")
    for handler in logger.handlers:
        if getattr(handler, "name", None) == _HANDLER_NAME:
            logger.setLevel(level)
            return

    handler = logging.StreamHandler(sys.stderr)
    handler.name = _HANDLER_NAME
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False
