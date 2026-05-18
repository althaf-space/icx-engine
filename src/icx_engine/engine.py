from __future__ import annotations
import asyncio
import re
from typing import TYPE_CHECKING, Callable
if TYPE_CHECKING:
    from rich.console import Console as RichConsole
from urllib.parse import urlparse

from icx_engine.exceptions import InvalidInput, NoConnectionError, NoLLMError
from icx_engine.models.config import AppConfig, BaseConnection
from icx_engine.models.output import RawIssueData, IssueContext, RawIssueResponse

_ISSUE_KEY_RE = re.compile(r"^[A-Z][A-Z0-9]*-[0-9]+$")

_IMAGE_EXTENSIONS = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".tif"
})


def _extract_project_key(issue_key: str) -> str:
    """Extract project prefix from issue key. Best-effort - not critical path."""
    if "-" in issue_key:
        return issue_key.split("-")[0]
    return issue_key


def _split_attachments(
    attachment_urls: dict[str, str],
) -> tuple[dict[str, str], list[str]]:
    """Return (non_image_urls, image_filenames)."""
    import os
    non_image: dict[str, str] = {}
    image_names: list[str] = []
    for filename, url in attachment_urls.items():
        if os.path.splitext(filename.lower())[1] in _IMAGE_EXTENSIONS:
            image_names.append(filename)
        else:
            non_image[filename] = url
    return non_image, image_names


def extract_domain(input_str: str) -> str | None:
    """
    Return the URL host for URL inputs, or None for bare issue keys.

    Used to resolve which connection to use before delegating full input
    parsing to the connector. Does NOT parse URL paths - that is the
    connector's responsibility.

    Raises InvalidInput for strings that are neither a bare key nor a URL.
    """
    raw = input_str.strip()

    if _ISSUE_KEY_RE.match(raw.upper()):
        return None

    has_scheme = raw.lower().startswith(("http://", "https://"))
    if not has_scheme and "." not in raw and "/" not in raw:
        raise InvalidInput(
            "Invalid issue key format. Expected something like ABC-123 or a full URL."
        )

    normalised = raw if has_scheme else f"https://{raw}"
    parsed = urlparse(normalised)
    if parsed.hostname:
        if parsed.username or parsed.password:
            raise InvalidInput(
                "Invalid URL: embedded credentials are not supported. "
                "Use `icx connection --add` to configure authentication."
            )
        return parsed.hostname

    raise InvalidInput(
        "Invalid issue key format. Expected something like ABC-123 or a full URL."
    )


def narrow_connections(connections: list[BaseConnection], raw_input: str) -> list[BaseConnection]:
    """
    Filter connections to those whose connector recognises raw_input's key format.

    Uses each connector's can_handle_bare_key() classmethod as a hint - not a
    guarantee. Falls back to the full list if nothing matches so the caller is
    never left with an empty set.

    Unknown connector types are included rather than excluded (safe default).
    """
    from icx_engine.connectors.base import get_all_connector_classes
    cls_map = {cls.connector_type(): cls for cls in get_all_connector_classes()}
    candidates = []
    for conn in connections:
        cls = cls_map.get(conn.connector_type)
        if cls is None or cls.can_handle_bare_key(raw_input):
            candidates.append(conn)
    return candidates if candidates else connections


def resolve_connection(
    domain: str | None,
    config: AppConfig,
    raw_input: str | None = None,
) -> BaseConnection | None:
    """
    Returns a BaseConnection or None.
    None means multiple connections remain after all disambiguation attempts -
    caller should show a selector.

    Resolution order for bare keys (domain=None):
      1. Single connection configured → use it
      2. default_connection set → use it
      3. raw_input provided → narrow by can_handle_bare_key(); auto-pick if
         exactly one candidate remains
      4. Still ambiguous → return None (caller prompts)

    Raises NoConnectionError if no connections configured or domain not found.
    """
    if not config.connections:
        raise NoConnectionError("No connectors configured. Run `icx connection --add` first.")

    if domain:
        for conn in config.connections:
            if conn.domain == domain:
                return conn
        raise NoConnectionError(
            f"No connection found for {domain}. Run `icx connection --add` to add it."
        )

    if len(config.connections) == 1:
        return config.connections[0]

    # Multiple connections - use default/active connection
    if config.default_connection:
        for conn in config.connections:
            if f"{conn.connector_type}:{conn.domain}" == config.default_connection:
                return conn
        # default_connection is set but points to a removed connection
        raise NoConnectionError(
            f"Your active connection '{config.default_connection}' no longer exists. "
            f"Run `icx connection --active <domain>` to set a new default."
        )

    # Narrow by key format - auto-pick only when exactly one connector matches
    if raw_input:
        candidates = narrow_connections(config.connections, raw_input)
        if len(candidates) == 1:
            return candidates[0]

    return None  # still ambiguous - caller must prompt


def _heuristic_confidence_triggered(
    result: IssueContext,
    raw: RawIssueData,
    images: dict[str, str],
) -> bool:
    """Return True if ANY heuristic condition indicates the analysis may be unreliable."""
    from icx_engine.connectors.attachments import _is_image

    if result.confidence_score < 0.8:
        return True

    if images:
        total_ocr = sum(len(t) for f, t in raw.attachment_texts.items() if _is_image(f))
        if total_ocr < 500:
            return True

    if raw.issue_type.lower() == "bug" and not result.reproduction_steps:
        return True

    return False


