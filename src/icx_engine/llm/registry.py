"""Single source of truth for LLM provider metadata.

Every provider fact - API style, default base URL, default models, and the CLI
selection label - lives here. Other modules derive from this table instead of
re-declaring provider lists:
  - `llm/base.get_provider`         maps names to `LLMProvider` classes (parity-checked here)
  - `connectors/attachments`        OpenAI-compatible base URLs + vision api-style dispatch
  - `connectors/audio`              base URLs for transcript-cleanup
  - `grounding`                     visual-grounding api-style dispatch
  - `cli`                           default models + provider menu

Adding a provider = one entry here + registering its class in `get_provider`.

Out of scope (separate subsystems, intentionally not driven by this table):
  - `graph/manager._ICX_PROVIDER_TO_PARSER` (vendored-parser backend names)
  - `graph/parser/llm.py` (graph pipeline's own provider table)
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    api_style: str                 # "openai" | "anthropic" | "google"
    default_base_url: str | None   # None = provider SDK uses its own default
    default_text_model: str
    default_image_model: str
    cli_label: str
    prompts_for_base_url: bool = False   # CLI connect flow: ask for a custom base URL
    prompts_for_api_key: bool = True     # CLI connect flow: ask for an API key


# Insertion order defines the CLI provider menu order.
PROVIDERS: dict[str, ProviderSpec] = {
    "ollama": ProviderSpec(
        "ollama", "openai", "http://localhost:11434/v1",
        "llama3", "llava",
        "Ollama / LM Studio  (local, free, no API key needed)",
        prompts_for_base_url=True, prompts_for_api_key=False,
    ),
    "nim": ProviderSpec(
        "nim", "openai", "https://integrate.api.nvidia.com/v1",
        "deepseek-ai/deepseek-v3", "meta/llama-3.2-11b-vision-instruct",
        "Nvidia NIM          (cloud, free tier at build.nvidia.com)",
        prompts_for_base_url=True, prompts_for_api_key=True,
    ),
    "openai": ProviderSpec(
        "openai", "openai", None,
        "gpt-4o", "gpt-4o",
        "OpenAI              (cloud, paid)",
    ),
    "anthropic": ProviderSpec(
        "anthropic", "anthropic", None,
        "claude-opus-4-5", "claude-opus-4-5",
        "Claude / Anthropic  (cloud, paid)",
    ),
    "google": ProviderSpec(
        "google", "google", None,
        "gemini-1.5-pro", "gemini-1.5-flash",
        "Google Gemini       (cloud, free tier + paid)",
    ),
    "xai": ProviderSpec(
        "xai", "openai", "https://api.x.ai/v1",
        "grok-beta", "grok-vision-beta",
        "xAI Grok            (cloud, paid)",
    ),
}


def api_style(provider: str) -> str:
    """Return the API dispatch style for a provider.

    Unknown providers default to "openai" - matching the historical
    else-branch fallthrough in the vision/attachment code paths.
    """
    spec = PROVIDERS.get(provider)
    return spec.api_style if spec else "openai"


def default_base_url(provider: str) -> str | None:
    spec = PROVIDERS.get(provider)
    return spec.default_base_url if spec else None


def openai_compat_base_urls() -> dict[str, str]:
    """Providers that speak the OpenAI protocol at a non-default base URL."""
    return {
        name: spec.default_base_url
        for name, spec in PROVIDERS.items()
        if spec.default_base_url
    }
