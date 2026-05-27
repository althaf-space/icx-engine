from __future__ import annotations
import time

from icx_engine.exceptions import OAuthRefreshError
from icx_engine.models.config import AppConfig
from icx_engine.connectors.jira.config import JiraConnection, JiraOAuthAuth
from icx_engine.auth.pkce import refresh_oauth_token

_REFRESH_BUFFER_SECS = 300  # refresh if token expires within 5 minutes
_TOKEN_URL = "https://auth.atlassian.com/oauth/token"


async def refresh_oauth_if_needed(
    conn: JiraConnection, config: AppConfig
) -> JiraConnection:
    """Return conn unchanged if token is fresh; otherwise refresh and persist."""
    auth = conn.auth
    if not isinstance(auth, JiraOAuthAuth):
        return conn
    if not auth.client_id or not auth.client_secret:
        return conn  # missing credentials - cannot refresh
    if time.time() < auth.expires_at - _REFRESH_BUFFER_SECS:
        return conn

    try:
        tokens = await refresh_oauth_token(
            token_endpoint=_TOKEN_URL,
            client_id=auth.client_id,
            client_secret=auth.client_secret,
            refresh_token=auth.refresh_token,
        )
    except Exception as exc:
        raise OAuthRefreshError(
            f"OAuth token refresh failed for {conn.domain}: {exc}"
        ) from exc

    new_auth = JiraOAuthAuth(
        auth_type="oauth",
        client_id=auth.client_id,
        client_secret=auth.client_secret,
        cloud_id=auth.cloud_id,
        access_token=tokens["access_token"],
        refresh_token=tokens.get("refresh_token", auth.refresh_token),
        expires_at=int(time.time()) + tokens.get("expires_in", 3600),
    )
    new_conn = JiraConnection(domain=conn.domain, auth=new_auth)

    from icx_engine.config_manager import ConfigManager

    config.connections = [
        new_conn if c.domain == conn.domain else c
        for c in config.connections
    ]
    ConfigManager.save(config)
    return new_conn
