"""
Jira API response → RawIssueData mapping.

Responsible for all data transformation from Jira's JSON format into the
platform-agnostic RawIssueData contract. Kept separate from the HTTP client
so the two concerns - network and data mapping - stay independent.
"""
from __future__ import annotations

from icx_engine.models.output import RawIssueData


def _adf_to_text(node: dict | None) -> str:
    """Recursively extract plain text from Atlassian Document Format (ADF)."""
    if node is None:
        return ""
    parts: list[str] = []

    def _walk(n: dict) -> None:
        if n.get("type") == "text":
            parts.append(n.get("text", ""))
        for child in n.get("content", []):
            _walk(child)
        if n.get("type") in ("paragraph", "heading", "listItem"):
            parts.append("\n")

    _walk(node)
    return "".join(parts).strip()


def parse_issue_response(issue_key: str, data: dict) -> RawIssueData:
    """Map a Jira REST API issue response dict to RawIssueData."""
    fields = data.get("fields", {})

    desc_raw = fields.get("description")
    description = (
        _adf_to_text(desc_raw) if isinstance(desc_raw, dict) else str(desc_raw or "")
    )

    comments: list[str] = []
    for c in fields.get("comment", {}).get("comments", []):
        body = c.get("body", "")
        comments.append(_adf_to_text(body) if isinstance(body, dict) else str(body))

    attachments = [a["filename"] for a in fields.get("attachment", []) if a.get("filename")]
    attachment_content_urls = {
        a["filename"]: a["content"]
        for a in fields.get("attachment", [])
        if a.get("filename") and a.get("content")
    }

    return RawIssueData(
        issue_key=issue_key,
        issue_type=fields.get("issuetype", {}).get("name", "Unknown"),
        summary=fields.get("summary", ""),
        description=description,
        comments=comments,
        attachments=attachments,
        priority=fields.get("priority", {}).get("name", "Unknown"),
        status=fields.get("status", {}).get("name", "Unknown"),
        metadata={
            "project": issue_key.split("-")[0],
            "reporter": (fields.get("reporter") or {}).get("displayName", ""),
            "assignee": (fields.get("assignee") or {}).get("displayName", ""),
        },
        due_date=fields.get("duedate"),
        attachment_content_urls=attachment_content_urls,
    )
