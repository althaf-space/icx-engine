import base64
import json
import os
import pytest
from pathlib import Path
from pydantic import ValidationError
from unittest.mock import patch

from icx_engine.models.config import AppConfig, BaseConnection, LLMConfig, ChannelConfig, OAuthAuth
from icx_engine.connectors.jira.config import JiraConnection, TokenAuth, JiraOAuthAuth
from icx_engine.models.output import RawIssueData, IssueContext


# ── ChannelConfig ─────────────────────────────────────────────────────────────

def test_channel_config_round_trip():
    ch = ChannelConfig(provider="openai", model="gpt-4o", api_key="sk-xyz", base_url=None)
    assert ch.provider == "openai"
    assert ch.model == "gpt-4o"
    assert "api_key" not in ch.model_dump()  # exclude=True

def test_llm_config_text_only():
    llm = LLMConfig(text_config=ChannelConfig(provider="ollama", model="llama3"))
    assert llm.text_config.provider == "ollama"
    assert llm.image_config is None

def test_llm_config_dual_channel():
    llm = LLMConfig(
        text_config=ChannelConfig(provider="openai", model="gpt-4o", api_key="sk-t"),
        image_config=ChannelConfig(provider="nim", model="llama-3.2-11b", api_key="nvap-i"),
    )
    assert llm.image_config.provider == "nim"

def test_channel_config_api_key_excluded_from_serialization():
    ch = ChannelConfig(provider="openai", model="gpt-4o", api_key="sk-secret")
    assert "api_key" not in ch.model_dump()
    assert "api_key" not in ch.model_dump_json()


# ── Generic OAuthAuth (models/config.py) ─────────────────────────────────────

def test_oauth_auth_generic_round_trip():
    """Generic OAuthAuth has no platform-specific fields."""
    auth = OAuthAuth(
        auth_type="oauth",
        access_token="acc",
        refresh_token="ref",
        expires_at=9_999_999_999,
    )
    assert auth.auth_type == "oauth"
    assert auth.client_id == ""
    assert auth.client_secret is None


# ── Jira-specific auth models (connectors/jira/config.py) ────────────────────

def test_token_auth_round_trip():
    auth = TokenAuth(auth_type="token", email="a@b.com", api_token="tok")
    assert auth.auth_type == "token"
    assert auth.email == "a@b.com"


def test_jira_oauth_auth_round_trip():
    auth = JiraOAuthAuth(
        auth_type="oauth",
        access_token="acc",
        refresh_token="ref",
        expires_at=9_999_999_999,
        cloud_id="cloud-abc",
    )
    assert auth.auth_type == "oauth"
    assert auth.cloud_id == "cloud-abc"


# ── JiraConnection discriminated union ────────────────────────────────────────

def test_jira_connection_discriminated_union_token():
    conn = JiraConnection(
        domain="foo.atlassian.net",
        auth={"auth_type": "token", "email": "x@y.com", "api_token": "t"},
    )
    assert isinstance(conn.auth, TokenAuth)


def test_jira_connection_discriminated_union_oauth():
    conn = JiraConnection(
        domain="foo.atlassian.net",
        auth={"auth_type": "oauth", "access_token": "a", "refresh_token": "r",
              "expires_at": 999, "cloud_id": "c"},
    )
    assert isinstance(conn.auth, JiraOAuthAuth)


# ── BaseConnection extra fields ───────────────────────────────────────────────

def test_base_connection_ignores_extra_fields():
    """Unknown extra fields are discarded (extra='ignore') for security."""
    conn = BaseConnection(connector_type="jira", domain="x.atlassian.net", auth={"auth_type": "token", "email": "a@b.com", "api_token": "t"})
    dumped = json.loads(conn.model_dump_json())
    assert "auth" not in dumped
    assert dumped["connector_type"] == "jira"
    assert dumped["domain"] == "x.atlassian.net"


# ── AppConfig ─────────────────────────────────────────────────────────────────

def test_app_config_defaults_to_empty():
    config = AppConfig()
    assert config.connections == []
    assert config.llm_profiles == {}
    assert config.current_llm_profile is None
    assert config.active_llm is None


def test_app_config_json_round_trip():
    config = AppConfig(
        connections=[
            JiraConnection(
                domain="x.atlassian.net",
                auth=TokenAuth(auth_type="token", email="a@b.com", api_token="t"),
            )
        ],
        llm_profiles={"personal": LLMConfig(
            text_config=ChannelConfig(provider="ollama", model="llama3"),
            image_config=ChannelConfig(provider="ollama", model="llava"),
        )},
        current_llm_profile="personal",
    )
    restored = AppConfig.model_validate_json(config.model_dump_json())
    assert restored.connections[0].domain == "x.atlassian.net"
    assert restored.active_llm.text_config.provider == "ollama"


