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

def test_list_hosts_returns_six_agents(monkeypatch, tmp_path):
    monkeypatch.setattr("icx_engine.mcp_hosts._home", lambda: tmp_path)
    names = [h.name for h in list_hosts()]
    assert set(names) == {"claude", "cursor", "windsurf", "codex", "antigravity", "vscode"}


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
    assert get_host("jetbrains") is None


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

    fast_tool = next(t for t in tools if t.name == "jira_analyze_issue_fast")
    schema = fast_tool.inputSchema
    assert "profile" in schema["properties"]
    assert "profile" not in schema.get("required", [])


async def test_list_tools_returns_core_tools():
    from icx_engine.mcp_server import _all_tools_full
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        tools = await _all_tools_full()
    names = {t.name for t in tools}
    assert {"jira_analyze_issue_fast", "jira_analyze_issue", "save_memory"}.issubset(names)


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
    """jira_analyze_issue_fast must use a 45s timeout."""
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
    """jira_analyze_issue must use a 660s timeout."""
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
    from icx_engine.mcp_server import _all_tools_full
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        tools = await _all_tools_full()

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
    from icx_engine.mcp_server import _all_tools_full
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        tools = await _all_tools_full()

    assert len(tools) == 167
    names = {t.name for t in tools}
    assert names == {
        "jira_analyze_issue_fast", "jira_analyze_issue", "save_memory", "icx_record_verification",
        "icx_get_methodology", "icx_lock_plan", "icx_boost", "icx_boost_refine",
        "icx_find_tools", "icx_call_tool",
        "icx_ui_auth_capture", "icx_ui_auth_inline",
        "graph_find_context", "graph_call_chain", "graph_impact", "graph_subsystem",
        "graph_cross_links", "graph_important_nodes", "graph_blast_radius",
        "graph_cycles", "graph_dead_code", "graph_ownership",
        "memory_find_by_file", "memory_get_hotspots", "memory_get_related",
        "memory_get_patterns", "memory_search", "memory_delete", "memory_update",
        "reinforce_memory_usage", "get_memory_audit",
        "testing_start_session", "testing_resume_session", "testing_get_session_status",
        "sonar_status", "sonar_projects", "sonar_branches",
        "sonar_measures", "sonar_quality_gate", "sonar_findings", "sonar_report",
        "sonar_top_files", "sonar_history", "sonar_analyses", "sonar_rule", "sonar_rules", "sonar_hotspot",
        "sonar_source", "sonar_metrics", "sonar_quality_gate_definition", "sonar_quality_profiles",
        "sonar_issue_authors", "sonar_issue_tags", "sonar_issue_changelog", "sonar_system_health", "sonar_languages",
        "icx_skill_get", "icx_skills_index", "icx_draft_skill", "icx_create_skill",
        "git_repo_status", "git_start_branch", "git_checkout_branch", "git_blame", "git_log", "git_show_commit", "git_diff",
        "git_diff_worktree", "git_read_file_at_ref",
        "git_stage_and_commit", "git_push",
        "git_reverse_merge", "git_get_conflict", "git_complete_resolution",
        "git_adopt_resolution", "git_discard_scratch",
        "git_create_mr", "git_finish_ticket", "git_create_tag", "git_delete_tag", "git_retag",
        "git_stash_create", "git_stash_list", "git_stash_apply", "git_stash_pop", "git_stash_drop",
        "git_fetch", "git_pull", "git_sync", "git_delete_branch",
        "git_get_conflict_details", "git_conflict_take_ours", "git_conflict_take_theirs",
        "git_conflict_apply_resolution", "git_conflict_mark_resolved", "git_conflict_abort",
        "git_check_branch_name_policy", "git_set_branch_policy",
        "git_check_dependency_pins", "git_restore_files", "git_list_merged_branches",
        "git_check_merge", "git_repin_dependency",
        "gitlab_list_merge_requests", "gitlab_mr_changes", "gitlab_list_commits", "gitlab_compare",
        "gitlab_list_tags", "gitlab_list_branches", "gitlab_list_pipelines",
        "gitlab_pipeline_status", "gitlab_job_log",
        "gitlab_close_merge_request", "gitlab_reopen_merge_request",
        "gitlab_merge_merge_request", "gitlab_refresh_merge_status",
        "workstatus_unread_notifications", "workstatus_my_profile", "workstatus_add_timesheet",
        "workstatus_list_projects", "workstatus_get_project", "workstatus_project_budget_analytics",
        "workstatus_list_tasks", "workstatus_list_task_statuses", "workstatus_list_milestones",
        "workstatus_list_task_checklist", "workstatus_list_members", "workstatus_list_teams",
        "workstatus_attendance_list", "workstatus_attendance_stats",
        "workstatus_list_timesheets", "workstatus_list_timesheet_clients",
        "workstatus_weekly_report", "workstatus_timesheet_submission_kpis",
        "workstatus_timesheet_submission_table", "workstatus_list_expenses",
        "workstatus_list_invoices", "workstatus_payroll_report",
        "workstatus_get_timesheet", "workstatus_edit_timesheet",
        "workstatus_recent_project_tasks",
        "jira_get_close_requirements", "jira_apply_update",
        "jira_list_issue_types", "jira_get_createmeta_fields",
        "jira_create_issue", "jira_delete_issue",
        "jira_comment_list", "jira_comment_add", "jira_comment_edit", "jira_comment_delete",
        "jira_search", "jira_get_issue",
        "jira_link_types", "jira_link_create", "jira_link_delete", "jira_set_assignee",
        "jira_search_assignable_users",
        "jira_attachment_upload", "jira_attachment_delete",
        "jira_get_current_user", "jira_list_watchers", "jira_list_worklogs",
        "jira_set_watcher", "jira_worklog_add", "jira_worklog_edit", "jira_worklog_delete",
    }


async def test_git_tool_descriptions_follow_sonar_use_when_must_convention():
    """Every git_* tool description must follow the same USE WHEN...MUST... convention
    already established by the sonar_* tool descriptions in mcp_server.py."""
    from icx_engine.mcp_server import _all_tools_full
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        tools = await _all_tools_full()

    git_tools = [t for t in tools if t.name.startswith("git_")]
    assert len(git_tools) == 43
    for tool in git_tools:
        assert "USE WHEN" in tool.description, f"{tool.name} missing 'USE WHEN'"
        assert "MUST" in tool.description, f"{tool.name} missing 'MUST'"


async def test_git_repo_status_description_mandates_icx_over_raw_git():
    """Regression: git_repo_status must declare ICX the sole git-workflow interface, the same
    way jira_analyze_issue_fast/jira_analyze_issue declare ICX the sole tracker interface - otherwise
    nothing stops the agent falling back to raw git commands."""
    from icx_engine.mcp_server import _list_tools
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        tools = await _list_tools()
    status_tool = next(t for t in tools if t.name == "git_repo_status")
    desc = status_tool.description
    assert "SOLE" in desc and "GIT-WORKFLOW INTERFACE" in desc
    assert "NEVER run" in desc


async def test_graph_cross_links_schema_has_file_path():
    from icx_engine.mcp_server import _all_tools_full
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        tools = await _all_tools_full()

    tool = next(t for t in tools if t.name == "graph_cross_links")
    assert "file_path" in tool.inputSchema["properties"]


async def test_analyze_tool_schema_issue_ref_required_project_paths_optional():
    from icx_engine.mcp_server import _list_tools
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        tools = await _list_tools()

    fast_tool = next(t for t in tools if t.name == "jira_analyze_issue_fast")
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

    for tool_name in ("jira_analyze_issue_fast", "jira_analyze_issue"):
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


async def test_handle_analyze_issue_instruction_suggests_git_workflow():
    """_icx_next.instruction nudges the agent toward git_repo_status + a feature branch."""
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
    instruction = data["_icx_next"]["instruction"]
    assert "git_repo_status" in instruction
    assert "feature branch" in instruction


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
    result = _runner.invoke(app, ["mcp", "setup", "--host", "jetbrains"])
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
        assert "jira_analyze_issue" in instruction, f"Escalation call missing for graph_status={graph_status!r}"
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
    from icx_engine.mcp_server import _all_tools_full
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        tools = await _all_tools_full()
    names = {t.name for t in tools}
    assert "memory_search" in names


async def test_memory_search_tool_has_required_inputs():
    from icx_engine.mcp_server import _all_tools_full
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        tools = await _all_tools_full()
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
    with patch("icx_engine.memory.mcp_tools._get_memory_state", return_value="cold"):
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
        with patch("icx_engine.memory.mcp_tools._get_related_sync", return_value=fake_results):
            result = await _call_tool("memory_get_related", {"files": ["auth/token.py"]})
    data = json.loads(result[0].text)
    assert data["count"] == 1
    assert data["results"][0]["issue_key"] == "PROJ-99"


async def test_call_tool_memory_get_related_issue_key_edge_path():
    from icx_engine.mcp_server import _call_tool
    fake_results = [{"issue_key": "PROJ-50", "relation_type": "shares_file", "strength": 0.9}]
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        with patch("icx_engine.memory.mcp_tools._get_related_sync", return_value=fake_results):
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
    """jira_analyze_issue with no project_paths omitted proceeds (no MISSING_PROJECT_PATH error)."""
    from icx_engine.mcp_server import _call_tool
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        with patch("icx_engine.mcp_server._resolve_paths_from_ticket", return_value=None):
            result = await _call_tool("jira_analyze_issue", {"issue_ref": "TEST-123"})
    data = json.loads(result[0].text)
    assert data.get("code") != "MISSING_PROJECT_PATH"


async def test_call_tool_analyze_empty_project_paths_proceeds_to_engine():
    """jira_analyze_issue with empty project_paths list proceeds (no MISSING_PROJECT_PATH error)."""
    from icx_engine.mcp_server import _call_tool
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        with patch("icx_engine.mcp_server._resolve_paths_from_ticket", return_value=None):
            result = await _call_tool("jira_analyze_issue", {"issue_ref": "TEST-123", "project_paths": []})
    data = json.loads(result[0].text)
    assert data.get("code") != "MISSING_PROJECT_PATH"


async def test_call_tool_analyze_issue_fast_attaches_ticket_skill_hint():
    from icx_engine.mcp_server import _call_tool
    from icx_engine.skills.schema import SkillEntry
    from unittest.mock import AsyncMock, patch

    class _FakeStorage:
        def read(self, name):
            assert name == "ticket-context-analysis"
            return SkillEntry(name=name, description="d", title="t", when_to_use="w",
                               procedure="p", pitfalls="x", verification="v")

    with patch("icx_engine.mcp_server._handle_analyze_issue",
               new=AsyncMock(return_value=json.dumps({"status": "ok"}))):
        with patch("icx_engine.skills.hints.SkillStorage", _FakeStorage):
            result = await _call_tool(
                "jira_analyze_issue_fast", {"issue_ref": "TEST-123", "project_paths": ["/projects/my-svc"]},
            )
    data = json.loads(result[0].text)
    assert data["skills"]["index"][0]["name"] == "ticket-context-analysis"


async def test_call_tool_analyze_issue_fast_appends_ranked_custom_skill(tmp_path):
    from icx_engine.mcp_server import _call_tool
    from icx_engine.skills.schema import SkillEntry
    from icx_engine.skills.storage import SkillStorage
    from unittest.mock import AsyncMock, patch

    storage = SkillStorage(root=tmp_path)
    default_entry = SkillEntry(name="ticket-context-analysis", description="d", title="t",
                                when_to_use="w", procedure="p", pitfalls="x", verification="v")
    storage.write(default_entry)
    custom_entry = SkillEntry(name="payment-gateway-skill", description="handles payment gateway retries",
                               tags=["paymentgateway"], title="Payment Gateway Skill",
                               when_to_use="w", procedure="p", pitfalls="x", verification="v")
    storage.write(custom_entry)

    fake_response = json.dumps({
        "work_item": {"type": "Bug", "summary": "paymentgateway timeout on checkout"},
    })
    with patch("icx_engine.mcp_server._handle_analyze_issue", new=AsyncMock(return_value=fake_response)):
        with patch("icx_engine.skills.hints.SkillStorage", lambda: storage):
            result = await _call_tool(
                "jira_analyze_issue_fast", {"issue_ref": "TEST-123", "project_paths": ["/projects/my-svc"]},
            )
    data = json.loads(result[0].text)
    names = [e["name"] for e in data["skills"]["index"]]
    assert names[0] == "ticket-context-analysis"
    assert "payment-gateway-skill" in names


async def test_call_tool_analyze_whitespace_only_paths_proceeds_to_engine():
    """jira_analyze_issue with whitespace-only paths proceeds after stripping (no MISSING_PROJECT_PATH)."""
    from icx_engine.mcp_server import _call_tool
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        with patch("icx_engine.mcp_server._resolve_paths_from_ticket", return_value=None):
            result = await _call_tool("jira_analyze_issue", {"issue_ref": "TEST-123", "project_paths": ["   "]})
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


def test_tool_descriptions_cover_full_tracker_crud_not_just_fetch():
    """Regression: create/search/lookup have no ticket key, so RULE 0 must not scope the
    sole-tracker-interface ban to fetching alone - it must cover every tracker action."""
    from icx_engine.mcp_server import _FAST_DESCRIPTION, _FULL_DESCRIPTION
    for desc in (_FAST_DESCRIPTION, _FULL_DESCRIPTION):
        assert "creating" in desc and "searching" in desc
        assert "no ticket key yet" in desc


def test_resume_desc_has_gate_posture_classification():
    """resume description must carry the gate-posture classification with agent-generate
    gates enumerated (2b, compat_scan, author_flow) and user-decision gates listed."""
    from icx_engine.testing.mcp_tools import _TESTING_RESUME_DESCRIPTION as d
    assert "GATE POSTURE CLASSIFICATION" in d
    assert "USER-DECISION" in d
    assert "AGENT-GENERATE" in d
    # 2b, compat_scan, and author_flow are all agent-generate gates.
    assert "2b" in d and "compat_scan" in d and "author_flow" in d
    # user-decision gate list includes the key human gates.
    for g in ("ui_check", "memory_save", "manual", "error", "limit"):
        assert g in d


def test_resume_desc_no_unscoped_auto_respond_phrase():
    """The contradiction is removed: no rule may say to never auto-respond to ANY gate (which
    fought the agent-generate 2b gate). It must be scoped to USER-DECISION gates."""
    from icx_engine.testing.mcp_tools import _TESTING_RESUME_DESCRIPTION as d
    assert "AUTO-RESPOND TO ANY GATE" not in d
    assert "AUTO-RESPOND TO A USER-DECISION GATE" in d


def test_default_posture_line_in_both_testing_descs():
    """Both start and resume descriptions state the default posture: gate data is the user's to
    decide; the agent only self-advances to generate the spec at Gate 2b."""
    from icx_engine.testing.mcp_tools import _TESTING_START_DESCRIPTION, _TESTING_RESUME_DESCRIPTION
    for d in (_TESTING_START_DESCRIPTION, _TESTING_RESUME_DESCRIPTION):
        assert "DEFAULT POSTURE" in d
        assert "for the USER to read and decide" in d
        assert "Gate 2b" in d


def test_start_desc_seed_selection_endpoint_and_backend_bridge():
    """start description must describe Phase A seed selection: the endpoint/route grep option
    and the backend-only API-path -> UI-repo grep bridge."""
    from icx_engine.testing.mcp_tools import _TESTING_START_DESCRIPTION as d
    assert "PHASE A" in d
    assert "endpoint/route" in d
    assert "BACKEND-ONLY" in d
    # backend bridge: grep the backend api path, then grep the UI repo for it.
    assert "grep the UI repo" in d


def test_gate1_desc_has_grep_import_fallback_when_no_graph():
    """Gate 1 must instruct the agent to grep-expand imports as the fallback when the UI repo
    graph is not built, and still end in a user confirm."""
    from icx_engine.testing.mcp_tools import _TESTING_RESUME_DESCRIPTION as d
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


