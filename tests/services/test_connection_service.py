"""Task 6 (S1 + Q2) regression guards, and Task 9 (A4) flag-driven prompting.

S1: LLM credential prompts must live in connection_service.py, not cli.py.
Q2: _connect_jira_token / _connect_jira_oauth share domain-resolution logic
via _resolve_and_confirm_domain, while each flow's own divergent step
(OAuth's cloud_id resolution) stays untouched.
A4: _prompt_channel_config's base-URL/API-key prompting is driven by the
registry's ProviderSpec.prompts_for_base_url/prompts_for_api_key flags,
not by literal `provider_key == "ollama"/"nim"` checks.
"""
from unittest.mock import patch

import httpx
import pytest
import respx

from icx_engine.config_manager import ConfigManager
from icx_engine.llm.registry import ProviderSpec
from icx_engine.models.config import AppConfig
from icx_engine.services import connection_service as cs


# -- S1: relocation ----------------------------------------------------------

def test_prompt_functions_live_in_connection_service():
    assert callable(cs._prompt_channel_config)
    assert callable(cs._prompt_vision_channel)


def test_prompt_functions_no_longer_defined_in_cli_module():
    import icx_engine.cli as cli_mod
    assert "_prompt_channel_config" not in vars(cli_mod)
    assert "_prompt_vision_channel" not in vars(cli_mod)


# -- Q2: shared domain-resolution helper --------------------------------------

def test_resolve_and_confirm_domain_called_by_token_flow():
    with patch.object(cs.typer, "prompt", return_value="https://company.atlassian.net") as mock_prompt, \
         patch.object(ConfigManager, "load", return_value=AppConfig()), \
         patch.object(cs, "_resolve_and_confirm_domain", return_value=None) as mock_resolve:
        cs._connect_jira_token(debug=True)

    mock_resolve.assert_called_once()
    call_args = mock_resolve.call_args.args
    assert call_args[0] == "https://company.atlassian.net"
    # cancelled resolution must short-circuit before the email/token prompts
    assert mock_prompt.call_count == 1


def test_resolve_and_confirm_domain_called_by_oauth_flow():
    def fake_prompt(text, **kwargs):
        if "Client ID" in text:
            return "cid"
        if "Client Secret" in text:
            return "secret"
        if "base URL" in text:
            return "https://company.atlassian.net"
        return ""

    with patch.object(cs.typer, "prompt", side_effect=fake_prompt) as mock_prompt, \
         patch.object(ConfigManager, "load", return_value=AppConfig()), \
         patch.object(cs, "_resolve_and_confirm_domain", return_value=None) as mock_resolve:
        cs._connect_jira_oauth(debug=True)

    mock_resolve.assert_called_once()
    call_args = mock_resolve.call_args.args
    assert call_args[0] == "https://company.atlassian.net"
    # cancelled resolution must short-circuit before the PKCE flow ever starts
    assert mock_prompt.call_count == 3  # client id, client secret, base url


# -- Q6: thin test coverage on _connect_jira_token's own risk surface --------
# (credential prompting + HTTPS verification - the security-sensitive path
# CLAUDE.md calls out as routing through this module specifically)

def test_connect_jira_token_invalid_domain_exits_cleanly():
    with patch.object(cs.typer, "prompt", return_value="not a valid domain!!"), \
         patch.object(ConfigManager, "load", return_value=AppConfig()):
        with pytest.raises(Exception):  # typer.Exit(1) via render_icx_error
            cs._connect_jira_token(debug=True)


def test_connect_jira_token_overwrite_cancel_does_not_save(tmp_path, monkeypatch):
    existing = AppConfig()
    from icx_engine.connectors.jira.config import JiraConnection, TokenAuth
    existing.connections = [JiraConnection(
        domain="company.atlassian.net",
        auth=TokenAuth(auth_type="token", email="e@x.com", api_token="tok"),
    )]

    def fake_prompt(text, **kwargs):
        if "base URL" in text or "Jira base URL" in text:
            return "https://company.atlassian.net"
        return ""

    with patch.object(cs.typer, "prompt", side_effect=fake_prompt), \
         patch.object(cs.typer, "confirm", return_value=False), \
         patch.object(ConfigManager, "load", return_value=existing), \
         patch.object(ConfigManager, "save") as mock_save:
        cs._connect_jira_token(debug=True)

    mock_save.assert_not_called()


