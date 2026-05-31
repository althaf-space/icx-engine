from __future__ import annotations
import json
import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from icx_engine.exceptions import AuthError, RateLimited, SourceUnavailable, ContextBuildError
from icx_engine.models.config import ChannelConfig
from icx_engine.models.output import RawIssueData


def _raw() -> RawIssueData:
    return RawIssueData(
        issue_key="T-1", issue_type="Bug", summary="s", description="d",
        comments=[], attachments=[], priority="High", status="Open", metadata={},
    )


def _nim_config() -> ChannelConfig:
    return ChannelConfig(provider="nim", model="deepseek-ai/deepseek-v4-flash", api_key="k")


def _openai_config() -> ChannelConfig:
    return ChannelConfig(provider="openai", model="gpt-4o", api_key="k")


def _anthropic_config() -> ChannelConfig:
    return ChannelConfig(provider="anthropic", model="claude-3-opus", api_key="k")


def _ollama_config() -> ChannelConfig:
    return ChannelConfig(provider="ollama", model="llama3")


# ── NIM ──────────────────────────────────────────────────────────────────────

def test_nim_auth_error_raises_auth_error():
    import openai
    from icx_engine.llm.nim import NIMProvider
    provider = NIMProvider(_nim_config())
    provider.client = MagicMock()
    provider.client.chat.completions.create = AsyncMock(
        side_effect=openai.AuthenticationError("bad key", response=MagicMock(status_code=401), body={})
    )
    with pytest.raises(AuthError):
        asyncio.run(provider.analyze(_raw()))


def test_nim_rate_limit_raises_rate_limited():
    import openai
    from icx_engine.llm.nim import NIMProvider
    provider = NIMProvider(_nim_config())
    provider.client = MagicMock()
    provider.client.chat.completions.create = AsyncMock(
        side_effect=openai.RateLimitError("429", response=MagicMock(status_code=429), body={})
    )
    with pytest.raises(RateLimited):
        asyncio.run(provider.analyze(_raw()))


def test_nim_connection_error_raises_source_unavailable():
    import openai
    from icx_engine.llm.nim import NIMProvider
    provider = NIMProvider(_nim_config())
    provider.client = MagicMock()
    provider.client.chat.completions.create = AsyncMock(
        side_effect=openai.APIConnectionError(request=MagicMock())
    )
    with pytest.raises(SourceUnavailable):
        asyncio.run(provider.analyze(_raw()))


def test_nim_405b_model_hint_in_context_build_error():
    from icx_engine.llm.nim import NIMProvider
    cfg = ChannelConfig(provider="nim", model="deepseek-ai/deepseek-r1-405b", api_key="k")
    provider = NIMProvider(cfg)
    provider.client = MagicMock()
    mock_resp = MagicMock()
    mock_resp.choices = [MagicMock(message=MagicMock(content="<think>...</think> not json"))]
    provider.client.chat.completions.create = AsyncMock(return_value=mock_resp)
    with pytest.raises(ContextBuildError) as exc_info:
        asyncio.run(provider.analyze(_raw()))
    assert "405B" in str(exc_info.value) or "reasoning" in str(exc_info.value).lower()


# ── OpenAI ───────────────────────────────────────────────────────────────────

def test_openai_auth_error_raises_auth_error():
    import openai
    from icx_engine.llm.openai import OpenAIProvider
    provider = OpenAIProvider(_openai_config())
    provider.client = MagicMock()
    provider.client.chat.completions.create = AsyncMock(
        side_effect=openai.AuthenticationError("bad key", response=MagicMock(status_code=401), body={})
    )
    with pytest.raises(AuthError):
        asyncio.run(provider.analyze(_raw()))


def test_openai_rate_limit_raises_rate_limited():
    import openai
    from icx_engine.llm.openai import OpenAIProvider
    provider = OpenAIProvider(_openai_config())
    provider.client = MagicMock()
    provider.client.chat.completions.create = AsyncMock(
        side_effect=openai.RateLimitError("429", response=MagicMock(status_code=429), body={})
    )
    with pytest.raises(RateLimited):
        asyncio.run(provider.analyze(_raw()))


def test_openai_connection_error_raises_source_unavailable():
    import openai
    from icx_engine.llm.openai import OpenAIProvider
    provider = OpenAIProvider(_openai_config())
    provider.client = MagicMock()
    provider.client.chat.completions.create = AsyncMock(
        side_effect=openai.APIConnectionError(request=MagicMock())
    )
    with pytest.raises(SourceUnavailable):
        asyncio.run(provider.analyze(_raw()))