async def test_graph_find_context_truncates_to_token_budget(monkeypatch, tmp_path):
    """Real bug fixed: find_context's own token_budget param is a documented no-op -
    it returned every scored file unconditionally, producing 700K+ char single-call
    responses. This proves the MCP-layer truncation actually caps output size."""
    from icx_engine import mcp_server
    from icx_engine.graph import mcp_tools as graph_mcp_tools
    from icx_engine.graph.query import ContextResult

    fake_graph_json = tmp_path / "graph.json"
    fake_graph_json.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        graph_mcp_tools, "_resolve_graph_path",
        lambda raw_path: ("/proj", "proj-id", None),
    )
    monkeypatch.setattr("icx_engine.graph.storage.graph_path", lambda project_id: fake_graph_json)

    class _FakeQuerier:
        def find_context(self, **kwargs):
            # Each result's `reason` is padded to make its serialized size predictable
            # and large enough that only a handful fit in a small token_budget.
            return [
                ContextResult(
                    file=f"src/file_{i}.py", node_id=f"node_{i}", score=1.0 - i * 0.001,
                    role_tag="service", degree=1, reason="x" * 500,
                )
                for i in range(200)
            ]

    monkeypatch.setattr(graph_mcp_tools, "_cached_querier", lambda graph_json: _FakeQuerier())

    result = await mcp_server._call_tool("graph_find_context", {
        "project_path": "/proj", "task": "auth token expiry", "token_budget": 500,
    })
    data = json.loads(result[0].text)
    assert data["status"] == "ok"
    assert data["total_matched"] == 200
    assert len(data["results"]) < 200
    assert data["truncated"] is True
    assert "token_budget" in data["note"]


async def test_graph_find_context_no_truncation_note_when_everything_fits(monkeypatch, tmp_path):
    from icx_engine import mcp_server
    from icx_engine.graph import mcp_tools as graph_mcp_tools
    from icx_engine.graph.query import ContextResult

    fake_graph_json = tmp_path / "graph.json"
    fake_graph_json.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(graph_mcp_tools, "_resolve_graph_path", lambda raw_path: ("/proj", "proj-id", None))
    monkeypatch.setattr("icx_engine.graph.storage.graph_path", lambda project_id: fake_graph_json)

    class _FakeQuerier:
        def find_context(self, **kwargs):
            return [ContextResult(file="src/a.py", node_id="n1", score=1.0, role_tag="service", degree=1, reason="short")]

    monkeypatch.setattr(graph_mcp_tools, "_cached_querier", lambda graph_json: _FakeQuerier())

    result = await mcp_server._call_tool("graph_find_context", {
        "project_path": "/proj", "task": "auth token expiry", "token_budget": 8000,
    })
    data = json.loads(result[0].text)
    assert data["status"] == "ok"
    assert len(data["results"]) == 1
    assert data["total_matched"] == 1
    assert "truncated" not in data


def test_degraded_graph_response_shape():
    """No-graph/stale is non-blocking: warn the user + fall back to native tools,
    never stop-and-wait."""
    from icx_engine.mcp_server import _degraded_graph_response
    r = _degraded_graph_response(
        code="NO_GRAPH", project_path="/proj",
        warn_user="Graph not built - build it for richer results.",
    )
    assert r["status"] == "degraded"
    assert r["code"] == "NO_GRAPH"
    assert r["action_required"] == "tell_user_then_use_native_tools"
    assert "warn_user" in r and r["warn_user"]
    assert "grep" in r["instruction"].lower()
    assert r["build_command"].startswith("icx graph build")
    # must NOT be a hard stop
    assert "stop" not in r["action_required"]


def test_degraded_graph_response_stale_includes_counts():
    from icx_engine.mcp_server import _degraded_graph_response
    r = _degraded_graph_response(
        code="GRAPH_STALE", project_path="/proj",
        warn_user="Graph 5% stale.",
        extra={"changed_files": 12, "total_files": 240, "changed_pct": 5.0},
    )
    assert r["status"] == "degraded"
    assert r["changed_files"] == 12 and r["changed_pct"] == 5.0
    assert r["action_required"] == "tell_user_then_use_native_tools"


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


# -- testing_start_session injects configured test_max_iterations ----------

async def test_start_session_injects_configured_max_iterations(monkeypatch):
    from icx_engine import mcp_server
    from icx_engine.models.config import AppConfig
    from unittest.mock import patch

    captured = {}

    class _Snap:
        tasks = []
        next = ()
        values = {}

    class _FakeGraph:
        async def ainvoke(self, state, config=None):
            if isinstance(state, dict):
                captured["max_iterations"] = state.get("max_iterations")

        async def aget_state(self, config):
            return _Snap()

    async def _fake_get_graph():
        return _FakeGraph()

    import icx_engine.testing.graph as _g
    monkeypatch.setattr(_g, "get_testing_graph", _fake_get_graph)

    cfg = AppConfig()
    cfg.test_max_iterations = 7
    with patch("icx_engine.config_manager.ConfigManager") as mock_cm:
        mock_cm.load.return_value = cfg
        await mcp_server._call_tool(
            "testing_start_session",
            {"file_paths": ["a.tsx"], "test_mode": "automated"},
        )
    assert captured["max_iterations"] == 7


async def test_start_session_injects_session_id_into_state(monkeypatch):
    # session_id was previously only used as the LangGraph thread_id, never placed in the state
    # itself - the Phase 2 browser-daemon registry needs it as a stable, collision-safe key.
    from icx_engine import mcp_server

    captured = {}

    class _Snap:
        tasks = []
        next = ()
        values = {}

    class _FakeGraph:
        async def ainvoke(self, state, config=None):
            if isinstance(state, dict):
                captured["session_id_in_state"] = state.get("session_id")

        async def aget_state(self, config):
            return _Snap()

    async def _fake_get_graph():
        return _FakeGraph()

    import icx_engine.testing.graph as _g
    monkeypatch.setattr(_g, "get_testing_graph", _fake_get_graph)

    result = await mcp_server._call_tool(
        "testing_start_session", {"file_paths": ["a.tsx"], "test_mode": "automated"},
    )
    data = json.loads(result[0].text)
    assert captured["session_id_in_state"] == data["session_id"]
    assert captured["session_id_in_state"]   # non-empty


async def test_start_session_attaches_testing_session_driver_skill_hint(monkeypatch):
    from icx_engine import mcp_server
    from icx_engine.skills.schema import SkillEntry
    from unittest.mock import patch

    class _Snap:
        tasks = []
        next = ()
        values = {}

    class _FakeGraph:
        async def ainvoke(self, state, config=None):
            return None

        async def aget_state(self, config):
            return _Snap()

    async def _fake_get_graph():
        return _FakeGraph()

    import icx_engine.testing.graph as _g
    monkeypatch.setattr(_g, "get_testing_graph", _fake_get_graph)

    class _FakeStorage:
        def read(self, name):
            assert name == "testing-session-driver"
            return SkillEntry(name=name, description="d", title="t", when_to_use="w",
                               procedure="p", pitfalls="x", verification="v")

    with patch("icx_engine.skills.hints.SkillStorage", _FakeStorage):
        result = await mcp_server._call_tool(
            "testing_start_session", {"file_paths": ["a.tsx"], "test_mode": "automated"},
        )
    data = json.loads(result[0].text)
    assert data["skills"]["index"][0]["name"] == "testing-session-driver"


async def test_start_session_appends_ranked_custom_skill_from_context(tmp_path, monkeypatch):
    from icx_engine import mcp_server
    from icx_engine.skills.schema import SkillEntry
    from icx_engine.skills.storage import SkillStorage
    from unittest.mock import patch

    class _Snap:
        tasks = []
        next = ()
        values = {}

    class _FakeGraph:
        async def ainvoke(self, state, config=None):
            return None

        async def aget_state(self, config):
            return _Snap()

    async def _fake_get_graph():
        return _FakeGraph()

    import icx_engine.testing.graph as _g
    monkeypatch.setattr(_g, "get_testing_graph", _fake_get_graph)

    storage = SkillStorage(root=tmp_path)
    default_entry = SkillEntry(name="testing-session-driver", description="d", title="t",
                                when_to_use="w", procedure="p", pitfalls="x", verification="v")
    storage.write(default_entry)
    custom_entry = SkillEntry(name="checkout-flow-tester", description="tests checkout flow edge cases",
                               tags=["checkoutflow"], title="Checkout Flow Tester",
                               when_to_use="w", procedure="p", pitfalls="x", verification="v")
    storage.write(custom_entry)

    with patch("icx_engine.skills.hints.SkillStorage", lambda: storage):
        result = await mcp_server._call_tool(
            "testing_start_session",
            {"file_paths": ["a.tsx"], "test_mode": "automated", "context": "verify checkoutflow works end to end"},
        )
    data = json.loads(result[0].text)
    names = [e["name"] for e in data["skills"]["index"]]
    assert names[0] == "testing-session-driver"
    assert "checkout-flow-tester" in names


# -- async job/poll pattern (opacity fix - status:"running" + testing_get_session_status) ------

class _RunningSnap:
    tasks = []
    next = ()
    values = {"status": "ok"}


class _SlowFakeGraph:
    """ainvoke sleeps past the (monkeypatched, tiny) quick-timeout so the caller must observe
    status:"running" instead of getting an inline gate."""
    def __init__(self, delay=0.05):
        self.delay = delay

    async def ainvoke(self, *_a, **_kw):
        await asyncio.sleep(self.delay)

    async def aget_state(self, config):
        return _RunningSnap()


async def test_resume_returns_running_when_node_exceeds_quick_timeout(monkeypatch):
    from icx_engine import mcp_server
    import icx_engine.testing.graph as _g

    fake = _SlowFakeGraph(delay=0.05)
    monkeypatch.setattr(_g, "get_testing_graph", lambda: _fut(fake))
    monkeypatch.setattr(mcp_server, "_TESTING_QUICK_TIMEOUT", 0.01)

    result = await mcp_server._call_tool(
        "testing_resume_session", {"session_id": "sess-slow", "response": {"choice": "1"}},
    )
    data = json.loads(result[0].text)
    assert data["status"] == "running"
    assert data["done"] is False
    assert data["gate"] is None

    # let the background task finish so pytest-asyncio doesn't warn about a pending task
    task = mcp_server._TESTING_RUNNING.get("sess-slow")
    if task is not None:
        await task


async def test_resume_rejects_second_call_while_session_running(monkeypatch):
    from icx_engine import mcp_server
    import icx_engine.testing.graph as _g

    fake = _SlowFakeGraph(delay=0.2)
    monkeypatch.setattr(_g, "get_testing_graph", lambda: _fut(fake))
    monkeypatch.setattr(mcp_server, "_TESTING_QUICK_TIMEOUT", 0.01)

    first = await mcp_server._call_tool(
        "testing_resume_session", {"session_id": "sess-busy", "response": {}},
    )
    assert json.loads(first[0].text)["status"] == "running"

    second = await mcp_server._call_tool(
        "testing_resume_session", {"session_id": "sess-busy", "response": {}},
    )
    data = json.loads(second[0].text)
    assert data["status"] == "running"
    assert "error" in data

    task = mcp_server._TESTING_RUNNING.get("sess-busy")
    if task is not None:
        await task


async def test_get_testing_session_status_polls_running_task_to_done(monkeypatch):
    from icx_engine import mcp_server
    import icx_engine.testing.graph as _g

    fake = _SlowFakeGraph(delay=0.05)
    monkeypatch.setattr(_g, "get_testing_graph", lambda: _fut(fake))
    monkeypatch.setattr(mcp_server, "_TESTING_QUICK_TIMEOUT", 0.01)

    await mcp_server._call_tool(
        "testing_resume_session", {"session_id": "sess-poll", "response": {}},
    )
    # first poll: still running
    first = await mcp_server._call_tool("testing_get_session_status", {"session_id": "sess-poll"})
    assert json.loads(first[0].text)["status"] == "running"

    # wait for the background task to actually finish
    task = mcp_server._TESTING_RUNNING.get("sess-poll")
    if task is not None:
        await task

    # second poll: task done, real state read
    second = await mcp_server._call_tool("testing_get_session_status", {"session_id": "sess-poll"})
    data = json.loads(second[0].text)
    assert data["status"] != "running"
    assert data["done"] is True


async def test_get_testing_session_status_unknown_session_falls_back_to_state_read(monkeypatch):
    from icx_engine import mcp_server
    import icx_engine.testing.graph as _g

    class _Snap:
        tasks = []
        next = ()
        values = {"status": "ok"}

    class _FakeGraph:
        async def aget_state(self, config):
            return _Snap()

    monkeypatch.setattr(_g, "get_testing_graph", lambda: _fut(_FakeGraph()))
    result = await mcp_server._call_tool(
        "testing_get_session_status", {"session_id": "never-tracked"},
    )
    data = json.loads(result[0].text)
    assert data["done"] is True
    assert "error" not in data or data["error"] is None


async def test_get_testing_session_status_missing_session_id_returns_error():
    from icx_engine.mcp_server import _call_tool
    result = await _call_tool("testing_get_session_status", {})
    data = json.loads(result[0].text)
    assert "error" in data


def _fut(value):
    async def _coro():
        return value
    return _coro()


# -- memory_get_hotspots -------------------------------------------------------

async def test_memory_get_hotspots_returns_empty_structure():
    from icx_engine.mcp_server import _call_tool
    with patch("icx_engine.memory.mcp_tools._get_hotspots_sync", return_value=[]):
        result = await _call_tool("memory_get_hotspots", {})
    data = json.loads(result[0].text)
    assert "results" in data
    assert "count" in data
    assert data["count"] == 0


async def test_memory_get_hotspots_returns_items_from_manager():
    from icx_engine.mcp_server import _call_tool
    fake = [{"file": "src/auth/token.py", "count": 5, "work_items": ["PROJ-1"]}]
    with patch("icx_engine.memory.mcp_tools._get_hotspots_sync", return_value=fake):
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
    with patch("icx_engine.memory.mcp_tools._find_by_file_sync", return_value=[]):
        result = await _call_tool("memory_find_by_file", {"file_path": "src/auth/token.py"})
    data = json.loads(result[0].text)
    assert "results" in data
    assert "count" in data
    assert data["count"] == 0


def test_find_by_file_sync_excludes_raw_embedding_vector(monkeypatch):
    """Real bug fixed: memory_find_by_file used to inline the raw 384-float
    save_context_vector embedding on every result - no MCP caller can use it,
    pure payload weight."""
    from icx_engine import mcp_server
    from icx_engine.memory.schema import MemoryEntry

    entry = MemoryEntry(
        id="e1", issue_key="ABC-1", project_key="ABC", source_type="jira",
        issue_type="Bug", summary="s", problem_description="p", resolution_note="r",
        files_changed=["src/auth/token.py"], resolution_confirmed=True, saved_at="2026-01-01",
        save_context_vector=[0.1] * 384,
    )
    monkeypatch.setattr(mcp_server, "_ensure_memory_manager", lambda: object())
    monkeypatch.setattr(
        "icx_engine.memory.bridge.find_work_items_by_file",
        lambda file_path, mem, project_key=None: [entry],
    )
    result = mcp_server._find_by_file_sync("src/auth/token.py", None)
    assert len(result) == 1
    assert "save_context_vector" not in result[0]
    assert result[0]["issue_key"] == "ABC-1"


# -- memory_get_patterns -------------------------------------------------------

async def test_memory_get_patterns_returns_empty_structure():
    from icx_engine.mcp_server import _call_tool
    with patch("icx_engine.memory.mcp_tools._get_patterns_sync", return_value=[]):
        result = await _call_tool("memory_get_patterns", {})
    data = json.loads(result[0].text)
    assert "results" in data
    assert "count" in data
    assert data["count"] == 0


async def test_memory_get_patterns_returns_items_from_manager():
    from icx_engine.mcp_server import _call_tool
    fake = [{"project_key": "PROJ", "pattern_type": "dominant_tag", "label": "auth", "entry_count": 8}]
    with patch("icx_engine.memory.mcp_tools._get_patterns_sync", return_value=fake):
        result = await _call_tool("memory_get_patterns", {"project_key": "PROJ"})
    data = json.loads(result[0].text)
    assert data["count"] == 1
    assert data["results"][0]["pattern_type"] == "dominant_tag"


async def test_memory_get_patterns_default_call_unchanged():
    from icx_engine.mcp_server import _call_tool
    fake = [{"pattern_type": f"p{i}"} for i in range(4)]
    with patch("icx_engine.memory.mcp_tools._get_patterns_sync", return_value=fake):
        result = await _call_tool("memory_get_patterns", {})
    data = json.loads(result[0].text)
    assert data["count"] == 4
    assert "total" not in data


