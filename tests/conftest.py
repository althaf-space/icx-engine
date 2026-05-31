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
    config_file = tmp_path / ".icx" / "config.json"
    monkeypatch.setattr("icx_engine.config_manager.CONFIG_PATH", config_file)
    return config_file