# ── RawIssueData ──────────────────────────────────────────────────────────────

def test_raw_issue_data_fields():
    raw = RawIssueData(
        issue_key="AB-1", issue_type="Bug", summary="Test",
        description="Desc", comments=[], attachments=[],
        priority="High", status="Open", metadata={},
    )
    assert raw.issue_key == "AB-1"


# ── IssueContext ──────────────────────────────────────────────────────────────

def test_issue_context_nullable_fields():
    ctx = IssueContext(
        problem_summary="p", detailed_description="d",
        reproduction_steps=[], expected_behavior=None, actual_behavior=None,
        acceptance_criteria=[], impact="i", priority="High", issue_type="Story",
        confidence_score=0.8, completeness_score=0.6, missing_information=[],
    )
    assert ctx.expected_behavior is None


def test_issue_context_score_range():
    ctx = IssueContext(
        problem_summary="p", detailed_description="d",
        reproduction_steps=[], expected_behavior=None, actual_behavior=None,
        acceptance_criteria=[], impact="i", priority="High", issue_type="Bug",
        confidence_score=0.5, completeness_score=0.5, missing_information=[],
    )
    assert 0.0 <= ctx.confidence_score <= 1.0
    assert 0.0 <= ctx.completeness_score <= 1.0


# ── AppConfig multi-profile LLM support ──────────────────────────────────────

def test_app_config_has_llm_profiles():
    config = AppConfig(
        llm_profiles={"personal": LLMConfig(text_config=ChannelConfig(provider="ollama", model="llama3"))},
        current_llm_profile="personal",
    )
    assert config.active_llm is not None
    assert config.active_llm.text_config.provider == "ollama"


def test_active_llm_returns_none_when_no_profile_set():
    assert AppConfig().active_llm is None


def test_active_llm_returns_none_for_missing_profile_name():
    config = AppConfig(current_llm_profile="nonexistent")
    assert config.active_llm is None



def test_app_config_json_round_trip_multi_profile():
    config = AppConfig(
        connections=[
            JiraConnection(
                domain="x.atlassian.net",
                auth=TokenAuth(auth_type="token", email="a@b.com", api_token="t"),
            )
        ],
        llm_profiles={
            "personal": LLMConfig(text_config=ChannelConfig(provider="ollama", model="llama3")),
            "work": LLMConfig(text_config=ChannelConfig(provider="openai", model="gpt-4o")),
        },
        current_llm_profile="work",
    )
    restored = AppConfig.model_validate_json(config.model_dump_json())
    assert restored.current_llm_profile == "work"
    assert restored.active_llm.text_config.provider == "openai"
    assert "personal" in restored.llm_profiles


# ── ConfigManager ─────────────────────────────────────────────────────────────

def test_config_manager_returns_empty_config_when_file_missing(isolated_config):
    from icx_engine.config_manager import ConfigManager
    assert ConfigManager.load().connections == []


def test_config_manager_save_and_load_round_trip(isolated_config):
    from icx_engine.config_manager import ConfigManager
    ConfigManager.save(AppConfig(
        llm_profiles={"personal": LLMConfig(
            text_config=ChannelConfig(provider="ollama", model="llama3"),
            image_config=ChannelConfig(provider="ollama", model="llava"),
        )},
        current_llm_profile="personal",
    ))
    loaded = ConfigManager.load()
    assert loaded.active_llm.text_config.provider == "ollama"
    assert isolated_config.exists()


def test_config_manager_atomic_write_creates_parent(isolated_config):
    from icx_engine.config_manager import ConfigManager
    ConfigManager.save(AppConfig())
    assert isolated_config.exists()


# ── Jira auth header builder ──────────────────────────────────────────────────

def test_build_auth_header_token():
    from icx_engine.connectors.jira.auth import build_auth_header
    conn = JiraConnection(
        domain="x.atlassian.net",
        auth=TokenAuth(auth_type="token", email="user@test.com", api_token="mytoken"),
    )
    expected = "Basic " + base64.b64encode(b"user@test.com:mytoken").decode()
    assert build_auth_header(conn) == expected


def test_build_auth_header_oauth():
    from icx_engine.connectors.jira.auth import build_auth_header
    conn = JiraConnection(
        domain="x.atlassian.net",
        auth=JiraOAuthAuth(
            auth_type="oauth", access_token="bearer-xyz",
            refresh_token="ref", expires_at=9_999_999_999, cloud_id="cid",
        ),
    )
    assert build_auth_header(conn) == "Bearer bearer-xyz"


