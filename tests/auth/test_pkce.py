from __future__ import annotations
import base64
import hashlib
import pytest
import respx
import httpx

from icx_engine.auth.pkce import refresh_oauth_token, run_pkce_flow


def _decode_verifier_and_challenge() -> tuple[str, str]:
    """Helper: generate a fresh verifier+challenge matching the S256 spec."""
    import secrets
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    return verifier, challenge


def test_s256_challenge_matches_verifier():
    verifier, challenge = _decode_verifier_and_challenge()
    expected = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    assert challenge == expected


async def test_run_pkce_flow_rejects_http_auth_endpoint():
    with pytest.raises(ValueError, match="HTTPS"):
        await run_pkce_flow(
            auth_endpoint="http://provider.example.com/authorize",
            token_endpoint="https://provider.example.com/oauth/token",
            client_id="client-123",
            scopes=["read"],
        )


async def test_run_pkce_flow_rejects_http_token_endpoint():
    with pytest.raises(ValueError, match="HTTPS"):
        await run_pkce_flow(
            auth_endpoint="https://provider.example.com/authorize",
            token_endpoint="http://provider.example.com/oauth/token",
            client_id="client-123",
            scopes=["read"],
        )


async def test_refresh_oauth_token_rejects_http_endpoint():
    with pytest.raises(ValueError, match="HTTPS"):
        await refresh_oauth_token(
            token_endpoint="http://provider.example.com/oauth/token",
            client_id="client-123",
            refresh_token="rfrsh-abc",
        )


@respx.mock
async def test_refresh_oauth_token_returns_new_tokens():
    respx.post("https://provider.example.com/oauth/token").mock(
        return_value=httpx.Response(200, json={
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "expires_in": 3600,
        })
    )
    tokens = await refresh_oauth_token(
        token_endpoint="https://provider.example.com/oauth/token",
        client_id="client-123",
        refresh_token="old-refresh",
    )
    assert tokens["access_token"] == "new-access"
    assert tokens["refresh_token"] == "new-refresh"


@respx.mock
async def test_refresh_oauth_token_sends_client_secret_when_provided():
    route = respx.post("https://provider.example.com/oauth/token").mock(
        return_value=httpx.Response(200, json={"access_token": "tok"})
    )
    await refresh_oauth_token(
        token_endpoint="https://provider.example.com/oauth/token",
        client_id="client-123",
        refresh_token="rfrsh",
        client_secret="super-secret",
    )
    body = route.calls[0].request.content
    import json
    payload = json.loads(body)
    assert payload["client_secret"] == "super-secret"
    assert payload["grant_type"] == "refresh_token"


@respx.mock
async def test_refresh_oauth_token_raises_on_http_error():
    respx.post("https://provider.example.com/oauth/token").mock(
        return_value=httpx.Response(401, json={"error": "invalid_grant"})
    )
    with pytest.raises(httpx.HTTPStatusError):
        await refresh_oauth_token(
            token_endpoint="https://provider.example.com/oauth/token",
            client_id="client-123",
            refresh_token="bad-rfrsh",
        )
