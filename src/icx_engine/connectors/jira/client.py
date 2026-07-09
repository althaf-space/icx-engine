from __future__ import annotations
import asyncio
import httpx
from contextlib import asynccontextmanager
from urllib.parse import urlparse, urljoin, quote

from icx_engine.connectors.http import check_http_status
from icx_engine.connectors.jira.parser import parse_issue_response
from icx_engine.exceptions import SourceUnavailable
from icx_engine.models.output import RawIssueData

_MAX_RETRIES = 3
_MAX_REDIRECT_HOPS = 3
_RETRY_STATUSES = {429, 500, 502, 503, 504}
_MAX_RETRY_DELAY = 60
_MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024  # 50 MB

_JIRA_FIELDS = (
    "summary,description,comment,attachment,"
    "issuetype,priority,status,reporter,assignee,duedate"
)


class JiraClient:
    def __init__(self, base_url: str, auth_header: str, allowed_hosts: set[str]):
        parsed_base = urlparse(base_url)
        if not parsed_base.scheme or not parsed_base.netloc:
            raise ValueError(f"base_url must be an absolute HTTPS URL, got: {base_url!r}")
        if parsed_base.scheme != "https":
            raise ValueError(f"base_url must use HTTPS, got scheme '{parsed_base.scheme}'.")
        if parsed_base.netloc not in allowed_hosts:
            raise ValueError(
                f"base_url host '{parsed_base.netloc}' is not in allowed_hosts."
            )
        self._base_url = base_url
        self._auth_header = auth_header
        self._allowed_hosts = allowed_hosts
        # Optional shared client for a batch of attachment downloads. When set
        # (via attachment_session), downloads reuse one connection pool instead
        # of opening a fresh client per attachment. None = per-call client.
        self._dl_client: httpx.AsyncClient | None = None

    async def fetch(self, issue_key: str) -> RawIssueData:
        async with httpx.AsyncClient(timeout=30.0) as client:
            for attempt in range(_MAX_RETRIES):
                response = await client.get(
                    f"{self._base_url}/issue/{quote(issue_key, safe='')}",
                    headers={
                        "Authorization": self._auth_header,
                        "Accept": "application/json",
                    },
                    params={"fields": _JIRA_FIELDS},
                )

                if response.status_code in _RETRY_STATUSES and attempt < _MAX_RETRIES - 1:
                    raw_retry = response.headers.get("Retry-After", "")
                    try:
                        delay = min(int(raw_retry), _MAX_RETRY_DELAY)
                    except (ValueError, TypeError):
                        delay = min(2 ** attempt, _MAX_RETRY_DELAY)
                    await asyncio.sleep(delay)
                    continue

                check_http_status(response)
                return parse_issue_response(issue_key, response.json())

        raise SourceUnavailable("Source is unavailable. Try again later.")

    @asynccontextmanager
    async def attachment_session(self):
        """Open one shared httpx client for a batch of attachment downloads so they
        reuse connections (keep-alive) instead of one TLS handshake per attachment.
        Downloads outside a session keep the original per-call client behavior."""
        if self._dl_client is not None:
            # Already in a session (nested) - reuse the outer one, do not double-close.
            yield
            return
        client = httpx.AsyncClient(timeout=60.0)
        self._dl_client = client
        try:
            yield
        finally:
            self._dl_client = None
            await client.aclose()

    async def download_attachment(self, content_url: str) -> bytes:
        shared = self._dl_client
        if shared is not None:
            return await self._download_attachment_with(shared, content_url)
        async with httpx.AsyncClient(timeout=60.0) as client:
            return await self._download_attachment_with(client, content_url)

    async def _download_attachment_with(self, client: httpx.AsyncClient, content_url: str) -> bytes:
        current_url = content_url
        send_auth = True

        for _hop in range(_MAX_REDIRECT_HOPS + 1):
            parsed = urlparse(current_url)
            if parsed.scheme != "https" or parsed.netloc not in self._allowed_hosts:
                raise SourceUnavailable(
                    f"Attachment download blocked: the redirect target '{parsed.netloc}' is outside "
                    f"the allowed hosts for this connection. "
                    f"This safeguard prevents server-side request forgery (SSRF). "
                    f"Contact your Jira administrator if this attachment URL is legitimate."
                )

            headers = {"Authorization": self._auth_header} if send_auth else {}

            async with client.stream(
                "GET", current_url,
                headers=headers,
                follow_redirects=False,
            ) as response:
                if response.status_code not in (301, 302, 303, 307, 308):
                    response.raise_for_status()
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in response.aiter_bytes(65536):
                        total += len(chunk)
                        if total > _MAX_ATTACHMENT_BYTES:
                            raise SourceUnavailable(
                                f"Attachment exceeds the {_MAX_ATTACHMENT_BYTES // (1024 * 1024)} MB size limit - skipped."
                            )
                        chunks.append(chunk)
                    return b"".join(chunks)

                location = response.headers.get("Location", "")
                if not location:
                    raise SourceUnavailable(
                        "Attachment download failed: redirect response is missing the Location header. "
                        "The attachment URL may be broken."
                    )
                # Resolve relative Location headers against the current URL so a
                # legitimate same-host relative redirect is not misread as a
                # host change. The loop re-validates scheme/host on the next hop.
                resolved = urljoin(current_url, location)
                next_netloc = urlparse(resolved).netloc
                send_auth = (
                    bool(next_netloc)
                    and next_netloc.split(":")[0].lower() == parsed.netloc.split(":")[0].lower()
                )
                current_url = resolved

        raise SourceUnavailable(
            "Attachment download failed: exceeded the maximum redirect limit. "
            "The attachment URL may be in a redirect loop."
        )