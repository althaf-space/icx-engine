from __future__ import annotations
from io import StringIO
import pytest
from rich.console import Console
from icx_engine.error_display import render_icx_error
from icx_engine.exceptions import (
    AuthError, ContextBuildError, ICXError, IssueNotFound, NoLLMError,
    NoConnectionError, OAuthRefreshError, RateLimited, SourceUnavailable,
    InvalidInput,
)


def _capture(exc: Exception, show_traceback: bool = False) -> str:
    """Raise exc, call render_icx_error inside the handler, return panel text."""
    buf = StringIO()
    con = Console(file=buf, highlight=False, markup=False, width=120)
    try:
        raise exc
    except Exception as caught:
        render_icx_error(caught, con, show_traceback=show_traceback)
    return buf.getvalue()


def test_render_auth_error_all_three_sections():
    out = _capture(AuthError("401 from Jira"))
    assert "401 from Jira" in out
    assert "What:" in out
    assert "Why:" in out
    assert "How:" in out
    assert "Authentication or permission failure" in out
    assert "icx connection --add" in out


def test_render_issue_not_found():
    out = _capture(IssueNotFound("ABC-999 not found"))
    assert "ABC-999 not found" in out
    assert "issue key does not exist" in out


def test_render_rate_limited():
    out = _capture(RateLimited("too many requests"))
    assert "rate limit" in out.lower()


def test_render_source_unavailable():
    out = _capture(SourceUnavailable("503"))
    assert "server error" in out.lower() or "unreachable" in out.lower()


def test_render_no_llm_error():
    out = _capture(NoLLMError("no provider configured"))
    assert "no provider configured" in out
    assert "icx model --add" in out


def test_render_no_connection_error():
    out = _capture(NoConnectionError("no connection for example.com"))
    assert "icx connection --add" in out


def test_render_oauth_refresh_error():
    out = _capture(OAuthRefreshError("token expired"))
    assert "OAuth" in out or "token" in out.lower()


def test_render_invalid_input():
    out = _capture(InvalidInput("not a valid key"))
    assert "not a valid key" in out
    assert "ABC-123" in out


def test_render_context_build_error_raw_output_hidden_without_traceback():
    exc = ContextBuildError("parse failed", raw_output="not-json-content")
    out = _capture(exc, show_traceback=False)
    assert "not-json-content" not in out


def test_render_context_build_error_raw_output_shown_with_traceback():
    exc = ContextBuildError("parse failed", raw_output="not-json-content")
    out = _capture(exc, show_traceback=True)
    assert "not-json-content" in out


def test_render_unknown_ice_error_uses_fallback():
    out = _capture(ICXError("edge case"))
    assert "edge case" in out


def test_render_non_ice_exception_uses_fallback():
    out = _capture(RuntimeError("unexpected"))
    assert "unexpected" in out


# ── Context-aware AuthError guidance ─────────────────────────────────────────

@pytest.mark.parametrize("msg,expected_how", [
    ("Gemini API key is invalid", "icx model --add"),
    ("OpenAI API key is invalid or expired", "icx model --add"),
    ("Anthropic API key is invalid", "icx model --add"),
    ("xAI API key is invalid or expired", "icx model --add"),
    ("NIM API key rejected", "icx model --add"),
    ("Grok authentication failed", "icx model --add"),
])
def test_auth_error_ai_provider_shows_model_command(msg, expected_how, capsys):
    from io import StringIO
    from rich.console import Console as RichConsole
    from icx_engine.exceptions import AuthError
    from icx_engine.error_display import render_icx_error

    buf = StringIO()
    console = RichConsole(file=buf, highlight=False)
    render_icx_error(AuthError(msg), console)
    output = buf.getvalue()
    assert expected_how in output
    assert "icx connection --add" not in output


def test_auth_error_jira_shows_connection_command():
    from io import StringIO
    from rich.console import Console as RichConsole
    from icx_engine.exceptions import AuthError
    from icx_engine.error_display import render_icx_error

    buf = StringIO()
    console = RichConsole(file=buf, highlight=False)
    render_icx_error(AuthError("401 Unauthorized from Jira API"), console)
    output = buf.getvalue()
    assert "icx connection --add" in output
    assert "icx model --add" not in output


def test_auth_error_case_insensitive_detection():
    from io import StringIO
    from rich.console import Console as RichConsole
    from icx_engine.exceptions import AuthError
    from icx_engine.error_display import render_icx_error

    buf = StringIO()
    console = RichConsole(file=buf, highlight=False)
    render_icx_error(AuthError("GEMINI key rejected"), console)
    output = buf.getvalue()
    assert "icx model --add" in output