async def test_memory_get_patterns_with_limit_pages_correctly():
    from icx_engine.mcp_server import _call_tool
    fake = [{"pattern_type": f"p{i}"} for i in range(4)]
    with patch("icx_engine.memory.mcp_tools._get_patterns_sync", return_value=fake):
        result = await _call_tool("memory_get_patterns", {"limit": 2})
    data = json.loads(result[0].text)
    assert data["count"] == 2
    assert data["total"] == 4
    assert data["has_more"] is True


# -- memory_delete --------------------------------------------------------------

async def test_memory_delete_tool_present_in_list_tools():
    from icx_engine.mcp_server import _all_tools_full
    tools = await _all_tools_full()
    assert any(t.name == "memory_delete" for t in tools)


async def test_memory_delete_confirmation_flow_actually_deletes(monkeypatch):
    from icx_engine.mcp_server import _call_tool

    store = {"PROJ-1": "entry"}

    class _FakeManager:
        def delete(self, issue_key):
            store.pop(issue_key, None)

    monkeypatch.setattr("icx_engine.mcp_server._ensure_memory_manager", lambda: _FakeManager())

    result = await _call_tool("memory_delete", {"issue_key": "PROJ-1"})
    data = json.loads(result[0].text)
    assert data["status"] == "pending_confirmation"
    token = data["token"]
    assert "PROJ-1" in store  # not deleted yet - only a token was issued

    result2 = await _call_tool("memory_delete", {"issue_key": "PROJ-1", "confirm_token": token})
    data2 = json.loads(result2[0].text)
    assert data2 == {"ok": True, "issue_key": "PROJ-1"}
    assert "PROJ-1" not in store


async def test_memory_delete_invalid_confirm_token_rejected():
    from icx_engine.mcp_server import _call_tool
    result = await _call_tool("memory_delete", {"issue_key": "PROJ-1", "confirm_token": "bogus-token"})
    data = json.loads(result[0].text)
    assert data == {
        "ok": False,
        "error": "Invalid or already-used confirm_token. Call again without a token to get a fresh one.",
    }


async def test_memory_delete_missing_issue_key_returns_error():
    from icx_engine.mcp_server import _call_tool
    result = await _call_tool("memory_delete", {})
    data = json.loads(result[0].text)
    assert "error" in data


# -- memory_update ---------------------------------------------------------------

async def test_memory_update_tool_present_in_list_tools():
    from icx_engine.mcp_server import _all_tools_full
    tools = await _all_tools_full()
    assert any(t.name == "memory_update" for t in tools)


async def test_memory_update_happy_path_returns_updated_fields(monkeypatch):
    from icx_engine.mcp_server import _call_tool
    from tests.memory.factories import make_entry

    class _FakeManager:
        def update(self, issue_key, **fields):
            return make_entry(issue_key, **fields)

    monkeypatch.setattr("icx_engine.mcp_server._ensure_memory_manager", lambda: _FakeManager())

    result = await _call_tool("memory_update", {
        "issue_key": "PROJ-1", "summary": "New summary", "tags": ["a", "b"],
    })
    data = json.loads(result[0].text)
    assert data["ok"] is True
    assert data["issue_key"] == "PROJ-1"
    assert sorted(data["updated_fields"]) == ["summary", "tags"]


async def test_memory_update_unknown_issue_key_returns_error(monkeypatch):
    from icx_engine.mcp_server import _call_tool
    from icx_engine.exceptions import ICXMemoryError

    class _FakeManager:
        def update(self, issue_key, **fields):
            raise ICXMemoryError(f"No memory entry found for {issue_key}")

    monkeypatch.setattr("icx_engine.mcp_server._ensure_memory_manager", lambda: _FakeManager())

    result = await _call_tool("memory_update", {"issue_key": "PROJ-9", "summary": "x"})
    data = json.loads(result[0].text)
    assert data == {"ok": False, "error": "No memory entry found for PROJ-9"}


async def test_memory_update_disallowed_field_returns_error():
    from icx_engine.mcp_server import _call_tool
    result = await _call_tool("memory_update", {"issue_key": "PROJ-1", "issue_type": "Task"})
    data = json.loads(result[0].text)
    assert data["ok"] is False
    assert "error" in data


# -- reinforce_memory_usage ----------------------------------------------------

async def test_reinforce_memory_usage_schema_untouched_by_internal_naming_cleanup():
    """Regression guard: the internal new_ticket_key -> used_by_key naming drift
    (mcp_server local var vs _reinforce_usage_sync/MemoryManager.reinforce_usage's
    already-consistent used_by_key/used_by_tickets) was fixed WITHOUT changing the
    external MCP schema - any agent already calling this tool with new_ticket_key
    must keep working identically. This must pass both before and after that fix."""
    from icx_engine.mcp_server import _all_tools_full
    from icx_engine.models.config import AppConfig
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        tools = await _all_tools_full()
    tool = next(t for t in tools if t.name == "reinforce_memory_usage")
    assert "new_ticket_key" in tool.inputSchema["properties"]
    assert "new_ticket_key" in tool.inputSchema["required"]
    assert "used_by_key" not in tool.inputSchema["properties"]


async def test_reinforce_memory_usage_memory_not_ready_returns_error():
    from icx_engine.mcp_server import _call_tool
    with patch("icx_engine.memory.mcp_tools._get_memory_state", return_value="cold"):
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


def test_validate_issue_key_arg_helper_matches_original_duplicated_error_text():
    """DRY refactor regression guard: _validate_issue_key_arg's error text/shape must
    be byte-for-byte identical to what each of the 3 call sites duplicated before this
    helper existed - a plain {"error": ...} dict, no "ok" key."""
    import json as _json
    from icx_engine.mcp_server import _validate_issue_key_arg

    value, err = _validate_issue_key_arg({}, "source_key")
    assert value is None
    assert _json.loads(err[0].text) == {"error": "source_key is required."}

    value, err = _validate_issue_key_arg({"issue_key": "not-a-key"}, "issue_key")
    assert value is None
    assert _json.loads(err[0].text) == {"error": "issue_key must be in PROJ-123 format."}

    value, err = _validate_issue_key_arg({"new_ticket_key": "  proj-142  "}, "new_ticket_key")
    assert err is None
    assert value == "proj-142"  # stripped, NOT uppercased (matches original .strip() call sites)


# -- get_memory_audit ----------------------------------------------------------

async def test_get_memory_audit_memory_not_ready_returns_error():
    from icx_engine.mcp_server import _call_tool
    with patch("icx_engine.memory.mcp_tools._get_memory_state", return_value="cold"):
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
    from icx_engine.mcp_server import _all_tools_full
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        tools = await _all_tools_full()
    names = {t.name for t in tools}
    expected = {
        "testing_start_session",
        "testing_resume_session",
    }
    assert expected.issubset(names)


async def test_magik_start_tool_requires_file_paths():
    from icx_engine.mcp_server import _all_tools_full
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        tools = await _all_tools_full()
    tool = next(t for t in tools if t.name == "testing_start_session")
    assert "file_paths" in tool.inputSchema["required"]
    assert "file_paths" in tool.inputSchema["properties"]
    assert tool.inputSchema["properties"]["file_paths"]["type"] == "array"


async def test_magik_resume_tool_requires_session_id_and_response():
    from icx_engine.mcp_server import _all_tools_full
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        tools = await _all_tools_full()
    tool = next(t for t in tools if t.name == "testing_resume_session")
    assert "session_id" in tool.inputSchema["required"]
    assert "response" in tool.inputSchema["required"]


async def test_start_session_validates_bad_input():
    import json
    from icx_engine.mcp_server import _call_tool
    result = await _call_tool("testing_start_session", {"file_paths": [], "test_mode": "automated"})
    payload = json.loads(result[0].text)
    assert payload.get("ok") is False
    assert "file_paths" in payload.get("error", "")


async def test_resume_description_lists_gate_shapes():
    from icx_engine.mcp_server import _all_tools_full
    from icx_engine.models.config import AppConfig
    from unittest.mock import patch
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        tools = {t.name: t for t in await _all_tools_full()}
    desc = tools["testing_resume_session"].description
    for token in ("pick_type", "compat_check", "auth_gate", "author_flow", "approve_iteration", "RULE"):
        assert token in desc


async def test_resume_description_lists_agent_gates():
    from icx_engine.mcp_server import _all_tools_full
    from icx_engine.models.config import AppConfig
    from unittest.mock import patch
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        tools = {t.name: t for t in await _all_tools_full()}
    desc = tools["testing_resume_session"].description
    for token in ("compat_scan", "author_flow", "steps", "all_compatible"):
        assert token in desc


async def test_compat_scan_description_is_open_ended_mandate():
    """compat_scan must be an open-ended agent mandate - no hardcoded blocker classes,
    it bans deferring to the runner, and it makes the agent report every finding to the
    user rather than deciding. ICX must not claim to verify the answer."""
    from icx_engine.mcp_server import _all_tools_full
    from icx_engine.models.config import AppConfig
    from unittest.mock import patch
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        tools = {t.name: t for t in await _all_tools_full()}
    desc = tools["testing_resume_session"].description
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
    from icx_engine.mcp_server import _all_tools_full
    from icx_engine.models.config import AppConfig
    from unittest.mock import patch
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        tools = {t.name: t for t in await _all_tools_full()}
    desc = tools["testing_resume_session"].description
    assert "RULEBOOK RULE" in desc
    assert "testing_rules" in desc
    assert "gate.rules" in desc
    # gate 2b advertises the presence-enforcement + accept_incomplete escape hatch
    assert "2b-8" in desc and "accept_incomplete" in desc


async def test_resume_description_has_expand_scan():
    from icx_engine.mcp_server import _all_tools_full
    from icx_engine.models.config import AppConfig
    from unittest.mock import patch
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        tools = {t.name: t for t in await _all_tools_full()}
    desc = tools["testing_resume_session"].description
    assert "expand_scan" in desc and "related_files" in desc


async def test_resume_description_has_reread_rule():
    from icx_engine.mcp_server import _all_tools_full
    from icx_engine.models.config import AppConfig
    from unittest.mock import patch
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        tools = {t.name: t for t in await _all_tools_full()}
    desc = tools["testing_resume_session"].description.lower()
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

    # A real authenticated session already exists (written by icx_ui_auth_capture) with a storage_state.
    _auth.save_session("proj-z", "host-z", "real-sess",
                       store=store, storage_state="/path/to/state.json")

    await mcp_server._call_tool("testing_resume_session", {
        "session_id": "sess-uuid",
        "response": {"auth_mode": "capture", "session_id": "SECRET-123"},
    })
    # session_id stripped from the payload that reaches the graph (and thus the checkpoint writes)
    assert "session_id" not in (captured["resume"] or {})
    assert captured["resume"].get("auth_mode") == "capture"
    # the pre-existing session record is NOT overwritten - its storage_state survives, so the replay
    # still runs authenticated (re-saving with an empty storage_state was the bug).
    rec = _auth.load_session("proj-z", "host-z", store=store)
    assert rec.session_id == "real-sess" and rec.storage_state == "/path/to/state.json"


# -- Default-skill hints on tool-family entrypoints ----------------------------

async def test_sonar_status_attaches_quality_skill_hint():
    from icx_engine import mcp_server
    from icx_engine.skills.schema import SkillEntry
    from unittest.mock import AsyncMock, patch

    class _FakeStorage:
        def read(self, name):
            assert name == "sonar-quality-review"
            return SkillEntry(name=name, description="d", title="t", when_to_use="w",
                               procedure="p", pitfalls="x", verification="v")

    with patch("icx_engine.sonar.service.status", new=AsyncMock(return_value={"connected": True})):
        with patch("icx_engine.skills.hints.SkillStorage", _FakeStorage):
            result = await mcp_server._call_tool("sonar_status", {})
    data = json.loads(result[0].text)
    assert data["ok"] is True
    assert data["skills"]["index"][0]["name"] == "sonar-quality-review"


async def test_sonar_status_appends_ranked_custom_skill(tmp_path):
    from icx_engine import mcp_server
    from icx_engine.skills.schema import SkillEntry
    from icx_engine.skills.storage import SkillStorage
    from unittest.mock import AsyncMock, patch

    storage = SkillStorage(root=tmp_path)
    default_entry = SkillEntry(name="sonar-quality-review", description="d", title="t",
                                when_to_use="w", procedure="p", pitfalls="x", verification="v")
    storage.write(default_entry)
    custom_entry = SkillEntry(name="custom-sonar-triage", description="triage sonar findings fast",
                               tags=["findings"], title="Custom Sonar Triage",
                               when_to_use="w", procedure="p", pitfalls="x", verification="v")
    storage.write(custom_entry)

    with patch("icx_engine.sonar.service.status", new=AsyncMock(return_value={"connected": True})):
        with patch("icx_engine.skills.hints.SkillStorage", lambda: storage):
            result = await mcp_server._call_tool("sonar_status", {})
    data = json.loads(result[0].text)
    names = [e["name"] for e in data["skills"]["index"]]
    assert names[0] == "sonar-quality-review"
    assert "custom-sonar-triage" in names


async def test_sonar_status_omits_skill_hint_when_lookup_fails():
    from icx_engine import mcp_server
    from unittest.mock import AsyncMock, patch

    class _BrokenStorage:
        def read(self, name):
            raise RuntimeError("boom")

    with patch("icx_engine.sonar.service.status", new=AsyncMock(return_value={"connected": True})):
        with patch("icx_engine.skills.hints.SkillStorage", _BrokenStorage):
            result = await mcp_server._call_tool("sonar_status", {})
    data = json.loads(result[0].text)
    assert data["ok"] is True
    assert "skills" not in data


# -- Sonar MCP tools -----------------------------------------------------------

async def test_sonar_tools_registered():
    from icx_engine.mcp_server import _all_tools_full
    from icx_engine.models.config import AppConfig
    from unittest.mock import patch
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        names = {t.name for t in await _all_tools_full()}
    for n in ("sonar_status", "sonar_projects", "sonar_branches",
              "sonar_measures", "sonar_quality_gate", "sonar_findings", "sonar_report",
              "sonar_top_files", "sonar_history", "sonar_analyses", "sonar_rule", "sonar_hotspot"):
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


# -- Persona selection layer ---------------------------------------------------

from icx_engine.mcp_server import _select_persona, _PERSONA_SLUGS


def _analysis(**kw):
    base = {
        "recommended_persona": "", "problem_summary": "", "detailed_description": "",
        "impact": "", "issue_type": "Bug",
    }
    base.update(kw)
    return base


def test_select_persona_llm_slug_wins():
    a = _analysis(recommended_persona="principal-security-architect",
                  problem_summary="jwt token refresh fails")
    slug, source = _select_persona(a)
    assert slug == "principal-security-architect"
    assert source == "llm"


def test_select_persona_unknown_slug_falls_to_keyword():
    a = _analysis(recommended_persona="wizard",
                  problem_summary="database index missing on orders query")
    slug, source = _select_persona(a)
    assert slug == "principal-database-architect"
    assert source == "keyword"


def test_select_persona_no_llm_uses_keyword():
    a = _analysis(problem_summary="api endpoint returns 500 in the service layer")
    slug, source = _select_persona(a)
    assert slug == "staff-backend-engineer"
    assert source == "keyword"


def test_select_persona_ui_pick_clamped_when_text_is_backend_only():
    a = _analysis(recommended_persona="principal-ui-ux-architect",
                  problem_summary="service api endpoint throws null pointer in repository")
    slug, source = _select_persona(a)
    assert slug == "staff-backend-engineer"
    assert source == "keyword"


def test_select_persona_ui_pick_kept_when_ui_vocab_present():
    a = _analysis(recommended_persona="principal-ui-ux-architect",
                  problem_summary="submit button layout broken on the form modal")
    slug, source = _select_persona(a)
    assert slug == "principal-ui-ux-architect"
    assert source == "llm"


async def test_icx_skill_get_registered():
    from icx_engine.mcp_server import _all_tools_full
    tools = await _all_tools_full()
    assert any(t.name == "icx_skill_get" for t in tools)


async def test_icx_skill_get_returns_full_body(tmp_path, monkeypatch):
    import json
    from icx_engine.mcp_server import _call_tool
    from icx_engine.skills.schema import SkillEntry
    from icx_engine.skills.storage import SkillStorage

    storage = SkillStorage(root=tmp_path)
    entry = SkillEntry(name="test-fetch-skill", description="d", tags=["x"], title="Test Fetch Skill",
                       when_to_use="x", procedure="the procedure text", pitfalls="x", verification="x")
    entry.icx_hash = entry.compute_hash()
    storage.write(entry)

    monkeypatch.setattr("icx_engine.skills.mcp_tools.SkillStorage", lambda: storage)
    res = await _call_tool("icx_skill_get", {"name": "test-fetch-skill"})
    data = json.loads(res[0].text)
    assert "the procedure text" in data["body"]


