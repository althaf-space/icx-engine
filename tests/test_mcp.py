"""Phase 3 tests - MCP host config management, MCP server handler, CLI mcp commands."""
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


# ── helpers ───────────────────────────────────────────────────────────────────

def _codex_host(tmp_path: Path) -> MCPHost:
    return MCPHost(
        "codex", "Codex",
        tmp_path / ".codex" / "config.toml",
        tmp_path / ".codex",
        "toml",
    )


# ── path helpers ──────────────────────────────────────────────────────────────

def test_home_indirection_is_monkeypatchable(monkeypatch, tmp_path):
    monkeypatch.setattr("icx_engine.mcp_hosts._home", lambda: tmp_path)
    from icx_engine.mcp_hosts import _home
    assert _home() == tmp_path


# ── WriteResult ───────────────────────────────────────────────────────────────

def test_write_result_normal_path(tmp_path):
    wr = WriteResult(path=tmp_path / "mcp.json", fallback=False)
    assert wr.fallback is False
    assert wr.path == tmp_path / "mcp.json"


def test_write_result_fallback_path(tmp_path):
    wr = WriteResult(path=tmp_path / ".mcp.json", fallback=True)
    assert wr.fallback is True


# ── list_hosts ────────────────────────────────────────────────────────────────

def test_list_hosts_returns_five_agents(monkeypatch, tmp_path):
    monkeypatch.setattr("icx_engine.mcp_hosts._home", lambda: tmp_path)
    names = [h.name for h in list_hosts()]
    assert set(names) == {"claude", "cursor", "windsurf", "codex", "antigravity"}


def test_list_hosts_no_cwd_param():
    import inspect
    sig = inspect.signature(list_hosts)
    assert "cwd" not in sig.parameters


def test_list_hosts_claude_config_is_global_settings(monkeypatch, tmp_path):
    monkeypatch.setattr("icx_engine.mcp_hosts._home", lambda: tmp_path)
    claude = next(h for h in list_hosts() if h.name == "claude")
    assert claude.config_path == tmp_path / ".claude" / "settings.json"
    assert claude.detect_path == tmp_path / ".claude"


def test_write_icx_entry_claude_merges_into_existing_settings(monkeypatch, tmp_path):
    monkeypatch.setattr("icx_engine.mcp_hosts._home", lambda: tmp_path)
    host = get_host("claude")
    host.detect_path.mkdir(parents=True, exist_ok=True)
    host.config_path.write_text(json.dumps({"hooks": {"PreToolUse": []}, "theme": "dark"}))
    write_icx_entry(host)
    raw = json.loads(host.config_path.read_text())
    assert raw["hooks"] == {"PreToolUse": []}
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


# ── detect_installed_hosts ────────────────────────────────────────────────────

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


# ── get_host ──────────────────────────────────────────────────────────────────

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


# ── write_icx_entry (JSON) ────────────────────────────────────────────────────

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
    raw = json.loads(result.path.read_text())
    assert raw["mcpServers"]["icx"] == ICX_MCP_ENTRY


def test_write_icx_entry_fallback_when_detect_missing(monkeypatch, tmp_path):
    monkeypatch.setattr("icx_engine.mcp_hosts._home", lambda: tmp_path)
    monkeypatch.chdir(tmp_path)
    host = get_host("cursor")
    assert not host.detect_path.exists()
    result = write_icx_entry(host)
    assert result.fallback is True
    assert result.path == tmp_path / ".mcp.json"
    raw = json.loads(result.path.read_text())
    assert raw["mcpServers"]["icx"] == ICX_MCP_ENTRY


def test_write_icx_entry_merges_with_existing_config(monkeypatch, tmp_path):
    monkeypatch.setattr("icx_engine.mcp_hosts._home", lambda: tmp_path)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".cursor").mkdir()
    host = get_host("cursor")
    host.config_path.parent.mkdir(parents=True, exist_ok=True)
    host.config_path.write_text(json.dumps({"mcpServers": {"other-tool": {"command": "other"}}}))
    write_icx_entry(host)
    raw = json.loads(host.config_path.read_text())
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
    raw = json.loads(host.config_path.read_text())
    assert list(raw["mcpServers"].keys()).count("icx") == 1


def test_write_icx_entry_windsurf_writes_json(monkeypatch, tmp_path):
    monkeypatch.setattr("icx_engine.mcp_hosts._home", lambda: tmp_path)
    monkeypatch.chdir(tmp_path)
    host = get_host("windsurf")
    host.detect_path.mkdir(parents=True, exist_ok=True)
    result = write_icx_entry(host)
    assert result.fallback is False
    raw = json.loads(result.path.read_text())
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
    host.config_path.write_text(json.dumps({"mcpServers": {"other-tool": {"command": "other"}}}))
    write_icx_entry(host)
    raw = json.loads(host.config_path.read_text())
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
    raw = json.loads(result.path.read_text())
    assert raw["mcpServers"]["icx"] == ICX_MCP_ENTRY