# ── Anthropic ────────────────────────────────────────────────────────────────

def test_anthropic_auth_error_raises_auth_error():
    import anthropic
    from icx_engine.llm.anthropic import AnthropicProvider
    provider = AnthropicProvider(_anthropic_config())
    provider.client = MagicMock()
    provider.client.messages.create = AsyncMock(
        side_effect=anthropic.AuthenticationError(
            message="bad key", response=MagicMock(status_code=401), body={}
        )
    )
    with pytest.raises(AuthError):
        asyncio.run(provider.analyze(_raw()))


def test_anthropic_rate_limit_raises_rate_limited():
    import anthropic
    from icx_engine.llm.anthropic import AnthropicProvider
    provider = AnthropicProvider(_anthropic_config())
    provider.client = MagicMock()
    provider.client.messages.create = AsyncMock(
        side_effect=anthropic.RateLimitError(
            message="429", response=MagicMock(status_code=429), body={}
        )
    )
    with pytest.raises(RateLimited):
        asyncio.run(provider.analyze(_raw()))


def test_anthropic_connection_error_raises_source_unavailable():
    import anthropic
    from icx_engine.llm.anthropic import AnthropicProvider
    provider = AnthropicProvider(_anthropic_config())
    provider.client = MagicMock()
    provider.client.messages.create = AsyncMock(
        side_effect=anthropic.APIConnectionError(request=MagicMock())
    )
    with pytest.raises(SourceUnavailable):
        asyncio.run(provider.analyze(_raw()))


# ── Ollama ───────────────────────────────────────────────────────────────────

def test_ollama_connection_error_raises_source_unavailable():
    import openai
    from icx_engine.llm.ollama import OllamaProvider
    provider = OllamaProvider(_ollama_config())
    provider.client = MagicMock()
    provider.client.chat.completions.create = AsyncMock(
        side_effect=openai.APIConnectionError(request=MagicMock())
    )
    with pytest.raises(SourceUnavailable):
        asyncio.run(provider.analyze(_raw()))


# ── get_provider ─────────────────────────────────────────────────────────────

def test_get_provider_unknown_raises_value_error():
    from icx_engine.llm.base import get_provider
    from icx_engine.models.config import ChannelConfig
    with pytest.raises(ValueError, match="Unknown provider"):
        get_provider(ChannelConfig(provider="unknown_llm", model="x"))


# ── Google Gemini ─────────────────────────────────────────────────────────────

def _gemini_config() -> ChannelConfig:
    return ChannelConfig(provider="google", model="gemini-1.5-pro", api_key="AIzaSy-test")


def _gemini_ctx_json() -> str:
    return json.dumps({
        "problem_summary": "p", "detailed_description": "d",
        "reproduction_steps": [], "expected_behavior": None, "actual_behavior": None,
        "acceptance_criteria": [], "impact": "i", "priority": "High",
        "issue_type": "Task", "confidence_score": 0.9,
        "completeness_score": 0.5, "missing_information": [],
    })


def _make_gemini_provider():
    """Return GeminiProvider. Client is created fresh per analyze() call."""
    from icx_engine.llm.google import GeminiProvider
    return GeminiProvider(_gemini_config())


def _gemini_mock_client(response_or_exc):
    """Return a mock genai.Client whose aio.models.generate_content is pre-configured."""
    mock_client = MagicMock()
    if isinstance(response_or_exc, BaseException):
        mock_client.aio.models.generate_content = AsyncMock(side_effect=response_or_exc)
    else:
        mock_client.aio.models.generate_content = AsyncMock(return_value=response_or_exc)
    return mock_client


def test_gemini_happy_path_returns_issue_context():
    mock_response = MagicMock()
    mock_response.text = _gemini_ctx_json()
    provider = _make_gemini_provider()

    with patch("google.genai.Client", return_value=_gemini_mock_client(mock_response)):
        result = asyncio.run(provider.analyze(_raw()))

    assert result.problem_summary == "p"
    assert result.issue_type == "Bug"  # finalize() overrides with raw.issue_type


def test_gemini_rate_limit_raises_rate_limited():
    from google.genai.errors import ClientError
    exc = ClientError(429, {"error": {"message": "quota exceeded"}})
    provider = _make_gemini_provider()

    with patch("google.genai.Client", return_value=_gemini_mock_client(exc)):
        with pytest.raises(RateLimited):
            asyncio.run(provider.analyze(_raw()))