async def test_icx_skill_get_unknown_name_returns_error_not_raise(tmp_path, monkeypatch):
    import json
    from icx_engine.mcp_server import _call_tool
    from icx_engine.skills.storage import SkillStorage

    monkeypatch.setattr("icx_engine.skills.mcp_tools.SkillStorage", lambda: SkillStorage(root=tmp_path))
    res = await _call_tool("icx_skill_get", {"name": "does-not-exist"})
    data = json.loads(res[0].text)
    assert "error" in data


async def test_icx_skill_get_validates_name():
    import json
    from icx_engine.mcp_server import _call_tool
    res = await _call_tool("icx_skill_get", {"name": ""})
    data = json.loads(res[0].text)
    assert "error" in data


async def test_icx_skills_index_registered():
    from icx_engine.mcp_server import _all_tools_full
    tools = await _all_tools_full()
    assert any(t.name == "icx_skills_index" for t in tools)


async def test_icx_skills_index_returns_all_skills_unranked(tmp_path, monkeypatch):
    import json
    from icx_engine.mcp_server import _call_tool
    from icx_engine.skills.schema import SkillEntry
    from icx_engine.skills.storage import SkillStorage

    storage = SkillStorage(root=tmp_path)
    for i in range(7):
        e = SkillEntry(name=f"skill-{i}", description=f"d{i}", tags=["x"], title=f"skill-{i}",
                       when_to_use="w", procedure="p", pitfalls="x", verification="v")
        e.icx_hash = e.compute_hash()
        storage.write(e)
    monkeypatch.setattr("icx_engine.skills.mcp_tools.SkillStorage", lambda: storage)

    res = await _call_tool("icx_skills_index", {})
    data = json.loads(res[0].text)
    assert len(data["skills"]) == 7   # NOT capped at 5, unlike rank_skills/rank_skills_for_tags
    assert set(data["skills"][0].keys()) == {"name", "description"}


async def test_icx_skills_index_empty_store(tmp_path, monkeypatch):
    import json
    from icx_engine.mcp_server import _call_tool
    from icx_engine.skills.storage import SkillStorage
    monkeypatch.setattr("icx_engine.skills.mcp_tools.SkillStorage", lambda: SkillStorage(root=tmp_path))
    res = await _call_tool("icx_skills_index", {})
    data = json.loads(res[0].text)
    assert data == {"skills": []}


def test_select_persona_default_when_nothing_matches():
    a = _analysis(problem_summary="please review this")
    slug, source = _select_persona(a)
    assert slug == "system-architect"
    assert source == "default"


def test_all_keyword_targets_are_valid_slugs():
    from icx_engine.mcp_server import _PERSONA_KEYWORDS
    for slug, _kws in _PERSONA_KEYWORDS:
        assert slug in _PERSONA_SLUGS


from icx_engine.mcp_server import _persona_preamble


def test_persona_preamble_has_role_identity_and_rubric():
    text = _persona_preamble("system-architect", 0.9, 0.9)
    assert "senior system architect" in text.lower()
    assert "root cause" in text.lower()
    assert "at least two approaches" in text.lower()
    assert "blast radius" in text.lower()


def test_persona_preamble_low_confidence_mandates_questions():
    text = _persona_preamble("staff-backend-engineer", 0.4, 0.9)
    assert "ask" in text.lower()
    assert "clarifying" in text.lower()


def test_persona_preamble_high_confidence_no_question_mandate():
    text = _persona_preamble("staff-backend-engineer", 0.95, 0.95)
    assert "before presenting a plan" not in text.lower()


def test_persona_preamble_none_scores_no_gate():
    text = _persona_preamble("system-architect", None, None)
    assert "senior system architect" in text.lower()


def test_persona_preamble_unknown_slug_uses_default_title():
    text = _persona_preamble("not-a-real-slug", 0.9, 0.9)
    assert "senior system architect" in text.lower()


def test_preamble_prepends_without_altering_body():
    body = "STEP 1: do the thing\nRULE 0 - ...\n"
    slug, source = _select_persona({"recommended_persona": "system-architect",
                                     "problem_summary": "architecture refactor",
                                     "detailed_description": "", "impact": "",
                                     "issue_type": "Task"})
    preamble = _persona_preamble(slug, 0.9, 0.9)
    combined = preamble + "\n\n" + body
    assert combined.endswith(body)
    assert "OPERATING PERSONA" in combined
    assert "STEP 1: do the thing" in combined
    assert source == "llm"


def test_persona_wiring_guard_returns_body_on_failure():
    from icx_engine.mcp_server import _apply_persona
    body = "STEP 1\n"
    out, persona = _apply_persona(None, body)  # None analysis -> guarded, unchanged
    assert out == body
    assert persona is None


def test_persona_wiring_prepends_and_reports():
    from icx_engine.mcp_server import _apply_persona
    body = "STEP 1: locate files\n"
    analysis = {"recommended_persona": "principal-security-architect",
                "problem_summary": "jwt token refresh fails",
                "detailed_description": "", "impact": "", "issue_type": "Bug",
                "confidence_score": 0.9, "completeness_score": 0.9}
    out, persona = _apply_persona(analysis, body)
    assert out.endswith(body)
    assert "OPERATING PERSONA" in out
    assert persona == {"role": "principal-security-architect", "source": "llm"}


def test_kw_hit_prefix_word_not_midword():
    from icx_engine.mcp_server import _kw_hit
    assert _kw_hit("list of endpoints", "endpoint") is True   # suffix ok
    assert _kw_hit("we made a decision", "ci") is False        # no mid-word
    assert _kw_hit("build the payment service", "ui") is False # no mid-word
    assert _kw_hit("upload the file", "load") is False         # no mid-word
    assert _kw_hit("react native app", "react native") is True # phrase substring


def test_select_persona_build_ui_pick_still_clamps():
    a = _analysis(recommended_persona="staff-frontend-engineer",
                  problem_summary="build the payment api service, null pointer in controller")
    slug, source = _select_persona(a)
    assert slug == "staff-backend-engineer"
    assert source == "keyword"


def test_select_persona_decision_word_does_not_route_to_devops():
    a = _analysis(problem_summary="we made a decision to refactor the endpoint handler")
    slug, source = _select_persona(a)
    assert slug != "staff-devops-sre"


def test_select_persona_no_llm_rawissue_shape_uses_keyword():
    # RawIssueResponse shape: summary/description, no problem_summary/impact.
    a = {"summary": "jwt auth token refresh fails", "description": "", "issue_type": "Bug"}
    slug, source = _select_persona(a)
    assert slug == "principal-security-architect"
    assert source == "keyword"


def test_persona_profile_keys_match_slugs():
    from icx_engine.mcp_server import _PERSONA_PROFILE, _PERSONA_SLUGS
    assert set(_PERSONA_PROFILE.keys()) == _PERSONA_SLUGS


def test_write_attachment_files_creates_sidecar_and_raw(tmp_path, monkeypatch):
    import base64
    from types import SimpleNamespace
    import icx_engine.mcp_server as mcp
    from icx_engine.graph import storage

    monkeypatch.setattr(storage, "temp_root", lambda: tmp_path)
    result = SimpleNamespace(
        attachment_full_texts={"report.xlsx": "FULL TABLE", "page.html": "<h1>hi</h1>"},
        attachment_raw={"report.xlsx": base64.b64encode(b"XLSXBYTES").decode(),
                        "page.html": base64.b64encode(b"<h1>hi</h1>").decode()},
    )
    paths = mcp._write_attachment_files(result, "P-1")
    assert paths["report.xlsx"]["full_text"].endswith("report.xlsx.full.md")
    assert paths["report.xlsx"]["raw"].endswith("report.xlsx")
    from pathlib import Path as _P
    assert _P(paths["report.xlsx"]["full_text"]).read_text(encoding="utf-8") == "FULL TABLE"
    assert _P(paths["report.xlsx"]["raw"]).read_bytes() == b"XLSXBYTES"


def test_write_attachment_files_guarded_on_bad_input():
    import icx_engine.mcp_server as mcp
    from types import SimpleNamespace
    assert mcp._write_attachment_files(SimpleNamespace(), "P-1") == {}


def test_periodic_temp_sweep_calls_sweep(monkeypatch):
    import asyncio
    import icx_engine.mcp_server as mcp
    calls = {"n": 0}

    def _fake_sweep(*a, **k):
        calls["n"] += 1

    from icx_engine.graph import storage
    monkeypatch.setattr(storage, "sweep_stale_temp_dirs", _fake_sweep)

    async def _run():
        task = asyncio.create_task(mcp._periodic_temp_sweep(interval_seconds=0.01))
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(_run())
    assert calls["n"] >= 1


def test_write_attachment_files_sanitizes_exotic_filename(tmp_path, monkeypatch):
    import base64
    from pathlib import Path as _P
    from types import SimpleNamespace
    import icx_engine.mcp_server as mcp
    from icx_engine.graph import storage

    monkeypatch.setattr(storage, "temp_root", lambda: tmp_path)
    # Filename with U+202F narrow no-break space (the real Windows-breaking case).
    exotic = "Screenshot 2026-07-07 at 3.45" + "\u202f" + "PM.png"  # \u202f = narrow no-break space
    result = SimpleNamespace(
        attachment_full_texts={},
        attachment_raw={exotic: base64.b64encode(b"IMGBYTES").decode()},
    )
    paths = mcp._write_attachment_files(result, "P-1")
    raw_path = paths[exotic]["raw"]
    assert all(ord(c) < 128 for c in raw_path)      # returned path is ASCII -> round-trips
    assert _P(raw_path).exists()                    # file actually written and openable
    assert _P(raw_path).read_bytes() == b"IMGBYTES"


# -- v0.4.1 Phase 1: Definition-of-Done gate -----------------------------------

def _make_ok():
    async def _ok(*a, **k):
        import json
        return json.dumps({"ok": True})
    return _ok


def test_record_verification_accepts_and_stores():
    import asyncio, json
    import icx_engine.mcp_server as mcp
    args = {
        "issue_key": "PROJ-1",
        "dod_items": [{"check": "repro fixed", "method": "reproduce",
                       "passed": True, "command": "pytest -k login", "output": "1 passed"}],
        "self_review_note": "checked edge cases",
        "layers_run": ["unit"],
    }
    out = asyncio.run(mcp._call_tool("icx_record_verification", args))
    data = json.loads(out[0].text)
    assert data["accepted"] is True
    assert "confidence" in data
    assert mcp._session_get("PROJ-1", "verification") is not None


def test_record_verification_rejects_incomplete():
    import asyncio, json
    import icx_engine.mcp_server as mcp
    args = {
        "issue_key": "PROJ-2",
        "dod_items": [{"check": "x", "method": "unit", "passed": True,
                       "command": "", "output": ""}],
        "self_review_note": "",
    }
    out = asyncio.run(mcp._call_tool("icx_record_verification", args))
    data = json.loads(out[0].text)
    assert data["accepted"] is False
    assert data["missing"]


def test_save_memory_rejects_unverified_success():
    import asyncio, json
    import icx_engine.mcp_server as mcp
    args = {
        "issue_key": "GUARD-1", "summary": "s", "problem_description": "p",
        "resolution_note": "r", "files_changed": ["a.py"], "tags": ["t"],
        "work_item_type": "Bug", "root_cause_pattern": "uncategorized",
        "pattern_confidence": 0.5, "outcome_verified": True,
        "outcome_feedback_note": "worked",
    }
    out = asyncio.run(mcp._call_tool("save_memory", args))
    data = json.loads(out[0].text)
    assert "error" in data and "verification" in data["error"].lower()


def test_save_memory_allows_human_override(monkeypatch):
    import asyncio, json
    import icx_engine.mcp_server as mcp
    monkeypatch.setattr(mcp, "_handle_save_memory", _make_ok())
    args = {
        "issue_key": "GUARD-2", "summary": "s", "problem_description": "p",
        "resolution_note": "r", "files_changed": ["a.py"], "tags": ["t"],
        "work_item_type": "Bug", "root_cause_pattern": "uncategorized",
        "pattern_confidence": 0.5, "outcome_verified": True,
        "outcome_feedback_note": "I tested it manually", "verified_by_human": True,
    }
    out = asyncio.run(mcp._call_tool("save_memory", args))
    data = json.loads(out[0].text)
    assert "error" not in data


def test_apply_dod_appends_verify_block_and_reports():
    from icx_engine.mcp_server import _apply_dod
    analysis = {"issue_type": "Bug", "problem_summary": "login 500",
                "reproduction_steps": ["POST /login empty"], "expected_behavior": "400",
                "actual_behavior": "500", "acceptance_criteria": []}
    body = "STEP 1: do thing\n"
    out, dod = _apply_dod(analysis, body, [])
    assert body in out
    assert "DEFINITION OF DONE" in out.upper()
    assert dod is not None and dod["risk_tier"] in {"low", "medium", "high", "critical"}
    assert isinstance(dod["checklist"], list) and dod["checklist"]


def test_apply_dod_guarded_on_bad_analysis():
    from icx_engine.mcp_server import _apply_dod
    out, dod = _apply_dod(None, "BODY", [])
    assert out == "BODY"
    assert dod is None


# -- icx_ui_auth_capture / icx_ui_auth_inline tools ------------------------------------

async def test_ui_auth_capture_validates_input():
    from icx_engine.mcp_server import _call_tool
    r = await _call_tool("icx_ui_auth_capture", {"url": "", "file_paths": ["a"]})
    assert json.loads(r[0].text)["ok"] is False
    r = await _call_tool("icx_ui_auth_capture", {"url": "http://x", "file_paths": []})
    assert json.loads(r[0].text)["ok"] is False


async def test_ui_auth_capture_success(monkeypatch):
    from icx_engine.mcp_server import _call_tool
    monkeypatch.setattr("icx_engine.testing.nodes._resolve_project_id", lambda fp: "proj")
    monkeypatch.setattr("icx_engine.testing.runners.install.is_installed", lambda name: True)

    async def _cap(project, host, url, success_url=""):
        return "/x/state.json", ""
    monkeypatch.setattr("icx_engine.testing.ui_auth.capture_session", _cap)
    r = await _call_tool("icx_ui_auth_capture", {"url": "http://x/login", "file_paths": ["a.jsx"]})
    payload = json.loads(r[0].text)
    assert payload["ok"] is True and payload["storage_state"] == "/x/state.json"


async def test_ui_auth_inline_requires_credentials(monkeypatch):
    from icx_engine.mcp_server import _call_tool
    monkeypatch.setattr("icx_engine.testing.nodes._resolve_project_id", lambda fp: "proj")
    monkeypatch.setattr("icx_engine.testing.runners.install.is_installed", lambda name: True)
    r = await _call_tool("icx_ui_auth_inline",
                         {"url": "http://x", "file_paths": ["a"], "username": "", "password": "p"})
    assert json.loads(r[0].text)["ok"] is False


async def test_ui_auth_inline_success(monkeypatch):
    from icx_engine.mcp_server import _call_tool
    monkeypatch.setattr("icx_engine.testing.nodes._resolve_project_id", lambda fp: "proj")
    monkeypatch.setattr("icx_engine.testing.runners.install.is_installed", lambda name: True)

    async def _inline(project, host, url, username, password, **kw):
        assert username == "admin" and password == "pw"
        return "/x/state.json", ""
    monkeypatch.setattr("icx_engine.testing.ui_auth.inline_session", _inline)
    r = await _call_tool("icx_ui_auth_inline", {"url": "http://x/login", "file_paths": ["a.jsx"],
                                            "username": "admin", "password": "pw"})
    assert json.loads(r[0].text)["ok"] is True


async def test_ui_auth_capture_broken_tooling_steers_to_setup(monkeypatch):
    from icx_engine.mcp_server import _call_tool
    monkeypatch.setattr("icx_engine.testing.nodes._resolve_project_id", lambda fp: "proj")
    monkeypatch.setattr("icx_engine.testing.runners.install.is_installed", lambda name: True)

    async def _cap(project, host, url, success_url=""):
        return None, r"Error [ERR_MODULE_NOT_FOUND]: Cannot find package 'playwright' imported from F:\icx-engine"
    monkeypatch.setattr("icx_engine.testing.ui_auth.capture_session", _cap)
    r = await _call_tool("icx_ui_auth_capture", {"url": "http://x/login", "file_paths": ["a.jsx"]})
    p = json.loads(r[0].text)
    assert p["ok"] is False
    assert "icx test setup --force" in p["error"]
    assert "DO NOT install" in p["error"]
    assert "repo" in p["error"].lower()