@respx.mock
def test_connect_jira_token_401_reports_auth_error_and_does_not_save():
    def fake_prompt(text, **kwargs):
        if "base URL" in text:
            return "https://company.atlassian.net"
        if "Email" in text:
            return "e@x.com"
        return "faketoken"

    respx.get("https://company.atlassian.net/rest/api/3/myself").mock(
        return_value=httpx.Response(401, json={"message": "Unauthorized"})
    )
    with patch.object(cs.typer, "prompt", side_effect=fake_prompt), \
         patch.object(ConfigManager, "load", return_value=AppConfig()), \
         patch.object(ConfigManager, "save") as mock_save, \
         patch.object(cs.err_console, "print"):
        with pytest.raises(Exception):  # typer.Exit(1)
            cs._connect_jira_token(debug=True)

    mock_save.assert_not_called()


@respx.mock
def test_connect_jira_token_403_reports_permission_error_and_does_not_save():
    def fake_prompt(text, **kwargs):
        if "base URL" in text:
            return "https://company.atlassian.net"
        if "Email" in text:
            return "e@x.com"
        return "faketoken"

    respx.get("https://company.atlassian.net/rest/api/3/myself").mock(
        return_value=httpx.Response(403, json={"message": "Forbidden"})
    )
    with patch.object(cs.typer, "prompt", side_effect=fake_prompt), \
         patch.object(ConfigManager, "load", return_value=AppConfig()), \
         patch.object(ConfigManager, "save") as mock_save, \
         patch.object(cs.err_console, "print"):
        with pytest.raises(Exception):  # typer.Exit(1)
            cs._connect_jira_token(debug=True)

    mock_save.assert_not_called()


# -- Q2: divergent OAuth-only step (cloud_id resolution) preserved -----------

@respx.mock
def test_oauth_flow_still_resolves_cloud_id_after_extraction():
    """The extraction must not remove OAuth's cloud_id fetch (accessible-resources)."""
    from icx_engine.connectors.jira.config import JiraOAuthAuth

    respx.get("https://api.atlassian.com/oauth/token/accessible-resources").mock(
        return_value=httpx.Response(200, json=[
            {"id": "cloud-xyz-123", "url": "https://company.atlassian.net"},
        ])
    )

    def fake_prompt(text, **kwargs):
        if "Client ID" in text:
            return "cid"
        if "Client Secret" in text:
            return "secret"
        if "base URL" in text:
            return "https://company.atlassian.net"
        return ""

    async def fake_run_pkce_flow(**kwargs):
        return {"access_token": "tok-abc", "refresh_token": "ref-abc", "expires_in": 3600}

    saved = {}

    def fake_save(config):
        saved["config"] = config

    with patch.object(cs.typer, "prompt", side_effect=fake_prompt), \
         patch.object(ConfigManager, "load", return_value=AppConfig()), \
         patch.object(ConfigManager, "save", side_effect=fake_save), \
         patch.object(ConfigManager, "warn_if_plaintext"), \
         patch("icx_engine.auth.pkce.run_pkce_flow", fake_run_pkce_flow):
        cs._connect_jira_oauth(debug=True)

    conn = saved["config"].connections[0]
    assert isinstance(conn.auth, JiraOAuthAuth)
    assert conn.auth.cloud_id == "cloud-xyz-123"


def test_token_flow_has_no_cloud_id_step():
    """Token auth has no cloud_id concept - the divergent step must stay OAuth-only."""
    from icx_engine.auth import token as token_mod
    from icx_engine.connectors.jira.config import TokenAuth

    saved = {}

    def fake_save(config):
        saved["config"] = config

    def fake_prompt(text, **kwargs):
        if "base URL" in text:
            return "https://company.atlassian.net"
        if "Email" in text:
            return "user@company.com"
        if "API token" in text:
            return "tok-123"
        return ""

    fake_resp = httpx.Response(200, json={"displayName": "User"})

    async def fake_check_http_credentials(**kwargs):
        return fake_resp

    with patch.object(cs.typer, "prompt", side_effect=fake_prompt), \
         patch.object(ConfigManager, "load", return_value=AppConfig()), \
         patch.object(ConfigManager, "save", side_effect=fake_save), \
         patch.object(ConfigManager, "warn_if_plaintext"), \
         patch.object(token_mod, "check_http_credentials", fake_check_http_credentials):
        cs._connect_jira_token(debug=True)

    conn = saved["config"].connections[0]
    assert isinstance(conn.auth, TokenAuth)
    assert not hasattr(conn.auth, "cloud_id")


