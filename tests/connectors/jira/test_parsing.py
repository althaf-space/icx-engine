"""Jira connector input parsing tests.

All URL and bare-key parsing that is Jira-specific lives here,
not in the generic engine tests.
"""
import pytest

from icx_engine.connectors.jira.connector import JiraConnector
from icx_engine.connectors.jira.config import JiraConnection, TokenAuth
from icx_engine.exceptions import InvalidInput

from test_data import JIRA_DOMAIN


@pytest.fixture
def connector():
    conn = JiraConnection(
        domain=JIRA_DOMAIN,
        auth=TokenAuth(auth_type="token", email="u@test.com", api_token="tok"),
    )
    return JiraConnector(conn)


# -- Bare key ------------------------------------------------------------------

def test_parse_input_bare_key(connector):
    parsed = connector.parse_input("ABC-123")
    assert parsed.issue_key == "ABC-123"


def test_parse_input_bare_key_lowercased_is_uppercased(connector):
    parsed = connector.parse_input("abc-123")
    assert parsed.issue_key == "ABC-123"


def test_parse_input_bare_key_alphanumeric_project(connector):
    parsed = connector.parse_input("AI6D-362")
    assert parsed.issue_key == "AI6D-362"


def test_parse_input_bare_key_alphanumeric_project_lowercase(connector):
    parsed = connector.parse_input("ai6d-362")
    assert parsed.issue_key == "AI6D-362"


# -- Standard URL patterns -----------------------------------------------------

def test_parse_input_full_browse_url(connector):
    parsed = connector.parse_input(f"https://{JIRA_DOMAIN}/browse/ABC-123")
    assert parsed.issue_key == "ABC-123"


def test_parse_input_browse_url_alphanumeric_project(connector):
    parsed = connector.parse_input(f"https://{JIRA_DOMAIN}/browse/AI6D-362")
    assert parsed.issue_key == "AI6D-362"


def test_parse_input_full_issues_url(connector):
    parsed = connector.parse_input(f"https://{JIRA_DOMAIN}/issues/ABC-123")
    assert parsed.issue_key == "ABC-123"


def test_parse_input_no_scheme_url(connector):
    parsed = connector.parse_input(f"{JIRA_DOMAIN}/browse/ABC-123")
    assert parsed.issue_key == "ABC-123"


def test_parse_input_url_with_trailing_query_params(connector):
    parsed = connector.parse_input(f"https://{JIRA_DOMAIN}/browse/ABC-123?atlOrigin=xyz")
    assert parsed.issue_key == "ABC-123"


# -- Board / backlog URL -------------------------------------------------------

def test_parse_input_selected_issue_query_param(connector):
    parsed = connector.parse_input(
        f"https://{JIRA_DOMAIN}/jira/software/projects/ABC/boards/1?selectedIssue=ABC-42"
    )
    assert parsed.issue_key == "ABC-42"


# -- Invalid inputs ------------------------------------------------------------

def test_parse_input_invalid_string_raises(connector):
    with pytest.raises(InvalidInput):
        connector.parse_input("not-a-key")


def test_parse_input_bad_url_path_raises(connector):
    with pytest.raises(InvalidInput):
        connector.parse_input(f"https://{JIRA_DOMAIN}/projects/ABC-123")


def test_parse_input_browse_url_non_key_raises(connector):
    # /browse/<segment> that is not a valid issue key must be rejected at parse
    # time rather than passed through to fetch (finding C4).
    with pytest.raises(InvalidInput):
        connector.parse_input(f"https://{JIRA_DOMAIN}/browse/not-a-key")


def test_parse_input_issues_url_non_key_raises(connector):
    with pytest.raises(InvalidInput):
        connector.parse_input(f"https://{JIRA_DOMAIN}/issues/random-page")