# -- icx_lock_plan spec-lock tool ---------------------------------------------------

async def test_lock_plan_validates_input():
    from icx_engine.mcp_server import _call_tool
    r = await _call_tool("icx_lock_plan", {"issue_ref": "", "chosen_files": ["a.py"]})
    assert json.loads(r[0].text)["ok"] is False
    r = await _call_tool("icx_lock_plan", {"issue_ref": "T-1", "chosen_files": []})
    assert json.loads(r[0].text)["ok"] is False


async def test_lock_plan_blocks_on_missed_high_signal(monkeypatch):
    from icx_engine.mcp_server import _call_tool
    import icx_engine.mcp_server as m
    # graph signal reports a direct dependent the plan omitted -> high-tier miss -> blocks.
    def _sigs(project_path, seeds, keywords):
        return (lambda: [("src/caller.py", "direct graph dependent of a planned file")],
                lambda: [], lambda: [], lambda: [])
    monkeypatch.setattr(m, "_context_signals", _sigs)
    monkeypatch.setattr(m, "_lock_plan_prior_fix", lambda chosen: set())
    r = await _call_tool("icx_lock_plan", {"issue_ref": "T-1", "chosen_files": ["src/svc.py"]})
    p = json.loads(r[0].text)
    assert p["ok"] is False
    assert [x["path"] for x in p["blocking_missed"]] == ["src/caller.py"]
    assert p["coverage"] == 0.0


async def test_lock_plan_ok_when_included_or_justified(monkeypatch):
    from icx_engine.mcp_server import _call_tool
    import icx_engine.mcp_server as m
    def _sigs(project_path, seeds, keywords):
        return (lambda: [("src/caller.py", "direct graph dependent")], lambda: [], lambda: [], lambda: [])
    monkeypatch.setattr(m, "_context_signals", _sigs)
    monkeypatch.setattr(m, "_lock_plan_prior_fix", lambda chosen: set())
    # include it
    r = await _call_tool("icx_lock_plan", {"issue_ref": "T-2", "chosen_files": ["src/svc.py", "src/caller.py"]})
    assert json.loads(r[0].text)["ok"] is True
    # or justify it
    r = await _call_tool("icx_lock_plan", {"issue_ref": "T-3", "chosen_files": ["src/svc.py"],
                                       "justifications": {"src/caller.py": "unrelated same-name"}})
    p = json.loads(r[0].text)
    assert p["ok"] is True and p["coverage"] == 1.0


async def test_lock_plan_stores_locked_plan(monkeypatch):
    from icx_engine.mcp_server import _call_tool, _session_get
    import icx_engine.mcp_server as m
    monkeypatch.setattr(m, "_context_signals", lambda p, s, k: (lambda: [], lambda: [], lambda: [], lambda: []))
    monkeypatch.setattr(m, "_lock_plan_prior_fix", lambda chosen: set())
    await _call_tool("icx_lock_plan", {"issue_ref": "T-9", "chosen_files": ["a.py"]})
    stored = _session_get("T-9", "locked_plan", None)
    assert stored is not None and stored["chosen"] == ["a.py"] and stored["ok"] is True


async def test_lock_plan_fan_out_runs_off_event_loop(monkeypatch):
    # Regression guard for the fan_out(...) synchronous-blocking-the-event-loop fix: fan_out must be
    # invoked via run_in_executor (a worker thread), and the final result must still be correct.
    import threading
    import icx_engine.context_completeness as cc
    from icx_engine.mcp_server import _call_tool
    import icx_engine.mcp_server as m

    main_thread = threading.current_thread()
    seen_threads: list = []
    real_fan_out = cc.fan_out

    def _tracking_fan_out(*a, **k):
        seen_threads.append(threading.current_thread())
        return real_fan_out(*a, **k)

    monkeypatch.setattr(cc, "fan_out", _tracking_fan_out)
    monkeypatch.setattr(m, "_context_signals",
                        lambda p, s, k: (lambda: [("src/caller.py", "direct graph dependent")],
                                          lambda: [], lambda: [], lambda: []))
    monkeypatch.setattr(m, "_lock_plan_prior_fix", lambda chosen: set())

    r = await _call_tool("icx_lock_plan", {"issue_ref": "T-EXEC", "chosen_files": ["src/svc.py"]})
    p = json.loads(r[0].text)

    assert len(seen_threads) == 1
    assert seen_threads[0] != main_thread                          # ran off the event-loop thread
    assert p["ok"] is False                                        # correctness preserved
    assert [x["path"] for x in p["blocking_missed"]] == ["src/caller.py"]


async def test_lock_plan_in_tool_order_before_testing():
    from icx_engine.mcp_server import _all_tools_full
    from icx_engine.models.config import AppConfig
    from unittest.mock import patch
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        names = [t.name for t in await _all_tools_full()]
    assert "icx_lock_plan" in names
    assert names.index("icx_lock_plan") < names.index("testing_start_session")   # spec-lock before testing


def test_context_signals_emit_from_graph_semantic_memory(monkeypatch):
    # Regression guard: the real signal providers must actually produce candidates (not silently
    # no-op due to a wrong return shape / field name).
    import icx_engine.mcp_server as m

    class _Ctx:
        def __init__(self, f): self.file = f; self.reason = "related"
    class _Q:
        def get_blast_radius(self, seeds, **k):
            return {"direct_dependents": ["dep.py"], "missing_changes": ["co.py"]}
        def find_context(self, query):
            return [_Ctx("sem.py")]

    monkeypatch.setattr(m, "_load_querier_simple", lambda p: (_Q(), "/repo"))
    monkeypatch.setattr(m, "_find_by_file_sync",
                        lambda f, k: [{"issue_key": "T-1", "files_changed": ["mem.py"]}])
    graph_sig, grep_sig, semantic_sig, memory_sig = m._context_signals("/repo", ["svc.py"], ["kw"])
    graph_paths = dict(graph_sig())
    assert "dep.py" in graph_paths and "co.py" in graph_paths
    assert "sem.py" in dict(semantic_sig())
    assert "mem.py" in dict(memory_sig())


def test_lock_plan_prior_fix_reads_files_changed(monkeypatch):
    import icx_engine.mcp_server as m
    monkeypatch.setattr(m, "_find_by_file_sync",
                        lambda f, k: [{"issue_key": "T-2", "files_changed": ["old_fix.py"]}])
    assert m._lock_plan_prior_fix(["svc.py"]) == {"old_fix.py"}


async def test_icx_boost_registered():
    from icx_engine.mcp_server import _list_tools
    tools = await _list_tools()
    assert any(t.name == "icx_boost" for t in tools)


async def test_icx_boost_returns_brief_for_doubt():
    import json
    from icx_engine.mcp_server import _call_tool
    res = await _call_tool("icx_boost", {"prompt": "what is a closure?"})
    data = json.loads(res[0].text)
    assert data["archetype"] == "doubt"
    assert data["context"]["files"] == []
    assert data["mandatory_directive"]
    assert data["boost_meta"]["llm_used"] is False


async def test_icx_boost_validates_prompt():
    import json
    from icx_engine.mcp_server import _call_tool
    res = await _call_tool("icx_boost", {"prompt": ""})
    data = json.loads(res[0].text)
    assert data.get("error")


async def test_icx_boost_never_raises_on_bad_repo():
    import json
    from icx_engine.mcp_server import _call_tool
    res = await _call_tool("icx_boost", {"prompt": "fix the auth crash", "repo_path": "/no/such/dir"})
    data = json.loads(res[0].text)
    assert data["archetype"] == "debugging"
    assert "boosted_prompt" in data


async def test_icx_boost_preserves_and_tiers_links():
    import json
    from icx_engine.mcp_server import _call_tool
    res = await _call_tool("icx_boost", {
        "prompt": "check this design https://www.figma.com/file/abc and ticket https://x.atlassian.net/browse/AB-1"})
    data = json.loads(res[0].text)
    links = data["links"]
    urls = {l["url"]: l for l in links}
    assert any("figma" in u for u in urls)
    assert any(l["target"] == "figma" and l["status"] == "agent_fetch" for l in links)
    assert any(l["target"] == "jira" for l in links)


async def test_every_tool_description_is_strict_and_substantial():
    """Ironclad contract: EVERY MCP tool description must carry a strict directive keyword (so the
    agent cannot treat it as optional) and be non-trivial. If this fails, the new/edited tool needs a
    MANDATORY/MUST/ALWAYS/CALL/USE WHEN-style trigger line."""
    from icx_engine.mcp_server import _list_tools
    strict = ("MANDATORY", "MUST", "ALWAYS", "NEVER", "CALL ", "USE WHEN", "USE THIS",
              "RUN ", "FOLLOW", "FIRST", "BEFORE ", "AFTER ", "REQUIRED", "DO NOT")
    tools = await _list_tools()
    weak = []
    for t in tools:
        d = (t.description or "").strip()
        if len(d) < 40 or not any(k in d.upper() for k in strict):
            weak.append(t.name)
    assert not weak, f"tool descriptions missing a strict directive: {weak}"


async def test_icx_boost_refine_builds_cto_prompt():
    import json
    from icx_engine.mcp_server import _call_tool
    res = await _call_tool("icx_boost_refine", {
        "prompt": "add a login endpoint", "archetype": "security",
        "objective": "Build a secure JWT login endpoint",
        "requirements": ["hash passwords"], "constraints": ["FastAPI"],
        "acceptance": ["all auth paths tested"],
        "dims": ["account lockout after N failed attempts"]})
    d = json.loads(res[0].text)
    assert d["boost_meta"]["pass"] == 2
    assert d["boost_meta"]["llm_used"] is False
    bp = d["boosted_prompt"]
    assert "# ROLE" in bp and "# ACCEPTANCE CRITERIA" in bp       # CTO-grade structure
    assert "security architect" in bp.lower()                     # per-problem persona
    assert "account lockout" in bp.lower()                        # agent's dim merged
    assert 'Original request (verbatim, for reference): "add a login endpoint"' in bp
    assert d["gates"]


async def test_icx_boost_refine_fills_gaps_from_minimal_input():
    import json
    from icx_engine.mcp_server import _call_tool
    # only an objective -> still a full CTO prompt (ICX fills persona/requirements/acceptance)
    d = json.loads((await _call_tool("icx_boost_refine", {
        "prompt": "add a cache", "objective": "add a caching layer"}))[0].text)
    assert "# REQUIREMENTS" in d["boosted_prompt"] and "# STANDARDS" in d["boosted_prompt"]


async def test_icx_boost_refine_fan_out_runs_off_event_loop(monkeypatch, tmp_path):
    # Regression guard for the fan_out(...) synchronous-blocking-the-event-loop fix on the
    # icx_boost_refine path: fan_out must run via run_in_executor and still produce correct context.
    import threading
    import icx_engine.context_completeness as cc
    from icx_engine.mcp_server import _call_tool
    import icx_engine.boost.mcp_tools as boost_mcp_tools

    main_thread = threading.current_thread()
    seen_threads: list = []
    real_fan_out = cc.fan_out

    def _tracking_fan_out(*a, **k):
        seen_threads.append(threading.current_thread())
        return real_fan_out(*a, **k)

    monkeypatch.setattr(cc, "fan_out", _tracking_fan_out)
    monkeypatch.setattr(boost_mcp_tools, "_context_signals",
                        lambda p, s, k: (lambda: [], lambda: [("src/caller.py", "references seed")],
                                          lambda: [], lambda: []))

    res = await _call_tool("icx_boost_refine", {
        "prompt": "add a login endpoint", "archetype": "security",
        "objective": "Build a secure JWT login endpoint",
        "repo_path": str(tmp_path), "current_file": "src/svc.py"})
    d = json.loads(res[0].text)

    assert len(seen_threads) == 1
    assert seen_threads[0] != main_thread                          # ran off the event-loop thread
    assert "src/caller.py" in d["boosted_prompt"]                  # context still fused in correctly


async def test_icx_boost_refine_validates_input():
    import json
    from icx_engine.mcp_server import _call_tool
    assert json.loads((await _call_tool("icx_boost_refine", {"prompt": ""}))[0].text).get("error")
    # no objective/requirements/dims -> rejected
    assert json.loads((await _call_tool("icx_boost_refine", {"prompt": "x"}))[0].text).get("error")


async def test_icx_boost_brief_points_to_refine():
    import json
    from icx_engine.mcp_server import _call_tool
    d = json.loads((await _call_tool("icx_boost", {"prompt": "add a login feature"}))[0].text)
    assert d["refine"]["tool"] == "icx_boost_refine"


async def test_sonar_disabled_returns_graceful_fallback():
    import json
    from unittest.mock import patch, AsyncMock
    from icx_engine.mcp_server import _call_tool
    from icx_engine.sonar import service as svc
    with patch.object(svc, "projects", AsyncMock(side_effect=svc.SonarDisabled("not configured"))):
        d = json.loads((await _call_tool("sonar_projects", {}))[0].text)
    assert d["ok"] is False
    assert "fallback" in d
    assert "connect ICX" in d["fallback"] and "your own" in d["fallback"].lower()
    assert "icx sonar --add" in d["fallback"]


def test_icx_fallback_is_three_tier():
    from icx_engine.mcp_server import _ICX_FALLBACK
    f = _ICX_FALLBACK("work-tracker", "icx connection --add")
    assert "not enabled" in f.lower() and "connect ICX" in f          # tier 1
    assert "your own" in f.lower()                                     # tier 2 (agent connector)
    assert "normal flow" in f.lower()                                  # tier 3
    assert "not fabricate" in f.lower()


async def test_icx_boost_skips_trivial_conversational():
    import json
    from icx_engine.mcp_server import _call_tool
    for p in ["thanks", "continue", "ok", "do it", "looks good"]:
        d = json.loads((await _call_tool("icx_boost", {"prompt": p}))[0].text)
        assert d.get("skip") is True, f"{p!r} should skip"
        assert d["boost_meta"]["trivial"] is True
        assert "methodology" not in d          # cheap - no heavy brief built


async def test_icx_boost_boosts_real_request_not_skipped():
    import json
    from icx_engine.mcp_server import _call_tool
    d = json.loads((await _call_tool("icx_boost", {"prompt": "fix the login crash"}))[0].text)
    assert not d.get("skip")
    assert d["archetype"] == "debugging"


# -- one call, two passes: icx_boost tool now auto-refines --------------------

async def test_icx_boost_tool_auto_refines_in_one_call():
    import json
    from icx_engine.mcp_server import _call_tool
    d = json.loads((await _call_tool("icx_boost", {"prompt": "fix the login crash"}))[0].text)
    bp = d["boosted_prompt"]
    assert "# ROLE" in bp and "# ACCEPTANCE CRITERIA" in bp     # CTO-grade structure, not the one-pass brief
    assert d["boost_meta"]["auto_refined"] is True
    assert "refine_note" in d and "icx_boost_refine" in d["refine_note"]


async def test_icx_boost_tool_auto_refine_skipped_for_trivial():
    import json
    from icx_engine.mcp_server import _call_tool
    d = json.loads((await _call_tool("icx_boost", {"prompt": "thanks"}))[0].text)
    assert d.get("skip") is True
    assert "boosted_prompt" not in d           # trivial skip stays cheap - no CTO prompt built


# -- MCP prompts primitive: icx-boost surfaced natively where the editor supports it ------------

async def test_list_prompts_exposes_icx_boost():
    from icx_engine.mcp_server import _list_prompts
    prompts = await _list_prompts()
    assert len(prompts) == 1
    assert prompts[0].name == "icx-boost"
    arg_names = {a.name for a in prompts[0].arguments}
    assert "prompt" in arg_names


async def test_get_prompt_returns_auto_refined_boosted_prompt():
    import json
    from icx_engine.mcp_server import _get_prompt
    res = await _get_prompt("icx-boost", {"prompt": "fix the login crash"})
    assert res.messages and res.messages[0].role == "user"
    d = json.loads(res.messages[0].content.text)
    assert "# ROLE" in d["boosted_prompt"]
    assert d["boost_meta"]["auto_refined"] is True


