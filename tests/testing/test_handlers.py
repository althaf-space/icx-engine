import pytest
from icx_engine.testing.handlers import get_handler, TestModeHandler, ApiHandler, AgentHandler
from icx_engine.testing.classify import FileClass


def test_registry_resolves_each_mode():
    assert isinstance(get_handler("agent"), AgentHandler)
    assert isinstance(get_handler("api"), ApiHandler)


def test_unknown_mode_raises():
    with pytest.raises(ValueError):
        get_handler("nope")


def test_relevant_layers():
    assert get_handler("agent").relevant_layers() == {"frontend", "shared"}
    assert get_handler("api").relevant_layers() == {"backend", "shared"}


def test_handler_compat_delegates():
    fc = FileClass(path="x.java", layer="backend", testability={"exposes_endpoint": False})
    v = get_handler("api").compat(fc)
    assert v.compatible is False
