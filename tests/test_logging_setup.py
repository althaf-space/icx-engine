"""Opt-in logging bootstrap (finding O1)."""
import logging

from icx_engine.logging_setup import configure_logging, _HANDLER_NAME


def _icx_handlers():
    return [
        h for h in logging.getLogger("icx_engine").handlers
        if getattr(h, "name", None) == _HANDLER_NAME
    ]


def _cleanup():
    lg = logging.getLogger("icx_engine")
    for h in list(lg.handlers):
        if getattr(h, "name", None) == _HANDLER_NAME:
            lg.removeHandler(h)
    lg.setLevel(logging.NOTSET)
    lg.propagate = True


def test_unset_env_is_noop(monkeypatch):
    monkeypatch.delenv("ICX_LOG_LEVEL", raising=False)
    _cleanup()
    configure_logging()
    assert _icx_handlers() == []


def test_invalid_level_is_noop(monkeypatch):
    _cleanup()
    monkeypatch.setenv("ICX_LOG_LEVEL", "NOTALEVEL")
    configure_logging()
    assert _icx_handlers() == []


def test_set_debug_attaches_handler(monkeypatch):
    _cleanup()
    monkeypatch.setenv("ICX_LOG_LEVEL", "DEBUG")
    try:
        configure_logging()
        assert len(_icx_handlers()) == 1
        assert logging.getLogger("icx_engine").level == logging.DEBUG
    finally:
        _cleanup()


def test_idempotent_no_duplicate_handler(monkeypatch):
    _cleanup()
    monkeypatch.setenv("ICX_LOG_LEVEL", "INFO")
    try:
        configure_logging()
        configure_logging()
        assert len(_icx_handlers()) == 1
    finally:
        _cleanup()
