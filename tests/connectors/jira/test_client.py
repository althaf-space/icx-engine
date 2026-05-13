"""Unit tests for JiraClient whitelist enforcement and JiraConnector URL rewriting."""
import re
import httpx
import pytest
import respx

from icx_engine.connectors.jira.client import JiraClient
from icx_engine.connectors.jira.connector import JiraConnector
from icx_engine.connectors.jira.config import JiraConnection, TokenAuth, JiraOAuthAuth
from icx_engine.exceptions import SourceUnavailable


# ── JiraClient whitelist enforcement ─────────────────────────────────────────

async def test_whitelist_rejects_host_not_in_set():
    client = JiraClient(
        "https://api.atlassian.com/ex/jira/abc123/rest/api/3",
        "Bearer tok",
        {"api.atlassian.com"},
    )
    with pytest.raises(SourceUnavailable):
        await client.download_attachment("https://evil.example.com/file.png")


async def test_whitelist_rejects_http_scheme():
    client = JiraClient(
        "https://test.atlassian.net/rest/api/3",
        "Basic tok",
        {"test.atlassian.net"},
    )
    with pytest.raises(SourceUnavailable):
        await client.download_attachment("http://test.atlassian.net/file.png")


async def test_whitelist_rejects_domain_outside_set():
    client = JiraClient(
        "https://api.atlassian.com/ex/jira/abc123/rest/api/3",
        "Bearer tok",
        {"api.atlassian.com"},
    )
    with pytest.raises(SourceUnavailable):
        await client.download_attachment(
            "https://mysite.atlassian.net/rest/api/3/attachment/content/10001"
        )


@respx.mock
async def test_whitelist_allows_permitted_host():
    url = "https://api.atlassian.com/ex/jira/abc123/rest/api/3/attachment/content/1"
    respx.get(url).mock(return_value=httpx.Response(200, content=b"data"))
    client = JiraClient(
        "https://api.atlassian.com/ex/jira/abc123/rest/api/3",
        "Bearer tok",
        {"api.atlassian.com"},
    )
    result = await client.download_attachment(url)
    assert result == b"data"


@respx.mock
async def test_redirect_to_allowed_host_is_followed():
    """Redirect from api.atlassian.com to api.media.atlassian.com (both in allowed_hosts) succeeds."""
    redirect_url = "https://api.media.atlassian.com/file/abc123"
    original_url = "https://api.atlassian.com/ex/jira/abc123/rest/api/3/attachment/content/1"
    respx.get(original_url).mock(
        return_value=httpx.Response(302, headers={"Location": redirect_url})
    )
    respx.get(redirect_url).mock(return_value=httpx.Response(200, content=b"filedata"))

    client = JiraClient(
        "https://api.atlassian.com/ex/jira/abc123/rest/api/3",
        "Bearer tok",
        {"api.atlassian.com", "api.media.atlassian.com"},
    )
    result = await client.download_attachment(original_url)
    assert result == b"filedata"


@respx.mock
async def test_redirect_to_disallowed_host_raises():
    """Redirect to a host not in allowed_hosts is rejected before the request is made."""
    original_url = "https://api.atlassian.com/ex/jira/abc123/rest/api/3/attachment/content/1"
    respx.get(original_url).mock(
        return_value=httpx.Response(302, headers={"Location": "https://evil.example.com/steal"})
    )

    client = JiraClient(
        "https://api.atlassian.com/ex/jira/abc123/rest/api/3",
        "Bearer tok",
        {"api.atlassian.com"},
    )
    with pytest.raises(SourceUnavailable):
        await client.download_attachment(original_url)


@respx.mock
async def test_too_many_redirects_raises():
    """More than _MAX_REDIRECT_HOPS redirects raises ValueError."""
    # Chain: url0 → url1 → url2 → url3 → url4 (4 redirects, exceeds limit of 3)
    base = "https://api.atlassian.com/ex/jira/abc123/rest/api/3/attachment/content"
    for i in range(4):
        respx.get(f"{base}/{i}").mock(
            return_value=httpx.Response(302, headers={"Location": f"{base}/{i + 1}"})
        )
    respx.get(f"{base}/4").mock(return_value=httpx.Response(200, content=b"data"))

    client = JiraClient(
        "https://api.atlassian.com/ex/jira/abc123/rest/api/3",
        "Bearer tok",
        {"api.atlassian.com"},
    )
    with pytest.raises(SourceUnavailable):
        await client.download_attachment(f"{base}/0")


@respx.mock
async def test_redirect_strips_auth_on_cross_host():
    """Auth header is not sent to a different host after redirect."""
    redirect_url = "https://api.media.atlassian.com/file/abc123"
    original_url = "https://api.atlassian.com/ex/jira/abc123/rest/api/3/attachment/content/1"
    respx.get(original_url).mock(
        return_value=httpx.Response(302, headers={"Location": redirect_url})
    )
    respx.get(redirect_url).mock(return_value=httpx.Response(200, content=b"filedata"))

    client = JiraClient(
        "https://api.atlassian.com/ex/jira/abc123/rest/api/3",
        "Bearer tok",
        {"api.atlassian.com", "api.media.atlassian.com"},
    )
    await client.download_attachment(original_url)

    # The CDN request must not carry the Authorization header
    cdn_request = respx.calls[-1].request
    assert "authorization" not in {k.lower() for k in cdn_request.headers}