# ── Phase 2: JiraOAuthAuth stores client_id ──────────────────────────────────

def test_jira_oauth_auth_stores_client_id():
    auth = JiraOAuthAuth(
        auth_type="oauth", access_token="a", refresh_token="r",
        expires_at=9_999_999_999, cloud_id="c", client_id="my-client-id",
    )
    assert auth.client_id == "my-client-id"


def test_jira_oauth_auth_client_id_defaults_empty():
    auth = JiraOAuthAuth(
        auth_type="oauth", access_token="a", refresh_token="r",
        expires_at=9_999_999_999, cloud_id="c",
    )
    assert auth.client_id == ""


# ── Phase 2: keychain integration ────────────────────────────────────────────

def _make_keychain_mock():
    store: dict = {}

    def _set(account, value):
        store[account] = value
        return True

    def _get(account):
        return store.get(account)

    def _del(account):
        store.pop(account, None)

    return store, _set, _get, _del


def test_config_manager_stores_token_in_keychain(isolated_config, monkeypatch):
    import icx_engine.config_manager as cm
    store, _set, _get, _del = _make_keychain_mock()
    monkeypatch.setattr(cm, "_keychain_ok", True)
    monkeypatch.setattr(cm, "_kset", _set)
    monkeypatch.setattr(cm, "_kget", _get)
    monkeypatch.setattr(cm, "_kdel", _del)

    config = AppConfig(
        connections=[
            JiraConnection(
                domain="x.atlassian.net",
                auth=TokenAuth(auth_type="token", email="u@test.com", api_token="secret-tok"),
            )
        ]
    )
    cm.ConfigManager.save(config)

    raw = json.loads(isolated_config.read_text())
    assert raw["connections"][0]["auth"]["api_token"] == cm._SENTINEL

    # Keychain uses new-format key
    assert store["jira_token:x.atlassian.net"] == "secret-tok"


def test_config_manager_loads_token_from_keychain(isolated_config, monkeypatch):
    import icx_engine.config_manager as cm
    store, _set, _get, _del = _make_keychain_mock()
    store["jira_token:x.atlassian.net"] = "secret-tok"
    monkeypatch.setattr(cm, "_keychain_ok", True)
    monkeypatch.setattr(cm, "_kset", _set)
    monkeypatch.setattr(cm, "_kget", _get)
    monkeypatch.setattr(cm, "_kdel", _del)

    skeleton = {
        "connections": [{
            "connector_type": "jira",
            "domain": "x.atlassian.net",
            "auth": {"auth_type": "token", "email": "u@test.com", "api_token": cm._SENTINEL},
        }],
        "default_connection": None,
    }
    isolated_config.parent.mkdir(parents=True, exist_ok=True)
    isolated_config.write_text(json.dumps(skeleton))

    loaded = cm.ConfigManager.load()
    assert loaded.connections[0].auth.api_token == "secret-tok"


def test_config_manager_plaintext_fallback_when_keychain_unavailable(isolated_config, monkeypatch):
    import icx_engine.config_manager as cm
    monkeypatch.setattr(cm, "_keychain_ok", False)

    config = AppConfig(
        connections=[
            JiraConnection(
                domain="x.atlassian.net",
                auth=TokenAuth(auth_type="token", email="u@test.com", api_token="plain-tok"),
            )
        ]
    )
    cm.ConfigManager.save(config)

    raw = json.loads(isolated_config.read_text())
    assert raw["connections"][0]["auth"]["api_token"] == "plain-tok"

    loaded = cm.ConfigManager.load()
    assert loaded.connections[0].auth.api_token == "plain-tok"


def test_config_manager_stores_llm_profile_in_keychain(isolated_config, monkeypatch):
    import icx_engine.config_manager as cm
    store, _set, _get, _del = _make_keychain_mock()
    monkeypatch.setattr(cm, "_keychain_ok", True)
    monkeypatch.setattr(cm, "_kset", _set)
    monkeypatch.setattr(cm, "_kget", _get)
    monkeypatch.setattr(cm, "_kdel", _del)

    config = AppConfig(
        llm_profiles={"work": LLMConfig(
            text_config=ChannelConfig(provider="openai", model="gpt-4o", api_key="sk-abc123"),
        )},
        current_llm_profile="work",
    )
    cm.ConfigManager.save(config)

    raw = json.loads(isolated_config.read_text())
    assert raw["llm_profiles"]["work"]["text_config"]["api_key"] == cm._SENTINEL
    assert store["llm_text:work"] == "sk-abc123"