# -- A4: flag-driven base-URL/API-key prompting -------------------------------

def test_new_provider_with_key_and_url_flags_prompts_for_both_and_enforces_https():
    """Proves the flags - not a name check - drive the behavior: a brand-new
    provider entry with prompts_for_base_url=True, prompts_for_api_key=True
    gets a base-URL prompt and an HTTPS rejection, with zero code change to
    _prompt_channel_config for this test to pass."""
    fake_spec = ProviderSpec(
        name="fakeprov",
        api_style="openai",
        default_base_url="https://fake.example.com/v1",
        default_text_model="fake-text-model",
        default_image_model="fake-image-model",
        cli_label="Fake Provider (test)",
        prompts_for_base_url=True,
        prompts_for_api_key=True,
    )
    test_specs = dict(cs._PROVIDER_SPECS)
    test_specs["fakeprov"] = fake_spec
    test_providers = list(cs._PROVIDERS) + [("fakeprov", fake_spec.cli_label)]
    test_default_models = dict(cs._DEFAULT_MODELS)
    test_default_models["fakeprov"] = {"text": "fake-text-model", "image": "fake-image-model"}

    calls = []

    def fake_prompt(text, **kwargs):
        calls.append((text, kwargs))
        if text == "Model":
            return kwargs.get("default", "")
        if text == "Fake Provider (test) API key":
            return "captured-key"
        if text == "Fake Provider (test) base URL":
            return "http://insecure.example.com/v1"  # deliberately non-HTTPS
        raise AssertionError(f"unexpected prompt: {text}")

    with patch.object(cs, "_PROVIDER_SPECS", test_specs), \
         patch.object(cs, "_PROVIDERS", test_providers), \
         patch.object(cs, "_DEFAULT_MODELS", test_default_models), \
         patch.object(cs.typer, "prompt", side_effect=fake_prompt):
        import typer
        with pytest.raises(typer.Exit):
            cs._prompt_channel_config("Text Intelligence", provider_key="fakeprov")

    prompted_texts = [t for t, _ in calls]
    assert "Fake Provider (test) API key" in prompted_texts
    assert "Fake Provider (test) base URL" in prompted_texts
    # key before URL, matching NIM's own order
    assert prompted_texts.index("Fake Provider (test) API key") < prompted_texts.index("Fake Provider (test) base URL")


def test_new_provider_url_only_flag_prompts_for_url_without_key():
    """A provider with prompts_for_base_url=True, prompts_for_api_key=False
    (Ollama's shape) gets a URL prompt only, no API key prompt, no HTTPS
    enforcement - proven via flags alone, no code change to the function."""
    fake_spec = ProviderSpec(
        name="fakelocal",
        api_style="openai",
        default_base_url="http://localhost:9999/v1",
        default_text_model="fake-text-model",
        default_image_model="fake-image-model",
        cli_label="Fake Local (test)",
        prompts_for_base_url=True,
        prompts_for_api_key=False,
    )
    test_specs = dict(cs._PROVIDER_SPECS)
    test_specs["fakelocal"] = fake_spec
    test_providers = list(cs._PROVIDERS) + [("fakelocal", fake_spec.cli_label)]
    test_default_models = dict(cs._DEFAULT_MODELS)
    test_default_models["fakelocal"] = {"text": "fake-text-model", "image": "fake-image-model"}

    calls = []

    def fake_prompt(text, **kwargs):
        calls.append((text, kwargs))
        return kwargs.get("default", "")

    with patch.object(cs, "_PROVIDER_SPECS", test_specs), \
         patch.object(cs, "_PROVIDERS", test_providers), \
         patch.object(cs, "_DEFAULT_MODELS", test_default_models), \
         patch.object(cs.typer, "prompt", side_effect=fake_prompt):
        cfg = cs._prompt_channel_config("Text Intelligence", provider_key="fakelocal")

    prompted_texts = [t for t, _ in calls]
    assert "Fake Local (test) base URL" in prompted_texts
    assert not any("API key" in t for t in prompted_texts)
    assert cfg.api_key is None


