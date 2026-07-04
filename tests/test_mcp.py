"""Phase 3 tests - MCP host config management, MCP server handler, CLI mcp commands."""
import asyncio
import json
import tomllib
import pytest
import respx
import httpx
from pathlib import Path
from unittest.mock import patch

from icx_engine.mcp_hosts import (
    MCPHost, WriteResult, list_hosts, detect_installed_hosts, get_host,
    write_icx_entry, remove_icx_entry, ICX_MCP_ENTRY,
)
from icx_engine.models.config import AppConfig, LLMConfig, ChannelConfig
from icx_engine.connectors.jira.config import JiraConnection, TokenAuth

from test_data import JIRA_BASE_URL, JIRA_ISSUE_PAYLOAD


# -- helpers -------------------------------------------------------------------

def _codex_host(tmp_path: Path) -> MCPHost:
    return MCPHost(
        "codex", "Codex",
        tmp_path / ".codex" / "config.toml",
        tmp_path / ".codex",
        "toml",
    )


# -- path helpers --------------------------------------------------------------

def test_home_indirection_is_monkeypatchable(monkeypatch, tmp_path):
    monkeypatch.setattr("icx_engine.mcp_hosts._home", lambda: tmp_path)
    from icx_engine.mcp_hosts import _home
    assert _home() == tmp_path


# -- WriteResult ---------------------------------------------------------------

def test_write_result_normal_path(tmp_path):
    wr = WriteResult(path=tmp_path / "mcp.json", fallback=False)
    assert wr.fallback is False
    assert wr.path == tmp_path / "mcp.json"


def test_write_result_fallback_path(tmp_path):
    wr = WriteResult(path=tmp_path / ".mcp.json", fallback=True)
    assert wr.fallback is True


# -- list_hosts ----------------------------------------------------------------

def test_list_hosts_returns_five_agents(monkeypatch, tmp_path):
    monkeypatch.setattr("icx_engine.mcp_hosts._home", lambda: tmp_path)
    names = [h.name for h in list_hosts()]
    assert set(names) == {"claude", "cursor", "windsurf", "codex", "antigravity"}


def test_list_hosts_no_cwd_param():
    import inspect
    sig = inspect.signature(list_hosts)
    assert "cwd" not in sig.parameters


def test_list_hosts_claude_config_is_user_json(monkeypatch, tmp_path):
    monkeypatch.setattr("icx_engine.mcp_hosts._home", lambda: tmp_path)
    claude = next(h for h in list_hosts() if h.name == "claude")
    assert claude.config_path == tmp_path / ".claude.json"
    assert claude.detect_path == tmp_path / ".claude"


def test_write_icx_entry_claude_merges_into_existing_user_json(monkeypatch, tmp_path):
    monkeypatch.setattr("icx_engine.mcp_hosts._home", lambda: tmp_path)
    host = get_host("claude")
    host.detect_path.mkdir(parents=True, exist_ok=True)
    host.config_path.write_text(json.dumps({"projects": {}, "theme": "dark"}), encoding="utf-8")
    write_icx_entry(host)
    raw = json.loads(host.config_path.read_text(encoding="utf-8"))
    assert raw["projects"] == {}
    assert raw["theme"] == "dark"
    assert raw["mcpServers"]["icx"] == ICX_MCP_ENTRY


def test_list_hosts_cursor_config_under_home(monkeypatch, tmp_path):
    monkeypatch.setattr("icx_engine.mcp_hosts._home", lambda: tmp_path)
    cursor = next(h for h in list_hosts() if h.name == "cursor")
    assert cursor.config_path == tmp_path / ".cursor" / "mcp.json"


def test_list_hosts_codex_config_under_home(monkeypatch, tmp_path):
    monkeypatch.setattr("icx_engine.mcp_hosts._home", lambda: tmp_path)
    codex = next(h for h in list_hosts() if h.name == "codex")
    assert codex.config_path == tmp_path / ".codex" / "config.toml"


def test_list_hosts_windsurf_config_under_home(monkeypatch, tmp_path):
    monkeypatch.setattr("icx_engine.mcp_hosts._home", lambda: tmp_path)
    ws = next(h for h in list_hosts() if h.name == "windsurf")
    assert str(ws.config_path).startswith(str(tmp_path))
    assert ws.config_format == "json"


def test_no_host_has_none_config_path(monkeypatch, tmp_path):
    monkeypatch.setattr("icx_engine.mcp_hosts._home", lambda: tmp_path)
    for h in list_hosts():
        assert h.config_path is not None, f"{h.name} has None config_path"


def test_no_host_has_manual_format(monkeypatch, tmp_path):
    monkeypatch.setattr("icx_engine.mcp_hosts._home", lambda: tmp_path)
    for h in list_hosts():
        assert h.config_format != "manual", f"{h.name} still uses manual format"


# -- detect_installed_hosts ----------------------------------------------------

def test_detect_installed_hosts_no_cwd_param():
    import inspect
    assert "cwd" not in inspect.signature(detect_installed_hosts).parameters


def test_detect_installed_hosts_includes_cursor_when_dir_exists(monkeypatch, tmp_path):
    monkeypatch.setattr("icx_engine.mcp_hosts._home", lambda: tmp_path)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".cursor").mkdir()
    names = [h.name for h in detect_installed_hosts()]
    assert "cursor" in names


def test_detect_installed_hosts_excludes_cursor_when_dir_absent(monkeypatch, tmp_path):
    monkeypatch.setattr("icx_engine.mcp_hosts._home", lambda: tmp_path)
    monkeypatch.chdir(tmp_path)
    names = [h.name for h in detect_installed_hosts()]
    assert "cursor" not in names


def test_detect_installed_hosts_includes_windsurf_when_dir_exists(monkeypatch, tmp_path):
    monkeypatch.setattr("icx_engine.mcp_hosts._home", lambda: tmp_path)
    ws = next(h for h in list_hosts() if h.name == "windsurf")
    ws.detect_path.mkdir(parents=True, exist_ok=True)
    names = [h.name for h in detect_installed_hosts()]
    assert "windsurf" in names


# -- get_host ------------------------------------------------------------------

def test_get_host_no_cwd_param():
    import inspect
    assert "cwd" not in inspect.signature(get_host).parameters


def test_get_host_returns_correct_host(monkeypatch, tmp_path):
    monkeypatch.setattr("icx_engine.mcp_hosts._home", lambda: tmp_path)
    host = get_host("cursor")
    assert host is not None
    assert host.name == "cursor"
    assert host.label == "Cursor"


def test_get_host_returns_none_for_unknown(monkeypatch, tmp_path):
    monkeypatch.setattr("icx_engine.mcp_hosts._home", lambda: tmp_path)
    assert get_host("vscode") is None


# -- write_icx_entry (JSON) ----------------------------------------------------

def test_write_icx_entry_returns_write_result(monkeypatch, tmp_path):
    monkeypatch.setattr("icx_engine.mcp_hosts._home", lambda: tmp_path)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".cursor").mkdir()
    host = get_host("cursor")
    result = write_icx_entry(host)
    assert isinstance(result, WriteResult)


def test_write_icx_entry_no_fallback_when_detect_exists(monkeypatch, tmp_path):
    monkeypatch.setattr("icx_engine.mcp_hosts._home", lambda: tmp_path)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".cursor").mkdir()
    host = get_host("cursor")
    result = write_icx_entry(host)
    assert result.fallback is False
    assert result.path == host.config_path
    raw = json.loads(result.path.read_text(encoding="utf-8"))
    assert raw["mcpServers"]["icx"] == ICX_MCP_ENTRY


def test_write_icx_entry_fallback_when_detect_missing(monkeypatch, tmp_path):
    monkeypatch.setattr("icx_engine.mcp_hosts._home", lambda: tmp_path)
    monkeypatch.chdir(tmp_path)
    host = get_host("cursor")
    assert not host.detect_path.exists()
    result = write_icx_entry(host)
    assert result.fallback is True
    assert result.path == tmp_path / ".mcp.json"
    raw = json.loads(result.path.read_text(encoding="utf-8"))
    assert raw["mcpServers"]["icx"] == ICX_MCP_ENTRY


def test_write_icx_entry_merges_with_existing_config(monkeypatch, tmp_path):
    monkeypatch.setattr("icx_engine.mcp_hosts._home", lambda: tmp_path)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".cursor").mkdir()
    host = get_host("cursor")
    host.config_path.parent.mkdir(parents=True, exist_ok=True)
    host.config_path.write_text(json.dumps({"mcpServers": {"other-tool": {"command": "other"}}}), encoding="utf-8")
    write_icx_entry(host)
    raw = json.loads(host.config_path.read_text(encoding="utf-8"))
    assert "other-tool" in raw["mcpServers"]
    assert "icx" in raw["mcpServers"]


def test_write_icx_entry_creates_parent_dirs(monkeypatch, tmp_path):
    monkeypatch.setattr("icx_engine.mcp_hosts._home", lambda: tmp_path)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".cursor").mkdir()
    host = get_host("cursor")
    write_icx_entry(host)
    assert host.config_path.exists()


def test_write_icx_entry_overwrites_existing_ice_entry(monkeypatch, tmp_path):
    monkeypatch.setattr("icx_engine.mcp_hosts._home", lambda: tmp_path)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".cursor").mkdir()
    host = get_host("cursor")
    write_icx_entry(host)
    write_icx_entry(host)
    raw = json.loads(host.config_path.read_text(encoding="utf-8"))
    assert list(raw["mcpServers"].keys()).count("icx") == 1


def test_write_icx_entry_windsurf_writes_json(monkeypatch, tmp_path):
    monkeypatch.setattr("icx_engine.mcp_hosts._home", lambda: tmp_path)
    monkeypatch.chdir(tmp_path)
    host = get_host("windsurf")
    host.detect_path.mkdir(parents=True, exist_ok=True)
    result = write_icx_entry(host)
    assert result.fallback is False
    raw = json.loads(result.path.read_text(encoding="utf-8"))
    assert raw["mcpServers"]["icx"] == ICX_MCP_ENTRY


def test_write_icx_entry_windsurf_uses_mcp_config_json(monkeypatch, tmp_path):
    monkeypatch.setattr("icx_engine.mcp_hosts._home", lambda: tmp_path)
    host = get_host("windsurf")
    assert host.config_path.name == "mcp_config.json"
    assert "mcp_settings" not in str(host.config_path)


def test_write_icx_entry_windsurf_merges_existing_entries(monkeypatch, tmp_path):
    monkeypatch.setattr("icx_engine.mcp_hosts._home", lambda: tmp_path)
    host = get_host("windsurf")
    host.detect_path.mkdir(parents=True, exist_ok=True)
    host.config_path.parent.mkdir(parents=True, exist_ok=True)
    host.config_path.write_text(json.dumps({"mcpServers": {"other-tool": {"command": "other"}}}), encoding="utf-8")
    write_icx_entry(host)
    raw = json.loads(host.config_path.read_text(encoding="utf-8"))
    assert "other-tool" in raw["mcpServers"]
    assert "icx" in raw["mcpServers"]


def test_list_hosts_antigravity_config_path(monkeypatch, tmp_path):
    monkeypatch.setattr("icx_engine.mcp_hosts._home", lambda: tmp_path)
    host = get_host("antigravity")
    assert host.config_path == tmp_path / ".gemini" / "antigravity" / "mcp_config.json"
    assert host.detect_path == tmp_path / ".gemini"


def test_write_icx_entry_antigravity_writes_json(monkeypatch, tmp_path):
    monkeypatch.setattr("icx_engine.mcp_hosts._home", lambda: tmp_path)
    monkeypatch.chdir(tmp_path)
    host = get_host("antigravity")
    host.detect_path.mkdir(parents=True, exist_ok=True)
    result = write_icx_entry(host)
    assert result.fallback is False
    assert result.path == tmp_path / ".gemini" / "antigravity" / "mcp_config.json"
    raw = json.loads(result.path.read_text(encoding="utf-8"))
    assert raw["mcpServers"]["icx"] == ICX_MCP_ENTRY


def test_write_icx_entry_antigravity_merges_existing_entries(monkeypatch, tmp_path):
    monkeypatch.setattr("icx_engine.mcp_hosts._home", lambda: tmp_path)
    host = get_host("antigravity")
    host.detect_path.mkdir(parents=True, exist_ok=True)
    host.config_path.parent.mkdir(parents=True, exist_ok=True)
    host.config_path.write_text(json.dumps({"mcpServers": {"gemini-tool": {"command": "gemini"}}}), encoding="utf-8")
    write_icx_entry(host)
    raw = json.loads(host.config_path.read_text(encoding="utf-8"))
    assert "gemini-tool" in raw["mcpServers"]
    assert "icx" in raw["mcpServers"]


# -- write_icx_entry (TOML / Codex) -------------------------------------------

def test_write_icx_entry_toml_creates_correct_content(tmp_path):
    host = _codex_host(tmp_path)
    host.detect_path.mkdir(parents=True, exist_ok=True)
    write_icx_entry(host)
    raw = tomllib.loads((tmp_path / ".codex" / "config.toml").read_text(encoding="utf-8"))
    assert raw["mcp_servers"]["icx"] == ICX_MCP_ENTRY


def test_write_icx_entry_toml_merges_with_existing(tmp_path):
    host = _codex_host(tmp_path)
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "config.toml").write_text('[other]\nkey = "val"', encoding="utf-8")
    write_icx_entry(host)
    raw = tomllib.loads((tmp_path / ".codex" / "config.toml").read_text(encoding="utf-8"))
    assert "other" in raw
    assert raw["mcp_servers"]["icx"] == ICX_MCP_ENTRY


# -- remove_icx_entry (JSON) ---------------------------------------------------

def test_remove_icx_entry_removes_and_returns_true(monkeypatch, tmp_path):
    monkeypatch.setattr("icx_engine.mcp_hosts._home", lambda: tmp_path)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".cursor").mkdir()
    host = get_host("cursor")
    write_icx_entry(host)
    removed = remove_icx_entry(host)
    assert removed is True
    raw = json.loads(host.config_path.read_text(encoding="utf-8"))
    assert "icx" not in raw.get("mcpServers", {})


def test_remove_icx_entry_returns_false_when_not_present(monkeypatch, tmp_path):
    monkeypatch.setattr("icx_engine.mcp_hosts._home", lambda: tmp_path)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".cursor").mkdir()
    host = get_host("cursor")
    host.config_path.parent.mkdir(parents=True, exist_ok=True)
    host.config_path.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    assert remove_icx_entry(host) is False


def test_remove_icx_entry_returns_false_when_file_missing(monkeypatch, tmp_path):
    monkeypatch.setattr("icx_engine.mcp_hosts._home", lambda: tmp_path)
    monkeypatch.chdir(tmp_path)
    host = get_host("cursor")
    assert remove_icx_entry(host) is False


def test_remove_icx_entry_preserves_other_tools(monkeypatch, tmp_path):
    monkeypatch.setattr("icx_engine.mcp_hosts._home", lambda: tmp_path)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".cursor").mkdir()
    host = get_host("cursor")
    write_icx_entry(host)
    existing = json.loads(host.config_path.read_text(encoding="utf-8"))
    existing["mcpServers"]["other"] = {"command": "other"}
    host.config_path.write_text(json.dumps(existing), encoding="utf-8")
    remove_icx_entry(host)
    raw = json.loads(host.config_path.read_text(encoding="utf-8"))
    assert "other" in raw["mcpServers"]


# -- remove_icx_entry (TOML) ---------------------------------------------------

