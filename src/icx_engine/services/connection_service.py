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

# Provider menu + default models are derived from the single-source registry.
# cli.py re-exports these two names for backward compatibility.
from icx_engine.llm.registry import PROVIDERS as _PROVIDER_SPECS

_PROVIDERS = [(name, spec.cli_label) for name, spec in _PROVIDER_SPECS.items()]

_DEFAULT_MODELS: dict[str, dict[str, str]] = {
    name: {"text": spec.default_text_model, "image": spec.default_image_model}
    for name, spec in _PROVIDER_SPECS.items()
}


def _prompt_channel_config(label: str, provider_key: str | None = None) -> "ChannelConfig":
    """Interactively prompt for one channel's provider/model/key/url."""
    from icx_engine.models.config import ChannelConfig

    typer.echo(f"\n-- {label} --")
    if provider_key is None:
        for i, (_, lbl) in enumerate(_PROVIDERS, 1):
            typer.echo(f"  {i}. {lbl}")
        choice = typer.prompt("Select provider", default="1")
        try:
            idx = int(choice.strip()) - 1
            provider_key, _ = _PROVIDERS[idx]
        except (ValueError, IndexError):
            err_console.print("Invalid choice.")
            raise typer.Exit(1)

    model_default = _DEFAULT_MODELS[provider_key]["text" if "Text" in label else "image"]
    model = typer.prompt("Model", default=model_default)

    api_key: str | None = None
    base_url: str | None = None

    spec = _PROVIDER_SPECS[provider_key]
    provider_label = next(lbl for k, lbl in _PROVIDERS if k == provider_key)

    if spec.prompts_for_base_url and not spec.prompts_for_api_key:
        # URL-only: no secret in play, so no HTTPS enforcement (e.g. Ollama).
        url_label = "Ollama base URL" if provider_key == "ollama" else f"{provider_label} base URL"
        default_url = spec.default_base_url or ""
        custom_url = typer.prompt(url_label, default=default_url)
        base_url = None if custom_url == default_url else custom_url
    elif spec.prompts_for_base_url and spec.prompts_for_api_key:
        # Key + URL: enforce HTTPS so the API key is never sent to a custom
        # endpoint in the clear (e.g. NIM).
        if provider_key == "nim":
            key_label, url_label = "Nvidia NIM API key", "NIM base URL"
            https_error = "NIM base URL must use HTTPS."
        else:
            key_label, url_label = f"{provider_label} API key", f"{provider_label} base URL"
            https_error = f"{url_label} must use HTTPS."
        api_key = typer.prompt(key_label, hide_input=True).strip()
        default_url = spec.default_base_url or ""
        custom_url = typer.prompt(url_label, default=default_url)
        if custom_url.startswith("http://"):
            render_icx_error(ValueError(https_error), err_console)
            raise typer.Exit(1)
        base_url = None if custom_url == default_url else custom_url
    elif spec.prompts_for_api_key:
        api_key = typer.prompt(f"{provider_label} API key", hide_input=True).strip()

    return ChannelConfig(provider=provider_key, model=model, api_key=api_key, base_url=base_url)


def _prompt_vision_channel(text_config: "ChannelConfig") -> "ChannelConfig | None":
    """Prompt for vision channel choice: same provider / different / skip."""
    from icx_engine.models.config import ChannelConfig

    typer.echo(
        "\nConfigure vision channel?\n"
        "  1. Same provider as text  (reuses key & URL, just pick image model)\n"
        "  2. Different provider     (full setup for image channel)\n"
        "  3. Skip - OCR only\n"
    )
    choice = typer.prompt("Select", default="1").strip()
    if choice == "3":
        return None
    if choice == "2":
        return _prompt_channel_config("Visual Intelligence", provider_key=None)
    # Same provider
    image_model = typer.prompt(
        "Image model",
        default=_DEFAULT_MODELS[text_config.provider]["image"],
    )
    return ChannelConfig(
        provider=text_config.provider,
        model=image_model,
        api_key=text_config.api_key,
        base_url=text_config.base_url,
    )


def _resolve_and_confirm_domain(raw_domain: str, config) -> str | None:
    """Strip scheme/trailing slash, validate hostname format, and confirm
    overwrite if a connection for the resolved domain already exists.

    Shared by _connect_jira_token and _connect_jira_oauth - identical logic
    in both flows up to this point. Returns the resolved domain, or None if
    the user declined to overwrite an existing connection (caller returns)."""
    domain = raw_domain.replace("https://", "").replace("http://", "").rstrip("/")
    if not domain or not _DOMAIN_RE.match(domain):
        from icx_engine.exceptions import InvalidInput
        render_icx_error(InvalidInput("Invalid domain. Enter just the hostname, e.g. company.atlassian.net"), err_console)
        raise typer.Exit(1)
    if any(c.domain == domain for c in config.connections):
        overwrite = typer.confirm(
            f"Connection for '{domain}' already exists. Overwrite?", default=False
        )
        if not overwrite:
            typer.echo("Cancelled.")
            return None
    return domain


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
    config = ConfigManager.load()
    domain = _resolve_and_confirm_domain(raw_domain, config)
    if domain is None:
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
    console.print(f"[green]OK Connected to {domain} as {display_name}[/green]")


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
    config = ConfigManager.load()
    domain = _resolve_and_confirm_domain(raw_domain, config)
    if domain is None:
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
            scopes=["read:jira-work", "write:jira-work", "read:jira-user", "offline_access"],
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
    console.print(f"[green]OK Connected to {domain} via OAuth PKCE[/green]")


