"""
Generic HTTP token-based authentication utilities (RFC 7617 Basic, RFC 6750 Bearer).

These utilities eliminate duplicated base64 encoding across connectors and CLI
flows. Any connector that uses username+password or API-token auth should call
build_basic_auth_header() instead of rolling its own encoding.

Usage - building auth headers:

    from icx_engine.auth.token import build_basic_auth_header, build_bearer_header

    # API token or username/password (HTTP Basic Auth)
    header = build_basic_auth_header(username="user@example.com", password="api-token")

    # OAuth access token or PAT (Bearer)
    header = build_bearer_header(token="access-token-xyz")

Usage - verifying credentials during connection setup:

    from icx_engine.auth.token import check_http_credentials

    response = await check_http_credentials(
        verify_url="https://api.example.com/me",
        auth_header=header,
    )
    if response.status_code == 200:
        display_name = response.json().get("displayName")
"""
from __future__ import annotations
import base64

import httpx


def build_basic_auth_header(username: str, password: str) -> str:
    """
    Build an HTTP Basic Authorization header value (RFC 7617).

    Works for any username+password or email+API-token combination.
    Returns the full header value including the "Basic " prefix.
    """
    encoded = base64.b64encode(f"{username}:{password}".encode()).decode()
    return f"Basic {encoded}"


def build_bearer_header(token: str) -> str:
    """Build an HTTP Bearer Authorization header value (RFC 6750)."""
    return f"Bearer {token}"


async def check_http_credentials(
    *,
    verify_url: str,
    auth_header: str,
    timeout: int = 10,
) -> httpx.Response:
    """
    Verify credentials by making a GET request to verify_url.

    Returns the raw httpx.Response so the caller can inspect status and body.
    Raises httpx.ConnectError / httpx.TimeoutException on network failure.
    Raises ValueError if verify_url does not use HTTPS.
    """
    if not verify_url.startswith("https://"):
        raise ValueError("verify_url must use HTTPS to protect credentials in transit.")
    async with httpx.AsyncClient(timeout=float(timeout)) as client:
        return await client.get(
            verify_url,
            headers={"Authorization": auth_header, "Accept": "application/json"},
        )