def test_remove_icx_entry_toml_removes_and_returns_true(tmp_path):
    host = _codex_host(tmp_path)
    host.detect_path.mkdir(parents=True, exist_ok=True)
    write_icx_entry(host)
    assert remove_icx_entry(host) is True
    raw = tomllib.loads((tmp_path / ".codex" / "config.toml").read_text(encoding="utf-8"))
    assert "icx" not in raw.get("mcp_servers", {})


def test_remove_icx_entry_toml_returns_false_when_not_present(tmp_path):
    host = _codex_host(tmp_path)
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "config.toml").write_text("[other]\nkey = 1", encoding="utf-8")
    assert remove_icx_entry(host) is False


# -- MCP server handler --------------------------------------------------------

from icx_engine.mcp_server import (
    _handle_analyze_issue,
    _handle_save_memory,
)
from unittest.mock import MagicMock, AsyncMock


@pytest.fixture
def mcp_config():
    """Jira-only config - used by tests that exercise pre-LLM error paths."""
    return AppConfig(
        connections=[
            JiraConnection(
                domain="test.atlassian.net",
                auth=TokenAuth(auth_type="token", email="u@test.com", api_token="tok"),
            )
        ]
    )


@pytest.fixture
def mcp_config_with_llm():
    """Full config with Ollama LLM - used by tests that need the full pipeline."""
    return AppConfig(
        connections=[
            JiraConnection(
                domain="test.atlassian.net",
                auth=TokenAuth(auth_type="token", email="u@test.com", api_token="tok"),
            )
        ],
        llm_profiles={"personal": LLMConfig(text_config=ChannelConfig(provider="ollama", model="llama3"), image_config=ChannelConfig(provider="ollama", model="llava"))},
        current_llm_profile="personal",
    )


def _mock_openai_response():
    from test_data import MOCK_LLM_JSON
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = MOCK_LLM_JSON
    return resp


@respx.mock
async def test_handle_analyze_issue_returns_work_item_json(mcp_config_with_llm):
    respx.get(f"{JIRA_BASE_URL}/issue/TEST-123").mock(
        return_value=httpx.Response(200, json=JIRA_ISSUE_PAYLOAD)
    )
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = mcp_config_with_llm
        with patch("icx_engine.llm.ollama.AsyncOpenAI") as mock_cls:
            mock_client = MagicMock()
            mock_cls.return_value = mock_client
            mock_client.chat.completions.create = AsyncMock(return_value=_mock_openai_response())
            with patch("icx_engine.mcp_server._get_graphs_info", return_value=[{"status": "not_registered", "path": "/projects/my-svc", "report_path": None, "access": "", "report_inline": "", "eta_seconds": None}]):
                result = await _handle_analyze_issue("TEST-123", project_paths=["/projects/my-svc"])
    data = json.loads(result)
    assert "work_item" in data
    assert data["work_item"]["type"] == "Bug"
    assert "problem_summary" in data["work_item"]["analysis"]
    assert "memory" in data
    assert "graphs" in data


async def test_handle_analyze_issue_returns_error_json_when_no_connection():
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        result = await _handle_analyze_issue("TEST-123", project_paths=["/projects/my-svc"])
    data = json.loads(result)
    assert data.get("status") == "error"
    assert data.get("code") == "NO_CONNECTION"
    assert "action_required" in data


async def test_handle_analyze_issue_returns_error_json_on_invalid_key():
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        result = await _handle_analyze_issue("not-a-valid-key", project_paths=["/projects/my-svc"])
    data = json.loads(result)
    assert data.get("status") == "error"


def test_run_mcp_server_is_callable():
    from icx_engine.mcp_server import run_mcp_server
    assert callable(run_mcp_server)


def test_server_registered_as_ice():
    from icx_engine.mcp_server import server
    assert server.name == "icx"


# -- Profile override - MCP ----------------------------------------------------

@respx.mock
async def test_handle_analyze_issue_with_profile_override(mcp_config_with_llm):
    """profile kwarg is forwarded to engine.run as profile_override."""
    respx.get(f"{JIRA_BASE_URL}/issue/TEST-123").mock(
        return_value=httpx.Response(200, json=JIRA_ISSUE_PAYLOAD)
    )
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = mcp_config_with_llm
        with patch("icx_engine.mcp_server.engine.run", new=AsyncMock()) as mock_run:
            from icx_engine.models.output import IssueContext
            mock_run.return_value = IssueContext(
                problem_summary="p", detailed_description="d",
                reproduction_steps=[], expected_behavior=None, actual_behavior=None,
                acceptance_criteria=[], impact="i", priority="High", issue_type="Bug",
                confidence_score=0.9, completeness_score=0.8, missing_information=[],
            )
            with patch("icx_engine.mcp_server._get_graphs_info", return_value=[{"status": "not_registered", "path": "/projects/my-svc", "report_path": None, "access": "", "report_inline": "", "eta_seconds": None}]):
                await _handle_analyze_issue("TEST-123", project_paths=["/projects/my-svc"], profile="personal")

    _, kwargs = mock_run.call_args
    assert kwargs.get("profile_override") == "personal"


async def test_handle_analyze_issue_unknown_profile_returns_error_json(mcp_config_with_llm):
    """An unknown profile_override surfaces as an error JSON, not an exception."""
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = mcp_config_with_llm
        result = await _handle_analyze_issue("TEST-123", project_paths=["/projects/my-svc"], profile="ghost-profile")
    data = json.loads(result)
    assert data.get("status") == "error"
    assert "ghost-profile" in data["message"]


async def test_list_tools_does_not_call_config_load():
    """_list_tools must not call ConfigManager.load() - doing so triggers a 3s keyring
    health check that blocks the MCP initialization handshake and prevents connection."""
    from icx_engine.mcp_server import _list_tools
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        tools = await _list_tools()
    mock_cm.load.assert_not_called()
    assert len(tools) > 0


async def test_list_tools_no_profile_hint_in_description():
    from icx_engine.mcp_server import _list_tools
    tools = await _list_tools()
    for tool in tools:
        assert "Available in your config" not in tool.description


async def test_list_tools_schema_has_optional_profile_property():
    from icx_engine.mcp_server import _list_tools
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        tools = await _list_tools()

    fast_tool = next(t for t in tools if t.name == "analyze_issue_fast")
    schema = fast_tool.inputSchema
    assert "profile" in schema["properties"]
    assert "profile" not in schema.get("required", [])


async def test_list_tools_returns_core_tools():
    from icx_engine.mcp_server import _list_tools
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        tools = await _list_tools()
    names = {t.name for t in tools}
    assert {"analyze_issue_fast", "analyze_issue", "save_memory"}.issubset(names)


@respx.mock
async def test_handle_analyze_issue_fast_returns_work_item_json(mcp_config_with_llm):
    """Fast mode with LLM returns IssueContext with attachment_processing='text_only'."""
    respx.get(f"{JIRA_BASE_URL}/issue/TEST-123").mock(
        return_value=httpx.Response(200, json=JIRA_ISSUE_PAYLOAD)
    )
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = mcp_config_with_llm
        with patch("icx_engine.llm.ollama.AsyncOpenAI") as mock_cls:
            mock_client = MagicMock()
            mock_cls.return_value = mock_client
            mock_client.chat.completions.create = AsyncMock(return_value=_mock_openai_response())
            with patch("icx_engine.mcp_server._get_graphs_info", return_value=[{"status": "not_registered", "path": "/projects/my-svc", "report_path": None, "access": "", "report_inline": "", "eta_seconds": None}]):
                result = await _handle_analyze_issue("TEST-123", project_paths=["/projects/my-svc"], skip_vision=True)
    data = json.loads(result)
    assert "work_item" in data
    assert data["work_item"]["type"] == "Bug"
    assert data["work_item"]["attachment_processing"] == "text_only"
    assert "pending_images" in data["work_item"]["analysis"]
    assert "pending_audio" in data["work_item"]["analysis"]
    assert "pending_documents" in data["work_item"]["analysis"]
    # image_paths always present in work_item (empty dict when no images)
    assert "image_paths" in data["work_item"]
    assert isinstance(data["work_item"]["image_paths"], dict)
    # raw base64 images must never appear in the analysis payload
    assert "images" not in data["work_item"]["analysis"]


async def test_mcp_fast_tool_uses_45s_timeout():
    """analyze_issue_fast must use a 45s timeout."""
    timeout_used = {}

    async def _capture_timeout(coro, timeout):
        timeout_used["value"] = timeout
        raise asyncio.TimeoutError()

    with patch("icx_engine.mcp_server.asyncio.wait_for", side_effect=_capture_timeout):
        with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
            mock_cm.load.return_value = AppConfig()
            result = await _handle_analyze_issue("TEST-123", project_paths=["/p"], skip_vision=True)

    assert timeout_used.get("value") == 45.0
    data = json.loads(result)
    assert data.get("status") == "error"


async def test_mcp_full_tool_uses_660s_timeout():
    """analyze_issue must use a 660s timeout."""
    timeout_used = {}

    async def _capture_timeout(coro, timeout):
        timeout_used["value"] = timeout
        raise asyncio.TimeoutError()

    with patch("icx_engine.mcp_server.asyncio.wait_for", side_effect=_capture_timeout):
        with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
            mock_cm.load.return_value = AppConfig()
            result = await _handle_analyze_issue("TEST-123", project_paths=["/p"], skip_vision=False)

    assert timeout_used.get("value") == 660.0
    data = json.loads(result)
    assert data.get("status") == "error"


@respx.mock
async def test_image_paths_written_to_disk_not_inline(mcp_config_with_llm, tmp_path):
    """Images must be written to disk and returned as paths, not inline base64.
    Verifies the pathlib.Path import fix - a NameError here means Path was not imported."""
    import base64
    from icx_engine.models.output import IssueContext

    respx.get(f"{JIRA_BASE_URL}/issue/TEST-123").mock(
        return_value=httpx.Response(200, json=JIRA_ISSUE_PAYLOAD)
    )

    fake_image_b64 = base64.b64encode(b"fake-png-bytes").decode()

    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = mcp_config_with_llm
        with patch("icx_engine.llm.ollama.AsyncOpenAI") as mock_cls:
            mock_client = MagicMock()
            mock_cls.return_value = mock_client
            mock_client.chat.completions.create = AsyncMock(return_value=_mock_openai_response())
            with patch("icx_engine.mcp_server._get_graphs_info", return_value=[{"status": "not_registered", "path": "/projects/my-svc", "report_path": None, "access": "", "report_inline": "", "eta_seconds": None}]):
                with patch("icx_engine.graph.storage.sweep_stale_temp_dirs"):
                    with patch("icx_engine.graph.storage.temp_images_dir", return_value=tmp_path):
                        with patch("icx_engine.engine.run") as mock_run:
                            ctx = IssueContext(
                                problem_summary="Bug summary",
                                detailed_description="desc",
                                reproduction_steps=[],
                                expected_behavior=None,
                                actual_behavior=None,
                                acceptance_criteria=[],
                                impact="low",
                                priority="Low",
                                issue_type="Bug",
                                confidence_score=0.9,
                                completeness_score=0.8,
                                missing_information=[],
                                images={"screenshot.png": fake_image_b64},
                            )
                            mock_run.return_value = ctx
                            result = await _handle_analyze_issue("TEST-123", project_paths=["/projects/my-svc"], skip_vision=True)

    data = json.loads(result)
    # image_paths must be non-empty and contain the filename
    assert data["work_item"]["image_paths"] != {}
    assert "screenshot.png" in data["work_item"]["image_paths"]
    # images_access key present when images exist
    assert "images_access" in data["work_item"]
    # raw base64 must never be in the analysis
    assert "images" not in data["work_item"]["analysis"]
    # image file must have been written to disk (no NameError from missing Path import)
    written_path = Path(data["work_item"]["image_paths"]["screenshot.png"])
    assert written_path.exists()
    assert written_path.read_bytes() == b"fake-png-bytes"


async def test_save_memory_tool_has_required_inputs():
    from icx_engine.mcp_server import _list_tools
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        tools = await _list_tools()

    save_tool = next(t for t in tools if t.name == "save_memory")
    schema = save_tool.inputSchema
    required = schema["required"]
    assert "issue_key" in required
    assert "summary" in required
    assert "problem_description" in required
    assert "resolution_note" in required
    assert "files_changed" in required
    assert "tags" in required
    assert "work_item_type" in required
    assert "pattern_used" not in required


# -- save_memory per-item input validation -------------------------------------

_SAVE_REQUIRED_BASE = {
    "issue_key": "TEST-1",
    "summary": "Root cause summary",
    "problem_description": "Detailed root cause analysis of the failure mechanism.",
    "resolution_note": "Changed comparison operator in auth.py:validate_token() line 47.",
    "files_changed": ["src/auth.py"],
    "tags": ["auth-middleware", "token-validation"],
    "work_item_type": "bug",
}


async def test_call_tool_save_memory_rejects_non_string_files_changed():
    """files_changed entries that are not strings must return an error."""
    from icx_engine.mcp_server import _call_tool
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        result = await _call_tool("save_memory", {
            **_SAVE_REQUIRED_BASE,
            "files_changed": [{"not": "a string"}],
        })
    data = json.loads(result[0].text)
    assert "error" in data
    assert "files_changed" in data["error"]


async def test_call_tool_save_memory_rejects_oversized_files_changed_entry():
    """files_changed entries over 4096 chars must return an error."""
    from icx_engine.mcp_server import _call_tool
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        result = await _call_tool("save_memory", {
            **_SAVE_REQUIRED_BASE,
            "files_changed": ["x" * 4097],
        })
    data = json.loads(result[0].text)
    assert "error" in data
    assert "files_changed" in data["error"]


async def test_call_tool_save_memory_rejects_non_string_tags():
    """tags entries that are not strings must return an error."""
    from icx_engine.mcp_server import _call_tool
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        result = await _call_tool("save_memory", {
            **_SAVE_REQUIRED_BASE,
            "tags": [42],
        })
    data = json.loads(result[0].text)
    assert "error" in data
    assert "tags" in data["error"]


async def test_call_tool_save_memory_rejects_oversized_tag_entry():
    """tags entries over 256 chars must return an error."""
    from icx_engine.mcp_server import _call_tool
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        result = await _call_tool("save_memory", {
            **_SAVE_REQUIRED_BASE,
            "tags": ["x" * 257],
        })
    data = json.loads(result[0].text)
    assert "error" in data
    assert "tags" in data["error"]


async def test_call_tool_save_memory_rejects_empty_summary():
    """summary must be a non-empty string."""
    from icx_engine.mcp_server import _call_tool
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        result = await _call_tool("save_memory", {
            **_SAVE_REQUIRED_BASE,
            "summary": "",
        })
    data = json.loads(result[0].text)
    assert "error" in data
    assert "summary" in data["error"]


async def test_call_tool_save_memory_rejects_empty_problem_description():
    """problem_description must be a non-empty string."""
    from icx_engine.mcp_server import _call_tool
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        result = await _call_tool("save_memory", {
            **_SAVE_REQUIRED_BASE,
            "problem_description": "",
        })
    data = json.loads(result[0].text)
    assert "error" in data
    assert "problem_description" in data["error"]


async def test_call_tool_save_memory_rejects_empty_work_item_type():
    """work_item_type must be a non-empty string."""
    from icx_engine.mcp_server import _call_tool
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        result = await _call_tool("save_memory", {
            **_SAVE_REQUIRED_BASE,
            "work_item_type": "",
        })
    data = json.loads(result[0].text)
    assert "error" in data
    assert "work_item_type" in data["error"]


# -- Tool count and schema -----------------------------------------------------

