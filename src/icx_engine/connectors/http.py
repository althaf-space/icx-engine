"""
Shared HTTP utilities for connector implementations.

Provides a standard HTTP status → ICX exception mapping so connectors don't
each duplicate the same error-handling logic. Import check_http_status() in
any connector that makes HTTP requests and needs to surface domain exceptions.

Usage:

    from icx_engine.connectors.http import check_http_status

    response = await client.get(url, headers=headers)
    check_http_status(response)   # raises ICX exception or falls through on 2xx
"""
from __future__ import annotations

import httpx

from icx_engine.exceptions import AuthError, IssueNotFound, RateLimited, SourceUnavailable


def check_http_status(response: httpx.Response) -> None:
    """
    Raise an ICX domain exception for error HTTP responses.

    Maps standard HTTP status codes to the appropriate ICX exception type.
    Falls back to httpx.raise_for_status() for any other 4xx/5xx codes not
    explicitly handled above.
    """
    if response.status_code == 401:
        raise AuthError("Authentication failed. Run `icx connection --add` to reconnect.")
    if response.status_code == 403:
        raise AuthError("Permission denied. Check your project access.")
    if response.status_code == 404:
        raise IssueNotFound("Issue not found. Check the URL or issue key.")
    if response.status_code == 429:
        raise RateLimited("Rate limited. Retrying...")
    if response.status_code >= 500:
        raise SourceUnavailable("Source is unavailable. Try again later.")
    if response.status_code >= 400:
        raise SourceUnavailable(
            f"Unexpected client error (HTTP {response.status_code}). "
            "Check the issue key, your permissions, and the tracker URL."
        )
