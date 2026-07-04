import pytest
import respx
import httpx
from icx_engine.testing.client import MagikClient, MagikUnreachable, MagikRunLost


MAGIK_BASE = "http://localhost:7646"


@pytest.fixture
def client():
    return MagikClient(base_url=MAGIK_BASE, api_key="test-key")


@respx.mock
async def test_health_check_success(client):
    respx.get(f"{MAGIK_BASE}/api/health").mock(
        return_value=httpx.Response(200, json={"ok": True, "data": {"status": "up", "uptimeSec": 42}})
    )
    result = await client.health_check()
    assert result["status"] == "up"


@respx.mock
async def test_health_check_unreachable_raises(client):
    respx.get(f"{MAGIK_BASE}/api/health").mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(MagikUnreachable):
        await client.health_check()


@respx.mock
async def test_get_active_profile_none_when_404(client):
    respx.get(f"{MAGIK_BASE}/api/v1/profiles/active").mock(
        return_value=httpx.Response(404, json={"ok": False, "error": {"status": 404, "message": "not found"}})
    )
    result = await client.get_active_profile()
    assert result is None


@respx.mock
async def test_submit_ui_test_returns_run_id(client):
    respx.post(f"{MAGIK_BASE}/api/v1/ui-tests").mock(
        return_value=httpx.Response(202, json={
            "ok": True,
            "data": {"runId": "ui-123-abc", "streamUrl": "/stream", "reportUrl": "/report", "startedAt": "2026-01-01T00:00:00Z"}
        })
    )
    result = await client.submit_ui_test(url="http://localhost:3000/login", profile_screen=None)
    assert result["runId"] == "ui-123-abc"


@respx.mock
async def test_submit_ui_test_includes_profile_screen_when_set(client):
    route = respx.post(f"{MAGIK_BASE}/api/v1/ui-tests").mock(
        return_value=httpx.Response(202, json={"ok": True, "data": {"runId": "ui-456", "streamUrl": "", "reportUrl": "", "startedAt": ""}})
    )
    await client.submit_ui_test(url="http://localhost:3000/login", profile_screen="Login")
    sent_body = route.calls[0].request.content
    import json
    body = json.loads(sent_body)
    assert body["profileScreen"] == "Login"


@respx.mock
async def test_submit_api_test_returns_run_id(client):
    respx.post(f"{MAGIK_BASE}/api/v1/api-tests").mock(
        return_value=httpx.Response(202, json={"ok": True, "data": {"runId": "api-789", "streamUrl": "", "reportUrl": "", "startedAt": ""}})
    )
    result = await client.submit_api_test(
        endpoint="http://localhost:8080/api/users",
        method="POST",
        payload='{"name":"test"}',
        payload_type="json",
    )
    assert result["runId"] == "api-789"


@respx.mock
async def test_submit_agent_run_returns_run_id(client):
    respx.post(f"{MAGIK_BASE}/api/v1/agent-runs").mock(
        return_value=httpx.Response(202, json={"ok": True, "data": {"runId": "agent-abc", "streamUrl": "", "reportUrl": "", "startedAt": ""}})
    )
    result = await client.submit_agent_run(
        url="http://localhost:3000/login",
        goal="Sign in with admin/admin123",
    )
    assert result["runId"] == "agent-abc"


@respx.mock
async def test_get_run_status_ui(client):
    respx.get(f"{MAGIK_BASE}/api/v1/runs/ui-123").mock(
        return_value=httpx.Response(200, json={
            "ok": True,
            "data": {"runId": "ui-123", "state": "running", "counters": {"pass": 5, "fail": 1}, "reportUrl": None}
        })
    )
    result = await client.get_run_status("ui-123")
    assert result["state"] == "running"