async def test_list_tools_returns_all_tools():
    from icx_engine.mcp_server import _list_tools
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        tools = await _list_tools()

    assert len(tools) == 37
    names = {t.name for t in tools}
    assert names == {
        "analyze_issue_fast", "analyze_issue", "save_memory",
        "graph_find_context", "graph_call_chain", "graph_impact", "graph_subsystem",
        "graph_cross_links", "graph_important_nodes", "graph_blast_radius",
        "graph_cycles", "graph_dead_code", "graph_ownership",
        "memory_find_by_file", "memory_get_hotspots", "memory_get_related",
        "memory_get_patterns", "memory_search",
        "reinforce_memory_usage", "get_memory_audit",
        "magik_health_check", "start_testing_session", "resume_testing_session",
        "magik_test_status", "magik_test_results",
        "magik_login_start", "magik_login_capture", "magik_login_cancel",
        "magik_login_inline", "magik_logout",
        "sonar_status", "sonar_projects", "sonar_branches",
        "sonar_measures", "sonar_quality_gate", "sonar_findings", "sonar_report",
    }


async def test_graph_cross_links_schema_has_file_path():
    from icx_engine.mcp_server import _list_tools
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        tools = await _list_tools()

    tool = next(t for t in tools if t.name == "graph_cross_links")
    assert "file_path" in tool.inputSchema["properties"]


async def test_analyze_tool_schema_issue_ref_required_project_paths_optional():
    from icx_engine.mcp_server import _list_tools
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        tools = await _list_tools()

    fast_tool = next(t for t in tools if t.name == "analyze_issue_fast")
    schema = fast_tool.inputSchema
    assert "issue_ref" in schema["required"]
    assert "project_paths" not in schema.get("required", []), "project_paths must be optional"
    assert "profile" not in schema.get("required", [])
    assert "project_path" not in schema["properties"], "old project_path must be removed"
    assert "additional_paths" not in schema["properties"], "old additional_paths must be removed"
    assert schema["properties"]["project_paths"]["type"] == "array"


async def test_analyze_tool_schema_project_paths_is_array():
    from icx_engine.mcp_server import _list_tools
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        tools = await _list_tools()

    for tool_name in ("analyze_issue_fast", "analyze_issue"):
        tool = next(t for t in tools if t.name == tool_name)
        schema = tool.inputSchema
        assert "project_paths" in schema["properties"], f"{tool_name} missing project_paths"
        assert "project_paths" not in schema.get("required", []), f"{tool_name} project_paths must be optional"
        assert schema["properties"]["project_paths"]["type"] == "array"
        assert "project_path" not in schema["properties"], f"{tool_name} old project_path must not exist"
        assert "additional_paths" not in schema["properties"], f"{tool_name} old additional_paths must not exist"


# -- _icx_next guidance hints --------------------------------------------------

async def test_handle_analyze_issue_success_includes_icx_next():
    """_handle_analyze_issue includes _icx_next with instruction in combined response."""
    from icx_engine.models.output import IssueContext
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        with patch("icx_engine.mcp_server.engine.run", new=AsyncMock()) as mock_run:
            mock_run.return_value = IssueContext(
                problem_summary="AuthService token expired",
                detailed_description="JWT refresh fails on rotation",
                reproduction_steps=[], expected_behavior=None, actual_behavior=None,
                acceptance_criteria=[], impact="high", priority="High", issue_type="Bug",
                confidence_score=0.9, completeness_score=0.9, missing_information=[],
            )
            with patch("icx_engine.mcp_server._get_graphs_info", return_value=[{"status": "ready", "path": "/projects/my-svc", "report_path": "/projects/my-svc/.icx/graphs/GRAPH_REPORT.md", "access": "pre-authorized", "eta_seconds": None}]):
                result = await _handle_analyze_issue("TEST-123", project_paths=["/projects/my-svc"])
    data = json.loads(result)
    assert "_icx_next" in data
    assert "instruction" in data["_icx_next"]
    assert "graphs" in data
    assert data["graphs"][0]["status"] == "ready"
    assert "memory" in data
    assert "work_item" in data
    assert "ITERATION RULE" in data["_icx_next"]["instruction"]
    assert "each new change requires its own fresh test confirmation" in data["_icx_next"]["instruction"]


async def test_handle_save_memory_returns_saved_true(mcp_config):
    mock_mem = MagicMock()
    mock_mem.save.return_value = None

    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = mcp_config
        with patch("icx_engine.mcp_server._ensure_memory_manager", return_value=mock_mem):
            result = await _handle_save_memory(
                "TEST-123",
                "JWT expiry check uses < instead of <= rejecting tokens at exact expiry second",
                "auth/middleware.py:validate_token() used strict less-than on token.exp field.",
                "Changed < to <= in validate_token() in auth/middleware.py line 47.",
                ["src/auth/token.py"],
                ["jwt-expiry", "auth-middleware"],
                "bug",
            )

    data = json.loads(result)
    assert data["saved"] is True
    assert data["issue_key"] == "TEST-123"
    mock_mem.save.assert_called_once()


async def test_handle_save_memory_no_connection_returns_error():
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        result = await _handle_save_memory(
            "TEST-123",
            "Root cause summary",
            "Detailed problem description.",
            "Resolution note.",
            [],
            ["some-tag"],
            "bug",
        )

    data = json.loads(result)
    assert "error" in data


async def test_handle_save_memory_pattern_used_defaults_to_empty(mcp_config):
    """pattern_used is the only optional field - omitting it saves entry with empty string."""
    mock_mem = MagicMock()

    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = mcp_config
        with patch("icx_engine.mcp_server._ensure_memory_manager", return_value=mock_mem):
            result = await _handle_save_memory(
                "TEST-123",
                "Root cause summary",
                "Detailed problem description.",
                "Resolution note describing the exact change.",
                [],
                ["some-tag"],
                "bug",
            )

    data = json.loads(result)
    assert data["saved"] is True
    saved_entry = mock_mem.save.call_args[0][0]
    assert saved_entry.pattern_used == ""


async def test_handle_save_memory_populates_tech_stack(mcp_config):
    """tech_stack is detected from the matched project path and saved on the entry."""
    mock_mem = MagicMock()
    stack = {".": {"languages": {"java": "17"}, "frameworks": {"spring-boot": "3.2.1"}, "package_manager": "maven"}}
    mock_project = MagicMock()
    mock_project.path = "/projects/my-svc"

    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = mcp_config
        with patch("icx_engine.mcp_server._ensure_memory_manager", return_value=mock_mem):
            with patch("icx_engine.graph.storage.find_projects_by_tracker_key", return_value=[mock_project]):
                with patch("icx_engine.memory.stack_fingerprint.detect_stack", return_value=stack):
                    result = await _handle_save_memory(
                        "TEST-123",
                        "Root cause summary",
                        "Detailed problem description.",
                        "Resolution note describing the exact change.",
                        [],
                        ["some-tag"],
                        "bug",
                    )

    data = json.loads(result)
    assert data["saved"] is True
    saved_entry = mock_mem.save.call_args[0][0]
    assert saved_entry.tech_stack == stack


async def test_handle_save_memory_tech_stack_empty_when_no_project_match(mcp_config):
    """tech_stack defaults to {} when no matching project is found for the issue key."""
    mock_mem = MagicMock()

    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = mcp_config
        with patch("icx_engine.mcp_server._ensure_memory_manager", return_value=mock_mem):
            with patch("icx_engine.graph.storage.find_projects_by_tracker_key", return_value=[]):
                result = await _handle_save_memory(
                    "TEST-123",
                    "Root cause summary",
                    "Detailed problem description.",
                    "Resolution note describing the exact change.",
                    [],
                    ["some-tag"],
                    "bug",
                )

    data = json.loads(result)
    assert data["saved"] is True
    saved_entry = mock_mem.save.call_args[0][0]
    assert saved_entry.tech_stack == {}


async def test_handle_save_memory_outcome_verified_new_entry_falls_through(mcp_config):
    """outcome_verified=True on a brand-new entry creates it instead of failing."""
    mock_mem = MagicMock()
    mock_mem.save.return_value = None
    mock_mem.verify_resolution.return_value = {"error": "entry not found", "issue_key": "TEST-1"}

    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = mcp_config
        with patch("icx_engine.mcp_server._ensure_memory_manager", return_value=mock_mem):
            result = await _handle_save_memory(
                "TEST-1",
                "Root cause summary",
                "Detailed problem description.",
                "Resolution note.",
                ["src/foo.py"],
                ["some-tag"],
                "Task",
                extra={"outcome_verified": True, "outcome_feedback_note": "Tested and confirmed."},
            )

    data = json.loads(result)
    assert data["saved"] is True, f"expected saved=True, got: {data}"
    mock_mem.save.assert_called_once()
    saved_entry = mock_mem.save.call_args[0][0]
    assert saved_entry.outcome_verified is True
    assert saved_entry.outcome_feedback_note == "Tested and confirmed."


async def test_handle_save_memory_outcome_verified_existing_entry(mcp_config):
    """outcome_verified=True on existing entry calls verify_resolution and returns success."""
    mock_mem = MagicMock()
    mock_mem.verify_resolution.return_value = {
        "issue_key": "TEST-2",
        "confirmation_count": 2,
        "memory_confidence": 0.5,
    }

    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = mcp_config
        with patch("icx_engine.mcp_server._ensure_memory_manager", return_value=mock_mem):
            result = await _handle_save_memory(
                "TEST-2",
                "Root cause summary",
                "Detailed problem description.",
                "Resolution note.",
                [],
                ["some-tag"],
                "bug",
                extra={"outcome_verified": True, "outcome_feedback_note": "Works in prod."},
            )

    data = json.loads(result)
    assert data["saved"] is True
    assert data["confirmation_count"] == 2
    mock_mem.save.assert_not_called()


async def test_handle_save_memory_outcome_verified_other_error_fails(mcp_config):
    """outcome_verified=True with a non-'entry not found' error still returns saved=False."""
    mock_mem = MagicMock()
    mock_mem.verify_resolution.return_value = {"error": "database locked", "issue_key": "TEST-3"}

    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = mcp_config
        with patch("icx_engine.mcp_server._ensure_memory_manager", return_value=mock_mem):
            result = await _handle_save_memory(
                "TEST-3",
                "Root cause summary",
                "Detailed problem description.",
                "Resolution note.",
                [],
                ["some-tag"],
                "bug",
                extra={"outcome_verified": True, "outcome_feedback_note": "Tested."},
            )

    data = json.loads(result)
    assert data["saved"] is False
    assert "database locked" in data.get("error", "")
    mock_mem.save.assert_not_called()


# -- Profile override - CLI ----------------------------------------------------

from icx_engine.cli import app as _cli_app
from typer.testing import CliRunner as _CLIRunner

_cli_runner = _CLIRunner()


def test_analyze_profile_flag_rejected_when_unknown():
    """--profile with a non-existent name exits 1 with a descriptive error."""
    config = AppConfig(
        connections=[
            JiraConnection(
                domain="test.atlassian.net",
                auth=TokenAuth(auth_type="token", email="u@t.com", api_token="tok"),
            )
        ],
        llm_profiles={"personal": LLMConfig(text_config=ChannelConfig(provider="ollama", model="llama3"))},
        current_llm_profile="personal",
    )
    # ConfigManager is a lazy import inside analyze(); patch at the source module.
    with patch("icx_engine.config_manager.ConfigManager.load", return_value=config):
        result = _cli_runner.invoke(_cli_app, ["analyze", "TEST-123", "--profile", "ghost"])
    assert result.exit_code == 1
    assert "ghost" in result.output
    assert "personal" in result.output


@pytest.mark.filterwarnings("ignore:coroutine 'run' was never awaited:RuntimeWarning")
@respx.mock
def test_analyze_profile_flag_passed_to_engine():
    """--profile accepted and forwarded; active profile in config unchanged after run."""
    config = AppConfig(
        connections=[
            JiraConnection(
                domain="test.atlassian.net",
                auth=TokenAuth(auth_type="token", email="u@t.com", api_token="tok"),
            )
        ],
        llm_profiles={
            "personal": LLMConfig(text_config=ChannelConfig(provider="ollama", model="llama3")),
            "fast":     LLMConfig(text_config=ChannelConfig(provider="ollama", model="mistral")),
        },
        current_llm_profile="personal",
    )
    respx.get(f"{JIRA_BASE_URL}/issue/TEST-123").mock(
        return_value=httpx.Response(200, json=JIRA_ISSUE_PAYLOAD)
    )

    captured: list[dict] = []
    import icx_engine.engine as _engine

    original_run = _engine.run

    async def spy_run(*args, **kwargs):
        captured.append({"profile_override": kwargs.get("profile_override")})
        return await original_run(*args, **kwargs)

    # Patch engine.run at the source so the lazy import in cli.analyze() sees the spy.
    with patch("icx_engine.config_manager.ConfigManager.load", return_value=config):
        with patch("icx_engine.engine.run", new=spy_run):
            with patch("icx_engine.llm.ollama.AsyncOpenAI") as mock_cls:
                mock_client = MagicMock()
                mock_cls.return_value = mock_client
                mock_client.chat.completions.create = AsyncMock(return_value=_mock_openai_response())
                result = _cli_runner.invoke(_cli_app, ["analyze", "TEST-123", "--profile", "fast"])

    assert result.exit_code == 0
    assert captured[0]["profile_override"] == "fast"
    assert config.current_llm_profile == "personal"


# -- CLI: mcp commands ---------------------------------------------------------

from icx_engine.cli import app
from typer.testing import CliRunner

_runner = CliRunner()


def test_mcp_setup_with_host_flag_writes_config(monkeypatch, tmp_path):
    monkeypatch.setattr("icx_engine.mcp_hosts._home", lambda: tmp_path)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".cursor").mkdir()
    result = _runner.invoke(app, ["mcp", "setup", "--host", "cursor"])
    assert result.exit_code == 0
    raw = json.loads((tmp_path / ".cursor" / "mcp.json").read_text(encoding="utf-8"))
    assert Path(raw["mcpServers"]["icx"]["command"]).stem.lower() == "icx"


def test_mcp_setup_auto_detects_installed_hosts(monkeypatch, tmp_path):
    monkeypatch.setattr("icx_engine.mcp_hosts._home", lambda: tmp_path)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".cursor").mkdir()
    result = _runner.invoke(app, ["mcp", "setup"])
    assert result.exit_code == 0
    assert (tmp_path / ".cursor" / "mcp.json").exists()


def test_mcp_setup_unknown_host_exits_nonzero(monkeypatch, tmp_path):
    monkeypatch.setattr("icx_engine.mcp_hosts._home", lambda: tmp_path)
    monkeypatch.chdir(tmp_path)
    result = _runner.invoke(app, ["mcp", "setup", "--host", "vscode"])
    assert result.exit_code != 0


def test_mcp_setup_no_hosts_detected_exits_nonzero(monkeypatch, tmp_path):
    monkeypatch.setattr("icx_engine.mcp_hosts._home", lambda: tmp_path)
    monkeypatch.chdir(tmp_path)
    with patch("icx_engine.mcp_hosts.detect_installed_hosts", return_value=[]):
        result = _runner.invoke(app, ["mcp", "setup"])
    assert result.exit_code != 0


def test_mcp_setup_windsurf_writes_config(monkeypatch, tmp_path):
    monkeypatch.setattr("icx_engine.mcp_hosts._home", lambda: tmp_path)
    monkeypatch.chdir(tmp_path)
    ws_detect = tmp_path / ".codeium" / "windsurf"
    ws_detect.mkdir(parents=True)
    result = _runner.invoke(app, ["mcp", "setup", "--host", "windsurf"])
    assert result.exit_code == 0
    assert "manual" not in result.output.lower()
    assert "OK" in result.output or "written" in result.output.lower()


