import pytest
from typer.testing import CliRunner

from icx_engine.models.config import AppConfig, BaseConnection, LLMConfig, ChannelConfig
from icx_engine.connectors.jira.config import JiraConnection, TokenAuth, JiraOAuthAuth
from icx_engine.models.output import RawIssueData, IssueContext
from test_data import JIRA_DOMAIN, _EMAIL, _API_TOKEN


@pytest.fixture(scope="session")
def cli_runner():
    return CliRunner()


@pytest.fixture
def token_connection():
    return JiraConnection(
        domain=JIRA_DOMAIN,
        auth=TokenAuth(auth_type="token", email=_EMAIL, api_token=_API_TOKEN),
    )


@pytest.fixture
def oauth_connection():
    return JiraConnection(
        domain=JIRA_DOMAIN,
        auth=JiraOAuthAuth(
            auth_type="oauth",
            access_token="acc-token-xyz",
            refresh_token="ref-token-xyz",
            expires_at=9_999_999_999,
            cloud_id="cloud-abc",
        ),
    )


@pytest.fixture
def app_config(token_connection):
    return AppConfig(
        connections=[token_connection],
        llm_profiles={"personal": LLMConfig(
            text_config=ChannelConfig(provider="ollama", model="llama3"),
            image_config=ChannelConfig(provider="ollama", model="llava"),
        )},
        current_llm_profile="personal",
    )


@pytest.fixture
def multi_connection_config():
    return AppConfig(
        connections=[
            JiraConnection(
                domain="alpha.atlassian.net",
                auth=TokenAuth(auth_type="token", email="a@alpha.com", api_token="tok-a"),
            ),
            JiraConnection(
                domain="beta.atlassian.net",
                auth=TokenAuth(auth_type="token", email="b@beta.com", api_token="tok-b"),
            ),
        ],
        llm_profiles={"personal": LLMConfig(
            text_config=ChannelConfig(provider="ollama", model="llama3"),
            image_config=ChannelConfig(provider="ollama", model="llava"),
        )},
        current_llm_profile="personal",
    )


@pytest.fixture
def raw_ticket():
    return RawIssueData(
        issue_key="TEST-123",
        issue_type="Bug",
        summary="Button not working on mobile",
        description="Steps to reproduce the issue.",
        comments=["Reproduced on iOS 17, Safari."],
        attachments=["screenshot.png"],
        priority="High",
        status="In Progress",
        metadata={"project": "TEST", "reporter": "Jane", "assignee": "John"},
        due_date="2026-06-01",
        attachment_content_urls={"screenshot.png": "https://test.atlassian.net/rest/api/3/attachment/content/10001"},
        attachment_texts={},
    )


@pytest.fixture
def jira_context():
    return IssueContext(
        problem_summary="Submit button unresponsive on mobile",
        detailed_description="Tapping submit on iOS Safari produces no response.",
        reproduction_steps=["Open on iOS Safari", "Tap submit"],
        expected_behavior="Form submits",
        actual_behavior="Nothing happens",
        acceptance_criteria=[],
        impact="Blocks mobile users from submitting",
        priority="High",
        issue_type="Bug",
        confidence_score=0.9,
        completeness_score=0.75,
        missing_information=[],
    )


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    """Isolates ConfigManager from every piece of real machine state, not just the
    plaintext config file. Two confirmed real flakes (both under pytest-xdist)
    traced back to this fixture previously patching only `CONFIG_PATH`:
    `test_workstatus_connect_command_saves_connection` read back a stale
    'Bearer x' from the real OS keyring instead of the value it had just saved,
    and `test_langfuse_enable_and_disable_toggle_config` similarly raced on
    `langfuse.secret_key`'s real keyring entry. Root cause: any test that
    exercises a real `ConfigManager.save()`/`load()` round trip (rather than
    mocking `ConfigManager.save` outright) reaches through `_kset`/`_kget` to the
    REAL system keyring - global, persistent, shared across every xdist worker
    and every pytest invocation on the machine, keyed only by connection/field
    name with no per-test-run namespacing. `_master_key_cache` (an in-process
    cache of the D-Lock encryption key) and `_MASTER_KEY_FILE` (its DPAPI file
    cache, computed once from the real home directory at import time - never
    re-derived from a patched `CONFIG_PATH`) compound this across tests sharing
    an xdist worker process. Fixed generically here so every one of this
    fixture's ~10 consuming test files gets it for free, matching the pattern
    several of them were already hand-rolling per-test (see
    tests/sonar/test_connection_config.py, tests/test_models.py) - individual
    tests remain free to override `_keychain_ok`/`_kset`/`_kget`/`_kdel` again to
    exercise the plaintext/env-var fallback path deliberately."""
    import icx_engine.config_manager as cm

    config_file = tmp_path / ".icx" / "config.json"
    monkeypatch.setattr(cm, "CONFIG_PATH", config_file)
    monkeypatch.setattr(cm, "_MASTER_KEY_FILE", tmp_path / ".icx" / ".master_key")
    monkeypatch.setattr(cm, "_master_key_cache", None)

    fake_keyring: dict[str, str] = {}
    monkeypatch.setattr(cm, "_keychain_ok", True)
    monkeypatch.setattr(cm, "_kset", lambda account, value: fake_keyring.__setitem__(account, value) or True)
    monkeypatch.setattr(cm, "_kget", lambda account: fake_keyring.get(account))
    monkeypatch.setattr(cm, "_kdel", lambda account: fake_keyring.pop(account, None))

    return config_file
