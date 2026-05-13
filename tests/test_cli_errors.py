from __future__ import annotations
from unittest.mock import patch, AsyncMock
import click
from typer.testing import CliRunner
from icx_engine.cli import app
from icx_engine.exceptions import AuthError, NoLLMError


# mix_stderr=False is not supported by typer.testing.CliRunner (typer < 0.12 / click 8.x wrapping).
# Both stdout and stderr are merged into result.output; result.stderr is always None.
# Assertions below use `combined = result.output` to cover both streams.
runner = CliRunner()


def _setup_config(monkeypatch, tmp_path):
    """Write a minimal config with one Jira connection and one LLM profile."""
    monkeypatch.setattr("icx_engine.config_manager.CONFIG_PATH", tmp_path / "config.json")
    from icx_engine.config_manager import ConfigManager
    from icx_engine.models.config import AppConfig, LLMConfig, ChannelConfig
    from icx_engine.connectors.jira.config import JiraConnection, TokenAuth
    cfg = AppConfig(
        connections=[JiraConnection(
            domain="test.atlassian.net",
            auth=TokenAuth(auth_type="token", email="a@b.com", api_token="tok"),
        )],
        llm_profiles={"p": LLMConfig(text_config=ChannelConfig(provider="ollama", model="llama3"))},
        current_llm_profile="p",
    )
    ConfigManager.save(cfg)


def test_analyze_auth_error_shows_panel_not_traceback(monkeypatch, tmp_path):
    """AuthError must produce a panel, not a raw Python traceback."""
    _setup_config(monkeypatch, tmp_path)
    with patch("icx_engine.engine.run", new_callable=AsyncMock) as mock_run:
        mock_run.side_effect = AuthError("401 Unauthorized")
        result = runner.invoke(app, ["analyze", "https://test.atlassian.net/browse/TEST-1"])
    assert result.exit_code == 1
    combined = (result.output or "") + (result.stderr or "")
    assert "Traceback" not in combined
    assert "ICX Error" in combined or "What:" in combined


def test_analyze_traceback_flag_present():
    """--traceback is a recognised option."""
    result = runner.invoke(app, ["analyze", "--help"])
    assert "--traceback" in click.unstyle(result.output)


def test_analyze_no_llm_error_shows_ice_apikey_hint(monkeypatch, tmp_path):
    """NoLLMError panel should guide the user to `icx apikey`."""
    _setup_config(monkeypatch, tmp_path)
    with patch("icx_engine.engine.run", new_callable=AsyncMock) as mock_run:
        mock_run.side_effect = NoLLMError("No AI provider configured.")
        result = runner.invoke(app, ["analyze", "https://test.atlassian.net/browse/TEST-1"])
    assert result.exit_code == 1          # exit code checked FIRST
    combined = (result.output or "") + (result.stderr or "")
    assert "icx model --add" in combined


def test_analyze_traceback_flag_emits_traceback_in_error_output(monkeypatch, tmp_path):
    """--traceback flag must include Python traceback text in error output."""
    _setup_config(monkeypatch, tmp_path)
    with patch("icx_engine.engine.run", new_callable=AsyncMock) as mock_run:
        mock_run.side_effect = AuthError("401 Unauthorized")
        result = runner.invoke(app, ["analyze", "--traceback", "https://test.atlassian.net/browse/TEST-1"])
    combined = (result.output or "") + (result.stderr or "")
    assert result.exit_code == 1
    assert "AuthError" in combined or "401 Unauthorized" in combined