def test_mcp_setup_fallback_prints_notice(monkeypatch, tmp_path):
    monkeypatch.setattr("icx_engine.mcp_hosts._home", lambda: tmp_path)
    monkeypatch.chdir(tmp_path)
    # cursor detect_path (tmp_path/.cursor) does NOT exist -> fallback
    result = _runner.invoke(app, ["mcp", "setup", "--host", "cursor"])
    assert result.exit_code == 0
    assert "fallback" in result.output.lower() or ".mcp.json" in result.output


def test_mcp_remove_with_host_flag_removes_entry(monkeypatch, tmp_path):
    monkeypatch.setattr("icx_engine.mcp_hosts._home", lambda: tmp_path)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".cursor").mkdir()
    _runner.invoke(app, ["mcp", "setup", "--host", "cursor"])
    result = _runner.invoke(app, ["mcp", "remove", "--host", "cursor"])
    assert result.exit_code == 0
    raw = json.loads((tmp_path / ".cursor" / "mcp.json").read_text(encoding="utf-8"))
    assert "icx" not in raw.get("mcpServers", {})


def test_mcp_remove_reports_not_found_when_entry_absent(monkeypatch, tmp_path):
    monkeypatch.setattr("icx_engine.mcp_hosts._home", lambda: tmp_path)
    monkeypatch.chdir(tmp_path)
    result = _runner.invoke(app, ["mcp", "remove", "--host", "cursor"])
    assert result.exit_code == 0
    assert "not found" in result.output.lower() or "No ICX" in result.output


def test_mcp_config_shows_all_host_labels(monkeypatch, tmp_path, cli_runner):
    monkeypatch.setattr("icx_engine.mcp_hosts._home", lambda: tmp_path)
    result = cli_runner.invoke(app, ["mcp", "config"])
    assert result.exit_code == 0
    assert "mcpServers" in result.output
    assert "mcp_servers" in result.output
    assert "Claude Code" in result.output
    assert "Cursor" in result.output
    assert "Windsurf" in result.output
    assert "Codex" in result.output
    assert "Antigravity" in result.output
    assert "Cline" not in result.output


async def test_icx_next_instruction_contains_confirmation_block():
    """_icx_next.instruction must contain the mandatory confirmation block format."""
    from icx_engine.models.output import IssueContext
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        with patch("icx_engine.mcp_server.engine.run", new=AsyncMock()) as mock_run:
            mock_run.return_value = IssueContext(
                problem_summary="Login fails on mobile",
                detailed_description="d",
                reproduction_steps=[], expected_behavior=None, actual_behavior=None,
                acceptance_criteria=[], impact="high", priority="High", issue_type="Bug",
                confidence_score=0.9, completeness_score=0.9, missing_information=[],
            )
            with patch("icx_engine.mcp_server._get_graphs_info", return_value=[{
                "status": "ready",
                "path": "/projects/my-svc",
                "report_path": "/projects/my-svc/GRAPH_REPORT.md",
                "access": "pre-authorized",
                "eta_seconds": None,
            }]):
                result = await _handle_analyze_issue("TEST-123", project_paths=["/projects/my-svc"])

    data = json.loads(result)
    instruction = data["_icx_next"]["instruction"]
    assert "**Problem understood:**" in instruction
    assert "**Approach:**" in instruction
    assert "**Shall I proceed?**" in instruction


async def test_icx_next_instruction_contains_vision_gate():
    """_icx_next.instruction must contain STEP 0 vision escalation gate for all graph statuses."""
    from icx_engine.models.output import IssueContext

    _issue = IssueContext(
        problem_summary="p", detailed_description="d",
        reproduction_steps=[], expected_behavior=None, actual_behavior=None,
        acceptance_criteria=[], impact="i", priority="High", issue_type="Bug",
        confidence_score=0.9, completeness_score=0.9, missing_information=[],
    )

    for graph_status, graph_info in [
        ("ready", {"status": "ready", "report_path": "/projects/my-svc/GRAPH_REPORT.md", "access": "pre-authorized", "eta_seconds": None}),
        ("building", {"status": "building", "report_path": None, "access": "", "report_inline": "", "eta_seconds": 30}),
        ("not_built", {"status": "not_built", "report_path": None, "access": "", "report_inline": "", "eta_seconds": None}),
        ("not_registered", {"status": "not_registered", "report_path": None, "access": "", "report_inline": "", "eta_seconds": None}),
    ]:
        with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
            mock_cm.load.return_value = AppConfig()
            with patch("icx_engine.mcp_server.engine.run", new=AsyncMock(return_value=_issue)):
                with patch("icx_engine.mcp_server._get_graphs_info", return_value=[{**graph_info, "path": "/projects/my-svc"}]):
                    result = await _handle_analyze_issue("TEST-123", project_paths=["/projects/my-svc"])

        data = json.loads(result)
        instruction = data["_icx_next"]["instruction"]
        assert "pending_images" in instruction, f"Vision gate missing for graph_status={graph_status!r}"
        assert "pending_audio" in instruction, f"Audio gate missing for graph_status={graph_status!r}"
        assert "analyze_issue" in instruction, f"Escalation call missing for graph_status={graph_status!r}"
        assert "STEP 0" in instruction, f"STEP 0 label missing for graph_status={graph_status!r}"


async def test_handle_analyze_multi_path_response_includes_graphs():
    """When additional_paths provided, response includes 'graphs' list with all paths."""
    from icx_engine.models.output import IssueContext
    _issue = IssueContext(
        problem_summary="p", detailed_description="d",
        reproduction_steps=[], expected_behavior=None, actual_behavior=None,
        acceptance_criteria=[], impact="i", priority="High", issue_type="Bug",
        confidence_score=0.9, completeness_score=0.9, missing_information=[],
    )
    ready_info = {"status": "ready", "report_path": "/projects/svc/GRAPH_REPORT.md", "access": "pre-authorized", "path": "/projects/svc", "eta_seconds": None}
    not_built_info = {"status": "not_built", "report_path": None, "access": "", "report_inline": "", "path": "/projects/ui", "eta_seconds": None}

    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        with patch("icx_engine.mcp_server.engine.run", new=AsyncMock(return_value=_issue)):
            with patch("icx_engine.mcp_server._get_graphs_info", return_value=[ready_info, not_built_info]):
                result = await _handle_analyze_issue(
                    "TEST-123",
                    project_paths=["/projects/svc", "/projects/ui"],
                )

    data = json.loads(result)
    assert "graphs" in data
    assert len(data["graphs"]) == 2
    paths_in_response = [g["path"] for g in data["graphs"]]
    assert "/projects/svc" in paths_in_response
    assert "/projects/ui" in paths_in_response


async def test_handle_analyze_single_path_no_graphs_key():
    """Single path (no additional_paths): response must NOT include 'graphs'."""
    from icx_engine.models.output import IssueContext
    _issue = IssueContext(
        problem_summary="p", detailed_description="d",
        reproduction_steps=[], expected_behavior=None, actual_behavior=None,
        acceptance_criteria=[], impact="i", priority="High", issue_type="Bug",
        confidence_score=0.9, completeness_score=0.9, missing_information=[],
    )
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        with patch("icx_engine.mcp_server.engine.run", new=AsyncMock(return_value=_issue)):
            with patch("icx_engine.mcp_server._get_graphs_info", return_value=[{"status": "not_built", "name": "my-svc", "path": "/projects/my-svc", "report_path": None, "access": "", "report_inline": "", "eta_seconds": None}]):
                result = await _handle_analyze_issue("TEST-123", project_paths=["/projects/my-svc"])

    data = json.loads(result)
    assert "graphs" in data
    assert len(data["graphs"]) == 1
    assert "graph" not in data


async def test_handle_analyze_multi_path_icx_next_mentions_all_paths():
    """_icx_next instruction for multi-path call must reference all path statuses."""
    from icx_engine.models.output import IssueContext
    _issue = IssueContext(
        problem_summary="p", detailed_description="d",
        reproduction_steps=[], expected_behavior=None, actual_behavior=None,
        acceptance_criteria=[], impact="i", priority="High", issue_type="Bug",
        confidence_score=0.9, completeness_score=0.9, missing_information=[],
    )
    ready_info = {"status": "ready", "report_path": "/projects/svc/GRAPH_REPORT.md", "access": "pre-authorized", "path": "/projects/svc", "eta_seconds": None}
    not_built_info = {"status": "not_built", "report_path": None, "access": "", "report_inline": "", "path": "/projects/ui", "eta_seconds": None}

    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        with patch("icx_engine.mcp_server.engine.run", new=AsyncMock(return_value=_issue)):
            with patch("icx_engine.mcp_server._get_graphs_info", return_value=[ready_info, not_built_info]):
                result = await _handle_analyze_issue(
                    "TEST-123",
                    project_paths=["/projects/svc", "/projects/ui"],
                )

    data = json.loads(result)
    instruction = data["_icx_next"]["instruction"]
    assert "/projects/svc" in instruction
    assert "/projects/ui" in instruction
    assert "MULTI-PROJECT" in instruction


async def test_handle_analyze_multi_path_unregistered_is_dropped_not_echoed():
    """A not_registered path mixed in with a registered one is silently dropped - never
    echoed back as an `icx graph add/build <path>` prompt (strict no-guessed-path policy)."""
    from icx_engine.models.output import IssueContext
    _issue = IssueContext(
        problem_summary="p", detailed_description="d",
        reproduction_steps=[], expected_behavior=None, actual_behavior=None,
        acceptance_criteria=[], impact="i", priority="High", issue_type="Bug",
        confidence_score=0.9, completeness_score=0.9, missing_information=[],
    )
    ready_info = {"status": "ready", "report_path": "/projects/svc/GRAPH_REPORT.md", "access": "pre-authorized", "path": "/projects/svc", "eta_seconds": None}
    not_reg_info = {"status": "not_registered", "report_path": None, "access": "", "report_inline": "", "path": "/projects/ui", "eta_seconds": None}

    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        with patch("icx_engine.mcp_server.engine.run", new=AsyncMock(return_value=_issue)):
            with patch("icx_engine.mcp_server._get_graphs_info", return_value=[ready_info, not_reg_info]):
                result = await _handle_analyze_issue(
                    "TEST-123",
                    project_paths=["/projects/svc", "/projects/ui"],
                )

    data = json.loads(result)
    paths = [g["path"] for g in data["graphs"]]
    assert paths == ["/projects/svc"]
    assert "/projects/ui" not in json.dumps(data)


async def test_handle_analyze_single_unregistered_path_shows_generic_add_no_guess():
    """A lone unregistered path is dropped; with no ticket match the graph is 'not present'
    and the instruction shows the user how to add+build one - generic placeholders only,
    never the guessed path, never an auto-build."""
    from icx_engine.models.output import IssueContext
    _issue = IssueContext(
        problem_summary="p", detailed_description="d",
        reproduction_steps=[], expected_behavior=None, actual_behavior=None,
        acceptance_criteria=[], impact="i", priority="High", issue_type="Bug",
        confidence_score=0.9, completeness_score=0.9, missing_information=[],
    )
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        with patch("icx_engine.mcp_server._resolve_paths_from_ticket", return_value=None):
            with patch("icx_engine.mcp_server.engine.run", new=AsyncMock(return_value=_issue)):
                with patch("icx_engine.mcp_server._get_graphs_info", return_value=[{
                    "status": "not_registered", "path": "/projects/my-svc", "report_path": None,
                    "access": "", "report_inline": "", "eta_seconds": None,
                }]):
                    result = await _handle_analyze_issue("TEST-123", project_paths=["/projects/my-svc"])

    data = json.loads(result)
    assert data["graphs"] == []
    instruction = data["_icx_next"]["instruction"]
    assert "icx graph add" in instruction
    assert "icx graph build" in instruction
    assert "<project-root>" in instruction
    assert "/projects/my-svc" not in instruction          # guessed path never echoed
    assert "auto" in instruction.lower()                  # states no auto-build


async def test_handle_analyze_all_unregistered_paths_fall_back_to_ticket_key():
    """A guessed path that is not registered must be discarded: ICX self-corrects to the
    ticket's registered graphs instead of telling the user to build the bogus path."""
    from icx_engine.models.output import IssueContext
    _issue = IssueContext(
        problem_summary="p", detailed_description="d",
        reproduction_steps=[], expected_behavior=None, actual_behavior=None,
        acceptance_criteria=[], impact="i", priority="High", issue_type="Bug",
        confidence_score=0.9, completeness_score=0.9, missing_information=[],
    )
    bogus = {"status": "not_registered", "path": "/projects/workspace-root", "report_path": None,
             "access": "", "report_inline": "", "eta_seconds": None}
    svc = {"status": "ready", "report_path": "/projects/svc/GRAPH_REPORT.md",
           "access": "pre-authorized", "path": "/projects/svc", "eta_seconds": None}
    ui = {"status": "ready", "report_path": "/projects/ui/GRAPH_REPORT.md",
          "access": "pre-authorized", "path": "/projects/ui", "eta_seconds": None}

    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        with patch("icx_engine.mcp_server.engine.run", new=AsyncMock(return_value=_issue)):
            with patch("icx_engine.mcp_server._resolve_paths_from_ticket",
                       return_value=[{"path": "/projects/svc", "name": "proj-svc"},
                                     {"path": "/projects/ui", "name": "proj-ui"}]):
                with patch("icx_engine.mcp_server._get_graphs_info",
                           side_effect=[[bogus], [svc, ui]]):
                    result = await _handle_analyze_issue(
                        "PROJ-123", project_paths=["/projects/workspace-root"],
                    )

    data = json.loads(result)
    resolved = {g["path"] for g in data["graphs"]}
    assert resolved == {"/projects/svc", "/projects/ui"}
    assert all(g.get("path_auto_resolved") for g in data["graphs"])
    assert "/projects/workspace-root" not in resolved
    instruction = data["_icx_next"]["instruction"]
    assert "icx graph build" not in instruction
    assert "icx graph add" not in instruction


async def test_handle_analyze_registered_paths_skip_ticket_fallback():
    """When supplied paths ARE registered, ICX must not override them via ticket key."""
    from icx_engine.models.output import IssueContext
    _issue = IssueContext(
        problem_summary="p", detailed_description="d",
        reproduction_steps=[], expected_behavior=None, actual_behavior=None,
        acceptance_criteria=[], impact="i", priority="High", issue_type="Bug",
        confidence_score=0.9, completeness_score=0.9, missing_information=[],
    )
    ready = {"status": "ready", "report_path": "/projects/svc/GRAPH_REPORT.md",
             "access": "pre-authorized", "path": "/projects/svc", "eta_seconds": None}

    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        with patch("icx_engine.mcp_server.engine.run", new=AsyncMock(return_value=_issue)):
            with patch("icx_engine.mcp_server._resolve_paths_from_ticket") as mock_resolve:
                with patch("icx_engine.mcp_server._get_graphs_info", return_value=[ready]):
                    result = await _handle_analyze_issue(
                        "PROJ-123", project_paths=["/projects/svc"],
                    )

    mock_resolve.assert_not_called()
    data = json.loads(result)
    assert [g["path"] for g in data["graphs"]] == ["/projects/svc"]