def _cli_label(provider_key: str) -> str:
    return next(lbl for k, lbl in cs._PROVIDERS if k == provider_key)


@pytest.mark.parametrize("provider_key,expect_key,expect_url,key_label,url_label,url_default", [
    ("ollama", False, True, None, "Ollama base URL", "http://localhost:11434/v1"),
    ("nim", True, True, "Nvidia NIM API key", "NIM base URL", "https://integrate.api.nvidia.com/v1"),
    ("openai", True, False, f"{_cli_label('openai')} API key", None, None),
    ("anthropic", True, False, f"{_cli_label('anthropic')} API key", None, None),
    ("google", True, False, f"{_cli_label('google')} API key", None, None),
    ("xai", True, False, f"{_cli_label('xai')} API key", None, None),
])
def test_real_providers_prompt_flow_unchanged(
    provider_key, expect_key, expect_url, key_label, url_label, url_default
):
    """Regression guard: every one of today's 6 real providers must ask for
    exactly the same things, in the same order, with the same labels and
    defaults, as before the flag-driven rewrite."""
    calls = []

    def fake_prompt(text, **kwargs):
        calls.append((text, kwargs))
        return kwargs.get("default", "")

    with patch.object(cs.typer, "prompt", side_effect=fake_prompt):
        cfg = cs._prompt_channel_config("Text Intelligence", provider_key=provider_key)

    prompted_texts = [t for t, _ in calls]

    if expect_key:
        assert key_label in prompted_texts
        key_kwargs = dict(calls[prompted_texts.index(key_label)][1])
        assert key_kwargs.get("hide_input") is True
    else:
        assert not any("API key" in t for t in prompted_texts)
        assert cfg.api_key is None

    if expect_url:
        assert url_label in prompted_texts
        url_kwargs = dict(calls[prompted_texts.index(url_label)][1])
        assert url_kwargs.get("default") == url_default
    else:
        assert not any("base URL" in t for t in prompted_texts)
        assert cfg.base_url is None

    if provider_key == "nim":
        # key prompted before URL, matching original order
        assert prompted_texts.index(key_label) < prompted_texts.index(url_label)


def test_nim_rejects_http_base_url_exact_message():
    """NIM's HTTPS enforcement (now generalized to any base-url+key provider)
    must still reject with the exact historical error text."""
    def fake_prompt(text, **kwargs):
        if text == "Model":
            return kwargs.get("default", "")
        if text == "Nvidia NIM API key":
            return "secret-key"
        if text == "NIM base URL":
            return "http://insecure.nim.example.com/v1"
        raise AssertionError(f"unexpected prompt: {text}")

    import typer
    with patch.object(cs.typer, "prompt", side_effect=fake_prompt), \
         patch.object(cs, "render_icx_error") as mock_render:
        with pytest.raises(typer.Exit):
            cs._prompt_channel_config("Text Intelligence", provider_key="nim")

    mock_render.assert_called_once()
    err = mock_render.call_args.args[0]
    assert str(err) == "NIM base URL must use HTTPS."


def test_ollama_url_only_no_https_enforcement():
    """Ollama has a URL prompt but no secret in play, so an http:// custom
    URL for Ollama must be accepted, not rejected (unchanged behavior)."""
    def fake_prompt(text, **kwargs):
        if text == "Model":
            return kwargs.get("default", "")
        if text == "Ollama base URL":
            return "http://custom-ollama-host:11434/v1"
        raise AssertionError(f"unexpected prompt: {text}")

    with patch.object(cs.typer, "prompt", side_effect=fake_prompt):
        cfg = cs._prompt_channel_config("Text Intelligence", provider_key="ollama")

    assert cfg.base_url == "http://custom-ollama-host:11434/v1"
    assert cfg.api_key is None