def test_config_manager_loads_llm_profile_from_keychain(isolated_config, monkeypatch):
    import icx_engine.config_manager as cm
    store, _set, _get, _del = _make_keychain_mock()
    store["llm_text:work"] = "sk-abc123"
    monkeypatch.setattr(cm, "_keychain_ok", True)
    monkeypatch.setattr(cm, "_kset", _set)
    monkeypatch.setattr(cm, "_kget", _get)
    monkeypatch.setattr(cm, "_kdel", _del)

    skeleton = {
        "connections": [],
        "llm_profiles": {
            "work": {
                "text_config": {"provider": "openai", "model": "gpt-4o", "api_key": cm._SENTINEL},
                "image_config": None,
            }
        },
        "current_llm_profile": "work",
        "default_connection": None,
    }
    isolated_config.parent.mkdir(parents=True, exist_ok=True)
    isolated_config.write_text(json.dumps(skeleton))

    loaded = cm.ConfigManager.load()
    assert loaded.llm_profiles["work"].text_config.api_key == "sk-abc123"


def test_delete_connection_secrets(monkeypatch):
    import icx_engine.config_manager as cm
    store, _set, _get, _del = _make_keychain_mock()
    store["jira_token:xyz.atlassian.net"] = "tok"
    monkeypatch.setattr(cm, "_keychain_ok", True)
    monkeypatch.setattr(cm, "_kset", _set)
    monkeypatch.setattr(cm, "_kget", _get)
    monkeypatch.setattr(cm, "_kdel", _del)

    from icx_engine.connectors.jira.config import JiraConnection, TokenAuth
    conn = JiraConnection(
        domain="xyz.atlassian.net",
        auth=TokenAuth(auth_type="token", email="u@test.com", api_token="tok"),
    )
    cm.ConfigManager.delete_connection_secrets(conn)
    assert "jira_token:xyz.atlassian.net" not in store


def test_delete_llm_profile_secrets(monkeypatch):
    import icx_engine.config_manager as cm
    store, _set, _get, _del = _make_keychain_mock()
    store["llm_text:work"] = "sk-abc"
    store["llm_image:work"] = "sk-img"
    monkeypatch.setattr(cm, "_keychain_ok", True)
    monkeypatch.setattr(cm, "_kset", _set)
    monkeypatch.setattr(cm, "_kget", _get)
    monkeypatch.setattr(cm, "_kdel", _del)

    cm.ConfigManager.delete_llm_profile_secrets("work")
    assert "llm_text:work" not in store
    assert "llm_image:work" not in store


def test_config_manager_stores_llm_text_in_keychain(isolated_config, monkeypatch):
    import icx_engine.config_manager as cm
    store, _set, _get, _del = _make_keychain_mock()
    monkeypatch.setattr(cm, "_keychain_ok", True)
    monkeypatch.setattr(cm, "_kset", _set)
    monkeypatch.setattr(cm, "_kget", _get)
    monkeypatch.setattr(cm, "_kdel", _del)

    config = AppConfig(
        llm_profiles={"work": LLMConfig(
            text_config=ChannelConfig(provider="openai", model="gpt-4o", api_key="sk-text"),
            image_config=ChannelConfig(provider="nim", model="llava", api_key="nvap-img"),
        )},
        current_llm_profile="work",
    )
    cm.ConfigManager.save(config)

    raw = json.loads(isolated_config.read_text())
    assert raw["llm_profiles"]["work"]["text_config"]["api_key"] == cm._SENTINEL
    assert raw["llm_profiles"]["work"]["image_config"]["api_key"] == cm._SENTINEL
    assert store["llm_text:work"] == "sk-text"
    assert store["llm_image:work"] == "nvap-img"


def test_config_manager_loads_llm_channels_from_keychain(isolated_config, monkeypatch):
    import icx_engine.config_manager as cm
    store, _set, _get, _del = _make_keychain_mock()
    store["llm_text:work"] = "sk-text"
    store["llm_image:work"] = "nvap-img"
    monkeypatch.setattr(cm, "_keychain_ok", True)
    monkeypatch.setattr(cm, "_kset", _set)
    monkeypatch.setattr(cm, "_kget", _get)
    monkeypatch.setattr(cm, "_kdel", _del)

    skeleton = {
        "connections": [],
        "llm_profiles": {
            "work": {
                "text_config": {"provider": "openai", "model": "gpt-4o", "api_key": cm._SENTINEL},
                "image_config": {"provider": "nim", "model": "llava", "api_key": cm._SENTINEL},
            }
        },
        "current_llm_profile": "work",
        "default_connection": None,
    }
    isolated_config.parent.mkdir(parents=True, exist_ok=True)
    isolated_config.write_text(json.dumps(skeleton))

    loaded = cm.ConfigManager.load()
    assert loaded.llm_profiles["work"].text_config.api_key == "sk-text"
    assert loaded.llm_profiles["work"].image_config.api_key == "nvap-img"