async def test_handle_analyze_multi_path_vision_gate_includes_additional_paths():
    """Vision gate re-call hint must include additional_paths when they were passed."""
    from icx_engine.models.output import IssueContext
    _issue = IssueContext(
        problem_summary="p", detailed_description="d",
        reproduction_steps=[], expected_behavior=None, actual_behavior=None,
        acceptance_criteria=[], impact="i", priority="High", issue_type="Bug",
        confidence_score=0.9, completeness_score=0.9, missing_information=[],
    )
    ready_info = {"status": "ready", "report_path": "/projects/svc/GRAPH_REPORT.md", "access": "pre-authorized", "path": "/projects/svc", "eta_seconds": None}
    extra_info = {"status": "not_built", "report_path": None, "access": "", "report_inline": "", "path": "/projects/ui", "eta_seconds": None}

    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        with patch("icx_engine.mcp_server.engine.run", new=AsyncMock(return_value=_issue)):
            with patch("icx_engine.mcp_server._get_graphs_info", return_value=[ready_info, extra_info]):
                result = await _handle_analyze_issue(
                    "TEST-123",
                    project_paths=["/projects/svc", "/projects/ui"],
                )

    data = json.loads(result)
    instruction = data["_icx_next"]["instruction"]
    assert "project_paths" in instruction


async def test_handle_analyze_issue_passes_log_callback_to_engine():
    """engine.run must receive a non-None callable log= kwarg from the MCP handler."""
    from icx_engine.models.output import IssueContext
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        with patch("icx_engine.mcp_server.engine.run", new=AsyncMock()) as mock_run:
            mock_run.return_value = IssueContext(
                problem_summary="p", detailed_description="d",
                reproduction_steps=[], expected_behavior=None, actual_behavior=None,
                acceptance_criteria=[], impact="i", priority="High", issue_type="Bug",
                confidence_score=0.9, completeness_score=0.8, missing_information=[],
            )
            with patch("icx_engine.mcp_server._get_graphs_info", return_value=[{
                "status": "not_registered", "path": "/projects/my-svc", "report_path": None,
                "access": "", "report_inline": "", "eta_seconds": None,
            }]):
                await _handle_analyze_issue("TEST-123", project_paths=["/projects/my-svc"])

    _, kwargs = mock_run.call_args
    assert kwargs.get("log") is not None
    assert callable(kwargs["log"])


def test_analyze_shows_missing_requirements_warning():
    """analyze prints ! MISSING REQUIREMENTS when missing_information is non-empty."""
    from icx_engine.models.output import IssueContext
    from unittest.mock import AsyncMock, patch
    config = AppConfig(
        connections=[
            JiraConnection(
                domain="test.atlassian.net",
                auth=TokenAuth(auth_type="token", email="u@t.com", api_token="tok"),
            )
        ],
        llm_profiles={"personal": LLMConfig(text_config=ChannelConfig(provider="ollama", model="llama3"))},
        current_llm_profile="personal",
    )
    issue = IssueContext(
        problem_summary="p", detailed_description="d",
        reproduction_steps=[], expected_behavior=None, actual_behavior=None,
        acceptance_criteria=[], impact="i", priority="High", issue_type="Bug",
        confidence_score=0.7, completeness_score=0.5,
        missing_information=["reproduction_steps", "expected_behavior"],
    )
    with patch("icx_engine.config_manager.ConfigManager.load", return_value=config):
        with patch("icx_engine.engine.run", new=AsyncMock(return_value=issue)):
            result = _cli_runner.invoke(_cli_app, ["analyze", "TEST-123"])
    assert result.exit_code == 0
    assert "MISSING REQUIREMENTS" in result.output
    assert "reproduction_steps" in result.output
    assert "expected_behavior" in result.output
    assert "problem_summary" in result.output  # JSON also present


def test_analyze_no_warning_when_missing_information_empty():
    """No warning printed when missing_information is []."""
    from icx_engine.models.output import IssueContext
    from unittest.mock import AsyncMock, patch
    config = AppConfig(
        connections=[
            JiraConnection(
                domain="test.atlassian.net",
                auth=TokenAuth(auth_type="token", email="u@t.com", api_token="tok"),
            )
        ],
        llm_profiles={"personal": LLMConfig(text_config=ChannelConfig(provider="ollama", model="llama3"))},
        current_llm_profile="personal",
    )
    issue = IssueContext(
        problem_summary="p", detailed_description="d",
        reproduction_steps=["step1"], expected_behavior="x", actual_behavior="y",
        acceptance_criteria=[], impact="i", priority="High", issue_type="Bug",
        confidence_score=0.9, completeness_score=1.0,
        missing_information=[],
    )
    with patch("icx_engine.config_manager.ConfigManager.load", return_value=config):
        with patch("icx_engine.engine.run", new=AsyncMock(return_value=issue)):
            result = _cli_runner.invoke(_cli_app, ["analyze", "TEST-123"])
    assert result.exit_code == 0
    assert "MISSING REQUIREMENTS" not in result.output


def test_analyze_images_written_to_disk_not_inline_in_cli():
    """CLI analyze: base64 images must not appear in stdout JSON; image_paths must be present."""
    import base64
    from icx_engine.models.output import IssueContext
    from unittest.mock import patch

    config = AppConfig(
        connections=[
            JiraConnection(
                domain="test.atlassian.net",
                auth=TokenAuth(auth_type="token", email="u@t.com", api_token="tok"),
            )
        ],
        llm_profiles={"personal": LLMConfig(text_config=ChannelConfig(provider="ollama", model="llama3"))},
        current_llm_profile="personal",
    )
    fake_b64 = base64.b64encode(b"fake-png-bytes").decode()
    issue = IssueContext(
        problem_summary="p", detailed_description="d",
        reproduction_steps=[], expected_behavior=None, actual_behavior=None,
        acceptance_criteria=[], impact="i", priority="High", issue_type="Bug",
        confidence_score=0.9, completeness_score=0.9, missing_information=[],
        images={"screen.png": fake_b64},
    )

    with patch("icx_engine.config_manager.ConfigManager.load", return_value=config):
        with patch("icx_engine.engine.run", new=AsyncMock(return_value=issue)):
            with patch("icx_engine.graph.storage.sweep_stale_temp_dirs"):
                with patch("icx_engine.graph.storage.temp_images_dir") as mock_tid:
                    import tempfile, pathlib
                    tmp = pathlib.Path(tempfile.mkdtemp())
                    mock_tid.return_value = tmp
                    result = _cli_runner.invoke(_cli_app, ["analyze", "TEST-123"])

    assert result.exit_code == 0
    import json as _json
    data = _json.loads(result.output)
    assert "images" not in data, "base64 images must not appear in CLI output"
    assert "image_paths" in data
    assert "screen.png" in data["image_paths"]


def test_analyze_no_warning_for_raw_issue_response():
    """No warning printed when engine returns RawIssueResponse (MCP headless / no LLM mode)."""
    from icx_engine.models.output import RawIssueResponse
    from unittest.mock import AsyncMock, patch
    config = AppConfig(
        connections=[
            JiraConnection(
                domain="test.atlassian.net",
                auth=TokenAuth(auth_type="token", email="u@t.com", api_token="tok"),
            )
        ],
    )
    raw_response = RawIssueResponse(
        issue_key="TEST-123",
        issue_type="Task",
        summary="Some issue",
        description="Some description",
        comments=[],
        attachments=[],
        priority="Medium",
        status="Open",
        metadata={},
    )
    with patch("icx_engine.config_manager.ConfigManager.load", return_value=config):
        with patch("icx_engine.engine.run", new=AsyncMock(return_value=raw_response)):
            result = _cli_runner.invoke(_cli_app, ["analyze", "TEST-123"])
    assert result.exit_code == 0
    assert "MISSING REQUIREMENTS" not in result.output


# -- CLI: --fast flag ----------------------------------------------------------

def test_analyze_fast_flag_passes_skip_vision_to_engine():
    """--fast flag passes skip_vision=True to engine.run()."""
    from icx_engine.models.output import IssueContext
    from unittest.mock import patch

    config = AppConfig(
        connections=[
            JiraConnection(
                domain="test.atlassian.net",
                auth=TokenAuth(auth_type="token", email="u@t.com", api_token="tok"),
            )
        ],
        llm_profiles={"personal": LLMConfig(text_config=ChannelConfig(provider="ollama", model="llama3"))},
        current_llm_profile="personal",
    )
    issue = IssueContext(
        problem_summary="p", detailed_description="d",
        reproduction_steps=[], expected_behavior=None, actual_behavior=None,
        acceptance_criteria=[], impact="i", priority="High", issue_type="Bug",
        confidence_score=0.9, completeness_score=0.9, missing_information=[],
        pending_images=["screenshot.png"],
    )
    captured: list[dict] = []

    async def spy_run(*args, **kwargs):
        captured.append({"skip_vision": kwargs["skip_vision"]})
        return issue

    with patch("icx_engine.config_manager.ConfigManager.load", return_value=config):
        with patch("icx_engine.engine.run", new=spy_run):
            result = _cli_runner.invoke(_cli_app, ["analyze", "TEST-123", "--fast"])

    assert result.exit_code == 0
    assert captured[0]["skip_vision"] is True


def test_analyze_without_fast_flag_uses_full_vision():
    """Without --fast, skip_vision defaults to False."""
    from icx_engine.models.output import IssueContext
    from unittest.mock import patch

    config = AppConfig(
        connections=[
            JiraConnection(
                domain="test.atlassian.net",
                auth=TokenAuth(auth_type="token", email="u@t.com", api_token="tok"),
            )
        ],
        llm_profiles={"personal": LLMConfig(text_config=ChannelConfig(provider="ollama", model="llama3"))},
        current_llm_profile="personal",
    )
    issue = IssueContext(
        problem_summary="p", detailed_description="d",
        reproduction_steps=[], expected_behavior=None, actual_behavior=None,
        acceptance_criteria=[], impact="i", priority="High", issue_type="Bug",
        confidence_score=0.9, completeness_score=0.9, missing_information=[],
    )
    captured: list[dict] = []

    async def spy_run(*args, **kwargs):
        captured.append({"skip_vision": kwargs["skip_vision"]})
        return issue

    with patch("icx_engine.config_manager.ConfigManager.load", return_value=config):
        with patch("icx_engine.engine.run", new=spy_run):
            result = _cli_runner.invoke(_cli_app, ["analyze", "TEST-123"])

    assert result.exit_code == 0
    assert captured[0]["skip_vision"] is False


# -- Timeout handling ----------------------------------------------------------

async def test_engine_run_timeout_returns_error_json():
    """When engine.run() exceeds 660s, _handle_analyze_issue returns a JSON error (no exception)."""
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        with patch("icx_engine.mcp_server.engine.run", new=AsyncMock(side_effect=asyncio.TimeoutError)):
            result = await _handle_analyze_issue("TEST-123", project_paths=["/projects/my-svc"])
    data = json.loads(result)
    assert data.get("status") == "error"
    assert data.get("code") == "TIMEOUT"
    assert "timed out" in data["message"].lower()
    assert "type" not in data


async def test_engine_run_timeout_error_is_not_generic_unexpected():
    """Timeout error message must be specific, not the generic 'Unexpected error' fallback."""
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        with patch("icx_engine.mcp_server.engine.run", new=AsyncMock(side_effect=asyncio.TimeoutError)):
            result = await _handle_analyze_issue("TEST-123", project_paths=["/projects/my-svc"])
    data = json.loads(result)
    assert "Unexpected error" not in data["message"]


# -- Non-bug instruction enhancement ------------------------------------------

async def test_non_bug_instruction_includes_convention_discovery():
    """For Story/Task issue types, _icx_next instruction includes the convention-discovery step."""
    from icx_engine.models.output import IssueContext
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        with patch("icx_engine.mcp_server.engine.run", new=AsyncMock()) as mock_run:
            mock_run.return_value = IssueContext(
                problem_summary="Add CSV export for users",
                detailed_description="Users need to download their data",
                reproduction_steps=[], expected_behavior=None, actual_behavior=None,
                acceptance_criteria=["Export button visible", "CSV downloads correctly"],
                impact="medium", priority="Medium", issue_type="Story",
                confidence_score=0.9, completeness_score=0.9, missing_information=[],
            )
            with patch("icx_engine.mcp_server._get_graphs_info", return_value=[{
                "status": "ready",
                "path": "/projects/my-svc",
                "report_path": "/projects/my-svc/GRAPH_REPORT.md",
                "access": "pre-authorized",
                "eta_seconds": None,
            }]):
                result = await _handle_analyze_issue("TEST-123", project_paths=["/projects/my-svc"])

    data = json.loads(result)
    instruction = data["_icx_next"]["instruction"]
    assert "CONVENTION DISCOVERY" in instruction
    assert "LAYER / FLOW PATTERN" in instruction
    assert "LOGGER PATTERN" in instruction
    assert "DEPENDENCY MANAGEMENT" in instruction
    assert "Conventions I will follow" in instruction
    assert "New external dependencies required" in instruction
    assert "**Shall I proceed?**" in instruction


async def test_bug_instruction_does_not_include_convention_discovery():
    """For Bug issue type, instruction must NOT include the convention-discovery step."""
    from icx_engine.models.output import IssueContext
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        with patch("icx_engine.mcp_server.engine.run", new=AsyncMock()) as mock_run:
            mock_run.return_value = IssueContext(
                problem_summary="Login fails on mobile",
                detailed_description="d",
                reproduction_steps=[], expected_behavior=None, actual_behavior=None,
                acceptance_criteria=[], impact="high", priority="High", issue_type="Bug",
                confidence_score=0.9, completeness_score=0.9, missing_information=[],
            )
            with patch("icx_engine.mcp_server._get_graphs_info", return_value=[{
                "status": "ready",
                "path": "/projects/my-svc",
                "report_path": "/projects/my-svc/GRAPH_REPORT.md",
                "access": "pre-authorized",
                "eta_seconds": None,
            }]):
                result = await _handle_analyze_issue("TEST-123", project_paths=["/projects/my-svc"])

    data = json.loads(result)
    instruction = data["_icx_next"]["instruction"]
    assert "CONVENTION DISCOVERY" not in instruction
    assert "Conventions I will follow" not in instruction
    assert "New external dependencies required" not in instruction
    assert "**Shall I proceed?**" in instruction


async def test_task_instruction_includes_convention_discovery():
    """Task issue type also triggers convention-discovery (it is non-bug)."""
    from icx_engine.models.output import IssueContext
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        with patch("icx_engine.mcp_server.engine.run", new=AsyncMock()) as mock_run:
            mock_run.return_value = IssueContext(
                problem_summary="Migrate database schema",
                detailed_description="d",
                reproduction_steps=[], expected_behavior=None, actual_behavior=None,
                acceptance_criteria=[], impact="high", priority="High", issue_type="Task",
                confidence_score=0.9, completeness_score=0.9, missing_information=[],
            )
            with patch("icx_engine.mcp_server._get_graphs_info", return_value=[{
                "status": "not_built",
                "path": "/projects/my-svc",
                "report_path": None,
                "access": "",
                "report_inline": "",
                "eta_seconds": None,
            }]):
                result = await _handle_analyze_issue("TEST-123", project_paths=["/projects/my-svc"])

    data = json.loads(result)
    instruction = data["_icx_next"]["instruction"]
    assert "CONVENTION DISCOVERY" in instruction
    assert "New external dependencies required" in instruction


# -- Session context accumulation ----------------------------------------------

def _make_issue_ctx(summary: str = "p", issue_type: str = "Bug"):
    from icx_engine.models.output import IssueContext
    return IssueContext(
        problem_summary=summary, detailed_description="d",
        reproduction_steps=[], expected_behavior=None, actual_behavior=None,
        acceptance_criteria=[], impact="i", priority="High", issue_type=issue_type,
        confidence_score=0.9, completeness_score=0.9, missing_information=[],
    )

