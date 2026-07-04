"""Parse a SonarQube dashboard URL into its parts.

Used by `configure` so a user can paste a dashboard URL instead of typing the
bare server URL. Only the server base URL is persisted; any project key or
branch found in the URL is surfaced to the caller but never stored as a default
(project and branch are always chosen per request).
"""
from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from icx_engine.exceptions import InvalidInput


class ParsedSonarUrl:
    __slots__ = ("base_url", "project_key", "branch")

    def __init__(self, base_url: str, project_key: str | None, branch: str | None):
        self.base_url = base_url
        self.project_key = project_key
        self.branch = branch


def parse_sonar_url(raw: str) -> ParsedSonarUrl:
    """Return the server base URL plus any project key / branch in the query.

    Accepts a bare server URL (``http://host:9000``) or a dashboard URL
    (``http://host:9000/dashboard?id=<key>&branch=<b>``). Raises InvalidInput
    for anything that is not an absolute http/https URL, or that embeds
    credentials.
    """
    value = (raw or "").strip()
    if any(c in value for c in ("\x00", "\r", "\n", "\t")):
        raise InvalidInput("Invalid Sonar URL: control characters are not allowed.")

    if not value.lower().startswith(("http://", "https://")):
        raise InvalidInput(
            "Invalid Sonar URL. Expected an absolute URL like http://host:9000."
        )

    parsed = urlparse(value)
    if not parsed.hostname:
        raise InvalidInput("Invalid Sonar URL: no host found.")
    if parsed.username or parsed.password:
        raise InvalidInput(
            "Invalid Sonar URL: embedded credentials are not supported. "
            "Configure the token separately."
        )

    base_url = f"{parsed.scheme}://{parsed.netloc}"
    query = parse_qs(parsed.query)
    project_key = (query.get("id") or [None])[0]
    branch = (query.get("branch") or [None])[0]
    return ParsedSonarUrl(base_url=base_url, project_key=project_key or None, branch=branch or None)