def test_delete_llm_profile_secrets_removes_both_slots(monkeypatch):
    import icx_engine.config_manager as cm
    store, _set, _get, _del = _make_keychain_mock()
    store["llm_text:work"] = "sk-text"
    store["llm_image:work"] = "nvap-img"
    monkeypatch.setattr(cm, "_keychain_ok", True)
    monkeypatch.setattr(cm, "_kset", _set)
    monkeypatch.setattr(cm, "_kget", _get)
    monkeypatch.setattr(cm, "_kdel", _del)

    cm.ConfigManager.delete_llm_profile_secrets("work")
    assert "llm_text:work" not in store
    assert "llm_image:work" not in store


# ── Phase 5: OAuth/keychain double-lock ──────────────────────────────────────

def test_secret_fields_excluded_from_serialization():
    """All credential fields are stripped from model_dump() - the double-lock guarantee."""
    auth_token = TokenAuth(auth_type="token", email="u@t.com", api_token="tok")
    dumped = auth_token.model_dump()
    assert "api_token" not in dumped

    auth_oauth = JiraOAuthAuth(
        auth_type="oauth", access_token="acc", refresh_token="ref",
        expires_at=9_999_999_999, cloud_id="c", client_secret="sec",
    )
    dumped_oauth = auth_oauth.model_dump()
    assert "access_token" not in dumped_oauth
    assert "refresh_token" not in dumped_oauth
    assert "client_secret" not in dumped_oauth

    ch = ChannelConfig(provider="openai", model="gpt-4o", api_key="sk-xyz")
    assert "api_key" not in ch.model_dump()


def test_config_manager_stores_oauth_tokens_in_keychain(isolated_config, monkeypatch):
    import icx_engine.config_manager as cm
    store, _set, _get, _del = _make_keychain_mock()
    monkeypatch.setattr(cm, "_keychain_ok", True)
    monkeypatch.setattr(cm, "_kset", _set)
    monkeypatch.setattr(cm, "_kget", _get)
    monkeypatch.setattr(cm, "_kdel", _del)

    config = AppConfig(
        connections=[
            JiraConnection(
                domain="x.atlassian.net",
                auth=JiraOAuthAuth(
                    auth_type="oauth", access_token="acc-tok", refresh_token="ref-tok",
                    expires_at=9_999_999_999, cloud_id="cid", client_secret="cli-sec",
                ),
            )
        ]
    )
    cm.ConfigManager.save(config)

    raw = json.loads(isolated_config.read_text())
    auth_raw = raw["connections"][0]["auth"]
    assert auth_raw["access_token"] == cm._SENTINEL
    assert auth_raw["refresh_token"] == cm._SENTINEL
    assert auth_raw["client_secret"] == cm._SENTINEL

    assert store["oauth_access:x.atlassian.net"] == "acc-tok"
    assert store["oauth_refresh:x.atlassian.net"] == "ref-tok"
    assert store["oauth_secret:x.atlassian.net"] == "cli-sec"


def test_config_manager_loads_oauth_tokens_from_keychain(isolated_config, monkeypatch):
    import icx_engine.config_manager as cm
    store, _set, _get, _del = _make_keychain_mock()
    store["oauth_access:x.atlassian.net"] = "acc-tok"
    store["oauth_refresh:x.atlassian.net"] = "ref-tok"
    store["oauth_secret:x.atlassian.net"] = "cli-sec"
    monkeypatch.setattr(cm, "_keychain_ok", True)
    monkeypatch.setattr(cm, "_kset", _set)
    monkeypatch.setattr(cm, "_kget", _get)
    monkeypatch.setattr(cm, "_kdel", _del)

    skeleton = {
        "connections": [{
            "connector_type": "jira",
            "domain": "x.atlassian.net",
            "auth": {
                "auth_type": "oauth",
                "access_token": cm._SENTINEL,
                "refresh_token": cm._SENTINEL,
                "expires_at": 9_999_999_999,
                "cloud_id": "cid",
                "client_secret": cm._SENTINEL,
            },
        }],
        "default_connection": None,
    }
    isolated_config.parent.mkdir(parents=True, exist_ok=True)
    isolated_config.write_text(json.dumps(skeleton))

    loaded = cm.ConfigManager.load()
    auth = loaded.connections[0].auth
    assert auth.access_token == "acc-tok"
    assert auth.refresh_token == "ref-tok"
    assert auth.client_secret == "cli-sec"