_GRAPH_NOT_REG = {"status": "not_registered", "path": "/p", "report_path": None, "access": "", "report_inline": "", "eta_seconds": None}


async def test_session_context_first_call_is_empty(monkeypatch):
    """First call in a fresh session: session_context in response is empty list."""
    import icx_engine.mcp_server as _mcp
    monkeypatch.setattr(_mcp, "_SESSION_CONTEXT", [])

    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        with patch("icx_engine.mcp_server.engine.run", new=AsyncMock(return_value=_make_issue_ctx("Auth token expired"))):
            with patch("icx_engine.mcp_server._get_graphs_info", return_value=[_GRAPH_NOT_REG]):
                result = await _handle_analyze_issue("PROJ-1", project_paths=["/p"])

    data = json.loads(result)
    assert "session_context" in data
    assert data["session_context"] == []


async def test_session_context_second_call_sees_first(monkeypatch):
    """Second call: session_context contains the first item; instruction references it."""
    import icx_engine.mcp_server as _mcp
    monkeypatch.setattr(_mcp, "_SESSION_CONTEXT", [])

    _graph = _GRAPH_NOT_REG
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        with patch("icx_engine.mcp_server._get_graphs_info", return_value=[_graph]):
            with patch("icx_engine.mcp_server.engine.run", new=AsyncMock(return_value=_make_issue_ctx("Auth token expired"))):
                await _handle_analyze_issue("PROJ-1", project_paths=["/p"])
            with patch("icx_engine.mcp_server.engine.run", new=AsyncMock(return_value=_make_issue_ctx("Login page crash"))):
                result2 = await _handle_analyze_issue("PROJ-2", project_paths=["/p"])

    data = json.loads(result2)
    assert len(data["session_context"]) == 1
    assert data["session_context"][0]["issue_key"] == "PROJ-1"
    assert data["session_context"][0]["issue_type"] == "Bug"
    assert "SESSION CONTEXT" in data["_icx_next"]["instruction"]
    assert "PROJ-1" in data["_icx_next"]["instruction"]


async def test_session_context_capped_at_session_max(monkeypatch):
    """Accumulating more than _SESSION_MAX items keeps only the last _SESSION_MAX."""
    import icx_engine.mcp_server as _mcp
    monkeypatch.setattr(_mcp, "_SESSION_CONTEXT", [])

    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        with patch("icx_engine.mcp_server._get_graphs_info", return_value=[_GRAPH_NOT_REG]):
            for i in range(_mcp._SESSION_MAX + 1):
                with patch("icx_engine.mcp_server.engine.run", new=AsyncMock(return_value=_make_issue_ctx(f"issue {i}"))):
                    await _handle_analyze_issue(f"PROJ-{i}", project_paths=["/p"])

    assert len(_mcp._SESSION_CONTEXT) == _mcp._SESSION_MAX


async def test_session_context_deduplicates_same_key(monkeypatch):
    """Re-analyzing the same key removes the old entry and adds it at the end."""
    import icx_engine.mcp_server as _mcp
    monkeypatch.setattr(_mcp, "_SESSION_CONTEXT", [])

    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        with patch("icx_engine.mcp_server._get_graphs_info", return_value=[_GRAPH_NOT_REG]):
            with patch("icx_engine.mcp_server.engine.run", new=AsyncMock(return_value=_make_issue_ctx("First look"))):
                await _handle_analyze_issue("PROJ-1", project_paths=["/p"])
            with patch("icx_engine.mcp_server.engine.run", new=AsyncMock(return_value=_make_issue_ctx("Second look"))):
                await _handle_analyze_issue("PROJ-2", project_paths=["/p"])
            with patch("icx_engine.mcp_server.engine.run", new=AsyncMock(return_value=_make_issue_ctx("Revisit"))):
                await _handle_analyze_issue("PROJ-1", project_paths=["/p"])

    # PROJ-1 should appear only once, at the end
    keys = [e["issue_key"] for e in _mcp._SESSION_CONTEXT]
    assert keys.count("PROJ-1") == 1
    assert keys[-1] == "PROJ-1"


# -- memory_search tool --------------------------------------------------------

async def test_memory_search_tool_present_in_list_tools():
    from icx_engine.mcp_server import _list_tools
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        tools = await _list_tools()
    names = {t.name for t in tools}
    assert "memory_search" in names


async def test_memory_search_tool_has_required_inputs():
    from icx_engine.mcp_server import _list_tools
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        tools = await _list_tools()
    tool = next(t for t in tools if t.name == "memory_search")
    assert "query" in tool.inputSchema["required"]
    assert "tags" in tool.inputSchema["required"]
    assert "top_k" not in tool.inputSchema.get("required", [])


async def test_call_tool_memory_search_rejects_empty_query():
    from icx_engine.mcp_server import _call_tool
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        result = await _call_tool("memory_search", {"query": "", "tags": ["jwt"]})
    data = json.loads(result[0].text)
    assert "error" in data


async def test_call_tool_memory_search_returns_empty_when_memory_not_ready():
    from icx_engine.mcp_server import _call_tool
    with patch("icx_engine.mcp_server._get_memory_state", return_value="cold"):
        result = await _call_tool("memory_search", {"query": "auth token", "tags": ["jwt"]})
    data = json.loads(result[0].text)
    assert data["count"] == 0
    assert data["results"] == []
    assert data["status"] == "cold"


# -- memory_get_related tool ---------------------------------------------------

async def test_call_tool_memory_get_related_no_params_returns_error():
    from icx_engine.mcp_server import _call_tool
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        result = await _call_tool("memory_get_related", {})
    data = json.loads(result[0].text)
    assert "error" in data


async def test_call_tool_memory_get_related_files_primary_path():
    from icx_engine.mcp_server import _call_tool
    fake_results = [{"issue_key": "PROJ-99", "relation_type": "shares_file", "strength": 0.75}]
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        with patch("icx_engine.mcp_server._get_related_sync", return_value=fake_results):
            result = await _call_tool("memory_get_related", {"files": ["auth/token.py"]})
    data = json.loads(result[0].text)
    assert data["count"] == 1
    assert data["results"][0]["issue_key"] == "PROJ-99"


async def test_call_tool_memory_get_related_issue_key_edge_path():
    from icx_engine.mcp_server import _call_tool
    fake_results = [{"issue_key": "PROJ-50", "relation_type": "shares_file", "strength": 0.9}]
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        with patch("icx_engine.mcp_server._get_related_sync", return_value=fake_results):
            result = await _call_tool("memory_get_related", {"issue_key": "PROJ-100"})
    data = json.loads(result[0].text)
    assert data["count"] == 1
    assert data["results"][0]["issue_key"] == "PROJ-50"


def test_get_related_sync_delegates_to_memory_manager(tmp_path):
    """_get_related_sync delegates to MemoryManager.get_related() using the shared instance."""
    import icx_engine.mcp_server as _mcp

    expected = [{"issue_key": "PROJ-5", "relation_type": "shares_file", "strength": 1.0}]
    mock_mem = MagicMock()
    mock_mem.get_related.return_value = expected

    with patch("icx_engine.mcp_server._ensure_memory_manager", return_value=mock_mem):
        result = _mcp._get_related_sync(None, None, ["auth/token.py"])

    mock_mem.get_related.assert_called_once_with(None, None, ["auth/token.py"])
    assert result == expected


# -- Structured error responses ------------------------------------------------

async def test_call_tool_analyze_missing_project_paths_proceeds_to_engine():
    """analyze_issue with no project_paths omitted proceeds (no MISSING_PROJECT_PATH error)."""
    from icx_engine.mcp_server import _call_tool
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        with patch("icx_engine.mcp_server._resolve_paths_from_ticket", return_value=None):
            result = await _call_tool("analyze_issue", {"issue_ref": "TEST-123"})
    data = json.loads(result[0].text)
    assert data.get("code") != "MISSING_PROJECT_PATH"


async def test_call_tool_analyze_empty_project_paths_proceeds_to_engine():
    """analyze_issue with empty project_paths list proceeds (no MISSING_PROJECT_PATH error)."""
    from icx_engine.mcp_server import _call_tool
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        with patch("icx_engine.mcp_server._resolve_paths_from_ticket", return_value=None):
            result = await _call_tool("analyze_issue", {"issue_ref": "TEST-123", "project_paths": []})
    data = json.loads(result[0].text)
    assert data.get("code") != "MISSING_PROJECT_PATH"


async def test_call_tool_analyze_whitespace_only_paths_proceeds_to_engine():
    """analyze_issue with whitespace-only paths proceeds after stripping (no MISSING_PROJECT_PATH)."""
    from icx_engine.mcp_server import _call_tool
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        with patch("icx_engine.mcp_server._resolve_paths_from_ticket", return_value=None):
            result = await _call_tool("analyze_issue", {"issue_ref": "TEST-123", "project_paths": ["   "]})
    data = json.loads(result[0].text)
    assert data.get("code") != "MISSING_PROJECT_PATH"


async def test_handle_analyze_issue_not_found_returns_structured_error():
    """IssueNotFound from engine returns code=ISSUE_NOT_FOUND with action_required."""
    from icx_engine.exceptions import IssueNotFound
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        with patch("icx_engine.mcp_server.engine.run", new=AsyncMock(side_effect=IssueNotFound("Issue not found. Check the URL or issue key."))):
            result = await _handle_analyze_issue("TEST-404", project_paths=["/projects/my-svc"])
    data = json.loads(result)
    assert data.get("status") == "error"
    assert data.get("code") == "ISSUE_NOT_FOUND"
    assert data.get("action_required") == "ask_user_to_verify_issue_key"
    assert "not found" in data["message"].lower()


async def test_handle_analyze_issue_auth_error_returns_structured_error():
    """AuthError from engine returns code=AUTH_FAILED with action_required."""
    from icx_engine.exceptions import AuthError
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        with patch("icx_engine.mcp_server.engine.run", new=AsyncMock(side_effect=AuthError("Authentication failed. Run `icx connection --add` to reconnect."))):
            result = await _handle_analyze_issue("TEST-123", project_paths=["/projects/my-svc"])
    data = json.loads(result)
    assert data.get("status") == "error"
    assert data.get("code") == "AUTH_FAILED"
    assert data.get("action_required") == "tell_user_to_run_icx_connection_add"


async def test_handle_analyze_issue_no_connection_returns_structured_error():
    """NoConnectionError from engine returns code=NO_CONNECTION with action_required."""
    from icx_engine.exceptions import NoConnectionError
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        with patch("icx_engine.mcp_server.engine.run", new=AsyncMock(side_effect=NoConnectionError("No connection configured."))):
            result = await _handle_analyze_issue("TEST-123", project_paths=["/projects/my-svc"])
    data = json.loads(result)
    assert data.get("status") == "error"
    assert data.get("code") == "NO_CONNECTION"
    assert data.get("action_required") == "tell_user_to_run_icx_connection_add"


async def test_handle_analyze_issue_rate_limited_returns_structured_error():
    """RateLimited from engine returns code=RATE_LIMITED with action_required."""
    from icx_engine.exceptions import RateLimited
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        with patch("icx_engine.mcp_server.engine.run", new=AsyncMock(side_effect=RateLimited("Rate limited. Wait a moment and try again."))):
            result = await _handle_analyze_issue("TEST-123", project_paths=["/projects/my-svc"])
    data = json.loads(result)
    assert data.get("status") == "error"
    assert data.get("code") == "RATE_LIMITED"
    assert data.get("action_required") == "wait_and_retry"


async def test_handle_analyze_issue_invalid_input_returns_structured_error():
    """InvalidInput from engine returns code=INVALID_INPUT with action_required."""
    from icx_engine.exceptions import InvalidInput
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        with patch("icx_engine.mcp_server.engine.run", new=AsyncMock(side_effect=InvalidInput("Invalid issue key format."))):
            result = await _handle_analyze_issue("TEST-123", project_paths=["/projects/my-svc"])
    data = json.loads(result)
    assert data.get("status") == "error"
    assert data.get("code") == "INVALID_INPUT"
    assert data.get("action_required") == "ask_user_for_correct_issue_key"


async def test_handle_analyze_issue_unexpected_exception_returns_structured_error():
    """Unhandled exceptions return code=INTERNAL_ERROR with action_required."""
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        with patch("icx_engine.mcp_server.engine.run", new=AsyncMock(side_effect=RuntimeError("Disk full"))):
            result = await _handle_analyze_issue("TEST-123", project_paths=["/projects/my-svc"])
    data = json.loads(result)
    assert data.get("status") == "error"
    assert data.get("code") == "INTERNAL_ERROR"
    assert data.get("action_required") == "report_error_to_user"
    assert "Disk full" in data["message"]


async def test_handle_analyze_issue_timeout_returns_structured_error():
    """TimeoutError returns code=TIMEOUT with action_required and no 'type' key."""
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        with patch("icx_engine.mcp_server.engine.run", new=AsyncMock(side_effect=asyncio.TimeoutError)):
            result = await _handle_analyze_issue("TEST-123", project_paths=["/projects/my-svc"])
    data = json.loads(result)
    assert data.get("status") == "error"
    assert data.get("code") == "TIMEOUT"
    assert data.get("action_required") == "tell_user_to_check_network_and_retry"
    assert "type" not in data


# -- project_paths auto-resolution ---------------------------------------------

async def test_handle_analyze_empty_paths_auto_resolves_from_jira_key():
    """Empty project_paths triggers registry lookup by Jira project key; match populates graphs."""
    from icx_engine.models.output import IssueContext
    _issue = IssueContext(
        problem_summary="p", detailed_description="d",
        reproduction_steps=[], expected_behavior=None, actual_behavior=None,
        acceptance_criteria=[], impact="i", priority="High", issue_type="Bug",
        confidence_score=0.9, completeness_score=0.9, missing_information=[],
    )
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        with patch("icx_engine.mcp_server._resolve_paths_from_ticket",
                   return_value=[{"path": "/clients/myapp", "name": "myapp"}]):
            with patch("icx_engine.mcp_server.engine.run", new=AsyncMock(return_value=_issue)):
                with patch("icx_engine.mcp_server._get_graphs_info", return_value=[{
                    "status": "not_built", "path": "/clients/myapp", "name": "myapp",
                    "report_path": None, "access": "", "report_inline": "", "eta_seconds": None,
                }]):
                    result = await _handle_analyze_issue("MYAPP-123", project_paths=[])

    data = json.loads(result)
    assert "graphs" in data
    assert len(data["graphs"]) == 1
    assert data["graphs"][0]["path"] == "/clients/myapp"
    assert data["graphs"][0].get("path_auto_resolved") is True


async def test_handle_analyze_empty_paths_auto_resolves_multiple_projects():
    """When Jira key maps to multiple projects, all are returned in graphs."""
    from icx_engine.models.output import IssueContext
    _issue = IssueContext(
        problem_summary="p", detailed_description="d",
        reproduction_steps=[], expected_behavior=None, actual_behavior=None,
        acceptance_criteria=[], impact="i", priority="High", issue_type="Bug",
        confidence_score=0.9, completeness_score=0.9, missing_information=[],
    )
    two_graphs = [
        {"status": "ready", "path": "/clients/app/svc", "name": "app-svc",
         "report_path": "/clients/app/svc/GRAPH_REPORT.md", "access": "pre-authorized", "eta_seconds": None},
        {"status": "not_built", "path": "/clients/app/ui", "name": "app-ui",
         "report_path": None, "access": "", "report_inline": "", "eta_seconds": None},
    ]
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        with patch("icx_engine.mcp_server._resolve_paths_from_ticket",
                   return_value=[{"path": "/clients/app/svc", "name": "app-svc"},
                                  {"path": "/clients/app/ui", "name": "app-ui"}]):
            with patch("icx_engine.mcp_server.engine.run", new=AsyncMock(return_value=_issue)):
                with patch("icx_engine.mcp_server._get_graphs_info", return_value=two_graphs):
                    result = await _handle_analyze_issue("APP-1", project_paths=[])

    data = json.loads(result)
    assert len(data["graphs"]) == 2
    assert all(g.get("path_auto_resolved") is True for g in data["graphs"])