async def test_get_prompt_rejects_unknown_name():
    from icx_engine.mcp_server import _get_prompt
    with pytest.raises(ValueError):
        await _get_prompt("not-icx-boost", {"prompt": "x"})


async def test_get_prompt_rejects_empty_prompt():
    from icx_engine.mcp_server import _get_prompt
    with pytest.raises(ValueError):
        await _get_prompt("icx-boost", {"prompt": "   "})


# -- save_memory: skills fields removed from schema --------------------------------------------

def test_save_memory_schema_has_no_skills_fields():
    import asyncio
    from icx_engine.mcp_server import _all_tools_full
    tools = asyncio.run(_all_tools_full())
    save_tool = next(t for t in tools if t.name == "save_memory")
    props = save_tool.inputSchema["properties"]
    for removed in ("worth_remembering", "skill_name", "skill_procedure", "skill_pitfalls", "skill_verification"):
        assert removed not in props, f"{removed} should have been removed from save_memory's schema"


# -- icx_boost: skills.index attachment --------------------------------------------------------

async def test_icx_boost_includes_skills_index_when_a_skill_matches(tmp_path, monkeypatch):
    import json
    from icx_engine.mcp_server import _call_tool
    from icx_engine.skills.schema import SkillEntry
    from icx_engine.skills.storage import SkillStorage

    storage = SkillStorage(root=tmp_path)
    entry = SkillEntry(name="debugging-helper", description="A debugging skill.",
                       tags=["debugging"], title="Debugging Helper",
                       when_to_use="x", procedure="x", pitfalls="x", verification="x")
    entry.icx_hash = entry.compute_hash()
    storage.write(entry)

    monkeypatch.setattr("icx_engine.mcp_server.SkillStorage", lambda: storage)
    d = json.loads((await _call_tool("icx_boost", {"prompt": "debug this crash please"}))[0].text)
    assert "skills" in d
    names = [s["name"] for s in d["skills"]["index"]]
    assert "debugging-helper" in names


async def test_icx_boost_omits_skills_field_when_no_match(tmp_path, monkeypatch):
    import json
    from icx_engine.mcp_server import _call_tool
    from icx_engine.skills.storage import SkillStorage

    monkeypatch.setattr("icx_engine.mcp_server.SkillStorage", lambda: SkillStorage(root=tmp_path))
    d = json.loads((await _call_tool("icx_boost", {"prompt": "fix the login crash"}))[0].text)
    assert "skills" not in d


async def test_icx_boost_skills_lookup_never_breaks_boost_on_failure(monkeypatch):
    """If skill ranking blows up, icx_boost must still return its normal brief."""
    import json
    from icx_engine.mcp_server import _call_tool

    def _boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr("icx_engine.mcp_server.rank_skills", _boom)
    d = json.loads((await _call_tool("icx_boost", {"prompt": "fix the login crash"}))[0].text)
    assert not d.get("skip")
    assert d["archetype"] == "debugging"
    assert "skills" not in d


def test_skills_list_command_runs_with_empty_store(monkeypatch, tmp_path):
    from icx_engine.cli import app
    from icx_engine.skills.storage import SkillStorage
    monkeypatch.setattr("icx_engine.skills.storage.SkillStorage", lambda: SkillStorage(root=tmp_path))
    result = _runner.invoke(app, ["skills", "list"])
    assert result.exit_code == 0


def test_skills_list_command_shows_learned_skills(monkeypatch, tmp_path):
    from icx_engine.cli import app
    from icx_engine.skills.schema import SkillEntry
    from icx_engine.skills.storage import SkillStorage
    storage = SkillStorage(root=tmp_path)
    entry = SkillEntry(name="shown-skill", description="A shown skill.", tags=["x"],
                       title="Shown Skill", when_to_use="x", procedure="x", pitfalls="x",
                       verification="x")
    entry.icx_hash = entry.compute_hash()
    storage.write(entry)
    monkeypatch.setattr("icx_engine.skills.storage.SkillStorage", lambda: storage)
    result = _runner.invoke(app, ["skills", "list"])
    assert "shown-skill" in result.output


def test_save_memory_includes_related_skills_when_a_match_exists(mcp_config, tmp_path):
    import asyncio, json
    from icx_engine.mcp_server import _handle_save_memory
    from icx_engine.skills.schema import SkillEntry
    from icx_engine.skills.storage import SkillStorage

    storage = SkillStorage(root=tmp_path)
    entry = SkillEntry(name="jwt-fix", description="d", tags=["jwt-expiry"], title="JWT Fix",
                       when_to_use="x", procedure="x", pitfalls="x", verification="x")
    entry.icx_hash = entry.compute_hash()
    storage.write(entry)

    mock_mem = MagicMock()
    mock_mem.save.return_value = None
    mock_mem.verify_resolution.return_value = {"error": "entry not found", "issue_key": "PROJ-1"}

    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = mcp_config
        with patch("icx_engine.mcp_server._ensure_memory_manager", return_value=mock_mem):
            with patch("icx_engine.mcp_server.SkillStorage", lambda: storage):
                result = asyncio.run(_handle_save_memory(
                    "PROJ-1", "s", "p", "r", ["a.py"], ["jwt-expiry"], "Bug",
                    extra={"outcome_verified": True, "outcome_feedback_note": "confirmed"},
                ))
    data = json.loads(result)
    assert data["saved"] is True
    assert "related_skills" in data
    assert "jwt-fix" in [s["name"] for s in data["related_skills"]]


def test_save_memory_omits_related_skills_when_no_match(mcp_config, tmp_path):
    import asyncio, json
    from icx_engine.mcp_server import _handle_save_memory
    from icx_engine.skills.storage import SkillStorage

    mock_mem = MagicMock()
    mock_mem.save.return_value = None
    mock_mem.verify_resolution.return_value = {"error": "entry not found", "issue_key": "PROJ-2"}

    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = mcp_config
        with patch("icx_engine.mcp_server._ensure_memory_manager", return_value=mock_mem):
            with patch("icx_engine.mcp_server.SkillStorage", lambda: SkillStorage(root=tmp_path)):
                result = asyncio.run(_handle_save_memory(
                    "PROJ-2", "s", "p", "r", ["a.py"], ["some-tag"], "Bug",
                    extra={"outcome_verified": True, "outcome_feedback_note": "confirmed"},
                ))
    data = json.loads(result)
    assert data["saved"] is True
    assert "related_skills" not in data


def test_save_memory_related_skills_lookup_never_breaks_save(mcp_config):
    import asyncio, json
    from icx_engine.mcp_server import _handle_save_memory

    mock_mem = MagicMock()
    mock_mem.save.return_value = None
    mock_mem.verify_resolution.return_value = {"error": "entry not found", "issue_key": "PROJ-3"}

    def _boom(*a, **k):
        raise RuntimeError("boom")

    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = mcp_config
        with patch("icx_engine.mcp_server._ensure_memory_manager", return_value=mock_mem):
            with patch("icx_engine.mcp_server.rank_skills_for_tags", _boom):
                result = asyncio.run(_handle_save_memory(
                    "PROJ-3", "s", "p", "r", ["a.py"], ["t"], "Bug",
                    extra={"outcome_verified": True, "outcome_feedback_note": "confirmed"},
                ))
    data = json.loads(result)
    assert data["saved"] is True
    assert "related_skills" not in data


async def test_draft_skill_registered():
    from icx_engine.mcp_server import _all_tools_full
    tools = await _all_tools_full()
    assert any(t.name == "icx_draft_skill" for t in tools)


async def test_draft_skill_worthy_false_is_a_noop(tmp_path, monkeypatch):
    import json
    from icx_engine.mcp_server import _call_tool
    from icx_engine.skills.storage import SkillStorage
    monkeypatch.setattr("icx_engine.skills.mcp_tools.SkillStorage", lambda: SkillStorage(root=tmp_path))
    res = await _call_tool("icx_draft_skill", {"issue_key": "PROJ-1", "skill_worthy": False})
    data = json.loads(res[0].text)
    assert data == {"status": "skipped"}
    assert SkillStorage(root=tmp_path).list_all() == []


async def test_draft_skill_creates_when_worthy(tmp_path, monkeypatch):
    import json
    from icx_engine.mcp_server import _call_tool
    from icx_engine.skills.storage import SkillStorage
    from tests.memory.factories import make_verified_entry
    monkeypatch.setattr("icx_engine.skills.mcp_tools.SkillStorage", lambda: SkillStorage(root=tmp_path))

    class _FakeManager:
        def show(self, issue_key):
            return make_verified_entry(issue_key)
    monkeypatch.setattr("icx_engine.mcp_server._ensure_memory_manager", lambda: _FakeManager())

    res = await _call_tool("icx_draft_skill", {
        "issue_key": "PROJ-1", "skill_worthy": True, "skill_name": "New Skill",
        "description": "d", "when_to_use": "w", "procedure": "p", "verification": "v",
    })
    data = json.loads(res[0].text)
    assert data["status"] == "created"
    assert data["name"] == "new-skill"
    assert SkillStorage(root=tmp_path).read("new-skill") is not None


async def test_draft_skill_worthy_true_missing_required_fields_returns_error():
    import json
    from icx_engine.mcp_server import _call_tool
    res = await _call_tool("icx_draft_skill", {"issue_key": "PROJ-1", "skill_worthy": True})
    data = json.loads(res[0].text)
    assert "error" in data


async def test_draft_skill_unknown_issue_key_returns_error(monkeypatch):
    import json
    from icx_engine.mcp_server import _call_tool

    class _FakeManager:
        def show(self, issue_key):
            return None
    monkeypatch.setattr("icx_engine.mcp_server._ensure_memory_manager", lambda: _FakeManager())

    res = await _call_tool("icx_draft_skill", {
        "issue_key": "NOPE-1", "skill_worthy": True, "skill_name": "X",
        "description": "d", "when_to_use": "w", "procedure": "p", "verification": "v",
    })
    data = json.loads(res[0].text)
    assert "error" in data


async def test_draft_skill_unverified_entry_returns_error_even_if_worthy(monkeypatch):
    import json
    from icx_engine.mcp_server import _call_tool
    from tests.memory.factories import make_entry

    class _FakeManager:
        def show(self, issue_key):
            return make_entry(issue_key)   # outcome_verified defaults to False
    monkeypatch.setattr("icx_engine.mcp_server._ensure_memory_manager", lambda: _FakeManager())

    res = await _call_tool("icx_draft_skill", {
        "issue_key": "PROJ-1", "skill_worthy": True, "skill_name": "X",
        "description": "d", "when_to_use": "w", "procedure": "p", "verification": "v",
    })
    data = json.loads(res[0].text)
    assert "error" in data


async def test_draft_skill_uses_the_memory_executor_not_the_event_loop(tmp_path, monkeypatch):
    """The real regression this test guards against: icx_draft_skill's memory lookup must run on the
    dedicated single-worker memory executor thread, like every other memory access in this file -
    never called inline on the async event-loop thread. Confirmed with a fake manager whose show()
    records threading.get_ident() - the real (unpatched) executor is what makes that id differ from
    the test's own thread, distinguishing an inline call from one routed via run_in_executor."""
    import json
    import threading
    from icx_engine.mcp_server import _call_tool
    from icx_engine.skills.storage import SkillStorage
    from tests.memory.factories import make_verified_entry

    monkeypatch.setattr("icx_engine.skills.mcp_tools.SkillStorage", lambda: SkillStorage(root=tmp_path))

    main_thread_id = threading.get_ident()
    observed_thread_ids = []

    class _FakeManager:
        def show(self, issue_key):
            observed_thread_ids.append(threading.get_ident())
            return make_verified_entry(issue_key)

    monkeypatch.setattr("icx_engine.mcp_server._ensure_memory_manager", lambda: _FakeManager())

    res = await _call_tool("icx_draft_skill", {
        "issue_key": "PROJ-1", "skill_worthy": True, "skill_name": "Thread Test Skill",
        "description": "d", "when_to_use": "w", "procedure": "p", "verification": "v",
    })
    data = json.loads(res[0].text)
    assert data["status"] == "created"
    assert len(observed_thread_ids) == 1
    assert observed_thread_ids[0] != main_thread_id, (
        "show() ran on the event-loop thread instead of the dedicated memory executor thread - "
        "this is the exact regression this test exists to catch"
    )


async def test_dynamic_step_sequences_mandate_draft_skill_after_save_memory():
    """Every _icx_next STEP sequence that ends at save_memory must also mandate icx_draft_skill right
    after it - this is the enforcement mechanism, not an optional suggestion."""
    from icx_engine.mcp_server import _FAST_DESCRIPTION, _FULL_DESCRIPTION
    # The 9 dynamic instruction blocks share one literal substring right before save_memory; if this
    # substring still exists unmodified anywhere reachable from these two descriptions' construction
    # path, the enforcement text was not updated. This test targets the substring change directly:
    marker = "then save_memory, then IMMEDIATELY call icx_draft_skill"
    # These two constants are the STATIC tool-sequence docs (RULE 5 / numbered list), not the dynamic
    # per-graph-status blocks (which are built inside _handle_analyze_issue and not directly importable
    # as a module-level string) - assert the static ones here, and cover the dynamic ones via the
    # existing analyze-issue-flow tests that already inspect instruction text for other STEP content.
    assert "icx_draft_skill" in _FAST_DESCRIPTION
    assert "icx_draft_skill" in _FULL_DESCRIPTION


# -- icx_create_skill ----------------------------------------------------------------

async def test_create_skill_registered():
    from icx_engine.mcp_server import _all_tools_full
    tools = await _all_tools_full()
    assert any(t.name == "icx_create_skill" for t in tools)


async def test_create_skill_creates_without_project_key(tmp_path, monkeypatch):
    from icx_engine.mcp_server import _call_tool
    from icx_engine.skills.storage import SkillStorage
    storage = SkillStorage(root=tmp_path)
    monkeypatch.setattr("icx_engine.skills.mcp_tools.SkillStorage", lambda: storage)

    res = await _call_tool("icx_create_skill", {
        "name": "General Skill", "description": "d", "when_to_use": "w",
        "procedure": "p", "verification": "v",
    })
    data = json.loads(res[0].text)
    assert data["status"] == "created"
    assert data["name"] == "general-skill"
    saved = storage.read("general-skill")
    assert saved is not None
    assert saved.scope_hint == "generic"
    assert saved.origin_projects == []


async def test_create_skill_creates_with_project_key(tmp_path, monkeypatch):
    from icx_engine.mcp_server import _call_tool
    from icx_engine.skills.storage import SkillStorage
    storage = SkillStorage(root=tmp_path)
    monkeypatch.setattr("icx_engine.skills.mcp_tools.SkillStorage", lambda: storage)

    res = await _call_tool("icx_create_skill", {
        "name": "Repo Skill", "description": "d", "when_to_use": "w",
        "procedure": "p", "verification": "v", "project_key": "PROJ",
    })
    data = json.loads(res[0].text)
    assert data["status"] == "created"
    saved = storage.read("repo-skill")
    assert saved is not None
    assert saved.scope_hint == "repo-specific"
    assert saved.origin_projects == ["PROJ"]


async def test_create_skill_called_twice_updates_not_duplicates(tmp_path, monkeypatch):
    from icx_engine.mcp_server import _call_tool
    from icx_engine.skills.storage import SkillStorage
    storage = SkillStorage(root=tmp_path)
    monkeypatch.setattr("icx_engine.skills.mcp_tools.SkillStorage", lambda: storage)

    args = {"name": "Dup Skill", "description": "d", "when_to_use": "w", "procedure": "p", "verification": "v"}
    res1 = await _call_tool("icx_create_skill", args)
    data1 = json.loads(res1[0].text)
    assert data1["status"] == "created"

    res2 = await _call_tool("icx_create_skill", {**args, "description": "d2"})
    data2 = json.loads(res2[0].text)
    assert data2["status"] == "updated"
    assert len(storage.list_all()) == 1


async def test_create_skill_missing_required_field_returns_error():
    from icx_engine.mcp_server import _call_tool
    res = await _call_tool("icx_create_skill", {"name": "X", "description": "d"})
    data = json.loads(res[0].text)
    assert "error" in data