def test_write_icx_entry_antigravity_merges_existing_entries(monkeypatch, tmp_path):
    monkeypatch.setattr("icx_engine.mcp_hosts._home", lambda: tmp_path)
    host = get_host("antigravity")
    host.detect_path.mkdir(parents=True, exist_ok=True)
    host.config_path.parent.mkdir(parents=True, exist_ok=True)
    host.config_path.write_text(json.dumps({"mcpServers": {"gemini-tool": {"command": "gemini"}}}))
    write_icx_entry(host)
    raw = json.loads(host.config_path.read_text())
    assert "gemini-tool" in raw["mcpServers"]
    assert "icx" in raw["mcpServers"]


# ── write_icx_entry (TOML / Codex) ───────────────────────────────────────────

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


# ── remove_icx_entry (JSON) ───────────────────────────────────────────────────

def test_remove_icx_entry_removes_and_returns_true(monkeypatch, tmp_path):
    monkeypatch.setattr("icx_engine.mcp_hosts._home", lambda: tmp_path)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".cursor").mkdir()
    host = get_host("cursor")
    write_icx_entry(host)
    removed = remove_icx_entry(host)
    assert removed is True
    raw = json.loads(host.config_path.read_text())
    assert "icx" not in raw.get("mcpServers", {})


def test_remove_icx_entry_returns_false_when_not_present(monkeypatch, tmp_path):
    monkeypatch.setattr("icx_engine.mcp_hosts._home", lambda: tmp_path)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".cursor").mkdir()
    host = get_host("cursor")
    host.config_path.parent.mkdir(parents=True, exist_ok=True)
    host.config_path.write_text(json.dumps({"mcpServers": {}}))
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
    existing = json.loads(host.config_path.read_text())
    existing["mcpServers"]["other"] = {"command": "other"}
    host.config_path.write_text(json.dumps(existing))
    remove_icx_entry(host)
    raw = json.loads(host.config_path.read_text())
    assert "other" in raw["mcpServers"]


# ── remove_icx_entry (TOML) ───────────────────────────────────────────────────

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


# ── MCP server handler ────────────────────────────────────────────────────────

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
            with patch("icx_engine.mcp_server._get_graph_info", return_value={"status": "not_registered", "report_path": None, "access": "", "report_inline": "", "eta_seconds": None}):
                result = await _handle_analyze_issue("TEST-123", project_paths=["/projects/my-svc"])
    data = json.loads(result)
    assert "work_item" in data
    assert data["work_item"]["type"] == "Bug"
    assert "problem_summary" in data["work_item"]["analysis"]
    assert "memory" in data
    assert "graph" in data


async def test_handle_analyze_issue_returns_error_json_when_no_connection():
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        result = await _handle_analyze_issue("TEST-123", project_paths=["/projects/my-svc"])
    data = json.loads(result)
    assert "error" in data
    assert "type" in data


async def test_handle_analyze_issue_returns_error_json_on_invalid_key():
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        result = await _handle_analyze_issue("not-a-valid-key", project_paths=["/projects/my-svc"])
    data = json.loads(result)
    assert "error" in data


def test_run_mcp_server_is_callable():
    from icx_engine.mcp_server import run_mcp_server
    assert callable(run_mcp_server)


def test_server_registered_as_ice():
    from icx_engine.mcp_server import server
    assert server.name == "icx"


# ── Profile override - MCP ────────────────────────────────────────────────────

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
            with patch("icx_engine.mcp_server._get_graph_info", return_value={"status": "not_registered", "report_path": None, "access": "", "report_inline": "", "eta_seconds": None}):
                await _handle_analyze_issue("TEST-123", project_paths=["/projects/my-svc"], profile="personal")

    _, kwargs = mock_run.call_args
    assert kwargs.get("profile_override") == "personal"


async def test_handle_analyze_issue_unknown_profile_returns_error_json(mcp_config_with_llm):
    """An unknown profile_override surfaces as an error JSON, not an exception."""
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = mcp_config_with_llm
        result = await _handle_analyze_issue("TEST-123", project_paths=["/projects/my-svc"], profile="ghost-profile")
    data = json.loads(result)
    assert "error" in data
    assert "ghost-profile" in data["error"]