async def test_handle_analyze_empty_paths_no_registry_match_returns_empty_graphs():
    """Empty project_paths with no registry match produces graphs=[] and grep/glob instruction."""
    from icx_engine.models.output import IssueContext
    _issue = IssueContext(
        problem_summary="p", detailed_description="d",
        reproduction_steps=[], expected_behavior=None, actual_behavior=None,
        acceptance_criteria=[], impact="i", priority="High", issue_type="Bug",
        confidence_score=0.9, completeness_score=0.9, missing_information=[],
    )
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        with patch("icx_engine.mcp_server._resolve_paths_from_ticket", return_value=None):
            with patch("icx_engine.mcp_server.engine.run", new=AsyncMock(return_value=_issue)):
                result = await _handle_analyze_issue("UNKNOWN-99", project_paths=[])

    data = json.loads(result)
    assert data.get("graphs") == []
    assert "grep" in data["_icx_next"]["instruction"].lower()


async def test_handle_analyze_unregistered_path_no_ticket_match_dropped():
    """Explicit unregistered path with a non-resolving ticket key: the path is dropped (never
    auto-registered, never echoed). Ticket fallback is attempted; with no match graphs is
    empty and the agent is told to grep/glob plus shown how to add a graph."""
    from icx_engine.models.output import IssueContext
    _issue = IssueContext(
        problem_summary="p", detailed_description="d",
        reproduction_steps=[], expected_behavior=None, actual_behavior=None,
        acceptance_criteria=[], impact="i", priority="High", issue_type="Bug",
        confidence_score=0.9, completeness_score=0.9, missing_information=[],
    )
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        with patch("icx_engine.mcp_server._resolve_paths_from_ticket", return_value=None) as mock_resolve:
            with patch("icx_engine.mcp_server.engine.run", new=AsyncMock(return_value=_issue)):
                with patch("icx_engine.mcp_server._get_graphs_info", return_value=[{
                    "status": "not_registered", "path": "/explicit/path",
                    "report_path": None, "access": "", "report_inline": "", "eta_seconds": None,
                }]):
                    result = await _handle_analyze_issue("TEST-1", project_paths=["/explicit/path"])

    mock_resolve.assert_called_once_with("TEST-1")
    data = json.loads(result)
    assert data["graphs"] == []
    assert "/explicit/path" not in json.dumps(data)


def test_tool_descriptions_forbid_external_tracker_mcp():
    """Both analyze tool descriptions must declare ICX the sole tracker interface and forbid
    routing tracker work through any other MCP - stated generically (no single provider name)."""
    from icx_engine.mcp_server import _FAST_DESCRIPTION, _FULL_DESCRIPTION
    for desc in (_FAST_DESCRIPTION, _FULL_DESCRIPTION):
        assert "SOLE TRACKER INTERFACE" in desc
        assert "MUST NOT connect to" in desc
        assert "ANY other MCP server" in desc
        # Generic - must not single out one provider in the rule text.
        assert "Jira MCP" not in desc
        assert "GitHub MCP" not in desc


def test_resume_desc_has_gate_posture_classification():
    """resume description must carry the gate-posture classification with agent-generate
    gates enumerated (2b, compat_scan, profile_gen) and user-decision gates listed."""
    from icx_engine.mcp_server import _MAGIK_RESUME_DESCRIPTION as d
    assert "GATE POSTURE CLASSIFICATION" in d
    assert "USER-DECISION" in d
    assert "AGENT-GENERATE" in d
    # 2b, compat_scan, and profile_gen are all agent-generate gates.
    assert "2b" in d and "compat_scan" in d and "profile_gen" in d
    # user-decision gate list includes the key human gates.
    for g in ("ui_check", "memory_save", "manual", "error", "limit"):
        assert g in d


def test_resume_desc_no_unscoped_auto_respond_phrase():
    """The contradiction is removed: no rule may say to never auto-respond to ANY gate (which
    fought the agent-generate 2b gate). It must be scoped to USER-DECISION gates."""
    from icx_engine.mcp_server import _MAGIK_RESUME_DESCRIPTION as d
    assert "AUTO-RESPOND TO ANY GATE" not in d
    assert "AUTO-RESPOND TO A USER-DECISION GATE" in d


def test_default_posture_line_in_both_testing_descs():
    """Both start and resume descriptions state the default posture: gate data is the user's to
    decide; the agent only self-advances to generate the spec at Gate 2b."""
    from icx_engine.mcp_server import _MAGIK_START_DESCRIPTION, _MAGIK_RESUME_DESCRIPTION
    for d in (_MAGIK_START_DESCRIPTION, _MAGIK_RESUME_DESCRIPTION):
        assert "DEFAULT POSTURE" in d
        assert "for the USER to read and decide" in d
        assert "Gate 2b" in d


def test_start_desc_seed_selection_endpoint_and_backend_bridge():
    """start description must describe Phase A seed selection: the endpoint/route grep option
    and the backend-only API-path -> UI-repo grep bridge."""
    from icx_engine.mcp_server import _MAGIK_START_DESCRIPTION as d
    assert "PHASE A" in d
    assert "endpoint/route" in d
    assert "BACKEND-ONLY" in d
    # backend bridge: grep the backend api path, then grep the UI repo for it.
    assert "grep the UI repo" in d


def test_gate1_desc_has_grep_import_fallback_when_no_graph():
    """Gate 1 must instruct the agent to grep-expand imports as the fallback when the UI repo
    graph is not built, and still end in a user confirm."""
    from icx_engine.mcp_server import _MAGIK_RESUME_DESCRIPTION as d
    assert "graph_available IS FALSE" in d
    assert "grep its own imports" in d
    assert "no-graph fallback" in d


# -- _extract_tracker_key_from_ref ----------------------------------------------

def test_extract_tracker_key_from_url():
    from icx_engine.mcp_server import _extract_tracker_key_from_ref
    assert _extract_tracker_key_from_ref("https://foo.atlassian.net/browse/CCBSS-19583") == "CCBSS-19583"


def test_extract_tracker_key_from_bare_key():
    from icx_engine.mcp_server import _extract_tracker_key_from_ref
    assert _extract_tracker_key_from_ref("PROJ-42") == "PROJ-42"


def test_extract_tracker_key_from_url_with_trailing_slash():
    from icx_engine.mcp_server import _extract_tracker_key_from_ref
    assert _extract_tracker_key_from_ref("https://foo.atlassian.net/browse/AB-1/") == "AB-1"


def test_extract_tracker_key_returns_empty_for_invalid():
    from icx_engine.mcp_server import _extract_tracker_key_from_ref
    assert _extract_tracker_key_from_ref("not-a-valid-ref") == ""


# -- graph_important_nodes -----------------------------------------------------

async def test_graph_important_nodes_missing_project_path_returns_error():
    from icx_engine.mcp_server import _call_tool
    result = await _call_tool("graph_important_nodes", {})
    data = json.loads(result[0].text)
    assert data.get("status") == "error" or "error" in data


async def test_graph_important_nodes_nonexistent_path_returns_error():
    from icx_engine.mcp_server import _call_tool
    result = await _call_tool("graph_important_nodes", {"project_path": "/nonexistent/icx_test_xyz_12345"})
    data = json.loads(result[0].text)
    assert "error" in data or data.get("status") == "error"


# -- graph_find_context --------------------------------------------------------

async def test_graph_find_context_missing_project_path_returns_error():
    from icx_engine.mcp_server import _call_tool
    result = await _call_tool("graph_find_context", {"task": "auth token expiry"})
    data = json.loads(result[0].text)
    assert data.get("status") == "error"
    assert data.get("code") == "NO_PATH"


async def test_graph_find_context_nonexistent_path_returns_error():
    from icx_engine.mcp_server import _call_tool
    result = await _call_tool("graph_find_context", {
        "project_path": "/nonexistent/icx_test_xyz_12345",
        "task": "auth token expiry",
    })
    data = json.loads(result[0].text)
    assert data.get("status") == "error"


# -- graph_subsystem -----------------------------------------------------------

async def test_graph_subsystem_missing_project_path_returns_error():
    from icx_engine.mcp_server import _call_tool
    result = await _call_tool("graph_subsystem", {"file_path": "src/auth/service.py"})
    data = json.loads(result[0].text)
    assert data.get("status") == "error"
    assert data.get("code") == "NO_PATH"


async def test_graph_subsystem_nonexistent_path_returns_error():
    from icx_engine.mcp_server import _call_tool
    result = await _call_tool("graph_subsystem", {
        "project_path": "/nonexistent/icx_test_xyz_12345",
        "file_path": "src/auth/service.py",
    })
    data = json.loads(result[0].text)
    assert data.get("status") == "error"


# -- graph_ownership -----------------------------------------------------------

async def test_graph_ownership_missing_project_path_returns_error():
    from icx_engine.mcp_server import _call_tool
    result = await _call_tool("graph_ownership", {"file_path": "src/billing/invoice.py"})
    data = json.loads(result[0].text)
    assert data.get("status") == "error"
    assert data.get("code") == "NO_PATH"


async def test_graph_ownership_nonexistent_path_returns_error():
    from icx_engine.mcp_server import _call_tool
    result = await _call_tool("graph_ownership", {
        "project_path": "/nonexistent/icx_test_xyz_12345",
        "file_path": "src/billing/invoice.py",
    })
    data = json.loads(result[0].text)
    assert "error" in data or data.get("status") == "error"


async def test_graph_ownership_rejects_path_traversal():
    from icx_engine.mcp_server import _call_tool
    result = await _call_tool("graph_ownership", {
        "project_path": "/nonexistent/icx_test_xyz_12345",
        "file_path": "../../../etc/passwd",
    })
    data = json.loads(result[0].text)
    assert "error" in data or data.get("status") == "error"
    assert "owners" not in data


# -- graph_call_chain ----------------------------------------------------------

async def test_graph_call_chain_missing_project_path_returns_error():
    from icx_engine.mcp_server import _call_tool
    result = await _call_tool("graph_call_chain", {"node_id": "auth_service"})
    data = json.loads(result[0].text)
    assert data.get("status") == "error"
    assert data.get("code") == "NO_PATH"


async def test_graph_call_chain_nonexistent_path_returns_error():
    from icx_engine.mcp_server import _call_tool
    result = await _call_tool("graph_call_chain", {
        "project_path": "/nonexistent/icx_test_xyz_12345",
        "node_id": "auth_service",
    })
    data = json.loads(result[0].text)
    assert data.get("status") == "error"


# -- graph_impact --------------------------------------------------------------

async def test_graph_impact_missing_project_path_returns_error():
    from icx_engine.mcp_server import _call_tool
    result = await _call_tool("graph_impact", {"node_id": "user_repository"})
    data = json.loads(result[0].text)
    assert data.get("status") == "error"
    assert data.get("code") == "NO_PATH"


async def test_graph_impact_nonexistent_path_returns_error():
    from icx_engine.mcp_server import _call_tool
    result = await _call_tool("graph_impact", {
        "project_path": "/nonexistent/icx_test_xyz_12345",
        "node_id": "user_repository",
    })
    data = json.loads(result[0].text)
    assert data.get("status") == "error"


# -- graph_cross_links ---------------------------------------------------------

async def test_graph_cross_links_missing_project_path_returns_error():
    from icx_engine.mcp_server import _call_tool
    result = await _call_tool("graph_cross_links", {})
    data = json.loads(result[0].text)
    assert data.get("status") == "error"
    assert data.get("code") == "NO_PATH"


async def test_graph_cross_links_nonexistent_path_returns_error():
    from icx_engine.mcp_server import _call_tool
    result = await _call_tool("graph_cross_links", {
        "project_path": "/nonexistent/icx_test_xyz_12345",
    })
    data = json.loads(result[0].text)
    assert data.get("status") == "error"


# -- graph_blast_radius --------------------------------------------------------

async def test_graph_blast_radius_missing_project_path_returns_error():
    from icx_engine.mcp_server import _call_tool
    result = await _call_tool("graph_blast_radius", {"changed_files": ["src/auth/token.py"]})
    data = json.loads(result[0].text)
    assert data.get("status") == "error"
    assert data.get("code") == "NO_PATH"


async def test_graph_blast_radius_nonexistent_path_returns_error():
    from icx_engine.mcp_server import _call_tool
    result = await _call_tool("graph_blast_radius", {
        "project_path": "/nonexistent/icx_test_xyz_12345",
        "changed_files": ["src/auth/token.py"],
    })
    data = json.loads(result[0].text)
    assert "error" in data or data.get("status") == "error"


# -- graph_cycles --------------------------------------------------------------

async def test_graph_cycles_missing_project_path_returns_error():
    from icx_engine.mcp_server import _call_tool
    result = await _call_tool("graph_cycles", {})
    data = json.loads(result[0].text)
    assert data.get("status") == "error"
    assert data.get("code") == "NO_PATH"


async def test_graph_cycles_nonexistent_path_returns_error():
    from icx_engine.mcp_server import _call_tool
    result = await _call_tool("graph_cycles", {
        "project_path": "/nonexistent/icx_test_xyz_12345",
    })
    data = json.loads(result[0].text)
    assert "error" in data or data.get("status") == "error"


# -- graph_dead_code -----------------------------------------------------------

async def test_graph_dead_code_missing_project_path_returns_error():
    from icx_engine.mcp_server import _call_tool
    result = await _call_tool("graph_dead_code", {})
    data = json.loads(result[0].text)
    assert data.get("status") == "error"
    assert data.get("code") == "NO_PATH"


async def test_graph_dead_code_nonexistent_path_returns_error():
    from icx_engine.mcp_server import _call_tool
    result = await _call_tool("graph_dead_code", {
        "project_path": "/nonexistent/icx_test_xyz_12345",
    })
    data = json.loads(result[0].text)
    assert "error" in data or data.get("status") == "error"


# -- start_testing_session injects configured agent_max_steps ----------

async def test_start_session_injects_configured_agent_steps(monkeypatch):
    from icx_engine import mcp_server
    from icx_engine.models.config import AppConfig
    from unittest.mock import patch

    captured = {}

    class _Snap:
        tasks = []
        next = ()

    class _FakeGraph:
        async def ainvoke(self, state, config=None):
            if isinstance(state, dict):
                captured["agent_max_steps"] = state.get("agent_max_steps")

        async def aget_state(self, config):
            return _Snap()

    async def _fake_get_graph():
        return _FakeGraph()

    import icx_engine.testing.graph as _g
    monkeypatch.setattr(_g, "get_testing_graph", _fake_get_graph)

    cfg = AppConfig()
    cfg.magik_agent_max_steps = 33
    with patch("icx_engine.config_manager.ConfigManager") as mock_cm:
        mock_cm.load.return_value = cfg
        await mcp_server._call_tool(
            "start_testing_session",
            {"file_paths": ["a.tsx"], "test_mode": "automated"},
        )
    assert captured["agent_max_steps"] == 33