def test_config_manager_oauth_plaintext_fallback_when_keychain_unavailable(isolated_config, monkeypatch):
    import icx_engine.config_manager as cm
    monkeypatch.setattr(cm, "_keychain_ok", False)

    config = AppConfig(
        connections=[
            JiraConnection(
                domain="x.atlassian.net",
                auth=JiraOAuthAuth(
                    auth_type="oauth", access_token="acc-plain", refresh_token="ref-plain",
                    expires_at=9_999_999_999, cloud_id="cid",
                ),
            )
        ]
    )
    cm.ConfigManager.save(config)

    raw = json.loads(isolated_config.read_text())
    auth_raw = raw["connections"][0]["auth"]
    assert auth_raw["access_token"] == "acc-plain"
    assert auth_raw["refresh_token"] == "ref-plain"

    loaded = cm.ConfigManager.load()
    auth = loaded.connections[0].auth
    assert auth.access_token == "acc-plain"
    assert auth.refresh_token == "ref-plain"


# ── Concurrent write safety ───────────────────────────────────────────────────

def test_pid_based_temp_file_name(isolated_config, monkeypatch):
    import icx_engine.config_manager as cm
    from pathlib import Path
    monkeypatch.setattr(cm, "_keychain_ok", False)
    seen_paths: list[str] = []
    original_replace = Path.replace

    def spy_replace(self, target):
        seen_paths.append(str(self))
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", spy_replace)
    cm.ConfigManager.save(AppConfig())

    tmp_writes = [p for p in seen_paths if ".tmp." in p]
    assert tmp_writes, "expected a .tmp.<PID> staging file"
    assert str(os.getpid()) in tmp_writes[0]


def test_no_orphaned_temp_files_after_save(isolated_config, monkeypatch):
    import icx_engine.config_manager as cm
    monkeypatch.setattr(cm, "_keychain_ok", False)
    cm.ConfigManager.save(AppConfig())
    leftovers = list(isolated_config.parent.glob("*.tmp.*"))
    assert not leftovers, f"orphaned temp files: {leftovers}"


def test_lock_file_removed_after_save(isolated_config, monkeypatch):
    import icx_engine.config_manager as cm
    monkeypatch.setattr(cm, "_keychain_ok", False)
    lock_path = cm.CONFIG_PATH.with_suffix(".lock")
    cm.ConfigManager.save(AppConfig())
    assert not lock_path.exists(), "lock file should be removed after save"


def test_lock_file_removed_after_save_exception(isolated_config, monkeypatch):
    import icx_engine.config_manager as cm
    monkeypatch.setattr(cm, "_keychain_ok", False)
    lock_path = cm.CONFIG_PATH.with_suffix(".lock")

    # Force an error during the atomic replace to confirm the lock is still released.
    original_replace = Path.replace

    def fail_replace(self, target):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated replace failure"):
        cm.ConfigManager.save(AppConfig())

    assert not lock_path.exists(), "lock must be released even when save raises"


def test_concurrent_saves_produce_valid_config(isolated_config, monkeypatch):
    import threading
    import icx_engine.config_manager as cm
    monkeypatch.setattr(cm, "_keychain_ok", False)

    errors: list[Exception] = []

    def save_worker(key: str) -> None:
        try:
            config = AppConfig(
                llm_profiles={"p": LLMConfig(
                    text_config=ChannelConfig(provider="openai", model="gpt-4o", api_key=key),
                )},
                current_llm_profile="p",
            )
            cm.ConfigManager.save(config)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=save_worker, args=(f"sk-{i}",)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"concurrent save errors: {errors}"
    # File must be valid JSON and loadable - no partial writes
    raw = json.loads(isolated_config.read_text())
    assert isinstance(raw, dict)
    loaded = cm.ConfigManager.load()
    assert loaded.current_llm_profile == "p"
    # No orphaned temp or lock files
    assert not list(isolated_config.parent.glob("*.tmp.*"))
    assert not isolated_config.with_suffix(".lock").exists()


