from __future__ import annotations
import pytest
from icx_engine.models.config import AppConfig, LLMConfig, ChannelConfig
from icx_engine.connectors.jira.config import JiraConnection, TokenAuth


def _cfg(domains=None, profiles=None, default=None, current_profile=None):
    conns = [
        JiraConnection(domain=d, auth=TokenAuth(auth_type="token", email="u@test.com", api_token="tok"))
        for d in (domains or [])
    ]
    llm_profiles = {
        n: LLMConfig(text_config=ChannelConfig(provider="ollama", model="llama3"))
        for n in (profiles or [])
    }
    return AppConfig(
        connections=conns,
        llm_profiles=llm_profiles,
        default_connection=default,
        current_llm_profile=current_profile,
    )


# resolve_connection

def test_resolve_connection_by_index():
    from icx_engine import management
    conn = management.resolve_connection(_cfg(["alpha.atlassian.net", "beta.atlassian.net"]), "1")
    assert conn.domain == "alpha.atlassian.net"

def test_resolve_connection_by_index_second():
    from icx_engine import management
    conn = management.resolve_connection(_cfg(["alpha.atlassian.net", "beta.atlassian.net"]), "2")
    assert conn.domain == "beta.atlassian.net"

def test_resolve_connection_by_domain():
    from icx_engine import management
    conn = management.resolve_connection(_cfg(["alpha.atlassian.net", "beta.atlassian.net"]), "beta.atlassian.net")
    assert conn.domain == "beta.atlassian.net"

def test_resolve_connection_index_out_of_range():
    from icx_engine import management
    with pytest.raises(management.ManagementError, match="between 1 and 1"):
        management.resolve_connection(_cfg(["alpha.atlassian.net"]), "5")

def test_resolve_connection_domain_not_found():
    from icx_engine import management
    with pytest.raises(management.ManagementError, match="xyz.atlassian.net"):
        management.resolve_connection(_cfg(["alpha.atlassian.net"]), "xyz.atlassian.net")

def test_resolve_connection_no_connections():
    from icx_engine import management
    with pytest.raises(management.ManagementError, match="No connections"):
        management.resolve_connection(AppConfig(), "1")


# resolve_llm_profile

def test_resolve_llm_profile_by_index():
    from icx_engine import management
    name, _ = management.resolve_llm_profile(_cfg(profiles=["personal", "work"]), "1")
    assert name == "personal"

def test_resolve_llm_profile_by_name():
    from icx_engine import management
    name, _ = management.resolve_llm_profile(_cfg(profiles=["personal", "work"]), "work")
    assert name == "work"

def test_resolve_llm_profile_index_out_of_range():
    from icx_engine import management
    with pytest.raises(management.ManagementError, match="between 1 and 1"):
        management.resolve_llm_profile(_cfg(profiles=["personal"]), "3")

def test_resolve_llm_profile_name_not_found():
    from icx_engine import management
    with pytest.raises(management.ManagementError, match="work"):
        management.resolve_llm_profile(_cfg(profiles=["personal"]), "work")

def test_resolve_llm_profile_no_profiles():
    from icx_engine import management
    with pytest.raises(management.ManagementError, match="No AI profiles"):
        management.resolve_llm_profile(AppConfig(), "1")


# disconnect

def test_disconnect_removes_connection(monkeypatch):
    import icx_engine.config_manager as cm
    monkeypatch.setattr(cm, "_keychain_ok", False)
    from icx_engine import management
    result = management.disconnect(_cfg(["alpha.atlassian.net", "beta.atlassian.net"]), "1")
    assert len(result.connections) == 1
    assert result.connections[0].domain == "beta.atlassian.net"

def test_disconnect_clears_default_when_removed(monkeypatch):
    import icx_engine.config_manager as cm
    monkeypatch.setattr(cm, "_keychain_ok", False)
    from icx_engine import management
    config = _cfg(["alpha.atlassian.net"], default="jira:alpha.atlassian.net")
    result = management.disconnect(config, "1")
    assert result.default_connection is None

def test_disconnect_preserves_default_for_other_connection(monkeypatch):
    import icx_engine.config_manager as cm
    monkeypatch.setattr(cm, "_keychain_ok", False)
    from icx_engine import management
    config = _cfg(["alpha.atlassian.net", "beta.atlassian.net"], default="jira:beta.atlassian.net")
    result = management.disconnect(config, "1")
    assert result.default_connection == "jira:beta.atlassian.net"


