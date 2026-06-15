from __future__ import annotations
import asyncio
import re
import typer
from rich.console import Console
from icx_engine.error_display import render_icx_error

console = Console()
err_console = Console(stderr=True, style="bold red")

_CLOUD_ID_RE = re.compile(r'^[a-zA-Z0-9\-]+$')
_DOMAIN_RE = re.compile(r'^[A-Za-z0-9]([A-Za-z0-9\-]*[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9\-]*[A-Za-z0-9])?)+(?::[0-9]{1,5})?$')


def _validated_cloud_id(value: str) -> str:
    if not _CLOUD_ID_RE.match(value):
        raise RuntimeError(
            f"Unexpected cloud_id format received from Atlassian: {value!r}. "
            "This may indicate a compromised or unexpected API response."
        )
    return value


def _connect_jira_token(debug: bool = False) -> None:
    from icx_engine.config_manager import ConfigManager
    from icx_engine.connectors.jira.config import JiraConnection, TokenAuth
    from icx_engine.auth.token import build_basic_auth_header, check_http_credentials

    typer.echo("\nJira - API Token Authentication")
    raw_domain = typer.prompt("Jira base URL (e.g. https://xyz.atlassian.net)").strip()
    domain = raw_domain.replace("https://", "").replace("http://", "").rstrip("/")
    if not domain or not _DOMAIN_RE.match(domain):
        from icx_engine.exceptions import InvalidInput
        render_icx_error(InvalidInput("Invalid domain. Enter just the hostname, e.g. company.atlassian.net"), err_console)
        raise typer.Exit(1)
    config = ConfigManager.load()
    if any(c.domain == domain for c in config.connections):
        overwrite = typer.confirm(
            f"Connection for '{domain}' already exists. Overwrite?", default=False
        )
        if not overwrite:
            typer.echo("Cancelled.")
            return
    email = typer.prompt("Email address").strip()
    if debug:
        typer.echo("(--debug: token input is visible)")
    api_token = typer.prompt(
        "API token (from id.atlassian.com/manage-profile/security/api-tokens)",
        hide_input=not debug,
    ).strip()
    if debug:
        masked = (api_token[:4] + "..." + api_token[-4:]) if len(api_token) > 8 else "***"
        typer.echo(f"  domain : {domain!r}")
        typer.echo(f"  email  : {email!r}")
        typer.echo(f"  token  : {masked}")

    auth_header = build_basic_auth_header(email, api_token)
    verify_url = f"https://{domain}/rest/api/3/myself"

    try:
        if debug:
            typer.echo(f"  verifying credentials with {domain}...", err=True)
            resp = asyncio.run(check_http_credentials(verify_url=verify_url, auth_header=auth_header))
            typer.echo(f"  status : {resp.status_code}", err=True)
        else:
            with console.status(f"[bold]Verifying credentials with {domain}...[/bold]", spinner="dots"):
                resp = asyncio.run(check_http_credentials(verify_url=verify_url, auth_header=auth_header))
        if resp.status_code == 401:
            from icx_engine.exceptions import AuthError
            render_icx_error(AuthError("Authentication failed. Check your email and API token."), err_console)
            raise typer.Exit(1)
        if resp.status_code == 403:
            from icx_engine.exceptions import AuthError
            render_icx_error(AuthError("Connected but permission denied. Check your Jira access level."), err_console)
            raise typer.Exit(1)
        if not resp.is_success:
            from icx_engine.exceptions import SourceUnavailable
            render_icx_error(SourceUnavailable(f"Unexpected response from Jira: {resp.status_code}"), err_console)
            raise typer.Exit(1)
        display_name = resp.json().get("displayName", email)
    except typer.Exit:
        raise
    except Exception as exc:
        if debug:
            raise
        render_icx_error(exc, err_console, show_traceback=False)
        raise typer.Exit(1)

    config.connections = [c for c in config.connections if c.domain != domain]
    new_conn = JiraConnection(
        domain=domain,
        auth=TokenAuth(auth_type="token", email=email, api_token=api_token),
    )
    config.connections.append(new_conn)
    if len(config.connections) == 1:
        config.default_connection = f"jira:{domain}"
    ConfigManager.save(config)
    ConfigManager.warn_if_plaintext()
    console.print(f"[green]✓ Connected to {domain} as {display_name}[/green]")