async def test_list_tools_includes_profile_names_in_description():
    from icx_engine.mcp_server import _list_tools
    config = AppConfig(
        llm_profiles={"fast": LLMConfig(text_config=ChannelConfig(provider="ollama", model="llama3")),
                      "pro":  LLMConfig(text_config=ChannelConfig(provider="ollama", model="mixtral"))},
    )
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = config
        tools = await _list_tools()

    fast_tool = next(t for t in tools if t.name == "analyze_issue_fast")
    description = fast_tool.description
    assert "fast" in description
    assert "pro" in description
    assert "profile" in description.lower()


async def test_list_tools_profile_hint_absent_when_no_profiles():
    from icx_engine.mcp_server import _list_tools
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
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
    respx.get(f"{JIRA_BASE_URL}/issue/TEST-123").mock(
        return_value=httpx.Response(200, json=JIRA_ISSUE_PAYLOAD)
    )
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = mcp_config_with_llm
        with patch("icx_engine.llm.ollama.AsyncOpenAI") as mock_cls:
            mock_client = MagicMock()
            mock_cls.return_value = mock_client
            mock_client.chat.completions.create = AsyncMock(return_value=_mock_openai_response())
            with patch("icx_engine.mcp_server._get_graph_info", return_value={"status": "not_registered", "report_path": None, "access": "", "report_inline": "", "eta_seconds": None}):
                result = await _handle_analyze_issue("TEST-123", project_paths=["/projects/my-svc"], skip_vision=True)
    data = json.loads(result)
    assert "work_item" in data
    assert data["work_item"]["type"] == "Bug"
    assert "pending_images" in data["work_item"]["analysis"]
    # image_paths always present in work_item (empty dict when no images)
    assert "image_paths" in data["work_item"]
    assert isinstance(data["work_item"]["image_paths"], dict)
    # raw base64 images must never appear in the analysis payload
    assert "images" not in data["work_item"]["analysis"]


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
            with patch("icx_engine.mcp_server._get_graph_info", return_value={"status": "not_registered", "report_path": None, "access": "", "report_inline": "", "eta_seconds": None}):
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
    assert "issue_key" in schema["required"]
    assert "resolution_note" in schema["required"]
    assert "files_changed" not in schema.get("required", [])
    assert "tags" not in schema.get("required", [])


# ── save_memory per-item input validation ─────────────────────────────────────

async def test_call_tool_save_memory_rejects_non_string_files_changed():
    """files_changed entries that are not strings must return an error."""
    from icx_engine.mcp_server import _call_tool
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        result = await _call_tool("save_memory", {
            "issue_key": "TEST-1",
            "resolution_note": "fixed it",
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
            "issue_key": "TEST-1",
            "resolution_note": "fixed it",
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
            "issue_key": "TEST-1",
            "resolution_note": "fixed it",
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
            "issue_key": "TEST-1",
            "resolution_note": "fixed it",
            "tags": ["x" * 257],
        })
    data = json.loads(result[0].text)
    assert "error" in data
    assert "tags" in data["error"]


# ── Tool count and schema ─────────────────────────────────────────────────────

async def test_list_tools_returns_three_tools():
    from icx_engine.mcp_server import _list_tools
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        tools = await _list_tools()

    assert len(tools) == 3
    names = {t.name for t in tools}
    assert names == {"analyze_issue_fast", "analyze_issue", "save_memory"}


async def test_analyze_tool_schema_requires_project_paths():
    from icx_engine.mcp_server import _list_tools
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        tools = await _list_tools()

    fast_tool = next(t for t in tools if t.name == "analyze_issue_fast")
    schema = fast_tool.inputSchema
    assert "project_paths" in schema["required"]
    assert "issue_ref" in schema["required"]
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
        assert "project_paths" in schema.get("required", []), f"{tool_name} project_paths must be required"
        assert schema["properties"]["project_paths"]["type"] == "array"
        assert "project_path" not in schema["properties"], f"{tool_name} old project_path must not exist"
        assert "additional_paths" not in schema["properties"], f"{tool_name} old additional_paths must not exist"


# ── _icx_next guidance hints ──────────────────────────────────────────────────

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
            with patch("icx_engine.mcp_server._get_graph_info", return_value={"status": "ready", "report_path": "/projects/my-svc/.icx/graphs/GRAPH_REPORT.md", "access": "pre-authorized", "eta_seconds": None}):
                result = await _handle_analyze_issue("TEST-123", project_paths=["/projects/my-svc"])
    data = json.loads(result)
    assert "_icx_next" in data
    assert "instruction" in data["_icx_next"]
    assert "graph" in data
    assert data["graph"]["status"] == "ready"
    assert "memory" in data
    assert "work_item" in data