@respx.mock
async def test_get_run_status_agent(client):
    respx.get(f"{MAGIK_BASE}/api/v1/agent-runs/agent-abc").mock(
        return_value=httpx.Response(200, json={
            "ok": True,
            "data": {"runId": "agent-abc", "state": "completed", "counters": {"steps": 8}, "reportUrl": "/report"}
        })
    )
    result = await client.get_run_status("agent-abc")
    assert result["state"] == "completed"


@respx.mock
async def test_get_run_status_404_raises_run_lost(client):
    respx.get(f"{MAGIK_BASE}/api/v1/runs/ui-old").mock(
        return_value=httpx.Response(404, json={"ok": False, "error": {"status": 404, "message": "not found"}})
    )
    with pytest.raises(MagikRunLost):
        await client.get_run_status("ui-old")


@respx.mock
async def test_get_run_report_json(client):
    respx.get(f"{MAGIK_BASE}/api/v1/runs/ui-123/report").mock(
        return_value=httpx.Response(200, json={"ok": True, "data": {"results": []}})
    )
    result = await client.get_run_report("ui-123")
    assert isinstance(result, dict)
    assert "results" in result  # unwrapped - data payload, not envelope


@respx.mock
async def test_get_run_status_agent_flat_format(client):
    """Agent-run status endpoint returns flat JSON - no ok/data wrapper."""
    respx.get(f"{MAGIK_BASE}/api/v1/agent-runs/agent-flat").mock(
        return_value=httpx.Response(200, json={
            "runId": "agent-flat", "kind": "agent", "state": "completed",
            "counters": {"steps": 5}, "verdict": {"success": True, "summary": "done"},
        })
    )
    result = await client.get_run_status("agent-flat")
    assert result["runId"] == "agent-flat"
    assert result["state"] == "completed"


@respx.mock
async def test_submit_agent_run_ok_false_raises_magik_error(client):
    from icx_engine.testing.client import MagikError
    respx.post(f"{MAGIK_BASE}/api/v1/agent-runs").mock(
        return_value=httpx.Response(200, json={"ok": False, "error": {"status": 400, "message": "invalid goal"}})
    )
    with pytest.raises(MagikError, match="invalid goal"):
        await client.submit_agent_run(url="http://localhost:3000", goal="test")


@respx.mock
async def test_get_run_status_ok_false_raises_magik_error(client):
    from icx_engine.testing.client import MagikError
    respx.get(f"{MAGIK_BASE}/api/v1/runs/ui-bad").mock(
        return_value=httpx.Response(200, json={"ok": False, "error": {"status": 500, "message": "internal error"}})
    )
    with pytest.raises(MagikError, match="internal error"):
        await client.get_run_status("ui-bad")


@respx.mock
async def test_get_run_report_425_raises(client):
    from icx_engine.testing.client import MagikReportNotReady
    respx.get(f"{MAGIK_BASE}/api/v1/runs/ui-123/report").mock(
        return_value=httpx.Response(425, json={"ok": False, "error": {"status": 425, "message": "too early"}})
    )
    with pytest.raises(MagikReportNotReady):
        await client.get_run_report("ui-123")