def _connect_jira_oauth(debug: bool = False) -> None:
    import time
    import httpx
    from icx_engine.config_manager import ConfigManager
    from icx_engine.connectors.jira.config import JiraConnection, JiraOAuthAuth
    from icx_engine.auth.pkce import run_pkce_flow

    typer.echo("\nJira - OAuth PKCE Authentication")
    typer.echo("You need an OAuth 2.0 (3LO) app at developer.atlassian.com")
    typer.echo(
        "Register these Callback URLs in your Atlassian OAuth app:\n"
        "  http://localhost:8765/callback      primary\n"
        "  http://localhost:8766/callback      fallback 1\n"
        "  http://localhost:8767/callback      fallback 2\n"
        "  http://localhost:8768/callback      fallback 3\n"
        "  http://localhost:8769/callback      fallback 4\n"
        "Register all 5 if port 8765 is often in use on your machine."
    )
    client_id = typer.prompt("Atlassian OAuth Client ID").strip()
    client_secret = typer.prompt("Atlassian OAuth Client Secret", hide_input=True).strip() or None
    raw_domain = typer.prompt("Jira base URL (e.g. https://xyz.atlassian.net)").strip()
    domain = raw_domain.replace("https://", "").replace("http://", "").rstrip("/")
    if not domain or not _DOMAIN_RE.match(domain):
        from icx_engine.exceptions import InvalidInput
        render_icx_error(InvalidInput("Invalid domain. Enter just the hostname, e.g. company.atlassian.net"), err_console)
        raise typer.Exit(1)
    config = ConfigManager.load()
    if any(c.domain == domain for c in config.connections):
        overwrite = typer.confirm(
            f"Connection for '{domain}' already exists. Overwrite?", default=False
        )
        if not overwrite:
            typer.echo("Cancelled.")
            return

    if debug:
        typer.echo("  starting OAuth PKCE flow (opening browser)...", err=True)
    else:
        typer.echo("\nOpening browser for Atlassian login...")

    try:
        tokens = asyncio.run(run_pkce_flow(
            auth_endpoint="https://auth.atlassian.com/authorize",
            token_endpoint="https://auth.atlassian.com/oauth/token",
            client_id=client_id,
            client_secret=client_secret,
            scopes=["read:jira-work", "read:jira-user", "offline_access"],
            extra_auth_params={"audience": "api.atlassian.com", "prompt": "consent"},
        ))
    except Exception as exc:
        if debug:
            raise
        from icx_engine.exceptions import AuthError
        import httpx as _httpx
        if isinstance(exc, _httpx.HTTPStatusError) and exc.response.status_code == 401:
            render_icx_error(
                AuthError(
                    "Token exchange failed (401). Verify your Client ID and Client Secret "
                    "match your app at developer.atlassian.com."
                ),
                err_console,
            )
        else:
            render_icx_error(exc, err_console, show_traceback=False)
        raise typer.Exit(1)

    access_token = tokens.get("access_token", "")
    if not access_token:
        from icx_engine.exceptions import AuthError
        render_icx_error(
            AuthError("OAuth token exchange did not return an access token. Check your OAuth app configuration."),
            err_console,
        )
        raise typer.Exit(1)
    refresh_token_val = tokens.get("refresh_token", "")
    expires_at = int(time.time()) + tokens.get("expires_in", 3600)

    async def _get_cloud_id():
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                "https://api.atlassian.com/oauth/token/accessible-resources",
                headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
            )
            resp.raise_for_status()
            resources = resp.json()
            if not resources:
                raise RuntimeError("No Jira sites found for this account.")
            from urllib.parse import urlparse as _urlparse
            for r in resources:
                if _urlparse(r.get("url", "")).hostname == domain:
                    return _validated_cloud_id(r["id"])
            return _validated_cloud_id(resources[0]["id"])

    try:
        if debug:
            typer.echo(f"  fetching cloud ID for {domain}...", err=True)
            cloud_id = asyncio.run(_get_cloud_id())
        else:
            with console.status(f"[bold]Fetching cloud ID for {domain}...[/bold]", spinner="dots"):
                cloud_id = asyncio.run(_get_cloud_id())
    except Exception as exc:
        if debug:
            raise
        render_icx_error(exc, err_console, show_traceback=False)
        raise typer.Exit(1)

    config.connections = [c for c in config.connections if c.domain != domain]
    new_conn = JiraConnection(
        domain=domain,
        auth=JiraOAuthAuth(
            auth_type="oauth",
            client_id=client_id,
            client_secret=client_secret,
            access_token=access_token,
            refresh_token=refresh_token_val,
            expires_at=expires_at,
            cloud_id=cloud_id,
        ),
    )
    config.connections.append(new_conn)
    if len(config.connections) == 1:
        config.default_connection = f"jira:{domain}"
    ConfigManager.save(config)
    ConfigManager.warn_if_plaintext()
    console.print(f"[green]✓ Connected to {domain} via OAuth PKCE[/green]")