def test_skills_create_command_writes_a_skill_general_purpose(monkeypatch, tmp_path):
    """No issue_key involved - a human creating a general-purpose skill with no project tie."""
    from icx_engine.cli import app
    from icx_engine.skills.storage import SkillStorage
    storage = SkillStorage(root=tmp_path)
    monkeypatch.setattr("icx_engine.skills.storage.SkillStorage", lambda: storage)
    inputs = iter([
        "Commit Message Style",   # name
        "Writes clear, imperative commit messages.",   # description
        "When writing any git commit message.",         # when_to_use
        "Use imperative present tense, lowercase, no period.",   # procedure
        "",                                              # pitfalls (blank allowed)
        "Reviewed by a human before merge.",             # verification
        "",                                               # tags (blank allowed)
        "n",                                             # tied to a specific project? No
    ])
    monkeypatch.setattr("typer.prompt", lambda *a, **k: next(inputs))
    monkeypatch.setattr("typer.confirm", lambda *a, **k: False)
    result = _runner.invoke(app, ["skills", "create"])
    assert result.exit_code == 0
    saved = storage.read("commit-message-style")
    assert saved is not None
    assert saved.scope_hint == "generic"
    assert saved.origin_projects == []
    assert saved.tags == []


def test_skills_create_command_populates_tags_from_comma_separated_input(monkeypatch, tmp_path):
    """The tags prompt (added because rank_skills/rank_skills_for_tags score purely on tag overlap)
    must be asked and its comma-separated answer parsed into a lowercased list."""
    from icx_engine.cli import app
    from icx_engine.skills.storage import SkillStorage
    storage = SkillStorage(root=tmp_path)
    monkeypatch.setattr("icx_engine.skills.storage.SkillStorage", lambda: storage)
    inputs = iter([
        "JWT Retry Fix",   # name
        "Retries a JWT check on transient failure.",   # description
        "When a JWT validation call intermittently fails.",   # when_to_use
        "Wrap the JWT check in a bounded retry.",   # procedure
        "",                                              # pitfalls (blank allowed)
        "Verified against the flaky auth endpoint.",     # verification
        "JWT, Auth, retry ",                              # tags
        "n",                                             # tied to a specific project? No
    ])
    monkeypatch.setattr("typer.prompt", lambda *a, **k: next(inputs))
    monkeypatch.setattr("typer.confirm", lambda *a, **k: False)
    result = _runner.invoke(app, ["skills", "create"])
    assert result.exit_code == 0
    saved = storage.read("jwt-retry-fix")
    assert saved is not None
    assert saved.tags == ["jwt", "auth", "retry"]


def test_skills_delete_command_removes_named_skill(monkeypatch, tmp_path):
    from icx_engine.cli import app
    from icx_engine.skills.schema import SkillEntry
    from icx_engine.skills.storage import SkillStorage
    storage = SkillStorage(root=tmp_path)
    e = SkillEntry(name="to-delete", description="d", tags=["x"], title="to-delete",
                   when_to_use="w", procedure="p", pitfalls="x", verification="v")
    e.icx_hash = e.compute_hash()
    storage.write(e)
    monkeypatch.setattr("icx_engine.skills.storage.SkillStorage", lambda: storage)
    monkeypatch.setattr("typer.confirm", lambda *a, **k: True)
    result = _runner.invoke(app, ["skills", "delete", "to-delete"])
    assert result.exit_code == 0
    assert storage.read("to-delete") is None


def test_skills_delete_command_cancelled_keeps_skill(monkeypatch, tmp_path):
    from icx_engine.cli import app
    from icx_engine.skills.schema import SkillEntry
    from icx_engine.skills.storage import SkillStorage
    storage = SkillStorage(root=tmp_path)
    e = SkillEntry(name="keep-me", description="d", tags=["x"], title="keep-me",
                   when_to_use="w", procedure="p", pitfalls="x", verification="v")
    e.icx_hash = e.compute_hash()
    storage.write(e)
    monkeypatch.setattr("icx_engine.skills.storage.SkillStorage", lambda: storage)
    monkeypatch.setattr("typer.confirm", lambda *a, **k: False)
    result = _runner.invoke(app, ["skills", "delete", "keep-me"])
    assert result.exit_code == 0
    assert storage.read("keep-me") is not None


def test_skills_create_and_delete_present_in_full_help():
    from icx_engine.cli import _FULL_HELP
    assert "icx skills create" in _FULL_HELP
    assert "icx skills delete" in _FULL_HELP


def test_cached_querier_reuses_instance_for_unchanged_mtime(tmp_path, monkeypatch):
    from icx_engine import mcp_server

    build_count = {"n": 0}

    class _FakeQuerier:
        def __init__(self, path):
            build_count["n"] += 1

    monkeypatch.setattr("icx_engine.graph.query.GraphQuerier", _FakeQuerier)
    monkeypatch.setattr(mcp_server, "_QUERIER_CACHE", {})
    graph_json = tmp_path / "graph.json"
    graph_json.write_text("{}", encoding="utf-8")

    first = mcp_server._cached_querier(graph_json)
    second = mcp_server._cached_querier(graph_json)
    assert first is second
    assert build_count["n"] == 1


def test_cached_querier_concurrent_calls_construct_once(tmp_path, monkeypatch):
    """Regression test for the _QUERIER_CACHE race: concurrent misses on the same
    unchanged graph.json must not each construct their own GraphQuerier."""
    import threading
    from icx_engine import mcp_server

    build_count = {"n": 0}
    build_lock = threading.Lock()

    class _SlowFakeQuerier:
        def __init__(self, path):
            import time
            with build_lock:
                build_count["n"] += 1
            time.sleep(0.05)  # widen the race window

    monkeypatch.setattr("icx_engine.graph.query.GraphQuerier", _SlowFakeQuerier)
    monkeypatch.setattr(mcp_server, "_QUERIER_CACHE", {})
    graph_json = tmp_path / "graph.json"
    graph_json.write_text("{}", encoding="utf-8")

    results = []
    threads = [threading.Thread(target=lambda: results.append(mcp_server._cached_querier(graph_json)))
               for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert build_count["n"] == 1
    assert len({id(r) for r in results}) == 1


def test_sonar_scope_schema_exposes_rules_and_tags():
    from icx_engine.mcp_server import _SONAR_SCOPE_SCHEMA
    assert "rules" in _SONAR_SCOPE_SCHEMA["properties"]
    assert "tags" in _SONAR_SCOPE_SCHEMA["properties"]


def test_sonar_scope_args_passes_through_rules_and_tags():
    from icx_engine.mcp_server import _sonar_scope_args
    result = _sonar_scope_args({"project": "myproj", "rules": ["python:S1481"], "tags": ["security"]})
    assert result["rules"] == ["python:S1481"]
    assert result["tags"] == ["security"]


# -- Sonar completeness tools (top_files/history/analyses/rule/hotspot) --------

async def test_sonar_top_files_tool_registered():
    from icx_engine.mcp_server import _all_tools_full
    tools = await _all_tools_full()
    names = {t.name for t in tools}
    assert {"sonar_top_files", "sonar_history", "sonar_analyses", "sonar_rule", "sonar_rules", "sonar_hotspot"} <= names


async def test_sonar_rules_tool_registered():
    from icx_engine.mcp_server import _all_tools_full
    tools = await _all_tools_full()
    tool = next(t for t in tools if t.name == "sonar_rules")
    assert tool.inputSchema["required"] == []
    assert "language" in tool.inputSchema["properties"]
    assert "tags" in tool.inputSchema["properties"]
    assert "repositories" in tool.inputSchema["properties"]


async def test_sonar_top_files_requires_project_and_metric(monkeypatch):
    import json
    from icx_engine import mcp_server
    out = await mcp_server._call_tool("sonar_top_files", {"project": "myproj"})
    payload = json.loads(out[0].text)
    assert payload["ok"] is False
    assert "metric" in payload["error"].lower()


async def test_sonar_top_files_tool_disabled(monkeypatch):
    import json
    from icx_engine import mcp_server
    from icx_engine.sonar import service
    from icx_engine.models.config import AppConfig
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: AppConfig()))
    out = await mcp_server._call_tool("sonar_top_files", {"project": "myproj", "metric": "coverage"})
    payload = json.loads(out[0].text)
    assert payload["ok"] is False
    assert "sonar add" in payload["error"].lower()


async def test_sonar_top_files_limit_zero_defaults_to_20():
    import json
    from unittest.mock import patch, AsyncMock
    from icx_engine.mcp_server import _call_tool
    from icx_engine.sonar import service as svc
    fake_data = {"files": [{"file": "a.py", "value": 1}], "metric": "coverage"}
    mock_top_files = AsyncMock(return_value=fake_data)
    with patch.object(svc, "top_files", mock_top_files):
        out = await _call_tool("sonar_top_files", {"project": "myproj", "metric": "coverage", "limit": 0})
    payload = json.loads(out[0].text)
    assert payload["ok"] is True
    assert payload["data"] == fake_data                # not an empty/wrong result
    _, kwargs = mock_top_files.call_args
    assert kwargs["limit"] == 20                        # limit=0 coerced to the tool's default


async def test_sonar_history_requires_project_and_metrics(monkeypatch):
    import json
    from icx_engine import mcp_server
    out = await mcp_server._call_tool("sonar_history", {"project": "myproj"})
    payload = json.loads(out[0].text)
    assert payload["ok"] is False
    assert "metrics" in payload["error"].lower()


async def test_sonar_history_tool_disabled(monkeypatch):
    import json
    from icx_engine import mcp_server
    from icx_engine.sonar import service
    from icx_engine.models.config import AppConfig
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: AppConfig()))
    out = await mcp_server._call_tool("sonar_history", {"project": "myproj", "metrics": ["coverage"]})
    payload = json.loads(out[0].text)
    assert payload["ok"] is False
    assert "sonar add" in payload["error"].lower()


async def test_sonar_analyses_requires_project(monkeypatch):
    import json
    from icx_engine import mcp_server
    out = await mcp_server._call_tool("sonar_analyses", {})
    payload = json.loads(out[0].text)
    assert payload["ok"] is False
    assert "project" in payload["error"].lower()


async def test_sonar_analyses_tool_disabled(monkeypatch):
    import json
    from icx_engine import mcp_server
    from icx_engine.sonar import service
    from icx_engine.models.config import AppConfig
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: AppConfig()))
    out = await mcp_server._call_tool("sonar_analyses", {"project": "myproj"})
    payload = json.loads(out[0].text)
    assert payload["ok"] is False
    assert "sonar add" in payload["error"].lower()


async def test_sonar_rule_requires_rule_key(monkeypatch):
    import json
    from icx_engine import mcp_server
    out = await mcp_server._call_tool("sonar_rule", {})
    payload = json.loads(out[0].text)
    assert payload["ok"] is False
    assert "rule_key" in payload["error"].lower()


async def test_sonar_rule_tool_disabled(monkeypatch):
    import json
    from icx_engine import mcp_server
    from icx_engine.sonar import service
    from icx_engine.models.config import AppConfig
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: AppConfig()))
    out = await mcp_server._call_tool("sonar_rule", {"rule_key": "python:S1481"})
    payload = json.loads(out[0].text)
    assert payload["ok"] is False
    assert "sonar add" in payload["error"].lower()


async def test_sonar_hotspot_requires_hotspot_key(monkeypatch):
    import json
    from icx_engine import mcp_server
    out = await mcp_server._call_tool("sonar_hotspot", {})
    payload = json.loads(out[0].text)
    assert payload["ok"] is False
    assert "hotspot_key" in payload["error"].lower()


async def test_sonar_hotspot_tool_disabled(monkeypatch):
    import json
    from icx_engine import mcp_server
    from icx_engine.sonar import service
    from icx_engine.models.config import AppConfig
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: AppConfig()))
    out = await mcp_server._call_tool("sonar_hotspot", {"hotspot_key": "AWhX...key"})
    payload = json.loads(out[0].text)
    assert payload["ok"] is False
    assert "sonar add" in payload["error"].lower()


# -- Sonar medium/low-tier completeness tools (source/metrics/quality-gate-definition/
#    quality-profiles/issue-authors/issue-tags/issue-changelog/system-health/languages) ---

async def test_sonar_medium_tier_tools_registered():
    from icx_engine.mcp_server import _all_tools_full
    tools = await _all_tools_full()
    names = {t.name for t in tools}
    assert {
        "sonar_source", "sonar_metrics", "sonar_quality_gate_definition", "sonar_quality_profiles",
        "sonar_issue_authors", "sonar_issue_tags", "sonar_issue_changelog", "sonar_system_health", "sonar_languages",
    } <= names


async def test_sonar_source_requires_project_and_path(monkeypatch):
    import json
    from icx_engine import mcp_server
    out = await mcp_server._call_tool("sonar_source", {"project": "myproj"})
    payload = json.loads(out[0].text)
    assert payload["ok"] is False
    assert "path" in payload["error"].lower()


async def test_sonar_source_tool_disabled(monkeypatch):
    import json
    from icx_engine import mcp_server
    from icx_engine.sonar import service
    from icx_engine.models.config import AppConfig
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: AppConfig()))
    out = await mcp_server._call_tool("sonar_source", {"project": "myproj", "path": "src/a.py"})
    payload = json.loads(out[0].text)
    assert payload["ok"] is False
    assert "sonar add" in payload["error"].lower()


async def test_sonar_metrics_tool_callable_with_empty_args():
    import json
    from unittest.mock import patch, AsyncMock
    from icx_engine.mcp_server import _call_tool
    from icx_engine.sonar import service as svc
    fake_data = {"total": 0, "returned": 0, "metrics": []}
    mock_metrics = AsyncMock(return_value=fake_data)
    with patch.object(svc, "metrics", mock_metrics):
        out = await _call_tool("sonar_metrics", {})
    payload = json.loads(out[0].text)
    assert payload["ok"] is True
    assert payload["data"] == fake_data


async def test_sonar_metrics_tool_disabled(monkeypatch):
    import json
    from icx_engine import mcp_server
    from icx_engine.sonar import service
    from icx_engine.models.config import AppConfig
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: AppConfig()))
    out = await mcp_server._call_tool("sonar_metrics", {})
    payload = json.loads(out[0].text)
    assert payload["ok"] is False
    assert "sonar add" in payload["error"].lower()


async def test_sonar_quality_gate_definition_requires_project_or_gate_name(monkeypatch):
    import json
    from icx_engine import mcp_server
    out = await mcp_server._call_tool("sonar_quality_gate_definition", {})
    payload = json.loads(out[0].text)
    assert payload["ok"] is False
    assert "project" in payload["error"].lower() or "gate_name" in payload["error"].lower()


async def test_sonar_quality_gate_definition_tool_disabled(monkeypatch):
    import json
    from icx_engine import mcp_server
    from icx_engine.sonar import service
    from icx_engine.models.config import AppConfig
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: AppConfig()))
    out = await mcp_server._call_tool("sonar_quality_gate_definition", {"project": "myproj"})
    payload = json.loads(out[0].text)
    assert payload["ok"] is False
    assert "sonar add" in payload["error"].lower()


async def test_sonar_quality_profiles_tool_callable_with_empty_args():
    import json
    from unittest.mock import patch, AsyncMock
    from icx_engine.mcp_server import _call_tool
    from icx_engine.sonar import service as svc
    fake_data = {"profiles": []}
    mock_profiles = AsyncMock(return_value=fake_data)
    with patch.object(svc, "quality_profiles", mock_profiles):
        out = await _call_tool("sonar_quality_profiles", {})
    payload = json.loads(out[0].text)
    assert payload["ok"] is True
    assert payload["data"] == fake_data


async def test_sonar_quality_profiles_tool_disabled(monkeypatch):
    import json
    from icx_engine import mcp_server
    from icx_engine.sonar import service
    from icx_engine.models.config import AppConfig
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: AppConfig()))
    out = await mcp_server._call_tool("sonar_quality_profiles", {})
    payload = json.loads(out[0].text)
    assert payload["ok"] is False
    assert "sonar add" in payload["error"].lower()


async def test_sonar_issue_authors_tool_callable_with_empty_args():
    import json
    from unittest.mock import patch, AsyncMock
    from icx_engine.mcp_server import _call_tool
    from icx_engine.sonar import service as svc
    fake_data = {"authors": []}
    mock_authors = AsyncMock(return_value=fake_data)
    with patch.object(svc, "issue_authors", mock_authors):
        out = await _call_tool("sonar_issue_authors", {})
    payload = json.loads(out[0].text)
    assert payload["ok"] is True
    assert payload["data"] == fake_data


