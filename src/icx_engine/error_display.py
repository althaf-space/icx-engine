from __future__ import annotations
from rich.console import Console
from rich.panel import Panel
from icx_engine.exceptions import (
    AuthError, ContextBuildError, ICXMemoryError, InvalidInput, IssueNotFound,
    NoConnectionError, NoLLMError, OAuthRefreshError, RateLimited, SourceUnavailable,
)

# Maps each ICXError subclass to (why_text, how_text) shown in the error panel.
_GUIDANCE: dict[type, tuple[str, str]] = {
    AuthError: (
        "Authentication or permission failure from the issue tracker.",
        "Run `icx connection --add` to re-enter your credentials.",
    ),
    IssueNotFound: (
        "The issue key does not exist or your account cannot access it.",
        "Verify the issue key and your user permissions in the tracker.",
    ),
    RateLimited: (
        "The issue tracker API rate limit was reached.",
        "Wait briefly and retry. Check Jira rate-limit settings if it persists.",
    ),
    SourceUnavailable: (
        "The issue tracker returned a server error (5xx) or is unreachable.",
        "Retry in a moment. If the problem persists, check the tracker's status page.",
    ),
    InvalidInput: (
        "The input is not a valid issue key or URL.",
        "Expected a bare key like ABC-123 or a full tracker URL (e.g. https://yourcompany.example.com/browse/ABC-123).",
    ),
    NoConnectionError: (
        "No connection is configured for this domain.",
        "Run `icx connection --add` to add a connection.",
    ),
    NoLLMError: (
        "No AI provider is configured.",
        "Run `icx model --add` to configure one.",
    ),
    OAuthRefreshError: (
        "The OAuth access token could not be refreshed.",
        "Run `icx connection --add` to re-authenticate.",
    ),
    ContextBuildError: (
        "The LLM returned output that could not be parsed into structured context.",
        "Pass --traceback to see the raw LLM response. Try a different model or retry.",
    ),
    ICXMemoryError: (
        "Local memory operation failed.",
        "Run `icx memory status` to check storage state. "
        "If the issue persists, run `icx memory clear --confirm` to reset.",
    ),
}

_AI_AUTH_KEYWORDS = ("gemini", "openai", "anthropic", "xai", "nim", "grok")


def render_icx_error(
    exc: Exception,
    console: Console,
    show_traceback: bool = False,
) -> None:
    """Render an exception as a Rich Panel with What / Why / How guidance."""
    why = "Unexpected error."
    how = "Pass --debug --traceback for full details."
    # Dict order matters: more-specific types must precede any base class they share.
    for exc_type, (w, h) in _GUIDANCE.items():
        if isinstance(exc, exc_type):
            why, how = w, h
            break

    if isinstance(exc, AuthError):
        msg = str(exc).lower()
        if any(kw in msg for kw in _AI_AUTH_KEYWORDS):
            why = "Authentication or permission failure from the AI provider."
            how = "Run `icx model --add` to update your AI credentials."

    body = (
        f"[bold]What:[/bold] {exc}\n"
        f"[bold]Why:[/bold]  {why}\n"
        f"[bold]How:[/bold]  {how}"
    )
    if isinstance(exc, ContextBuildError) and exc.raw_output and show_traceback:
        body += f"\n\n[dim]Raw LLM output:[/dim]\n{exc.raw_output}"

    console.print(Panel(body, title="[bold red]ICX Error[/bold red]", border_style="red"))

    if show_traceback:
        import traceback as _tb
        console.print("".join(_tb.format_exception(type(exc), exc, exc.__traceback__)))