# -- memory_get_hotspots -------------------------------------------------------

async def test_memory_get_hotspots_returns_empty_structure():
    from icx_engine.mcp_server import _call_tool
    with patch("icx_engine.mcp_server._get_hotspots_sync", return_value=[]):
        result = await _call_tool("memory_get_hotspots", {})
    data = json.loads(result[0].text)
    assert "results" in data
    assert "count" in data
    assert data["count"] == 0


async def test_memory_get_hotspots_returns_items_from_manager():
    from icx_engine.mcp_server import _call_tool
    fake = [{"file": "src/auth/token.py", "count": 5, "work_items": ["PROJ-1"]}]
    with patch("icx_engine.mcp_server._get_hotspots_sync", return_value=fake):
        result = await _call_tool("memory_get_hotspots", {"top_n": 5})
    data = json.loads(result[0].text)
    assert data["count"] == 1
    assert data["results"][0]["file"] == "src/auth/token.py"


# -- memory_find_by_file -------------------------------------------------------

async def test_memory_find_by_file_missing_file_path_returns_error():
    from icx_engine.mcp_server import _call_tool
    result = await _call_tool("memory_find_by_file", {})
    data = json.loads(result[0].text)
    assert "error" in data


async def test_memory_find_by_file_returns_empty_structure():
    from icx_engine.mcp_server import _call_tool
    with patch("icx_engine.mcp_server._find_by_file_sync", return_value=[]):
        result = await _call_tool("memory_find_by_file", {"file_path": "src/auth/token.py"})
    data = json.loads(result[0].text)
    assert "results" in data
    assert "count" in data
    assert data["count"] == 0


# -- memory_get_patterns -------------------------------------------------------

async def test_memory_get_patterns_returns_empty_structure():
    from icx_engine.mcp_server import _call_tool
    with patch("icx_engine.mcp_server._get_patterns_sync", return_value=[]):
        result = await _call_tool("memory_get_patterns", {})
    data = json.loads(result[0].text)
    assert "results" in data
    assert "count" in data
    assert data["count"] == 0


async def test_memory_get_patterns_returns_items_from_manager():
    from icx_engine.mcp_server import _call_tool
    fake = [{"project_key": "PROJ", "pattern_type": "dominant_tag", "label": "auth", "entry_count": 8}]
    with patch("icx_engine.mcp_server._get_patterns_sync", return_value=fake):
        result = await _call_tool("memory_get_patterns", {"project_key": "PROJ"})
    data = json.loads(result[0].text)
    assert data["count"] == 1
    assert data["results"][0]["pattern_type"] == "dominant_tag"


# -- reinforce_memory_usage ----------------------------------------------------

async def test_reinforce_memory_usage_memory_not_ready_returns_error():
    from icx_engine.mcp_server import _call_tool
    with patch("icx_engine.mcp_server._get_memory_state", return_value="cold"):
        result = await _call_tool("reinforce_memory_usage", {
            "source_key": "PROJ-88",
            "new_ticket_key": "PROJ-142",
        })
    data = json.loads(result[0].text)
    assert "error" in data


async def test_reinforce_memory_usage_invalid_key_format_returns_error():
    from icx_engine.mcp_server import _call_tool
    result = await _call_tool("reinforce_memory_usage", {
        "source_key": "not-a-valid-key",
        "new_ticket_key": "PROJ-142",
    })
    data = json.loads(result[0].text)
    assert "error" in data


# -- get_memory_audit ----------------------------------------------------------

async def test_get_memory_audit_memory_not_ready_returns_error():
    from icx_engine.mcp_server import _call_tool
    with patch("icx_engine.mcp_server._get_memory_state", return_value="cold"):
        result = await _call_tool("get_memory_audit", {"issue_key": "PROJ-88"})
    data = json.loads(result[0].text)
    assert "error" in data


async def test_get_memory_audit_invalid_key_format_returns_error():
    from icx_engine.mcp_server import _call_tool
    result = await _call_tool("get_memory_audit", {"issue_key": "not-a-key"})
    data = json.loads(result[0].text)
    assert "error" in data


# -- Magik-AI testing tools ----------------------------------------------------

async def test_magik_tools_registered():
    from icx_engine.mcp_server import _list_tools
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        tools = await _list_tools()
    names = {t.name for t in tools}
    expected = {
        "magik_health_check",
        "start_testing_session",
        "resume_testing_session",
        "magik_test_status",
        "magik_test_results",
    }
    assert expected.issubset(names)


async def test_magik_health_tool_schema_has_no_required_fields():
    from icx_engine.mcp_server import _list_tools
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        tools = await _list_tools()
    tool = next(t for t in tools if t.name == "magik_health_check")
    assert tool.inputSchema.get("required", []) == []


async def test_magik_start_tool_requires_file_paths():
    from icx_engine.mcp_server import _list_tools
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        tools = await _list_tools()
    tool = next(t for t in tools if t.name == "start_testing_session")
    assert "file_paths" in tool.inputSchema["required"]
    assert "file_paths" in tool.inputSchema["properties"]
    assert tool.inputSchema["properties"]["file_paths"]["type"] == "array"


async def test_magik_resume_tool_requires_session_id_and_response():
    from icx_engine.mcp_server import _list_tools
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        tools = await _list_tools()
    tool = next(t for t in tools if t.name == "resume_testing_session")
    assert "session_id" in tool.inputSchema["required"]
    assert "response" in tool.inputSchema["required"]


async def test_magik_status_tool_requires_run_id():
    from icx_engine.mcp_server import _list_tools
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        tools = await _list_tools()
    tool = next(t for t in tools if t.name == "magik_test_status")
    assert "run_id" in tool.inputSchema["required"]


async def test_magik_results_tool_requires_run_id():
    from icx_engine.mcp_server import _list_tools
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        tools = await _list_tools()
    tool = next(t for t in tools if t.name == "magik_test_results")
    assert "run_id" in tool.inputSchema["required"]


async def test_magik_health_check_returns_ok_on_success():
    from icx_engine.mcp_server import _call_tool
    from unittest.mock import AsyncMock
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        with patch("icx_engine.testing.client.MagikClient.health_check", new=AsyncMock(return_value={"status": "ok"})):
            with patch("icx_engine.testing.client.MagikClient.aclose", new=AsyncMock()):
                result = await _call_tool("magik_health_check", {})
    data = json.loads(result[0].text)
    assert data["ok"] is True
    assert "data" in data


async def test_magik_health_check_returns_not_ok_when_unreachable():
    from icx_engine.mcp_server import _call_tool
    from icx_engine.testing.client import MagikUnreachable
    from unittest.mock import AsyncMock
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        with patch("icx_engine.testing.client.MagikClient.health_check", new=AsyncMock(side_effect=MagikUnreachable("unreachable"))):
            with patch("icx_engine.testing.client.MagikClient.aclose", new=AsyncMock()):
                result = await _call_tool("magik_health_check", {})
    data = json.loads(result[0].text)
    assert data["ok"] is False
    assert "error" in data


async def test_magik_status_returns_ok_on_success():
    from icx_engine.mcp_server import _call_tool
    from unittest.mock import AsyncMock
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        with patch("icx_engine.testing.client.MagikClient.get_run_status", new=AsyncMock(return_value={"state": "running"})):
            with patch("icx_engine.testing.client.MagikClient.aclose", new=AsyncMock()):
                result = await _call_tool("magik_test_status", {"run_id": "ui-123"})
    data = json.loads(result[0].text)
    assert data["ok"] is True
    assert data["data"]["state"] == "running"


async def test_magik_status_returns_not_ok_when_run_lost():
    from icx_engine.mcp_server import _call_tool
    from icx_engine.testing.client import MagikRunLost
    from unittest.mock import AsyncMock
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        with patch("icx_engine.testing.client.MagikClient.get_run_status", new=AsyncMock(side_effect=MagikRunLost("not found"))):
            with patch("icx_engine.testing.client.MagikClient.aclose", new=AsyncMock()):
                result = await _call_tool("magik_test_status", {"run_id": "ui-999"})
    data = json.loads(result[0].text)
    assert data["ok"] is False
    assert "not found" in data["error"]


async def test_magik_results_returns_not_ok_when_report_not_ready():
    from icx_engine.mcp_server import _call_tool
    from icx_engine.testing.client import MagikReportNotReady
    from unittest.mock import AsyncMock
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        with patch("icx_engine.testing.client.MagikClient.get_run_report", new=AsyncMock(side_effect=MagikReportNotReady("not ready"))):
            with patch("icx_engine.testing.client.MagikClient.aclose", new=AsyncMock()):
                result = await _call_tool("magik_test_results", {"run_id": "ui-123"})
    data = json.loads(result[0].text)
    assert data["ok"] is False
    assert "not complete yet" in data["error"]


async def test_auth_tools_registered():
    from icx_engine.mcp_server import _list_tools
    from icx_engine.models.config import AppConfig
    from unittest.mock import patch
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        tools = await _list_tools()
    names = {t.name for t in tools}
    for n in ("magik_login_start", "magik_login_capture", "magik_login_cancel",
              "magik_login_inline", "magik_logout"):
        assert n in names


async def test_login_inline_validates():
    import json
    from icx_engine.mcp_server import _call_tool
    result = await _call_tool("magik_login_inline", {"loginUrl": "http://x"})
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert "username" in payload["error"] or "password" in payload["error"]


async def test_start_session_validates_bad_input():
    import json
    from icx_engine.mcp_server import _call_tool
    result = await _call_tool("start_testing_session", {"file_paths": [], "test_mode": "automated"})
    payload = json.loads(result[0].text)
    assert payload.get("ok") is False
    assert "file_paths" in payload.get("error", "")


async def test_resume_description_lists_gate_shapes():
    from icx_engine.mcp_server import _list_tools
    from icx_engine.models.config import AppConfig
    from unittest.mock import patch
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        tools = {t.name: t for t in await _list_tools()}
    desc = tools["resume_testing_session"].description
    for token in ("pick_type", "compat_check", "auth_gate", "profile_push", "approve_iteration", "RULE"):
        assert token in desc


async def test_resume_description_lists_agent_gates():
    from icx_engine.mcp_server import _list_tools
    from icx_engine.models.config import AppConfig
    from unittest.mock import patch
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        tools = {t.name: t for t in await _list_tools()}
    desc = tools["resume_testing_session"].description
    for token in ("compat_scan", "profile_gen", "profile_markdown", "all_compatible"):
        assert token in desc


async def test_compat_scan_description_is_open_ended_mandate():
    """compat_scan must be an open-ended agent mandate - no hardcoded blocker classes,
    it bans deferring to the runner, and it makes the agent report every finding to the
    user rather than deciding. ICX must not claim to verify the answer."""
    from icx_engine.mcp_server import _list_tools
    from icx_engine.models.config import AppConfig
    from unittest.mock import patch
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        tools = {t.name: t for t in await _list_tools()}
    desc = tools["resume_testing_session"].description
    # forbidden-deferral rule (the exact rationalization that caused the miss)
    assert "work around it" in desc and "less robust but fine" in desc
    assert "tolerance is never your excuse" in desc
    # completeness is the agent's job; no fixed checklist
    assert "first principles" in desc and "There is NO" in desc
    # report-don't-decide; the user decides
    assert "REPORT, DO NOT DECIDE" in desc
    # no leftover hardcoded blocker taxonomy
    for stale in ("B1 REACHABILITY", "B2 INPUT", "B3 LOCATABILITY", "suspected_blockers", "addressed_suspected"):
        assert stale not in desc
    # compat_check lets the user accept-as-is, not only drop/manual
    assert '"drop"|"manual"|"accept"' in desc


async def test_resume_description_has_rulebook_rule():
    """The agent must be told gate.rules is the binding rulebook loaded from
    ~/.icx/testing_rules, and gate 2b must document ICX section-presence enforcement."""
    from icx_engine.mcp_server import _list_tools
    from icx_engine.models.config import AppConfig
    from unittest.mock import patch
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        tools = {t.name: t for t in await _list_tools()}
    desc = tools["resume_testing_session"].description
    assert "RULEBOOK RULE" in desc
    assert "testing_rules" in desc
    assert "gate.rules" in desc
    # gate 2b advertises the presence-enforcement + accept_incomplete escape hatch
    assert "2b-8" in desc and "accept_incomplete" in desc


async def test_resume_description_has_expand_scan():
    from icx_engine.mcp_server import _list_tools
    from icx_engine.models.config import AppConfig
    from unittest.mock import patch
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        tools = {t.name: t for t in await _list_tools()}
    desc = tools["resume_testing_session"].description
    assert "expand_scan" in desc and "related_files" in desc


async def test_resume_description_has_reread_rule():
    from icx_engine.mcp_server import _list_tools
    from icx_engine.models.config import AppConfig
    from unittest.mock import patch
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        tools = {t.name: t for t in await _list_tools()}
    desc = tools["resume_testing_session"].description.lower()
    assert "re-read" in desc or "read again" in desc
    assert "read_receipts" in desc
    assert "stale" in desc


async def test_resume_strips_session_id_from_payload(monkeypatch, tmp_path):
    import json
    from icx_engine import mcp_server
    from icx_engine.testing import auth as _auth

    store = tmp_path / "auth.json"
    monkeypatch.setattr(_auth, "store_path", lambda: store)

    captured = {}

    class _Snap:
        values = {"project": "proj-z", "url": "http://host-z/app", "file_paths": ["a.tsx"]}
        tasks = []
        next = ()

    class _FakeGraph:
        async def aget_state(self, config): return _Snap()
        async def ainvoke(self, command, config=None):
            captured["resume"] = getattr(command, "resume", None)

    async def _fake_get_graph():
        return _FakeGraph()

    # handler does `from icx_engine.testing.graph import get_testing_graph` inside the function body
    import icx_engine.testing.graph as _g
    monkeypatch.setattr(_g, "get_testing_graph", _fake_get_graph)

    await mcp_server._call_tool("resume_testing_session", {
        "session_id": "sess-uuid",
        "response": {"auth_mode": "capture", "session_id": "SECRET-123"},
    })
    # session_id stripped from the payload that reaches the graph (and thus the checkpoint writes)
    assert "session_id" not in (captured["resume"] or {})
    assert captured["resume"].get("auth_mode") == "capture"
    # and it was saved to the out-of-band auth store
    assert _auth.load_session("proj-z", "host-z", store=store).session_id == "SECRET-123"


# -- Sonar MCP tools -----------------------------------------------------------

async def test_sonar_tools_registered():
    from icx_engine.mcp_server import _list_tools
    from icx_engine.models.config import AppConfig
    from unittest.mock import patch
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        names = {t.name for t in await _list_tools()}
    for n in ("sonar_status", "sonar_projects", "sonar_branches",
              "sonar_measures", "sonar_quality_gate", "sonar_findings", "sonar_report"):
        assert n in names


async def test_sonar_findings_requires_project(monkeypatch):
    import json
    from icx_engine import mcp_server
    out = await mcp_server._call_tool("sonar_findings", {})
    payload = json.loads(out[0].text)
    assert payload["ok"] is False
    assert "project" in payload["error"].lower()


async def test_sonar_report_tool_disabled(monkeypatch):
    import json
    from icx_engine import mcp_server
    from icx_engine.sonar import service
    from icx_engine.models.config import AppConfig
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: AppConfig()))
    out = await mcp_server._call_tool("sonar_report", {"project": "my-project"})
    payload = json.loads(out[0].text)
    assert payload["ok"] is False
    assert "sonar add" in payload["error"].lower()