async def test_sonar_issue_authors_tool_disabled(monkeypatch):
    import json
    from icx_engine import mcp_server
    from icx_engine.sonar import service
    from icx_engine.models.config import AppConfig
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: AppConfig()))
    out = await mcp_server._call_tool("sonar_issue_authors", {})
    payload = json.loads(out[0].text)
    assert payload["ok"] is False
    assert "sonar add" in payload["error"].lower()


async def test_sonar_issue_tags_tool_callable_with_empty_args():
    import json
    from unittest.mock import patch, AsyncMock
    from icx_engine.mcp_server import _call_tool
    from icx_engine.sonar import service as svc
    fake_data = {"tags": []}
    mock_tags = AsyncMock(return_value=fake_data)
    with patch.object(svc, "issue_tags", mock_tags):
        out = await _call_tool("sonar_issue_tags", {})
    payload = json.loads(out[0].text)
    assert payload["ok"] is True
    assert payload["data"] == fake_data


async def test_sonar_issue_tags_tool_disabled(monkeypatch):
    import json
    from icx_engine import mcp_server
    from icx_engine.sonar import service
    from icx_engine.models.config import AppConfig
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: AppConfig()))
    out = await mcp_server._call_tool("sonar_issue_tags", {})
    payload = json.loads(out[0].text)
    assert payload["ok"] is False
    assert "sonar add" in payload["error"].lower()


async def test_sonar_issue_changelog_requires_issue_key(monkeypatch):
    import json
    from icx_engine import mcp_server
    out = await mcp_server._call_tool("sonar_issue_changelog", {})
    payload = json.loads(out[0].text)
    assert payload["ok"] is False
    assert "issue_key" in payload["error"].lower()


async def test_sonar_issue_changelog_tool_disabled(monkeypatch):
    import json
    from icx_engine import mcp_server
    from icx_engine.sonar import service
    from icx_engine.models.config import AppConfig
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: AppConfig()))
    out = await mcp_server._call_tool("sonar_issue_changelog", {"issue_key": "AWhX...issue"})
    payload = json.loads(out[0].text)
    assert payload["ok"] is False
    assert "sonar add" in payload["error"].lower()


async def test_sonar_system_health_tool_callable_with_empty_args():
    import json
    from unittest.mock import patch, AsyncMock
    from icx_engine.mcp_server import _call_tool
    from icx_engine.sonar import service as svc
    fake_data = {"health": "GREEN", "causes": []}
    mock_health = AsyncMock(return_value=fake_data)
    with patch.object(svc, "system_health", mock_health):
        out = await _call_tool("sonar_system_health", {})
    payload = json.loads(out[0].text)
    assert payload["ok"] is True
    assert payload["data"] == fake_data


async def test_sonar_system_health_tool_disabled(monkeypatch):
    import json
    from icx_engine import mcp_server
    from icx_engine.sonar import service
    from icx_engine.models.config import AppConfig
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: AppConfig()))
    out = await mcp_server._call_tool("sonar_system_health", {})
    payload = json.loads(out[0].text)
    assert payload["ok"] is False
    assert "sonar add" in payload["error"].lower()


async def test_sonar_languages_tool_callable_with_empty_args():
    import json
    from unittest.mock import patch, AsyncMock
    from icx_engine.mcp_server import _call_tool
    from icx_engine.sonar import service as svc
    fake_data = {"languages": []}
    mock_languages = AsyncMock(return_value=fake_data)
    with patch.object(svc, "languages", mock_languages):
        out = await _call_tool("sonar_languages", {})
    payload = json.loads(out[0].text)
    assert payload["ok"] is True
    assert payload["data"] == fake_data


async def test_sonar_languages_tool_disabled(monkeypatch):
    import json
    from icx_engine import mcp_server
    from icx_engine.sonar import service
    from icx_engine.models.config import AppConfig
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: AppConfig()))
    out = await mcp_server._call_tool("sonar_languages", {})
    payload = json.loads(out[0].text)
    assert payload["ok"] is False
    assert "sonar add" in payload["error"].lower()


# -- Tool description length ceiling -------------------------------------------------------
# Some MCP clients (Bedrock/OpenAI-style proxies) hard-clamp or reject a tool description over
# 2048 chars. New tools must stay under this; a handful of existing tools are known, deliberate
# exceptions - see developer.md's "What NOT to touch" table for why each one can't safely shrink
# further without regressing tests that pin down specific incident-derived instruction text.
_DESCRIPTION_LENGTH_CEILING = 2048
_DESCRIPTION_LENGTH_EXCEPTIONS = {
    "testing_resume_session",
    "testing_start_session",
    "jira_analyze_issue_fast",
}


async def test_no_new_tool_exceeds_description_length_ceiling():
    from icx_engine.mcp_server import _list_tools
    from icx_engine.models.config import AppConfig
    from unittest.mock import patch
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        tools = await _list_tools()
    offenders = {
        t.name: len(t.description) for t in tools
        if len(t.description) > _DESCRIPTION_LENGTH_CEILING and t.name not in _DESCRIPTION_LENGTH_EXCEPTIONS
    }
    assert offenders == {}, (
        f"Tool description(s) over {_DESCRIPTION_LENGTH_CEILING} chars: {offenders}. "
        "Either trim the description or, if it's a deliberate exception like the ones in "
        "_DESCRIPTION_LENGTH_EXCEPTIONS, add it there AND document why in developer.md."
    )


async def test_description_length_exceptions_list_is_still_accurate():
    """If one of the known exceptions ever gets successfully shrunk under the ceiling, this
    catches it so the exception list (and developer.md's note) gets cleaned up rather than
    silently going stale."""
    from icx_engine.mcp_server import _all_tools_full
    from icx_engine.models.config import AppConfig
    from unittest.mock import patch
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        tools = {t.name: t for t in await _all_tools_full()}
    for name in _DESCRIPTION_LENGTH_EXCEPTIONS:
        assert name in tools, f"{name} is listed as a length exception but no longer exists as a tool."
        assert len(tools[name].description) > _DESCRIPTION_LENGTH_CEILING, (
            f"{name} is under the ceiling now - remove it from _DESCRIPTION_LENGTH_EXCEPTIONS "
            "and from developer.md's note."
        )


# -- _call_tool telemetry wrapper ----------------------------------------------------------

async def test_call_tool_logs_successful_call(tmp_path, monkeypatch):
    from icx_engine import mcp_server
    from icx_engine.telemetry.logger import ToolCallLogger
    monkeypatch.setattr("icx_engine.telemetry.logger.ToolCallLogger", lambda: ToolCallLogger(root=tmp_path))
    await mcp_server._call_tool("git_check_branch_name_policy", {"repo_path": "/fake", "branch_name": "x"})

    files = list(tmp_path.rglob("tool_calls.jsonl"))
    assert len(files) == 1
    import json
    record = json.loads(files[0].read_text(encoding="utf-8").strip().splitlines()[0])
    assert record["tool"] == "git_check_branch_name_policy"
    assert "duration_ms" in record


async def test_call_tool_logs_tool_reported_error_as_not_ok(tmp_path, monkeypatch):
    from icx_engine import mcp_server
    from icx_engine.telemetry.logger import ToolCallLogger
    monkeypatch.setattr("icx_engine.telemetry.logger.ToolCallLogger", lambda: ToolCallLogger(root=tmp_path))
    # missing repo_path -> the tool itself returns ok:false, no exception raised
    await mcp_server._call_tool("git_check_branch_name_policy", {})

    files = list(tmp_path.rglob("tool_calls.jsonl"))
    import json
    record = json.loads(files[0].read_text(encoding="utf-8").strip().splitlines()[0])
    assert record["ok"] is False
    assert record["error_type"] == "tool_error"


async def test_call_tool_logs_and_reraises_on_unhandled_exception(tmp_path, monkeypatch):
    from icx_engine import mcp_server
    from icx_engine.telemetry.logger import ToolCallLogger

    async def _boom(name, args):
        raise RuntimeError("simulated dispatch crash")
    monkeypatch.setattr(mcp_server, "_call_tool_impl", _boom)
    monkeypatch.setattr("icx_engine.telemetry.logger.ToolCallLogger", lambda: ToolCallLogger(root=tmp_path))

    with pytest.raises(RuntimeError, match="simulated dispatch crash"):
        await mcp_server._call_tool("anything", {})

    files = list(tmp_path.rglob("tool_calls.jsonl"))
    import json
    record = json.loads(files[0].read_text(encoding="utf-8").strip().splitlines()[0])
    assert record["ok"] is False
    assert record["error_type"] == "RuntimeError"


# -- icx_find_tools / icx_call_tool - discovery + forwarding dispatch ----------------------
# tools/list now advertises only _CORE_TOOL_ORDER (8 tools); every other tool stays fully
# callable (via icx_call_tool, or directly by name) and discoverable via icx_find_tools, which
# searches _all_tools_full()'s complete, unfiltered set. See mcp_server.py's _list_tools/
# _all_tools_full/_module_index/_dispatch_find_tools/_dispatch_call_tool/_dispatch_with_telemetry.

def _tmp_git_repo(tmp_path):
    import subprocess
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ["init", "-b", "main"],
        ["config", "user.email", "t@e.com"],
        ["config", "user.name", "T"],
    ):
        subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True)
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=str(repo), check=True, capture_output=True)
    return repo


async def test_list_tools_returns_only_the_core_set():
    from icx_engine.mcp_server import _list_tools, _CORE_TOOL_ORDER
    tools = await _list_tools()
    assert [t.name for t in tools] == _CORE_TOOL_ORDER
    assert len(tools) == 8


async def test_all_tools_full_still_has_every_tool():
    from icx_engine.mcp_server import _all_tools_full, _list_tools
    full = await _all_tools_full()
    core = await _list_tools()
    # 165 original tools + the 2 new discovery/dispatch tools themselves
    assert len(full) == 167
    assert {t.name for t in core} <= {t.name for t in full}


async def test_formerly_visible_tools_still_directly_callable_by_name():
    """Critical regression: the LISTING shrank, the CALLING capability did not. 5 tools across
    different modules, no longer in tools/list, must still work exactly as before when called
    directly through _call_tool_impl/_call_tool - not just via icx_call_tool."""
    from icx_engine.mcp_server import _call_tool_impl, _list_tools
    core_names = {t.name for t in await _list_tools()}
    probes = [
        ("git_check_branch_name_policy", {"repo_path": "/nonexistent", "branch_name": "x"}),
        ("memory_search", {"query": "x"}),
        ("sonar_projects", {}),
        ("gitlab_list_tags", {"project": "1"}),
        ("graph_important_nodes", {"project_path": "/nonexistent"}),
    ]
    for name, args in probes:
        assert name not in core_names, f"{name} unexpectedly still in the core tools/list set"
        result = await _call_tool_impl(name, args)
        payload = json.loads(result[0].text)
        assert isinstance(payload, dict) and payload, f"{name} returned an unusable response: {payload}"


async def test_find_tools_module_directory_with_no_args():
    from icx_engine.mcp_server import _call_tool_impl
    result = await _call_tool_impl("icx_find_tools", {})
    payload = json.loads(result[0].text)
    assert payload["ok"] is True
    names = {m["name"] for m in payload["modules"]}
    for expected in ("git", "gitlab", "jira", "workstatus", "sonar", "graph", "memory", "testing", "skills", "boost", "core"):
        assert expected in names


async def test_find_tools_by_module():
    from icx_engine.mcp_server import _call_tool_impl
    for module, min_count in (("git", 40), ("gitlab", 10), ("memory", 5)):
        result = await _call_tool_impl("icx_find_tools", {"module": module})
        payload = json.loads(result[0].text)
        assert payload["ok"] is True
        assert len(payload["tools"]) >= min_count
        for t in payload["tools"]:
            assert "name" in t and "description" in t and "inputSchema" in t


async def test_find_tools_unknown_module_returns_guidance_not_empty():
    from icx_engine.mcp_server import _call_tool_impl
    result = await _call_tool_impl("icx_find_tools", {"module": "not_a_real_module"})
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert "Valid modules" in payload["error"]


async def test_find_tools_by_query_finds_known_tool():
    from icx_engine.mcp_server import _call_tool_impl
    result = await _call_tool_impl("icx_find_tools", {"query": "push"})
    payload = json.loads(result[0].text)
    assert payload["ok"] is True
    assert "git_push" in {t["name"] for t in payload["tools"]}


async def test_find_tools_query_no_match_returns_guidance():
    from icx_engine.mcp_server import _call_tool_impl
    result = await _call_tool_impl("icx_find_tools", {"query": "zzz_totally_no_such_tool_zzz"})
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert "No tools matched" in payload["error"]


async def test_call_tool_forwards_identically_to_a_direct_call():
    from icx_engine.mcp_server import _call_tool_impl
    args = {"repo_path": "/nonexistent", "branch_name": "feature/x-ABC-1"}
    direct = await _call_tool_impl("git_check_branch_name_policy", args)
    forwarded = await _call_tool_impl("icx_call_tool", {"tool_name": "git_check_branch_name_policy", "arguments": args})
    assert direct[0].text == forwarded[0].text


async def test_call_tool_missing_tool_name_returns_named_error():
    from icx_engine.mcp_server import _call_tool_impl
    result = await _call_tool_impl("icx_call_tool", {})
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert "tool_name" in payload["error"]


async def test_call_tool_unknown_tool_name_returns_clean_error_not_crash():
    from icx_engine.mcp_server import _call_tool_impl
    result = await _call_tool_impl("icx_call_tool", {"tool_name": "definitely_not_a_real_tool", "arguments": {}})
    payload = json.loads(result[0].text)
    assert "error" in payload


async def test_call_tool_rejects_non_object_arguments():
    from icx_engine.mcp_server import _call_tool_impl
    result = await _call_tool_impl("icx_call_tool", {"tool_name": "git_repo_status", "arguments": "not-an-object"})
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert "arguments" in payload["error"]


async def test_call_tool_omitted_arguments_defaults_to_empty_dict(tmp_path):
    from icx_engine.mcp_server import _call_tool_impl
    repo = _tmp_git_repo(tmp_path)
    result = await _call_tool_impl("icx_call_tool", {"tool_name": "git_repo_status", "arguments": {"repo_path": str(repo)}})
    payload = json.loads(result[0].text)
    assert payload["ok"] is True


async def test_call_tool_preserves_confirm_token_gating(tmp_path):
    """A confirm_token-gated tool reached through icx_call_tool must still require the two-call
    pattern - forwarding must not bypass gating."""
    from icx_engine.mcp_server import _call_tool_impl
    from icx_engine.git.gitcmd import stage_files
    repo = _tmp_git_repo(tmp_path)
    (repo / "new.txt").write_text("x", encoding="utf-8")
    first = await _call_tool_impl("icx_call_tool", {
        "tool_name": "git_stage_and_commit",
        "arguments": {"repo_path": str(repo), "files": ["new.txt"], "message": "add new.txt", "ticket_key": None},
    })
    payload = json.loads(first[0].text)
    assert payload.get("status") == "pending_confirmation"
    assert "token" in payload
    # not committed yet - a second call is required
    log = __import__("subprocess").run(["git", "log", "--oneline"], cwd=str(repo), capture_output=True, text=True)
    assert "add new.txt" not in log.stdout


async def test_call_tool_via_native_entry_point_logs_the_real_inner_tool(tmp_path, monkeypatch):
    """icx_call_tool reached through the native @server.call_tool() entry point (_call_tool, not
    _call_tool_impl directly) must produce a telemetry record for the REAL inner tool it forwarded
    to, not just for icx_call_tool itself."""
    from icx_engine import mcp_server
    from icx_engine.telemetry.logger import ToolCallLogger
    monkeypatch.setattr("icx_engine.telemetry.logger.ToolCallLogger", lambda: ToolCallLogger(root=tmp_path))
    await mcp_server._call_tool("icx_call_tool", {
        "tool_name": "git_check_branch_name_policy",
        "arguments": {"repo_path": "/nonexistent", "branch_name": "x"},
    })
    files = list(tmp_path.rglob("tool_calls.jsonl"))
    assert len(files) == 1
    records = [json.loads(l) for l in files[0].read_text(encoding="utf-8").strip().splitlines()]
    tool_names_logged = {r["tool"] for r in records}
    assert "git_check_branch_name_policy" in tool_names_logged
    assert "icx_call_tool" in tool_names_logged
    assert len(records) == 2
