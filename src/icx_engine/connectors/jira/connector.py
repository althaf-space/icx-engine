from __future__ import annotations
import re
from urllib.parse import urlparse, parse_qs

from icx_engine.connectors.base import ConnectorBase, ParsedInput
from icx_engine.models.config import BaseConnection
from icx_engine.connectors.jira.config import JiraConnection, JiraOAuthAuth
from icx_engine.models.output import RawIssueData
from icx_engine.exceptions import InvalidInput, SourceUnavailable

_ISSUE_KEY_RE = re.compile(r"^[A-Z][A-Z0-9]*-[0-9]+$")


class JiraConnector(ConnectorBase):
    def __init__(self, connection: BaseConnection):
        super().__init__(connection)
        self._jira_conn = (
            connection
            if isinstance(connection, JiraConnection)
            else JiraConnection.model_validate(connection.model_dump())
        )
        self._client = None  # initialized in fetch()

    @classmethod
    def connector_type(cls) -> str:
        return "jira"

    @classmethod
    def can_handle_bare_key(cls, key: str) -> bool:
        return bool(_ISSUE_KEY_RE.match(key.upper()))

    def parse_input(self, input_str: str) -> ParsedInput:
        """
        Parse Jira-style user input into a structured ParsedInput.

        Handles:
          - Bare key:                    ABC-123
          - Standard browse URL:         https://domain/browse/ABC-123
          - Issues URL:                  https://domain/issues/ABC-123
          - Board/backlog URL:           https://domain/...?selectedIssue=ABC-123
          - No-scheme URL:               domain.example.com/browse/ABC-123

        Raises InvalidInput for unrecognised formats.
        """
        raw = input_str.strip()

        # Bare issue key
        if _ISSUE_KEY_RE.match(raw.upper()):
            return ParsedInput(issue_key=raw.upper())

        # Normalise scheme
        has_scheme = raw.lower().startswith(("http://", "https://"))
        normalised = raw if has_scheme else f"https://{raw}"
        parsed = urlparse(normalised)

        if not parsed.netloc:
            raise InvalidInput(
                "Invalid issue key format. Expected something like ABC-123 or a full URL."
            )

        # Board/backlog view: ?selectedIssue=ABC-123
        params = parse_qs(parsed.query)
        if "selectedIssue" in params:
            key = params["selectedIssue"][0].upper()
            if _ISSUE_KEY_RE.match(key):
                return ParsedInput(issue_key=key)

        path_parts = [p for p in parsed.path.strip("/").split("/") if p]

        # /browse/<KEY>  or  /issues/<KEY>
        if len(path_parts) >= 2 and path_parts[-2].lower() in ("browse", "issues"):
            return ParsedInput(issue_key=path_parts[-1].upper())

        raise InvalidInput(
            "Invalid URL format. Expected a full issue URL or a bare key like PROJ-123."
        )

    async def fetch(self, issue_key: str, config=None, log=None) -> RawIssueData:
        from icx_engine.connectors.jira.oauth import refresh_oauth_if_needed
        from icx_engine.connectors.jira.auth import build_auth_header
        from icx_engine.connectors.jira.client import JiraClient

        conn = self._jira_conn
        if config is not None:
            conn = await refresh_oauth_if_needed(conn, config)
            self._jira_conn = conn

        if isinstance(conn.auth, JiraOAuthAuth):
            base_url = f"https://api.atlassian.com/ex/jira/{conn.auth.cloud_id}/rest/api/3"
            allowed_hosts = {"api.atlassian.com", "api.media.atlassian.com", conn.domain}
        else:
            base_url = f"https://{conn.domain}/rest/api/3"
            allowed_hosts = {conn.domain, "api.media.atlassian.com"}

        self._client = JiraClient(base_url, build_auth_header(conn), allowed_hosts)
        return await self._client.fetch(issue_key)

    def _rewrite_attachment_url(self, url: str) -> str:
        conn = self._jira_conn
        if not isinstance(conn.auth, JiraOAuthAuth):
            parsed = urlparse(url)
            if parsed.netloc != conn.domain:
                raise SourceUnavailable(
                    f"Attachment URL host '{parsed.netloc}' does not match "
                    f"the configured domain '{conn.domain}'."
                )
            return url
        parsed = urlparse(url)
        if parsed.netloc == "api.atlassian.com":
            return url
        return f"https://api.atlassian.com/ex/jira/{conn.auth.cloud_id}{parsed.path}"

    async def download_attachment(self, url: str) -> bytes:
        if self._client is None:
            raise RuntimeError("JiraConnector.fetch() must be called before download_attachment()")
        rewritten = self._rewrite_attachment_url(url)
        return await self._client.download_attachment(rewritten)

    async def process_attachments(self, raw: RawIssueData, llm_config, log=None) -> tuple[dict[str, str], dict[str, str]]:
        from icx_engine.connectors.attachments import process_attachments as _pa
        return await _pa(raw, self, llm_config, log=log)