def test_oauth_plaintext_fallback_warns_to_stderr(isolated_config, monkeypatch, capsys):
    import icx_engine.config_manager as cm
    monkeypatch.setattr(cm, "_keychain_ok", False)
    config = AppConfig(connections=[JiraConnection(
        domain="x.atlassian.net",
        auth=JiraOAuthAuth(auth_type="oauth", access_token="a", refresh_token="r",
                           expires_at=9_999_999_999, cloud_id="c"),
    )])
    cm.ConfigManager.save(config)
    captured = capsys.readouterr()
    assert "keyring unavailable" in captured.err
    assert "access_token" in captured.err


def test_delete_llm_image_secrets_removes_only_image_slot(monkeypatch):
    import icx_engine.config_manager as cm
    store, _set, _get, _del = _make_keychain_mock()
    store["llm_text:work"] = "sk-text"
    store["llm_image:work"] = "nvap-img"
    monkeypatch.setattr(cm, "_keychain_ok", True)
    monkeypatch.setattr(cm, "_kdel", _del)
    cm.ConfigManager.delete_llm_image_secrets("work")
    assert "llm_image:work" not in store   # deleted
    assert "llm_text:work" in store        # untouched


# ── D-Lock encryption ─────────────────────────────────────────────────────────

def test_dlock_encrypt_decrypt_roundtrip(monkeypatch):
    import icx_engine.config_manager as cm
    store, _set, _get, _del = _make_keychain_mock()
    monkeypatch.setattr(cm, "_keychain_ok", True)
    monkeypatch.setattr(cm, "_kset", _set)
    monkeypatch.setattr(cm, "_kget", _get)

    value = "x" * 600
    encrypted = cm._dlock_encrypt(value)
    assert encrypted.startswith(cm._DLOCK_PREFIX)
    assert cm._dlock_decrypt(encrypted) == value


def test_dlock_encrypt_produces_unique_ciphertext(monkeypatch):
    import icx_engine.config_manager as cm
    store, _set, _get, _del = _make_keychain_mock()
    monkeypatch.setattr(cm, "_keychain_ok", True)
    monkeypatch.setattr(cm, "_kset", _set)
    monkeypatch.setattr(cm, "_kget", _get)

    value = "secret" * 100
    assert cm._dlock_encrypt(value) != cm._dlock_encrypt(value)


def test_dlock_long_oauth_token_stored_in_config_not_keychain(isolated_config, monkeypatch):
    import icx_engine.config_manager as cm
    store, _set, _get, _del = _make_keychain_mock()
    monkeypatch.setattr(cm, "_keychain_ok", True)
    monkeypatch.setattr(cm, "_kset", _set)
    monkeypatch.setattr(cm, "_kget", _get)
    monkeypatch.setattr(cm, "_kdel", _del)

    long_token = "eyJhbGci" * 80  # 640 bytes - over threshold
    config = AppConfig(connections=[JiraConnection(
        domain="x.atlassian.net",
        auth=JiraOAuthAuth(
            auth_type="oauth",
            access_token=long_token,
            refresh_token="short-ref",
            expires_at=9_999_999_999,
            cloud_id="cloud-abc",
        ),
    )])
    cm.ConfigManager.save(config)

    raw = json.loads(isolated_config.read_text())
    stored = raw["connections"][0]["auth"]["access_token"]
    assert stored.startswith(cm._DLOCK_PREFIX)
    assert "oauth_access:x.atlassian.net" not in store  # NOT written to keychain


def test_dlock_load_decrypts_access_token(isolated_config, monkeypatch):
    import icx_engine.config_manager as cm
    store, _set, _get, _del = _make_keychain_mock()
    monkeypatch.setattr(cm, "_keychain_ok", True)
    monkeypatch.setattr(cm, "_kset", _set)
    monkeypatch.setattr(cm, "_kget", _get)
    monkeypatch.setattr(cm, "_kdel", _del)

    long_token = "eyJhbGci" * 80
    config = AppConfig(connections=[JiraConnection(
        domain="x.atlassian.net",
        auth=JiraOAuthAuth(
            auth_type="oauth",
            access_token=long_token,
            refresh_token="short-ref",
            expires_at=9_999_999_999,
            cloud_id="cloud-abc",
        ),
    )])
    cm.ConfigManager.save(config)
    loaded = cm.ConfigManager.load()
    assert loaded.connections[0].auth.access_token == long_token