def test_gemini_client_error_raises_auth_error():
    from google.genai.errors import ClientError
    exc = ClientError(401, {"error": {"message": "invalid api key"}})
    provider = _make_gemini_provider()

    with patch("google.genai.Client", return_value=_gemini_mock_client(exc)):
        with pytest.raises(AuthError):
            asyncio.run(provider.analyze(_raw()))


def test_gemini_server_error_raises_source_unavailable():
    from google.genai.errors import ServerError
    exc = ServerError(503, {"error": {"message": "service unavailable"}})
    provider = _make_gemini_provider()

    with patch("google.genai.Client", return_value=_gemini_mock_client(exc)):
        with pytest.raises(SourceUnavailable):
            asyncio.run(provider.analyze(_raw()))


def test_gemini_malformed_json_raises_context_build_error():
    mock_response = MagicMock()
    mock_response.text = "not valid json"
    provider = _make_gemini_provider()

    with patch("google.genai.Client", return_value=_gemini_mock_client(mock_response)):
        with pytest.raises(ContextBuildError):
            asyncio.run(provider.analyze(_raw()))


def test_gemini_finalize_applied():
    ctx_json = json.dumps({
        "problem_summary": "p", "detailed_description": "d",
        "reproduction_steps": [], "expected_behavior": None, "actual_behavior": None,
        "acceptance_criteria": [], "impact": "i", "priority": "High",
        "issue_type": "Story",  # LLM says Story - finalize() must override
        "confidence_score": 0.9, "completeness_score": 0.5, "missing_information": [],
    })
    mock_response = MagicMock()
    mock_response.text = ctx_json
    provider = _make_gemini_provider()

    with patch("google.genai.Client", return_value=_gemini_mock_client(mock_response)):
        result = asyncio.run(provider.analyze(_raw()))

    assert result.issue_type == "Bug"  # _raw() has issue_type="Bug"


# ── xAI Grok ──────────────────────────────────────────────────────────────────

def _xai_config() -> ChannelConfig:
    return ChannelConfig(provider="xai", model="grok-beta", api_key="xai-test-key")


def _xai_ctx_json() -> str:
    return json.dumps({
        "problem_summary": "p", "detailed_description": "d",
        "reproduction_steps": [], "expected_behavior": None, "actual_behavior": None,
        "acceptance_criteria": [], "impact": "i", "priority": "High",
        "issue_type": "Task", "confidence_score": 0.9,
        "completeness_score": 0.5, "missing_information": [],
    })


def test_xai_happy_path_returns_issue_context():
    from icx_engine.llm.xai import XAIProvider
    provider = XAIProvider(_xai_config())
    mock_resp = MagicMock()
    mock_resp.choices = [MagicMock(message=MagicMock(content=_xai_ctx_json()))]
    provider.client = MagicMock()
    provider.client.chat.completions.create = AsyncMock(return_value=mock_resp)

    result = asyncio.run(provider.analyze(_raw()))
    assert result.problem_summary == "p"
    assert result.issue_type == "Bug"  # finalize() overrides with raw.issue_type


def test_xai_auth_error_raises_auth_error():
    import openai
    from icx_engine.llm.xai import XAIProvider
    provider = XAIProvider(_xai_config())
    provider.client = MagicMock()
    provider.client.chat.completions.create = AsyncMock(
        side_effect=openai.AuthenticationError("bad key", response=MagicMock(status_code=401), body={})
    )
    with pytest.raises(AuthError):
        asyncio.run(provider.analyze(_raw()))


def test_xai_rate_limit_raises_rate_limited():
    import openai
    from icx_engine.llm.xai import XAIProvider
    provider = XAIProvider(_xai_config())
    provider.client = MagicMock()
    provider.client.chat.completions.create = AsyncMock(
        side_effect=openai.RateLimitError("429", response=MagicMock(status_code=429), body={})
    )
    with pytest.raises(RateLimited):
        asyncio.run(provider.analyze(_raw()))


def test_xai_connection_error_raises_source_unavailable():
    import openai
    from icx_engine.llm.xai import XAIProvider
    provider = XAIProvider(_xai_config())
    provider.client = MagicMock()
    provider.client.chat.completions.create = AsyncMock(
        side_effect=openai.APIConnectionError(request=MagicMock())
    )
    with pytest.raises(SourceUnavailable):
        asyncio.run(provider.analyze(_raw()))


def test_xai_malformed_json_raises_context_build_error():
    from icx_engine.llm.xai import XAIProvider
    provider = XAIProvider(_xai_config())
    provider.client = MagicMock()
    mock_resp = MagicMock()
    mock_resp.choices = [MagicMock(message=MagicMock(content="not json at all"))]
    provider.client.chat.completions.create = AsyncMock(return_value=mock_resp)
    with pytest.raises(ContextBuildError):
        asyncio.run(provider.analyze(_raw()))


