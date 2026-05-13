"""Jira API response parsing tests."""
import pytest
import respx
import httpx

from icx_engine.connectors.jira.parser import _adf_to_text, parse_issue_response
from icx_engine.connectors.jira.client import JiraClient

from test_data import JIRA_BASE_URL, JIRA_AUTH_HEADER, JIRA_ISSUE_PAYLOAD


def test_parse_issue_response_maps_standard_fields():
    raw = parse_issue_response("TEST-123", JIRA_ISSUE_PAYLOAD)
    assert raw.issue_key == "TEST-123"
    assert raw.issue_type == "Bug"
    assert raw.summary == "Button not working on mobile"
    assert raw.priority == "High"
    assert raw.status == "In Progress"
    assert raw.due_date == "2026-06-01"
    assert raw.metadata["reporter"] == "Jane"
    assert raw.metadata["assignee"] == "John"


def test_parse_issue_response_adf_description():
    raw = parse_issue_response("TEST-123", JIRA_ISSUE_PAYLOAD)
    assert "Steps to reproduce" in raw.description


def test_parse_issue_response_adf_comments():
    raw = parse_issue_response("TEST-123", JIRA_ISSUE_PAYLOAD)
    assert len(raw.comments) == 1
    assert "Reproduced on iOS 17" in raw.comments[0]


def test_parse_issue_response_attachment_urls():
    raw = parse_issue_response("TEST-123", JIRA_ISSUE_PAYLOAD)
    assert raw.attachment_content_urls == {
        "screenshot.png": "https://test.atlassian.net/rest/api/3/attachment/content/10001"
    }