def test_dlock_short_token_still_goes_to_keychain(isolated_config, monkeypatch):
    import icx_engine.config_manager as cm
    store, _set, _get, _del = _make_keychain_mock()
    monkeypatch.setattr(cm, "_keychain_ok", True)
    monkeypatch.setattr(cm, "_kset", _set)
    monkeypatch.setattr(cm, "_kget", _get)
    monkeypatch.setattr(cm, "_kdel", _del)

    config = AppConfig(connections=[JiraConnection(
        domain="x.atlassian.net",
        auth=TokenAuth(auth_type="token", email="a@b.com", api_token="short-tok"),
    )])
    cm.ConfigManager.save(config)
    raw = json.loads(isolated_config.read_text())
    assert raw["connections"][0]["auth"]["api_token"] == cm._SENTINEL
    assert store["jira_token:x.atlassian.net"] == "short-tok"


def test_dlock_decrypt_raises_config_error_on_tampered_data(monkeypatch):
    import icx_engine.config_manager as cm
    from icx_engine.exceptions import ConfigError
    store, _set, _get, _del = _make_keychain_mock()
    monkeypatch.setattr(cm, "_keychain_ok", True)
    monkeypatch.setattr(cm, "_kset", _set)
    monkeypatch.setattr(cm, "_kget", _get)

    encrypted = cm._dlock_encrypt("test-value")
    tampered = encrypted[:-4] + "XXXX"
    with pytest.raises(ConfigError):
        cm._dlock_decrypt(tampered)


def test_dlock_long_llm_api_key_stored_in_config_not_keychain(isolated_config, monkeypatch):
    import icx_engine.config_manager as cm
    store, _set, _get, _del = _make_keychain_mock()
    monkeypatch.setattr(cm, "_keychain_ok", True)
    monkeypatch.setattr(cm, "_kset", _set)
    monkeypatch.setattr(cm, "_kget", _get)
    monkeypatch.setattr(cm, "_kdel", _del)

    long_key = "sk-" + "x" * 520  # 523 bytes - over threshold
    config = AppConfig(
        llm_profiles={"work": LLMConfig(
            text_config=ChannelConfig(provider="openai", model="gpt-4o", api_key=long_key),
        )},
        current_llm_profile="work",
    )
    cm.ConfigManager.save(config)
    raw = json.loads(isolated_config.read_text())
    stored = raw["llm_profiles"]["work"]["text_config"]["api_key"]
    assert stored.startswith(cm._DLOCK_PREFIX)
    assert "llm_text:work" not in store


# ── Keyring read-only probe and caching ──────────────────────────────────────

def test_keyring_available_does_not_write_to_keychain():
    """_keyring_available() is a read-only probe - set_password must never be called."""
    import icx_engine.config_manager as cm
    with patch("keyring.get_password", return_value=None) as mock_get, \
         patch("keyring.set_password") as mock_set:
        result = cm._keyring_available()
    mock_get.assert_called_once()
    mock_set.assert_not_called()
    assert result is True


def test_check_keychain_caches_result(monkeypatch):
    """_check_keychain() calls _keyring_available() exactly once regardless of repeated calls."""
    import icx_engine.config_manager as cm
    call_count = [0]

    def _counting_probe() -> bool:
        call_count[0] += 1
        return True

    monkeypatch.setattr(cm, "_keychain_ok", None)
    monkeypatch.setattr(cm, "_keyring_available", _counting_probe)

    cm._check_keychain()
    cm._check_keychain()
    cm._check_keychain()

    assert call_count[0] == 1


def test_issue_context_pending_images_defaults_empty():
    ctx = IssueContext(
        problem_summary="p", detailed_description="d",
        reproduction_steps=[], expected_behavior=None, actual_behavior=None,
        acceptance_criteria=[], impact="i", priority="High", issue_type="Bug",
        confidence_score=0.9, completeness_score=0.9, missing_information=[],
    )
    assert ctx.pending_images == []


def test_issue_context_pending_images_populated():
    ctx = IssueContext(
        problem_summary="p", detailed_description="d",
        reproduction_steps=[], expected_behavior=None, actual_behavior=None,
        acceptance_criteria=[], impact="i", priority="High", issue_type="Bug",
        confidence_score=0.9, completeness_score=0.9, missing_information=[],
        pending_images=["screenshot.png", "error.jpeg"],
    )
    assert ctx.pending_images == ["screenshot.png", "error.jpeg"]


def test_issue_context_pending_images_in_json_output():
    ctx = IssueContext(
        problem_summary="p", detailed_description="d",
        reproduction_steps=[], expected_behavior=None, actual_behavior=None,
        acceptance_criteria=[], impact="i", priority="High", issue_type="Bug",
        confidence_score=0.9, completeness_score=0.9, missing_information=[],
        pending_images=["diagram.png"],
    )
    data = json.loads(ctx.model_dump_json())
    assert data["pending_images"] == ["diagram.png"]