# set_default_connection

def test_set_default_connection_by_index():
    from icx_engine import management
    result = management.set_default_connection(_cfg(["alpha.atlassian.net", "beta.atlassian.net"]), "2")
    assert result.default_connection == "jira:beta.atlassian.net"

def test_set_default_connection_by_domain():
    from icx_engine import management
    result = management.set_default_connection(_cfg(["alpha.atlassian.net", "beta.atlassian.net"]), "alpha.atlassian.net")
    assert result.default_connection == "jira:alpha.atlassian.net"


# use_ai_profile

def test_use_ai_profile_by_index():
    from icx_engine import management
    result = management.use_ai_profile(_cfg(profiles=["personal", "work"]), "2")
    assert result.current_llm_profile == "work"

def test_use_ai_profile_by_name():
    from icx_engine import management
    result = management.use_ai_profile(_cfg(profiles=["personal", "work"]), "personal")
    assert result.current_llm_profile == "personal"


# unset_llm_profile

def test_unset_llm_profile_removes_profile(monkeypatch):
    import icx_engine.config_manager as cm
    monkeypatch.setattr(cm, "_keychain_ok", False)
    from icx_engine import management
    config = _cfg(profiles=["personal", "work"], current_profile="work")
    result = management.unset_llm_profile(config, "1")
    assert "personal" not in result.llm_profiles
    assert result.current_llm_profile == "work"

def test_unset_llm_profile_clears_current_when_active(monkeypatch):
    import icx_engine.config_manager as cm
    monkeypatch.setattr(cm, "_keychain_ok", False)
    from icx_engine import management
    config = _cfg(profiles=["personal"], current_profile="personal")
    result = management.unset_llm_profile(config, "personal")
    assert "personal" not in result.llm_profiles
    assert result.current_llm_profile is None


# unset_llm_channel

def test_unset_llm_channel_removes_image_channel(monkeypatch):
    import icx_engine.config_manager as cm
    monkeypatch.setattr(cm, "_keychain_ok", False)
    from icx_engine import management
    from icx_engine.models.config import ChannelConfig
    config = AppConfig(
        llm_profiles={"work": LLMConfig(
            text_config=ChannelConfig(provider="openai", model="gpt-4o"),
            image_config=ChannelConfig(provider="nim", model="llava"),
        )},
        current_llm_profile="work",
    )
    result = management.unset_llm_channel(config, "work", 2)
    assert result.llm_profiles["work"].image_config is None
    assert result.llm_profiles["work"].text_config is not None


def test_unset_llm_channel_errors_removing_text_when_image_exists(monkeypatch):
    import icx_engine.config_manager as cm
    monkeypatch.setattr(cm, "_keychain_ok", False)
    from icx_engine import management
    from icx_engine.models.config import ChannelConfig
    config = AppConfig(
        llm_profiles={"work": LLMConfig(
            text_config=ChannelConfig(provider="openai", model="gpt-4o"),
            image_config=ChannelConfig(provider="nim", model="llava"),
        )},
        current_llm_profile="work",
    )
    with pytest.raises(management.ManagementError, match="Cannot remove the text channel"):
        management.unset_llm_channel(config, "work", 1)


def test_unset_llm_channel_errors_on_invalid_channel():
    from icx_engine import management
    from icx_engine.models.config import ChannelConfig
    config = AppConfig(
        llm_profiles={"work": LLMConfig(
            text_config=ChannelConfig(provider="openai", model="gpt-4o"),
        )},
    )
    with pytest.raises(management.ManagementError, match="Invalid channel"):
        management.unset_llm_channel(config, "work", 3)


def test_unset_llm_channel_errors_when_no_image_to_remove():
    from icx_engine import management
    from icx_engine.models.config import ChannelConfig
    config = AppConfig(
        llm_profiles={"work": LLMConfig(
            text_config=ChannelConfig(provider="openai", model="gpt-4o"),
        )},
    )
    with pytest.raises(management.ManagementError, match="no image channel"):
        management.unset_llm_channel(config, "work", 2)