def _connect_gitlab(debug: bool = False, default_name: str = "default") -> None:
    """Interactive GitLab connect flow, full parity with `_sonar_add_flow` -
    name, URL, token, TLS, active-or-not, all prompted. Routed through this
    module per CLAUDE.md's credential-prompt rule, not inlined into cli.py."""
    import asyncio
    from icx_engine.gitlab import service as gitlab_service

    name = typer.prompt("Connection name", default=default_name)
    url = typer.prompt("GitLab server URL").strip()
    token = typer.prompt(
        "Personal access token (blank to keep existing)",
        default="", hide_input=True, show_default=False,
    ).strip()
    verify_tls = typer.confirm("Verify TLS certificates?", default=True)
    make_active = typer.confirm("Make this the active connection?", default=True)

    with console.status(f"[bold]Verifying credentials with {url}...[/bold]", spinner="dots"):
        out = asyncio.run(gitlab_service.add_connection(
            name.strip(), url, token or None, verify_tls=verify_tls, make_active=make_active,
        ))

    validation = out["validation"]
    if validation.get("valid") is True:
        user = validation.get("user", {})
        console.print(f"[green]Connected '{out['name']}' as {user.get('name', user.get('username', ''))}[/green]")
    else:
        console.print(f"[yellow]Saved connection '{out['name']}', but validation failed: {validation.get('error', validation)}[/yellow]")
    console.print(f"  active: {out['active']}")
    from icx_engine.config_manager import ConfigManager
    ConfigManager.warn_if_plaintext()


def _connect_workstatus(debug: bool = False, default_name: str = "default") -> None:
    """Interactive Workstatus connect flow, full parity with `_connect_gitlab`/
    `_sonar_add_flow` - name, session values, active-or-not, all prompted.
    Workstatus has no public API docs and its login request body was never
    captured (see developer.md) - rather than guess it, this prompts for the
    four session header values the user copies from their own authenticated
    browser session's Network tab (Authorization/UserID/OrgID/SDToken on any
    api.workstatus.io request)."""
    import asyncio
    from icx_engine.workstatus import service as workstatus_service

    name = typer.prompt("Connection name", default=default_name).strip()
    typer.echo("\nWorkstatus - Session Credentials")
    typer.echo(
        "Workstatus has no public API. Log into app.workstatus.io in your browser, open "
        "DevTools > Network, click any request to web-api.workstatus.io, and copy these "
        "header values from its Request Headers exactly as shown (including a 'Bearer ' "
        "prefix on Authorization if one is present - it is sent through unmodified):\n"
    )
    user_id = typer.prompt("UserID header value").strip()
    org_id = typer.prompt("OrgID header value").strip()
    if debug:
        typer.echo("(--debug: values are visible)")
    authorization = typer.prompt("Authorization header value", hide_input=not debug).strip()
    sd_token = typer.prompt("SDToken header value", hide_input=not debug).strip()
    device_type = typer.prompt("deviceType header value", default="web").strip()
    make_active = typer.confirm("Make this the active connection?", default=True)

    with console.status("[bold]Verifying Workstatus session...[/bold]", spinner="dots"):
        out = asyncio.run(workstatus_service.add_connection(
            name, user_id, org_id, authorization, sd_token,
            device_type=device_type, make_active=make_active,
        ))

    validation = out["validation"]
    if validation.get("valid") is True:
        console.print(f"[green]Connected '{out['name']}'. Unread notifications: {validation.get('unread_notifications')}[/green]")
    else:
        console.print(f"[yellow]Saved connection '{out['name']}', but validation failed: {validation.get('error', validation)}[/yellow]")
    console.print(f"  active: {out['active']}")
    from icx_engine.config_manager import ConfigManager
    ConfigManager.warn_if_plaintext()


def _sonar_add_flow(default_name: str = "default") -> None:
    from icx_engine.sonar import service
    name = typer.prompt("Connection name", default=default_name)
    url = typer.prompt("SonarQube server URL")
    token = typer.prompt("SonarQube token (blank to keep existing)", default="", hide_input=True, show_default=False)
    verify_tls = typer.confirm("Verify TLS certificates?", default=True)
    make_active = typer.confirm("Make this the active connection?", default=True)
    out = asyncio.run(service.add_connection(
        name.strip(), url.strip(), token.strip() or None,
        verify_tls=verify_tls, make_active=make_active,
    ))
    console.print(f"[green]Sonar connection '{out['name']}' saved.[/green]")
    console.print(f"  url:        {out['url']}")
    console.print(f"  verify_tls: {out['verify_tls']}")
    console.print(f"  active:     {out['active']}")
    v = out.get("validation") or {}
    if v.get("valid") is True:
        console.print(f"  connection: [green]ok[/green] (SonarQube {v.get('version', '')})")
    elif v.get("valid") is False:
        console.print(f"  connection: [red]failed[/red] {v.get('error', '')}")