@pytest.mark.asyncio
@respx.mock
async def test_interactive_login_start():
    respx.post("http://m/api/v1/login/interactive/start").mock(
        return_value=httpx.Response(200, json={"ok": True, "data": {"interactiveId": "i1", "message": "go"}})
    )
    c = MagikClient("http://m")
    data = await c.interactive_login_start("http://host-x/login")
    assert data["interactiveId"] == "i1"
    await c.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_inline_login_returns_session():
    respx.post("http://m/api/v1/login").mock(
        return_value=httpx.Response(200, json={"ok": True, "data": {"sessionId": "s9", "currentUrl": "http://host-x/home"}})
    )
    c = MagikClient("http://m")
    data = await c.inline_login("http://host-x/login", "u", "p")
    assert data["sessionId"] == "s9"
    await c.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_submit_ui_test_sends_session_and_recover():
    route = respx.post("http://m/api/v1/ui-tests").mock(
        return_value=httpx.Response(202, json={"ok": True, "data": {"runId": "ui-1"}})
    )
    c = MagikClient("http://m")
    await c.submit_ui_test(url="http://host-x/app", session_id="s1", auto_auth_recover=False)
    sent = route.calls.last.request
    import json as _j
    body = _j.loads(sent.content)
    assert body["sessionId"] == "s1"
    assert body["autoAuthRecover"] is False
    await c.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_parse_spec():
    respx.post("http://m/api/v1/api-tests/parse-spec").mock(
        return_value=httpx.Response(200, json={"ok": True, "data": {"method": "POST", "fields": []}})
    )
    c = MagikClient("http://m")
    data = await c.parse_spec('{"x":1}', "json")
    assert data["method"] == "POST"
    await c.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_submit_profile():
    respx.post("http://m/api/v1/profiles").mock(
        return_value=httpx.Response(200, json={"ok": True, "data": {"projectName": "P", "screens": 1}})
    )
    c = MagikClient("http://m")
    data = await c.submit_profile("# Project: P")
    assert data["screens"] == 1
    await c.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_report_format_param():
    route = respx.get("http://m/api/v1/runs/ui-1/report").mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    c = MagikClient("http://m")
    await c.get_run_report("ui-1", fmt="json")
    assert route.calls.last.request.url.params["format"] == "json"
    await c.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_get_profile_creation_prompt():
    respx.get("http://m/profile/creation-prompt").mock(
        return_value=httpx.Response(200, text="PROFILE PROMPT BODY")
    )
    c = MagikClient("http://m")
    text = await c.get_profile_creation_prompt()
    assert text == "PROFILE PROMPT BODY"
    await c.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_sonar_scanner_check():
    respx.get("http://m/api/v1/sonar/scanner-check").mock(
        return_value=httpx.Response(200, json={"found": False, "hint": "install it"}))
    c = MagikClient("http://m")
    data = await c.sonar_scanner_check()
    assert data["found"] is False
    await c.aclose()

@pytest.mark.asyncio
@respx.mock
async def test_sonar_scan_sends_folder():
    route = respx.post("http://m/api/v1/sonar-scans").mock(
        return_value=httpx.Response(200, json={"jobId": "j1", "streamUrl": "http://m/api/v1/sonar-scans/stream?jobId=j1"}))
    c = MagikClient("http://m")
    data = await c.sonar_scan("/code", project_key="pk", sources="src")
    import json as _j
    body = _j.loads(route.calls.last.request.content)
    assert body["projectFolder"] == "/code" and body["projectKey"] == "pk" and body["sources"] == "src"
    assert data["jobId"] == "j1"
    await c.aclose()

@pytest.mark.asyncio
@respx.mock
async def test_sonar_report_fmt():
    route = respx.get("http://m/api/v1/sonar/report").mock(return_value=httpx.Response(200, json={"metrics": {}}))
    c = MagikClient("http://m")
    await c.sonar_report(fmt="json")
    assert route.calls.last.request.url.params["format"] == "json"
    await c.aclose()

@pytest.mark.asyncio
@respx.mock
async def test_sonar_ping_omits_empty():
    route = respx.post("http://m/api/v1/sonar/ping").mock(return_value=httpx.Response(200, json={"ok": True}))
    c = MagikClient("http://m")
    await c.sonar_ping()  # no url/token -> empty body
    import json as _j
    assert _j.loads(route.calls.last.request.content) == {}
    await c.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_sonar_report_html_returns_text_no_json_crash():
    # Magik serves raw HTML for format=html; sonar_report must return text, not crash on json()
    respx.get("http://m/api/v1/sonar/report").mock(
        return_value=httpx.Response(200, text="<html><body>report</body></html>",
                                    headers={"Content-Type": "text/html"}))
    c = MagikClient("http://m")
    out = await c.sonar_report(fmt="html")
    assert isinstance(out, str) and "<html>" in out
    await c.aclose()