def test_xai_uses_xai_base_url():
    from icx_engine.llm.xai import XAIProvider
    provider = XAIProvider(_xai_config())
    assert "x.ai" in str(provider.client.base_url)


def test_xai_finalize_applied():
    from icx_engine.llm.xai import XAIProvider
    provider = XAIProvider(_xai_config())
    ctx_json = json.dumps({
        "problem_summary": "p", "detailed_description": "d",
        "reproduction_steps": [], "expected_behavior": None, "actual_behavior": None,
        "acceptance_criteria": [], "impact": "i", "priority": "High",
        "issue_type": "Epic",
        "confidence_score": 0.9, "completeness_score": 0.5, "missing_information": [],
    })
    provider.client = MagicMock()
    mock_resp = MagicMock()
    mock_resp.choices = [MagicMock(message=MagicMock(content=ctx_json))]
    provider.client.chat.completions.create = AsyncMock(return_value=mock_resp)

    result = asyncio.run(provider.analyze(_raw()))
    assert result.issue_type == "Bug"  # _raw() has issue_type="Bug", finalize() wins


# ── Provider registry ─────────────────────────────────────────────────────────

def test_get_provider_resolves_google():
    from icx_engine.llm.base import get_provider
    from icx_engine.llm.google import GeminiProvider
    cfg = ChannelConfig(provider="google", model="gemini-1.5-pro", api_key="k")
    provider = get_provider(cfg)
    assert isinstance(provider, GeminiProvider)


def test_get_provider_resolves_xai():
    from icx_engine.llm.base import get_provider
    from icx_engine.llm.xai import XAIProvider
    cfg = ChannelConfig(provider="xai", model="grok-beta", api_key="k")
    provider = get_provider(cfg)
    assert isinstance(provider, XAIProvider)


def test_providers_list_contains_google_and_xai():
    from icx_engine.cli import _PROVIDERS, _DEFAULT_MODELS
    provider_keys = [k for k, _ in _PROVIDERS]
    assert "google" in provider_keys
    assert "xai" in provider_keys
    assert _DEFAULT_MODELS["google"]["text"] == "gemini-1.5-pro"
    assert _DEFAULT_MODELS["google"]["image"] == "gemini-1.5-flash"
    assert _DEFAULT_MODELS["xai"]["text"] == "grok-beta"
    assert _DEFAULT_MODELS["xai"]["image"] == "grok-vision-beta"


# ── JSON sanitizer ────────────────────────────────────────────────────────────

def test_strip_json_fencing_plain_json():
    from icx_engine.llm.base import _strip_json_fencing
    raw = '{"key": "value"}'
    assert _strip_json_fencing(raw) == raw


def test_strip_json_fencing_markdown_wrapped():
    from icx_engine.llm.base import _strip_json_fencing
    raw = '```json\n{"key": "value"}\n```'
    assert _strip_json_fencing(raw) == '{"key": "value"}'


def test_strip_json_fencing_prose_before_and_after():
    from icx_engine.llm.base import _strip_json_fencing
    raw = 'Here is the result:\n{"key": "value"}\nDone.'
    assert _strip_json_fencing(raw) == '{"key": "value"}'


def test_strip_json_fencing_no_braces_returns_original():
    from icx_engine.llm.base import _strip_json_fencing
    raw = "no json here"
    assert _strip_json_fencing(raw) == raw


def test_gemini_handles_markdown_wrapped_json():
    """Gemini 2.5+ wraps JSON in ```json fences - provider must parse cleanly."""
    mock_response = MagicMock()
    mock_response.text = '```json\n' + _gemini_ctx_json() + '\n```'
    provider = _make_gemini_provider()

    with patch("google.genai.Client", return_value=_gemini_mock_client(mock_response)):
        result = asyncio.run(provider.analyze(_raw()))

    assert result.problem_summary == "p"


def test_xai_handles_markdown_wrapped_json():
    from icx_engine.llm.xai import XAIProvider
    provider = XAIProvider(_xai_config())
    provider.client = MagicMock()
    mock_resp = MagicMock()
    mock_resp.choices = [MagicMock(message=MagicMock(
        content='```json\n' + _xai_ctx_json() + '\n```'
    ))]
    provider.client.chat.completions.create = AsyncMock(return_value=mock_resp)

    result = asyncio.run(provider.analyze(_raw()))
    assert result.problem_summary == "p"
