from __future__ import annotations
import base64
import pytest
import respx
import httpx

from icx_engine.auth.token import (
    build_basic_auth_header,
    build_bearer_header,
    check_http_credentials,
)


def test_build_basic_auth_header_encodes_correctly():
    header = build_basic_auth_header("user@example.com", "secret-token")
    assert header.startswith("Basic ")
    decoded = base64.b64decode(header.removeprefix("Basic ")).decode()
    assert decoded == "user@example.com:secret-token"


def test_build_basic_auth_header_prefix():
    header = build_basic_auth_header("alice", "pw")
    assert header.startswith("Basic ")


def test_build_bearer_header_format():
    header = build_bearer_header("my-access-token")
    assert header == "Bearer my-access-token"


def test_build_bearer_header_no_encoding():
    token = "tok_abc123"
    header = build_bearer_header(token)
    assert token in header


async def test_check_http_credentials_raises_on_http():
    with pytest.raises(ValueError, match="HTTPS"):
        await check_http_credentials(
            verify_url="http://api.example.com/me",
            auth_header="Bearer tok",
        )


@respx.mock
async def test_check_http_credentials_returns_response():
    respx.get("https://api.example.com/me").mock(
        return_value=httpx.Response(200, json={"displayName": "Alice"})
    )
    resp = await check_http_credentials(
        verify_url="https://api.example.com/me",
        auth_header="Basic dXNlcjp0b2s=",
    )
    assert resp.status_code == 200
    assert resp.json()["displayName"] == "Alice"


@respx.mock
async def test_check_http_credentials_sends_auth_header():
    route = respx.get("https://api.example.com/me").mock(
        return_value=httpx.Response(401)
    )
    await check_http_credentials(
        verify_url="https://api.example.com/me",
        auth_header="Bearer my-token",
    )
    sent = route.calls[0].request
    assert sent.headers["Authorization"] == "Bearer my-token"