async def run(
    input_str: str,
    config: AppConfig,
    connection: BaseConnection | None = None,
    log: Callable[[str], None] | None = None,
    mcp_mode: bool = False,
    profile_override: str | None = None,
    debug_console: "RichConsole | None" = None,
    skip_vision: bool = False,
) -> IssueContext | RawIssueResponse:
    """
    Core engine entry point. Called by both CLI and MCP.

    Fetches the issue, processes attachments (parallel, UAE), runs LLM analysis,
    and returns a structured IssueContext.

    profile_override selects an LLM profile by name for this request only.
    It never mutates config - the active profile is unchanged after the call.

    When mcp_mode=True and no LLM is configured, returns RawIssueResponse with
    all raw issue data, processed attachment content, and Base64 images.

    When mcp_mode=False (default, CLI behavior) and no LLM is configured,
    raises NoLLMError as before.

    Pass `connection` when the caller already resolved it (e.g. after a workspace selector).
    Pass `log` to receive step-by-step debug output (printed to stderr by the CLI).
    """
    conn = connection
    if conn is None:
        domain = extract_domain(input_str)
        conn = resolve_connection(domain, config, raw_input=input_str)
        if conn is None:
            raise NoConnectionError(
                "Multiple connections configured and no default set. "
                "Run `icx connection --active <domain>` to set one, or pass the full issue URL."
            )

    # Resolve which LLM profile to use - volatile, never written back to config.
    if profile_override is not None:
        active_llm = config.llm_profiles.get(profile_override)
        if active_llm is None:
            available = ", ".join(sorted(config.llm_profiles)) or "none"
            raise NoLLMError(
                f"Profile '{profile_override}' not found. "
                f"Available profiles: {available}. Run `icx model --add` to add one."
            )
    else:
        active_llm = config.active_llm

    from icx_engine.connectors.base import get_connector
    connector = get_connector(conn)
    parsed = connector.parse_input(input_str)
    issue_key = parsed.issue_key

    if log:
        log(f"  fetching {issue_key} from {conn.domain}...")
    raw = await connector.fetch(issue_key, config, log=log)
    if log:
        log(f"  ✓ {issue_key}: {raw.summary[:72]}")

    images: dict[str, str] = {}
    pending_images: list[str] = []
    if raw.attachment_content_urls:
        if log:
            names = ", ".join(raw.attachment_content_urls)
            log(f"  attachments: {names}")
        if skip_vision:
            non_image_urls, pending_images = _split_attachments(raw.attachment_content_urls)
            raw_for_attach = raw.model_copy(update={"attachment_content_urls": non_image_urls})
            attachment_texts, images = await connector.process_attachments(raw_for_attach, active_llm, log=log)
        else:
            attachment_texts, images = await connector.process_attachments(raw, active_llm, log=log)
        raw = raw.model_copy(update={"attachment_texts": attachment_texts})

    if active_llm is None:
        if mcp_mode:
            return RawIssueResponse(
                issue_key=raw.issue_key,
                issue_type=raw.issue_type,
                summary=raw.summary,
                description=raw.description,
                comments=raw.comments,
                attachments=raw.attachments,
                priority=raw.priority,
                status=raw.status,
                metadata=raw.metadata,
                due_date=raw.due_date,
                attachment_texts=raw.attachment_texts,
                images=images,
            )
        raise NoLLMError("No AI provider configured. Run `icx model --add` first.")

    from icx_engine.llm.base import get_provider, build_user_message
    text_cfg = active_llm.text_config
    if log:
        log(f"  analyzing with {text_cfg.provider} / {text_cfg.model}...")
    if debug_console is not None:
        from rich.rule import Rule
        from rich.syntax import Syntax
        from rich.panel import Panel
        prompt_text = build_user_message(raw)
        debug_console.print(Rule(f"[bold cyan]Prompt → {text_cfg.provider}/{text_cfg.model}[/bold cyan]"))
        debug_console.print(Panel(
            Syntax(prompt_text, "text", word_wrap=True),
            title="[cyan]User message sent to LLM[/cyan]",
            border_style="cyan",
        ))
        debug_console.print(Rule(style="cyan"))
    elif log:
        log(f"\n── prompt sent to LLM ──\n{build_user_message(raw)}\n────────────────────────")

    provider = get_provider(text_cfg)
    try:
        result = await asyncio.wait_for(provider.analyze(raw), timeout=120.0)
    except asyncio.TimeoutError:
        from icx_engine.exceptions import NoLLMError
        raise NoLLMError(
            f"LLM request timed out after 120s ({text_cfg.provider}/{text_cfg.model}). "
            "Check your API key and network connection."
        )

    from icx_engine.grounding import visual_grounding_pass
    result = await visual_grounding_pass(result, raw, active_llm.image_config, connector, log=log)

    # Attach Base64 images when present - skipped automatically when skip_vision=True (images={}).
    if images:
        if log and _heuristic_confidence_triggered(result, raw, images):
            log("  ⚠ Low confidence or poor OCR - raw images attached to output for verification")
        result = result.model_copy(update={"images": images})

    # Set pending_images signal for fast/text-only mode.
    if pending_images:
        result = result.model_copy(update={"pending_images": pending_images})

    # Memory enrichment - CLI mode only. MCP agents call search_memory deliberately.
    if not mcp_mode:
        try:
            from icx_engine.memory import MemoryManager, MemoryQueryInput
            _mem = MemoryManager()
            _query = MemoryQueryInput(
                issue_key=raw.issue_key,
                project_key=_extract_project_key(raw.issue_key),
                source_type=connector.connector_type(),
                summary=result.problem_summary,
                description=result.detailed_description,
                issue_type=raw.issue_type,
            )
            _insights = _mem.query(_query)
            if _insights:
                result = result.model_copy(update={"past_insights": _insights})
        except Exception as _mem_exc:
            if log:
                log(f"[memory] query skipped: {_mem_exc}")

    return result
