"""ChannelConfig.base_url must be honored by the OpenAI and Anthropic providers.

Regression guard: previously OpenAIProvider/AnthropicProvider dropped base_url,
routing text analysis to the SDK default even when a custom (Azure / proxy /
self-hosted OpenAI-compatible) endpoint was configured - while the vision path
(grounding) honored it. The two must agree.
"""
from __future__ import annotations
from unittest.mock import patch
from icx_engine.models.config import ChannelConfig


def test_openai_provider_honors_configured_base_url():
    from icx_engine.llm.openai import OpenAIProvider
    cfg = ChannelConfig(
        provider="openai", model="gpt-4o", api_key="k",
        base_url="https://proxy.internal/v1",
    )
    with patch("icx_engine.llm.openai.AsyncOpenAI") as mock_cls:
        OpenAIProvider(cfg)
        assert mock_cls.call_args.kwargs.get("base_url") == "https://proxy.internal/v1"


def test_openai_provider_base_url_none_when_unset():
    from icx_engine.llm.openai import OpenAIProvider
    cfg = ChannelConfig(provider="openai", model="gpt-4o", api_key="k")
    with patch("icx_engine.llm.openai.AsyncOpenAI") as mock_cls:
        OpenAIProvider(cfg)
        assert mock_cls.call_args.kwargs.get("base_url") is None


def test_anthropic_provider_honors_configured_base_url():
    from icx_engine.llm.anthropic import AnthropicProvider
    cfg = ChannelConfig(
        provider="anthropic", model="claude-opus-4-5", api_key="k",
        base_url="https://anthropic.internal",
    )
    with patch("icx_engine.llm.anthropic.AsyncAnthropic") as mock_cls:
        AnthropicProvider(cfg)
        assert mock_cls.call_args.kwargs.get("base_url") == "https://anthropic.internal"


def test_anthropic_provider_base_url_none_when_unset():
    from icx_engine.llm.anthropic import AnthropicProvider
    cfg = ChannelConfig(provider="anthropic", model="claude-opus-4-5", api_key="k")
    with patch("icx_engine.llm.anthropic.AsyncAnthropic") as mock_cls:
        AnthropicProvider(cfg)
        assert mock_cls.call_args.kwargs.get("base_url") is None