def test_init_rejects_schemeless_base_url():
    with pytest.raises(ValueError, match="absolute HTTPS URL"):
        JiraClient("api.atlassian.com/ex/jira/abc/rest/api/3", "Bearer tok", {"api.atlassian.com"})


def test_init_rejects_http_base_url():
    with pytest.raises(ValueError, match="must use HTTPS"):
        JiraClient("http://test.atlassian.net/rest/api/3", "Basic tok", {"test.atlassian.net"})


# ── JiraConnector URL rewriting ───────────────────────────────────────────────

def _make_oauth_connector() -> JiraConnector:
    conn = JiraConnection(
        domain="mysite.atlassian.net",
        auth=JiraOAuthAuth(
            auth_type="oauth",
            cloud_id="abc-123",
            access_token="tok",
            refresh_token="rtok",
            expires_at=9999999999,
            client_id="cid",
            client_secret="csec",
        ),
    )
    return JiraConnector(conn)


def _make_token_connector() -> JiraConnector:
    conn = JiraConnection(
        domain="mysite.atlassian.net",
        auth=TokenAuth(auth_type="token", email="u@test.com", api_token="tok"),
    )
    return JiraConnector(conn)


def test_rewrite_token_returns_url_unchanged():
    connector = _make_token_connector()
    url = "https://mysite.atlassian.net/rest/api/3/attachment/content/10001"
    assert connector._rewrite_attachment_url(url) == url


def test_rewrite_oauth_atlassian_net_rewrites_to_proxy():
    connector = _make_oauth_connector()
    url = "https://mysite.atlassian.net/rest/api/3/attachment/content/10001"
    expected = "https://api.atlassian.com/ex/jira/abc-123/rest/api/3/attachment/content/10001"
    assert connector._rewrite_attachment_url(url) == expected


def test_rewrite_oauth_already_proxy_url_unchanged():
    connector = _make_oauth_connector()
    url = "https://api.atlassian.com/ex/jira/abc-123/rest/api/3/attachment/content/10001"
    assert connector._rewrite_attachment_url(url) == url


def test_rewrite_oauth_url_without_rest_api_rewrites_to_proxy():
    connector = _make_oauth_connector()
    url = "https://mysite.atlassian.net/files/attachment/10001"
    expected = "https://api.atlassian.com/ex/jira/abc-123/files/attachment/10001"
    assert connector._rewrite_attachment_url(url) == expected


def test_rewrite_token_rejects_wrong_host():
    connector = _make_token_connector()
    with pytest.raises(ValueError, match="does not match the configured domain"):
        connector._rewrite_attachment_url("https://evil.example.com/rest/api/3/attachment/content/1")


@pytest.mark.asyncio
async def test_download_attachment_ssrf_redirect_raises_source_unavailable(token_connection):
    """Redirecting to a disallowed host must raise SourceUnavailable, not ValueError."""
    from icx_engine.connectors.jira.client import JiraClient
    from icx_engine.exceptions import SourceUnavailable
    import respx
    import httpx

    client = JiraClient(
        base_url=f"https://{token_connection.domain}/rest/api/3",
        auth_header="Basic dGVzdA==",
        allowed_hosts={token_connection.domain},
    )

    with respx.mock:
        respx.get(f"https://{token_connection.domain}/content/1").mock(
            return_value=httpx.Response(
                302, headers={"Location": "https://external-evil.com/file.bin"}
            )
        )
        with pytest.raises(SourceUnavailable) as exc_info:
            await client.download_attachment(f"https://{token_connection.domain}/content/1")

    msg = str(exc_info.value).lower()
    assert re.search(r"(?<![.\w])external-evil\.com(?![.\w])", msg) or "ssrf" in msg or "allowed" in msg


@pytest.mark.asyncio
async def test_download_attachment_size_limit_raises_source_unavailable(token_connection):
    """Exceeding the size limit must raise SourceUnavailable, not ValueError."""
    from icx_engine.connectors.jira.client import JiraClient, _MAX_ATTACHMENT_BYTES
    from icx_engine.exceptions import SourceUnavailable
    import respx
    import httpx

    client = JiraClient(
        base_url=f"https://{token_connection.domain}/rest/api/3",
        auth_header="Basic dGVzdA==",
        allowed_hosts={token_connection.domain},
    )
    oversized = b"x" * (_MAX_ATTACHMENT_BYTES + 1)

    with respx.mock:
        respx.get(f"https://{token_connection.domain}/content/1").mock(
            return_value=httpx.Response(200, content=oversized)
        )
        with pytest.raises(SourceUnavailable):
            await client.download_attachment(f"https://{token_connection.domain}/content/1")
