"""Tests for JiraConnector classmethods used by the generic connector registry."""
from icx_engine.connectors.jira.connector import JiraConnector


def test_extract_bare_key_from_ref_bare_key():
    assert JiraConnector.extract_bare_key_from_ref("PROJ-123") == "PROJ-123"


def test_extract_bare_key_from_ref_browse_url():
    assert JiraConnector.extract_bare_key_from_ref(
        "https://foo.atlassian.net/browse/PROJ-123"
    ) == "PROJ-123"


def test_extract_bare_key_from_ref_lowercase_normalised():
    assert JiraConnector.extract_bare_key_from_ref("proj-123") == "PROJ-123"


def test_extract_bare_key_from_ref_no_match_returns_none():
    assert JiraConnector.extract_bare_key_from_ref("not-a-valid-ref") is None


def test_extract_project_key_default_split():
    assert JiraConnector.extract_project_key("PROJ-123") == "PROJ"
