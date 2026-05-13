"""Shared test constants - imported by conftest.py and individual test modules."""
import json
from icx_engine.auth.token import build_basic_auth_header

JIRA_DOMAIN = "test.atlassian.net"
JIRA_BASE_URL = f"https://{JIRA_DOMAIN}/rest/api/3"
JIRA_ALLOWED_HOSTS = {JIRA_DOMAIN}

_EMAIL = "user@test.com"
_API_TOKEN = "token123"
JIRA_AUTH_HEADER = build_basic_auth_header(_EMAIL, _API_TOKEN)

JIRA_ISSUE_PAYLOAD = {
    "key": "TEST-123",
    "fields": {
        "summary": "Button not working on mobile",
        "description": {
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": "Steps to reproduce the issue."}],
                }
            ],
        },
        "comment": {
            "comments": [
                {
                    "body": {
                        "type": "doc",
                        "content": [
                            {
                                "type": "paragraph",
                                "content": [{"type": "text", "text": "Reproduced on iOS 17."}],
                            }
                        ],
                    }
                }
            ]
        },
        "attachment": [
            {
                "filename": "screenshot.png",
                "content": "https://test.atlassian.net/rest/api/3/attachment/content/10001",
                "mimeType": "image/png",
            }
        ],
        "issuetype": {"name": "Bug"},
        "priority": {"name": "High"},
        "status": {"name": "In Progress"},
        "reporter": {"displayName": "Jane"},
        "assignee": {"displayName": "John"},
        "duedate": "2026-06-01",
    },
}

# issue_type is intentionally "Story" - engine must override it from raw ticket data.
MOCK_LLM_JSON = json.dumps({
    "problem_summary": "Submit button broken",
    "detailed_description": "Tapping submit on iOS Safari produces no response.",
    "reproduction_steps": ["Open on iOS Safari", "Tap submit"],
    "expected_behavior": "Form submits",
    "actual_behavior": "Nothing happens",
    "acceptance_criteria": [],
    "impact": "Blocks mobile users",
    "priority": "High",
    "issue_type": "Story",
    "confidence_score": 0.9,
    "completeness_score": 0.75,
    "missing_information": [],
})
