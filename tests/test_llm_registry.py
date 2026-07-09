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
    """Every registry provider must have a class wired in the built-in seed."""
    from icx_engine.llm.base import _default_providers

    src = inspect.getsource(_default_providers)
    for name in r.PROVIDERS:
        assert f'"{name}"' in src, f"{name} missing from provider dispatch"


def test_builtin_providers_resolve():
    """Built-in names map to their historical classes via the registry.

    Identity check only - provider SDK clients require an api_key at construction,
    so we assert the wired class, not an instance.
    """
    from icx_engine.llm.base import _default_providers, _provider_registry

    builtins = _default_providers()
    registry = _provider_registry()
    for name, cls in builtins.items():
        assert registry.get(name) is cls, name


def test_unknown_provider_raises():
    from icx_engine.models.config import ChannelConfig

    try:
        get_provider(ChannelConfig(provider="does-not-exist", model="x"))
    except ValueError as exc:
        assert "Unknown provider" in str(exc)
    else:
        raise AssertionError("expected ValueError for unknown provider")


def test_register_provider_adds_and_overrides():
    """Third-party registration is honored and overrides survive seeding."""
    from icx_engine.llm import base
    from icx_engine.llm.base import LLMProvider, get_provider, register_provider
    from icx_engine.models.config import ChannelConfig

    class _Dummy(LLMProvider):
        def __init__(self, config):
            self.config = config

        async def analyze(self, raw):  # pragma: no cover - not invoked
            raise NotImplementedError

    saved = dict(base._PROVIDER_CLASSES)
    try:
        register_provider("dummy", _Dummy)
        assert isinstance(get_provider(ChannelConfig(provider="dummy", model="x")), _Dummy)
        # Override a built-in name, then confirm seeding does not clobber it.
        register_provider("openai", _Dummy)
        assert isinstance(get_provider(ChannelConfig(provider="openai", model="x")), _Dummy)
    finally:
        base._PROVIDER_CLASSES.clear()
        base._PROVIDER_CLASSES.update(saved)


def test_attachments_base_urls_derived_from_registry():
    from icx_engine.connectors import attachments as a
    assert a._DEFAULT_BASE_URLS == r.openai_compat_base_urls()


def test_cli_menu_and_models_derived_from_registry():
    from icx_engine import cli
    assert [n for n, _ in cli._PROVIDERS] == list(r.PROVIDERS)
    for name, spec in r.PROVIDERS.items():
        assert cli._DEFAULT_MODELS[name]["text"] == spec.default_text_model
        assert cli._DEFAULT_MODELS[name]["image"] == spec.default_image_model
