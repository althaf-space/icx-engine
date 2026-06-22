"""
Generic OAuth 2.0 PKCE (RFC 7636) authorization code flow utilities.

Reusable by any connector that needs browser-based OAuth. The caller provides
platform-specific parameters (endpoints, scopes, extra params) and receives the
raw token dict. No platform assumptions are made here.

Usage - connecting a new platform:

    from icx_engine.auth.pkce import run_pkce_flow, refresh_oauth_token

    tokens = await run_pkce_flow(
        auth_endpoint="https://provider.example.com/authorize",
        token_endpoint="https://provider.example.com/oauth/token",
        client_id=my_client_id,
        scopes=["read:issues", "offline_access"],
        # client_secret=None for public PKCE-only clients (RFC 7636 §4)
        # client_secret=my_secret for confidential clients (some providers require it)
    )
"""
from __future__ import annotations
import asyncio
import base64
import hashlib
import hmac
import os
import secrets
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

_SUCCESS_HTML = b"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Authentication Successful</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif;background:#0a0a0a;color:#f5f5f5;min-height:100vh;display:flex;align-items:center;justify-content:center}
.card{background:#111;border:1px solid #222;border-radius:16px;padding:48px 56px;text-align:center;max-width:440px;width:90%;box-shadow:0 0 0 1px #1a1a1a,0 32px 64px rgba(0,0,0,.5)}
.icon{width:56px;height:56px;background:linear-gradient(135deg,#4ade80,#22c55e);border-radius:50%;display:flex;align-items:center;justify-content:center;margin:0 auto 24px;font-size:26px;line-height:1}
h1{font-size:22px;font-weight:600;letter-spacing:-.3px;margin-bottom:10px;color:#f9f9f9}
p{font-size:14px;color:#888;line-height:1.6}
.badge{display:inline-block;margin-top:24px;padding:4px 14px;background:rgba(74,222,128,.08);border:1px solid rgba(74,222,128,.2);border-radius:20px;font-size:12px;color:#4ade80;letter-spacing:.3px}
</style>
</head>
<body>
<div class="card">
  <div class="icon">&#10003;</div>
  <h1>Authentication successful</h1>
  <p>You may close this tab and return to your terminal.</p>
  <span class="badge">ICX &middot; Connected</span>
</div>
</body>
</html>"""

_ERROR_HTML = b"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Authentication Failed</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif;background:#0a0a0a;color:#f5f5f5;min-height:100vh;display:flex;align-items:center;justify-content:center}
.card{background:#111;border:1px solid #222;border-radius:16px;padding:48px 56px;text-align:center;max-width:440px;width:90%;box-shadow:0 0 0 1px #1a1a1a,0 32px 64px rgba(0,0,0,.5)}
.icon{width:56px;height:56px;background:linear-gradient(135deg,#f87171,#ef4444);border-radius:50%;display:flex;align-items:center;justify-content:center;margin:0 auto 24px;font-size:26px;line-height:1}
h1{font-size:22px;font-weight:600;letter-spacing:-.3px;margin-bottom:10px;color:#f9f9f9}
p{font-size:14px;color:#888;line-height:1.6}
.badge{display:inline-block;margin-top:24px;padding:4px 14px;background:rgba(248,113,113,.08);border:1px solid rgba(248,113,113,.2);border-radius:20px;font-size:12px;color:#f87171;letter-spacing:.3px}
</style>
</head>
<body>
<div class="card">
  <div class="icon">&#10005;</div>
  <h1>Authentication failed</h1>
  <p>You may close this tab and return to your terminal.</p>
  <span class="badge">ICX &middot; Error</span>
</div>
</body>
</html>"""


async def run_pkce_flow(
    *,
    auth_endpoint: str,
    token_endpoint: str,
    client_id: str,
    scopes: list[str],
    callback_port: int = 8765,
    client_secret: str | None = None,
    extra_auth_params: dict | None = None,
    timeout: int = 120,
) -> dict:
    """
    Run a complete OAuth 2.0 PKCE (RFC 7636) authorization code flow.

    Opens the user's browser, starts a temporary local HTTP server to receive the
    callback, then exchanges the authorization code for tokens.

    Args:
        auth_endpoint:       Authorization server URL
        token_endpoint:      Token exchange URL
        client_id:           OAuth application client ID
        scopes:              OAuth scopes to request
        callback_port:       Local port for the redirect callback (default 8765)
        client_secret:       Optional. Pass None for pure PKCE public clients.
                             Some providers require it even with PKCE (e.g. Atlassian).
        extra_auth_params:   Extra query params forwarded to the auth endpoint.
        timeout:             Seconds to wait for the browser callback (default 120)

    Returns:
        Raw token response dict (access_token, refresh_token, expires_in, etc)

    Raises:
        TimeoutError:        Browser callback did not arrive within timeout
        ValueError:          Callback received without authorization code
        httpx.HTTPStatusError: Token exchange request failed
    """
    if not auth_endpoint.startswith("https://"):
        raise ValueError(
            "auth_endpoint must use HTTPS to protect the authorization code in transit."
        )
    if not token_endpoint.startswith("https://"):
        raise ValueError(
            "token_endpoint must use HTTPS to protect authorization codes and tokens in transit."
        )

    code_verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    code_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(code_verifier.encode()).digest()
    ).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)

    auth_code: dict = {}
    server_done = threading.Event()

    class _CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed_path = urlparse(self.path)
            if parsed_path.path != "/callback":
                self.send_response(404)
                self.end_headers()
                return
            params = parse_qs(parsed_path.query)
            state_ok = hmac.compare_digest(params.get("state", [""])[0], state)
            if "code" in params and state_ok:
                auth_code["code"] = params["code"][0]
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(_SUCCESS_HTML)
                server_done.set()
            elif "error" in params and state_ok:
                # Provider sent an explicit error (e.g. access_denied) - unblock immediately
                auth_code["error"] = params["error"][0][:200].encode("ascii", "replace").decode()
                self.send_response(400)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(_ERROR_HTML)
                server_done.set()
            else:
                self.send_response(400)
                self.end_headers()

        def log_message(self, *args):
            pass  # suppress default server output

    _PORT_RANGE = 5
    http_server = None
    actual_port = callback_port
    for _port in range(callback_port, callback_port + _PORT_RANGE):
        try:
            http_server = HTTPServer(("127.0.0.1", _port), _CallbackHandler)
            actual_port = _port
            break
        except OSError:
            continue

    if http_server is None:
        _ports = ", ".join(str(p) for p in range(callback_port, callback_port + _PORT_RANGE))
        raise OSError(
            f"OAuth callback server could not start - ports {callback_port}-"
            f"{callback_port + _PORT_RANGE - 1} are all in use.\n"
            f"Free one of these ports and retry: {_ports}\n"
            f"On Linux/macOS: lsof -i :{callback_port}  |  On Windows: netstat -ano | findstr :{callback_port}"
        )

    if actual_port != callback_port:
        print(
            f"\n  Port {callback_port} is in use - using port {actual_port} for OAuth callback.\n"
            f"     Ensure http://localhost:{actual_port}/callback is registered in your\n"
            f"     Atlassian OAuth app at https://developer.atlassian.com/console/myapps/\n",
            file=sys.stderr,
        )

    redirect_uri = f"http://localhost:{actual_port}/callback"
    server_thread = threading.Thread(target=http_server.serve_forever, daemon=True)
    server_thread.start()

    auth_params: dict = {
        "client_id": client_id,
        "scope": " ".join(scopes),
        "redirect_uri": redirect_uri,
        "state": state,
        "response_type": "code",
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        **(extra_auth_params or {}),
    }
    auth_url = f"{auth_endpoint}?{urlencode(auth_params)}"

    _is_linux = sys.platform == "linux"
    _has_display = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))

    if _is_linux:
        if _has_display:
            print(
                f"\nIf the browser did not open, copy this URL to authenticate:\n\n"
                f"  {auth_url}\n\n"
                f"Waiting for authentication callback... ({timeout}s timeout)",
                file=sys.stderr,
            )
            try:
                # Use xdg-open directly with stderr suppressed so gio/xdg errors
                # don't leak to the terminal - URL is already printed above.
                subprocess.Popen(
                    ["xdg-open", auth_url],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception:
                pass
        else:
            print(
                f"\nNo browser available. Copy this URL to authenticate:\n\n"
                f"  {auth_url}\n\n"
                f"Waiting for authentication callback... ({timeout}s timeout)",
                file=sys.stderr,
            )
    else:
        browser_opened = False
        try:
            browser_opened = webbrowser.open(auth_url)
        except Exception:
            pass
        if not browser_opened:
            print(
                f"\nNo browser detected. Copy this URL to authenticate:\n\n"
                f"  {auth_url}\n\n"
                f"Waiting for authentication callback... ({timeout}s timeout)",
                file=sys.stderr,
            )

    try:
        loop = asyncio.get_running_loop()
        got_code = await loop.run_in_executor(None, lambda: server_done.wait(timeout))
    finally:
        http_server.shutdown()
        http_server.server_close()

    if not got_code:
        raise TimeoutError(f"OAuth callback timed out after {timeout} seconds.")
    if "error" in auth_code:
        raise ValueError(f"OAuth provider returned an error: {auth_code['error']}")
    if "code" not in auth_code:
        raise ValueError("OAuth callback did not receive an authorization code.")

    payload: dict = {
        "grant_type": "authorization_code",
        "client_id": client_id,
        "code": auth_code["code"],
        "redirect_uri": redirect_uri,
        "code_verifier": code_verifier,
    }
    if client_secret is not None:
        payload["client_secret"] = client_secret

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            token_endpoint,
            json=payload,
            headers={"Content-Type": "application/json"},
        )
        resp.raise_for_status()
        return resp.json()


async def refresh_oauth_token(
    *,
    token_endpoint: str,
    client_id: str,
    refresh_token: str,
    client_secret: str | None = None,
) -> dict:
    """
    Exchange a refresh token for a new access token.

    client_secret is optional - pass None for pure PKCE public clients.
    Some providers require it even with PKCE (confidential clients).

    Returns the raw token response dict.
    Raises httpx.HTTPStatusError on failure.
    Raises ValueError if token_endpoint does not use HTTPS.
    """
    if not token_endpoint.startswith("https://"):
        raise ValueError(
            "token_endpoint must use HTTPS to protect refresh tokens in transit."
        )

    payload: dict = {
        "grant_type": "refresh_token",
        "client_id": client_id,
        "refresh_token": refresh_token,
    }
    if client_secret is not None:
        payload["client_secret"] = client_secret

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            token_endpoint,
            json=payload,
            headers={"Content-Type": "application/json"},
        )
        resp.raise_for_status()
        return resp.json()