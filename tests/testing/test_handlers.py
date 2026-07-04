import pytest
from icx_engine.testing.handlers import get_handler, TestModeHandler, UiHandler, ApiHandler, AgentHandler
from icx_engine.testing.classify import FileClass


def test_registry_resolves_each_mode():
    assert isinstance(get_handler("ui"), UiHandler)
    assert isinstance(get_handler("agent"), AgentHandler)
    assert isinstance(get_handler("api"), ApiHandler)


def test_unknown_mode_raises():
    with pytest.raises(ValueError):
        get_handler("nope")


def test_relevant_layers():
    assert get_handler("ui").relevant_layers() == {"frontend", "shared"}
    assert get_handler("agent").relevant_layers() == {"frontend", "shared"}
    assert get_handler("api").relevant_layers() == {"backend", "shared"}


def test_handler_compat_delegates():
    fc = FileClass(path="x.java", layer="backend", testability={"exposes_endpoint": False})
    v = get_handler("api").compat(fc)
    assert v.compatible is False


class _FakeClient:
    def __init__(self):
        self.calls = []

    async def submit_ui_test(self, **kw):
        self.calls.append(("ui", kw)); return {"runId": "ui-1"}

    async def submit_agent_run(self, **kw):
        self.calls.append(("agent", kw)); return {"runId": "agent-1"}

    async def submit_api_test(self, **kw):
        self.calls.append(("api", kw)); return {"runId": "ui-2"}


@pytest.mark.asyncio
async def test_ui_handler_submits_ui_test():
    c = _FakeClient()
    state = {"url": "http://host-x/app", "headless": True, "agent_provider": "openai",
             "profile_screen": None}
    data = await get_handler("ui").submit(c, state)
    assert data["runId"] == "ui-1"
    assert c.calls[0][0] == "ui"
    assert c.calls[0][1]["url"] == "http://host-x/app"


@pytest.mark.asyncio
async def test_api_handler_submits_api_test():
    c = _FakeClient()
    state = {"api_endpoint": "http://host-x/api/items", "api_method": "POST",
             "api_payload": "{}", "api_payload_type": "json", "api_headers": None}
    data = await get_handler("api").submit(c, state)
    assert c.calls[0][0] == "api"
    assert c.calls[0][1]["endpoint"] == "http://host-x/api/items"


@pytest.mark.asyncio
async def test_ui_handler_forwards_session():
    captured = {}
    class C:
        async def submit_ui_test(self, **kw):
            captured.update(kw); return {"runId": "ui-1"}
    state = {"url": "http://host-x/app", "headless": True, "agent_provider": "openai",
             "profile_screen": None, "_auth_session_id": "s5", "auto_auth_recover": False}
    await get_handler("ui").submit(C(), state)
    assert captured["session_id"] == "s5"
    assert captured["auto_auth_recover"] is False


@pytest.mark.asyncio
async def test_agent_handler_forwards_session():
    captured = {}
    class C:
        async def submit_agent_run(self, **kw):
            captured.update(kw); return {"runId": "agent-1"}
    state = {"url": "http://host-x/app", "headless": True, "agent_provider": "openai",
             "context": "goal", "agent_max_steps": 20, "_auth_session_id": "s7", "auto_auth_recover": True}
    await get_handler("agent").submit(C(), state)
    assert captured["session_id"] == "s7"
    assert captured["auto_auth_recover"] is True
