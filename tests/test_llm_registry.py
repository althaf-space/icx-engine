"""LLM provider registry - single source of truth (finding A2 / Phase C-1)."""
import inspect

from icx_engine.llm import registry as r
from icx_engine.llm.base import get_provider


def test_all_providers_present():
    assert set(r.PROVIDERS) == {"ollama", "nim", "openai", "anthropic", "google", "xai"}


def test_api_style_values():
    assert r.api_style("anthropic") == "anthropic"
    assert r.api_style("google") == "google"
    for p in ("openai", "nim", "xai", "ollama"):
        assert r.api_style(p) == "openai"
    # Unknown provider falls back to openai-compat (historical else-branch).
    assert r.api_style("some-new-provider") == "openai"


def test_openai_compat_base_urls_exact():
    assert r.openai_compat_base_urls() == {
        "ollama": "http://localhost:11434/v1",
        "nim": "https://integrate.api.nvidia.com/v1",
        "xai": "https://api.x.ai/v1",
    }


def test_default_models_drift_guard():
    """Lock the historical default models so an accidental edit is caught."""
    expected = {
        "ollama": ("llama3", "llava"),
        "nim": ("deepseek-ai/deepseek-v3", "meta/llama-3.2-11b-vision-instruct"),
        "openai": ("gpt-4o", "gpt-4o"),
        "anthropic": ("claude-opus-4-5", "claude-opus-4-5"),
        "google": ("gemini-1.5-pro", "gemini-1.5-flash"),
        "xai": ("grok-beta", "grok-vision-beta"),
    }
    for name, (text, image) in expected.items():
        spec = r.PROVIDERS[name]
        assert (spec.default_text_model, spec.default_image_model) == (text, image), name


def test_get_provider_parity_with_registry():
    """Every registry provider must have a class wired in get_provider."""
    src = inspect.getsource(get_provider)
    for name in r.PROVIDERS:
        assert f'"{name}"' in src, f"{name} missing from get_provider dispatch"


def test_attachments_base_urls_derived_from_registry():
    from icx_engine.connectors import attachments as a
    assert a._DEFAULT_BASE_URLS == r.openai_compat_base_urls()


def test_cli_menu_and_models_derived_from_registry():
    from icx_engine import cli
    assert [n for n, _ in cli._PROVIDERS] == list(r.PROVIDERS)
    for name, spec in r.PROVIDERS.items():
        assert cli._DEFAULT_MODELS[name]["text"] == spec.default_text_model
        assert cli._DEFAULT_MODELS[name]["image"] == spec.default_image_model