@respx.mock
async def test_handle_save_memory_returns_saved_true(mcp_config):
    respx.get(f"{JIRA_BASE_URL}/issue/TEST-123").mock(
        return_value=httpx.Response(200, json=JIRA_ISSUE_PAYLOAD)
    )
    mock_mem = MagicMock()
    mock_mem.save.return_value = None

    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = mcp_config
        with patch("icx_engine.mcp_server._ensure_memory_manager", return_value=mock_mem):
            result = await _handle_save_memory(
                "TEST-123",
                "Fixed by increasing JWT TTL from 1h to 24h",
                ["src/auth/token.py"],
                ["auth", "jwt"],
            )

    data = json.loads(result)
    assert data["saved"] is True
    assert data["issue_key"] == "TEST-123"
    mock_mem.save.assert_called_once()


async def test_handle_save_memory_no_connection_returns_error():
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        result = await _handle_save_memory("TEST-123", "some fix", [], [])

    data = json.loads(result)
    assert "error" in data


@respx.mock
async def test_handle_save_memory_optional_fields_default_to_empty(mcp_config):
    respx.get(f"{JIRA_BASE_URL}/issue/TEST-123").mock(
        return_value=httpx.Response(200, json=JIRA_ISSUE_PAYLOAD)
    )
    mock_mem = MagicMock()

    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = mcp_config
        with patch("icx_engine.mcp_server._ensure_memory_manager", return_value=mock_mem):
            result = await _handle_save_memory("TEST-123", "some fix", [], [])

    data = json.loads(result)
    assert data["saved"] is True
    saved_entry = mock_mem.save.call_args[0][0]
    assert saved_entry.files_changed == []
    assert saved_entry.tags == []


# ── Profile override - CLI ────────────────────────────────────────────────────

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


# ── CLI: mcp commands ─────────────────────────────────────────────────────────

from icx_engine.cli import app
from typer.testing import CliRunner

_runner = CliRunner()


def test_mcp_setup_with_host_flag_writes_config(monkeypatch, tmp_path):
    monkeypatch.setattr("icx_engine.mcp_hosts._home", lambda: tmp_path)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".cursor").mkdir()
    result = _runner.invoke(app, ["mcp", "setup", "--host", "cursor"])
    assert result.exit_code == 0
    raw = json.loads((tmp_path / ".cursor" / "mcp.json").read_text())
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
    assert "✓" in result.output or "written" in result.output.lower()


def test_mcp_setup_fallback_prints_notice(monkeypatch, tmp_path):
    monkeypatch.setattr("icx_engine.mcp_hosts._home", lambda: tmp_path)
    monkeypatch.chdir(tmp_path)
    # cursor detect_path (tmp_path/.cursor) does NOT exist → fallback
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
    raw = json.loads((tmp_path / ".cursor" / "mcp.json").read_text())
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
            with patch("icx_engine.mcp_server._get_graph_info", return_value={
                "status": "ready",
                "report_path": "/projects/my-svc/GRAPH_REPORT.md",
                "access": "pre-authorized",
                "eta_seconds": None,
            }):
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
                with patch("icx_engine.mcp_server._get_graph_info", return_value=graph_info):
                    result = await _handle_analyze_issue("TEST-123", project_paths=["/projects/my-svc"])

        data = json.loads(result)
        instruction = data["_icx_next"]["instruction"]
        assert "pending_images" in instruction, f"Vision gate missing for graph_status={graph_status!r}"
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
            with patch("icx_engine.mcp_server._get_graph_info", return_value=ready_info):
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
    assert "graph" in data  # primary path still present for backward compat


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
            with patch("icx_engine.mcp_server._get_graph_info", return_value={"status": "not_registered", "report_path": None, "access": "", "report_inline": "", "eta_seconds": None}):
                result = await _handle_analyze_issue("TEST-123", project_paths=["/projects/my-svc"])

    data = json.loads(result)
    assert "graphs" not in data
    assert "graph" in data


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
            with patch("icx_engine.mcp_server._get_graph_info", return_value=ready_info):
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
            with patch("icx_engine.mcp_server._get_graph_info", return_value=ready_info):
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
            with patch("icx_engine.mcp_server._get_graph_info", return_value={
                "status": "not_registered", "report_path": None,
                "access": "", "report_inline": "", "eta_seconds": None,
            }):
                await _handle_analyze_issue("TEST-123", project_paths=["/projects/my-svc"])

    _, kwargs = mock_run.call_args
    assert kwargs.get("log") is not None
    assert callable(kwargs["log"])


def test_analyze_shows_missing_requirements_warning():
    """analyze prints ⚠ MISSING REQUIREMENTS when missing_information is non-empty."""
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


# ── CLI: --fast flag ──────────────────────────────────────────────────────────

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

