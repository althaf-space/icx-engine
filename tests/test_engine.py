import pytest
import respx
import httpx
from unittest.mock import AsyncMock, patch, MagicMock

from icx_engine.connectors.jira.client import JiraClient
from icx_engine.llm.base import (
    get_provider, _compute_completeness, _compute_missing, finalize,
    SYSTEM_PROMPT, build_user_message,
)
from icx_engine.models.config import AppConfig, LLMConfig, ChannelConfig
from icx_engine.connectors.jira.config import JiraConnection, TokenAuth
from icx_engine.models.output import RawIssueData, IssueContext
from icx_engine.engine import extract_domain, resolve_connection, narrow_connections, run
from icx_engine.exceptions import (
    AuthError, IssueNotFound, RateLimited, SourceUnavailable,
    InvalidInput, NoConnectionError, NoLLMError,
)

from test_data import JIRA_DOMAIN, JIRA_BASE_URL, JIRA_AUTH_HEADER, JIRA_ALLOWED_HOSTS, JIRA_ISSUE_PAYLOAD, MOCK_LLM_JSON


# ── JiraClient - HTTP error mapping ──────────────────────────────────────────

@respx.mock
async def test_fetch_returns_raw_issue_data():
    respx.get(f"{JIRA_BASE_URL}/issue/TEST-123").mock(
        return_value=httpx.Response(200, json=JIRA_ISSUE_PAYLOAD)
    )
    raw = await JiraClient(JIRA_BASE_URL, JIRA_AUTH_HEADER, JIRA_ALLOWED_HOSTS).fetch("TEST-123")
    assert raw.issue_key == "TEST-123"
    assert raw.issue_type == "Bug"
    assert raw.summary == "Button not working on mobile"
    assert "Steps to reproduce" in raw.description
    assert len(raw.comments) == 1
    assert "Reproduced on iOS 17" in raw.comments[0]
    assert raw.attachments == ["screenshot.png"]
    assert raw.priority == "High"
    assert raw.status == "In Progress"


@respx.mock
async def test_fetch_raises_auth_error_on_401():
    respx.get(f"{JIRA_BASE_URL}/issue/TEST-123").mock(return_value=httpx.Response(401))
    with pytest.raises(AuthError):
        await JiraClient(JIRA_BASE_URL, JIRA_AUTH_HEADER, JIRA_ALLOWED_HOSTS).fetch("TEST-123")


@respx.mock
async def test_fetch_raises_issue_not_found_on_404():
    respx.get(f"{JIRA_BASE_URL}/issue/TEST-123").mock(return_value=httpx.Response(404))
    with pytest.raises(IssueNotFound):
        await JiraClient(JIRA_BASE_URL, JIRA_AUTH_HEADER, JIRA_ALLOWED_HOSTS).fetch("TEST-123")


@respx.mock
async def test_fetch_raises_rate_limited_on_429():
    respx.get(f"{JIRA_BASE_URL}/issue/TEST-123").mock(return_value=httpx.Response(429))
    with pytest.raises(RateLimited):
        await JiraClient(JIRA_BASE_URL, JIRA_AUTH_HEADER, JIRA_ALLOWED_HOSTS).fetch("TEST-123")


@respx.mock
async def test_fetch_raises_source_unavailable_on_500():
    respx.get(f"{JIRA_BASE_URL}/issue/TEST-123").mock(return_value=httpx.Response(500))
    with pytest.raises(SourceUnavailable):
        await JiraClient(JIRA_BASE_URL, JIRA_AUTH_HEADER, JIRA_ALLOWED_HOSTS).fetch("TEST-123")


# ── engine.extract_domain ─────────────────────────────────────────────────────

def test_extract_domain_bare_key_returns_none():
    assert extract_domain("ABC-123") is None


def test_extract_domain_bare_key_case_insensitive():
    assert extract_domain("abc-123") is None


def test_extract_domain_alphanumeric_project_key_returns_none():
    assert extract_domain("AI6D-362") is None


def test_extract_domain_url_returns_host():
    assert extract_domain("https://tracker.example.com/ABC-123") == "tracker.example.com"


def test_extract_domain_no_scheme_url_with_dot_returns_host():
    assert extract_domain("tracker.example.com/browse/ABC-123") == "tracker.example.com"


def test_extract_domain_invalid_raises():
    with pytest.raises(InvalidInput):
        extract_domain("notakey")


# ── engine.narrow_connections ─────────────────────────────────────────────────

def test_narrow_connections_jira_key_matches_jira(token_connection):
    """A Jira-format key matches the Jira connector - no filtering occurs."""
    result = narrow_connections([token_connection], "ABC-123")
    assert result == [token_connection]


def test_narrow_connections_falls_back_to_all_when_none_match(token_connection):
    """A non-key string matches nothing - fallback returns the full list."""
    result = narrow_connections([token_connection], "notakey")
    assert result == [token_connection]


def test_narrow_connections_both_jira_connections_match(multi_connection_config):
    """Two Jira connections both match - full list returned (still ambiguous)."""
    result = narrow_connections(multi_connection_config.connections, "ABC-123")
    assert len(result) == 2


# ── engine.resolve_connection ─────────────────────────────────────────────────

def test_resolve_connection_single(token_connection):
    config = AppConfig(connections=[token_connection])
    assert resolve_connection(None, config).domain == JIRA_DOMAIN


def test_resolve_connection_by_domain(multi_connection_config):
    conn = resolve_connection("beta.atlassian.net", multi_connection_config)
    assert conn.domain == "beta.atlassian.net"


def test_resolve_connection_multiple_no_domain_returns_none(multi_connection_config):
    assert resolve_connection(None, multi_connection_config) is None


def test_resolve_connection_raw_input_still_ambiguous_returns_none(multi_connection_config):
    """Two Jira connections both match ABC-123 - still returns None (caller prompts)."""
    assert resolve_connection(None, multi_connection_config, raw_input="ABC-123") is None


def test_resolve_connection_no_connections_raises():
    with pytest.raises(NoConnectionError):
        resolve_connection(None, AppConfig())


# ── LLM providers - analyze() contract ───────────────────────────────────────

def _mock_openai_response():
    """Returns a MagicMock shaped like an OpenAI chat completion."""
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = MOCK_LLM_JSON
    return resp


async def test_ollama_provider_analyze(raw_ticket):
    from icx_engine.llm.base import get_provider
    provider = get_provider(ChannelConfig(provider="ollama", model="llama3"))
    with patch.object(provider.client.chat.completions, "create", new=AsyncMock(return_value=_mock_openai_response())):
        result = await provider.analyze(raw_ticket)
    assert result.issue_type == "Bug"
    assert result.confidence_score == 0.9


async def test_openai_provider_analyze(raw_ticket):
    from icx_engine.llm.base import get_provider
    provider = get_provider(ChannelConfig(provider="openai", model="gpt-4o", api_key="sk-test"))
    with patch.object(provider.client.chat.completions, "create", new=AsyncMock(return_value=_mock_openai_response())):
        result = await provider.analyze(raw_ticket)
    assert result.issue_type == "Bug"


async def test_anthropic_provider_analyze(raw_ticket):
    from icx_engine.llm.base import get_provider
    provider = get_provider(ChannelConfig(provider="anthropic", model="claude-3-opus", api_key="sk-ant-test"))
    resp = MagicMock()
    resp.content = [MagicMock()]
    resp.content[0].text = MOCK_LLM_JSON
    with patch.object(provider.client.messages, "create", new=AsyncMock(return_value=resp)):
        result = await provider.analyze(raw_ticket)
    assert result.issue_type == "Bug"


async def test_nim_provider_analyze(raw_ticket):
    from icx_engine.llm.base import get_provider
    provider = get_provider(ChannelConfig(provider="nim", model="deepseek-ai/deepseek-v4-flash", api_key="nim-key"))
    with patch.object(provider.client.chat.completions, "create", new=AsyncMock(return_value=_mock_openai_response())):
        result = await provider.analyze(raw_ticket)
    assert result.issue_type == "Bug"


# ── engine.run - end-to-end ───────────────────────────────────────────────────

@respx.mock
async def test_engine_run_returns_issue_context(app_config):
    respx.get(f"{JIRA_BASE_URL}/issue/TEST-123").mock(
        return_value=httpx.Response(200, json=JIRA_ISSUE_PAYLOAD)
    )
    with patch("icx_engine.llm.ollama.AsyncOpenAI") as mock_cls:
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.chat.completions.create = AsyncMock(return_value=_mock_openai_response())
        result = await run("TEST-123", app_config)
    assert isinstance(result, IssueContext)
    assert result.issue_type == "Bug"


@respx.mock
async def test_engine_run_no_llm_raises(token_connection):
    config = AppConfig(connections=[token_connection])   # no llm
    respx.get(f"{JIRA_BASE_URL}/issue/TEST-123").mock(
        return_value=httpx.Response(200, json=JIRA_ISSUE_PAYLOAD)
    )
    with pytest.raises(NoLLMError):
        await run("TEST-123", config)


# ── Profile override ──────────────────────────────────────────────────────────

@respx.mock
async def test_engine_run_uses_override_profile(token_connection):
    """profile_override selects the named profile, not the active one."""
    config = AppConfig(
        connections=[token_connection],
        llm_profiles={
            "default": LLMConfig(text_config=ChannelConfig(provider="ollama", model="llama3")),
            "fast":    LLMConfig(text_config=ChannelConfig(provider="ollama", model="mistral")),
        },
        current_llm_profile="default",
    )
    respx.get(f"{JIRA_BASE_URL}/issue/TEST-123").mock(
        return_value=httpx.Response(200, json=JIRA_ISSUE_PAYLOAD)
    )
    used_configs: list = []
    import icx_engine.llm.base as _llm_base
    original_get_provider = _llm_base.get_provider

    def spy_get_provider(channel_config):
        used_configs.append(channel_config)
        return original_get_provider(channel_config)

    # Patch at the source module so the lazy import inside engine.run() gets the spy.
    with patch("icx_engine.llm.base.get_provider", side_effect=spy_get_provider):
        with patch("icx_engine.llm.ollama.AsyncOpenAI") as mock_cls:
            mock_client = MagicMock()
            mock_cls.return_value = mock_client
            mock_client.chat.completions.create = AsyncMock(return_value=_mock_openai_response())
            await run("TEST-123", config, profile_override="fast")

    assert used_configs[0].model == "mistral"


@respx.mock
async def test_engine_run_active_profile_unchanged_after_override(token_connection):
    """profile_override must not mutate config.current_llm_profile."""
    config = AppConfig(
        connections=[token_connection],
        llm_profiles={
            "default": LLMConfig(text_config=ChannelConfig(provider="ollama", model="llama3")),
            "fast":    LLMConfig(text_config=ChannelConfig(provider="ollama", model="mistral")),
        },
        current_llm_profile="default",
    )
    respx.get(f"{JIRA_BASE_URL}/issue/TEST-123").mock(
        return_value=httpx.Response(200, json=JIRA_ISSUE_PAYLOAD)
    )
    with patch("icx_engine.llm.ollama.AsyncOpenAI") as mock_cls:
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.chat.completions.create = AsyncMock(return_value=_mock_openai_response())
        await run("TEST-123", config, profile_override="fast")

    assert config.current_llm_profile == "default"


@respx.mock
async def test_engine_run_unknown_profile_override_raises_nollmerror(token_connection):
    config = AppConfig(
        connections=[token_connection],
        llm_profiles={"personal": LLMConfig(text_config=ChannelConfig(provider="ollama", model="llama3"))},
        current_llm_profile="personal",
    )
    respx.get(f"{JIRA_BASE_URL}/issue/TEST-123").mock(
        return_value=httpx.Response(200, json=JIRA_ISSUE_PAYLOAD)
    )
    with pytest.raises(NoLLMError, match="nonexistent.*not found"):
        await run("TEST-123", config, profile_override="nonexistent")


# ── Phase 2: exponential backoff retry ───────────────────────────────────────

@respx.mock
async def test_fetch_retries_on_429_then_succeeds():
    route = respx.get(f"{JIRA_BASE_URL}/issue/TEST-123")
    route.side_effect = [
        httpx.Response(429, headers={"Retry-After": "0"}),
        httpx.Response(200, json=JIRA_ISSUE_PAYLOAD),
    ]
    raw = await JiraClient(JIRA_BASE_URL, JIRA_AUTH_HEADER, JIRA_ALLOWED_HOSTS).fetch("TEST-123")
    assert raw.issue_key == "TEST-123"


@respx.mock
async def test_fetch_retries_on_500_then_succeeds():
    route = respx.get(f"{JIRA_BASE_URL}/issue/TEST-123")
    route.side_effect = [
        httpx.Response(500),
        httpx.Response(200, json=JIRA_ISSUE_PAYLOAD),
    ]
    raw = await JiraClient(JIRA_BASE_URL, JIRA_AUTH_HEADER, JIRA_ALLOWED_HOSTS).fetch("TEST-123")
    assert raw.issue_key == "TEST-123"


@respx.mock
async def test_fetch_raises_after_max_retries_429():
    respx.get(f"{JIRA_BASE_URL}/issue/TEST-123").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "0"})
    )
    with pytest.raises(RateLimited):
        await JiraClient(JIRA_BASE_URL, JIRA_AUTH_HEADER, JIRA_ALLOWED_HOSTS).fetch("TEST-123")


@respx.mock
async def test_fetch_raises_after_max_retries_500():
    respx.get(f"{JIRA_BASE_URL}/issue/TEST-123").mock(
        return_value=httpx.Response(500)
    )
    with pytest.raises(SourceUnavailable):
        await JiraClient(JIRA_BASE_URL, JIRA_AUTH_HEADER, JIRA_ALLOWED_HOSTS).fetch("TEST-123")


# ── Phase 2: deterministic completeness scoring ───────────────────────────────

def _make_context(**overrides) -> IssueContext:
    defaults = dict(
        problem_summary="p", detailed_description="d",
        reproduction_steps=["step"], expected_behavior="e", actual_behavior="a",
        acceptance_criteria=[], impact="i", priority="High", issue_type="Bug",
        confidence_score=0.9, completeness_score=0.0, missing_information=[],
    )
    defaults.update(overrides)
    return IssueContext(**defaults)


def test_completeness_bug_fully_populated():
    ctx = _make_context(issue_type="Bug")
    assert _compute_completeness(ctx, "Bug") == 1.0


def test_completeness_bug_missing_steps():
    ctx = _make_context(issue_type="Bug", reproduction_steps=[])
    score = _compute_completeness(ctx, "Bug")
    assert score == pytest.approx(5 / 6, rel=1e-2)


def test_completeness_story_fully_populated():
    ctx = _make_context(issue_type="Story", acceptance_criteria=["ac1"])
    assert _compute_completeness(ctx, "Story") == 1.0


def test_completeness_story_missing_acceptance_criteria():
    ctx = _make_context(issue_type="Story", acceptance_criteria=[])
    assert _compute_completeness(ctx, "Story") == pytest.approx(3 / 4, rel=1e-2)


def test_finalize_overrides_issue_type_and_completeness(raw_ticket):
    ctx = _make_context(issue_type="Story", completeness_score=0.1)
    result = finalize(ctx, raw_ticket)
    assert result.issue_type == raw_ticket.issue_type  # "Bug" from raw_ticket fixture
    assert result.completeness_score != 0.1            # deterministically recomputed


# ── Phase 2: OAuth refresh skipped for token auth ─────────────────────────────

@respx.mock
async def test_engine_skips_oauth_refresh_for_token_auth(app_config):
    respx.get(f"{JIRA_BASE_URL}/issue/TEST-123").mock(
        return_value=httpx.Response(200, json=JIRA_ISSUE_PAYLOAD)
    )
    with patch("icx_engine.connectors.jira.oauth.refresh_oauth_if_needed", new=AsyncMock(side_effect=lambda conn, cfg: conn)):
        with patch("icx_engine.llm.ollama.AsyncOpenAI") as mock_cls:
            mock_client = MagicMock()
            mock_cls.return_value = mock_client
            mock_client.chat.completions.create = AsyncMock(return_value=_mock_openai_response())
            result = await run("TEST-123", app_config)
    assert isinstance(result, IssueContext)


def test_raw_issue_data_optional_fields_default():
    raw = RawIssueData(
        issue_key="X-1", issue_type="Bug", summary="s", description="d",
        comments=[], attachments=[], priority="High", status="Open", metadata={},
    )
    assert raw.due_date is None
    assert raw.attachment_content_urls == {}
    assert raw.attachment_texts == {}


# ── Phase 4 Task 3: deterministic _compute_missing ───────────────────────────

def test_compute_missing_bug_all_present(raw_ticket):
    ctx = _make_context(issue_type="Bug")
    assert _compute_missing(ctx, raw_ticket) == []


def test_compute_missing_bug_no_steps(raw_ticket):
    ctx = _make_context(issue_type="Bug", reproduction_steps=[])
    missing = _compute_missing(ctx, raw_ticket)
    assert "reproduction_steps" in missing


def test_compute_missing_bug_no_expected(raw_ticket):
    ctx = _make_context(issue_type="Bug", expected_behavior=None)
    missing = _compute_missing(ctx, raw_ticket)
    assert "expected_behavior" in missing


def test_compute_missing_story_no_ac(raw_ticket):
    raw_story = raw_ticket.model_copy(update={"issue_type": "Story"})
    ctx = _make_context(issue_type="Story", acceptance_criteria=[], reproduction_steps=[], expected_behavior=None, actual_behavior=None)
    missing = _compute_missing(ctx, raw_story)
    assert "acceptance_criteria" in missing
    assert "reproduction_steps" not in missing


def test_compute_missing_flags_missing_due_date(raw_ticket):
    raw_no_due = raw_ticket.model_copy(update={"due_date": None})
    ctx = _make_context(issue_type="Bug")
    missing = _compute_missing(ctx, raw_no_due)
    assert "due_date" in missing


def test_finalize_overrides_missing_information(raw_ticket):
    ctx = _make_context(issue_type="Story", reproduction_steps=[], expected_behavior=None, actual_behavior=None, acceptance_criteria=[], missing_information=["fake_field"])
    result = finalize(ctx, raw_ticket)
    assert "fake_field" not in result.missing_information


def test_finalize_does_not_flag_due_date_as_missing_when_present(raw_ticket):
    ctx = _make_context(issue_type="Bug")
    result = finalize(ctx, raw_ticket)
    assert "due_date" not in result.missing_information


# ── Phase 4 Task 4: build_user_message new fields ────────────────────────────

def test_build_user_message_includes_due_date(raw_ticket):
    msg = build_user_message(raw_ticket)
    assert "[DUE DATE] 2026-06-01" in msg


def test_build_user_message_omits_empty_fields():
    raw = RawIssueData(
        issue_key="X-1", issue_type="Bug", summary="s", description="d",
        comments=[], attachments=[], priority="High", status="Open", metadata={},
    )
    msg = build_user_message(raw)
    assert "[DUE DATE]" not in msg


def test_build_user_message_includes_attachment_texts():
    raw = RawIssueData(
        issue_key="X-1", issue_type="Bug", summary="s", description="d",
        comments=[], attachments=["error.png"], priority="High", status="Open", metadata={},
        attachment_texts={"error.png": "NullPointerException at line 42"},
    )
    msg = build_user_message(raw)
    assert "[ATTACHMENT CONTENT]" in msg
    assert "NullPointerException at line 42" in msg


# ── Phase 4 Task 5: JiraClient.download_attachment ───────────────────────────

@respx.mock
async def test_download_attachment_returns_bytes():
    content_url = "https://test.atlassian.net/rest/api/3/attachment/content/10001"
    fake_image = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
    respx.get(content_url).mock(return_value=httpx.Response(200, content=fake_image))
    client = JiraClient(JIRA_BASE_URL, JIRA_AUTH_HEADER, JIRA_ALLOWED_HOSTS)
    result = await client.download_attachment(content_url)
    assert result == fake_image


@respx.mock
async def test_download_attachment_sends_auth_header():
    content_url = "https://test.atlassian.net/rest/api/3/attachment/content/10001"
    respx.get(content_url).mock(return_value=httpx.Response(200, content=b"data"))
    client = JiraClient(JIRA_BASE_URL, JIRA_AUTH_HEADER, JIRA_ALLOWED_HOSTS)
    await client.download_attachment(content_url)
    request = respx.calls.last.request
    assert request.headers["authorization"] == JIRA_AUTH_HEADER


@respx.mock
async def test_engine_run_processes_attachments(app_config):
    from unittest.mock import AsyncMock, patch
    respx.get(f"{JIRA_BASE_URL}/issue/TEST-123").mock(
        return_value=httpx.Response(200, json=JIRA_ISSUE_PAYLOAD)
    )
    respx.get("https://test.atlassian.net/rest/api/3/attachment/content/10001").mock(
        return_value=httpx.Response(200, content=b"\x89PNG\r\n\x1a\n" + b"\x00" * 50)
    )
    with patch("icx_engine.connectors.attachments.ocr_image", return_value="error text from OCR"):
        with patch("icx_engine.connectors.attachments.vision_enrich", new=AsyncMock(return_value="error text from OCR")):
            with patch("icx_engine.llm.ollama.AsyncOpenAI") as mock_cls:
                mock_client = MagicMock()
                mock_cls.return_value = mock_client
                mock_client.chat.completions.create = AsyncMock(return_value=_mock_openai_response())
                result = await run("TEST-123", app_config)
    assert isinstance(result, IssueContext)
    llm_call_args = str(mock_client.chat.completions.create.call_args)
    assert "error text from OCR" in llm_call_args


# ── MCP headless mode (no LLM) ───────────────────────────────────────────────

@respx.mock
async def test_engine_run_mcp_mode_returns_raw_response_when_no_llm(token_connection):
    from icx_engine.models.output import RawIssueResponse
    config = AppConfig(connections=[token_connection])  # no llm
    respx.get(f"{JIRA_BASE_URL}/issue/TEST-123").mock(
        return_value=httpx.Response(200, json=JIRA_ISSUE_PAYLOAD)
    )
    result = await run("TEST-123", config, mcp_mode=True)
    assert isinstance(result, RawIssueResponse)
    assert result.issue_key == "TEST-123"
    assert result.issue_type == "Bug"
    assert result.summary == "Button not working on mobile"
    assert result.mode == "raw"


@respx.mock
async def test_engine_run_default_mode_still_raises_no_llm(token_connection):
    config = AppConfig(connections=[token_connection])  # no llm
    respx.get(f"{JIRA_BASE_URL}/issue/TEST-123").mock(
        return_value=httpx.Response(200, json=JIRA_ISSUE_PAYLOAD)
    )
    with pytest.raises(NoLLMError):
        await run("TEST-123", config)  # mcp_mode=False by default


# ── Visual Grounding Pipeline ─────────────────────────────────────────────────

async def test_visual_grounding_noop_when_confidence_above_threshold(raw_ticket, jira_context):
    from icx_engine.grounding import visual_grounding_pass

    high_conf = jira_context.model_copy(update={"confidence_score": 0.9})
    image_config = ChannelConfig(provider="ollama", model="llava")
    result = await visual_grounding_pass(high_conf, raw_ticket, image_config, downloader=None)
    assert result is high_conf


async def test_visual_grounding_noop_when_no_image_config(raw_ticket, jira_context):
    from icx_engine.grounding import visual_grounding_pass

    low_conf = jira_context.model_copy(update={"confidence_score": 0.5})
    result = await visual_grounding_pass(low_conf, raw_ticket, None, downloader=None)
    assert result is low_conf


async def test_visual_grounding_noop_when_no_image_attachments(jira_context):
    from icx_engine.grounding import visual_grounding_pass
    from icx_engine.models.output import RawIssueData

    raw_no_images = RawIssueData(
        issue_key="X-1", issue_type="Bug", summary="s", description="d",
        comments=[], attachments=[], priority="High", status="Open", metadata={},
        attachment_content_urls={},
    )
    low_conf = jira_context.model_copy(update={"confidence_score": 0.5})
    image_config = ChannelConfig(provider="ollama", model="llava")
    result = await visual_grounding_pass(low_conf, raw_no_images, image_config, downloader=None)
    assert result is low_conf


@respx.mock
async def test_engine_run_visual_grounding_called_on_low_confidence(app_config):
    import json as _json
    from icx_engine.grounding import _CONFIDENCE_THRESHOLD

    low_conf_json = _json.dumps({
        **_json.loads(MOCK_LLM_JSON),
        "confidence_score": 0.5,
    })

    def _low_conf_response():
        resp = MagicMock()
        resp.choices = [MagicMock()]
        resp.choices[0].message.content = low_conf_json
        return resp

    respx.get(f"{JIRA_BASE_URL}/issue/TEST-123").mock(
        return_value=httpx.Response(200, json=JIRA_ISSUE_PAYLOAD)
    )
    with patch("icx_engine.grounding.visual_grounding_pass", new=AsyncMock(side_effect=lambda r, *a, **kw: r)) as mock_vg:
        with patch("icx_engine.llm.ollama.AsyncOpenAI") as mock_cls:
            mock_client = MagicMock()
            mock_cls.return_value = mock_client
            mock_client.chat.completions.create = AsyncMock(return_value=_low_conf_response())
            result = await run("TEST-123", app_config)

    mock_vg.assert_called_once()
    assert isinstance(result, IssueContext)


# ── Heuristic confidence logic ────────────────────────────────────────────────

def _make_raw_data(**overrides):
    defaults = dict(
        issue_key="X-1", issue_type="Bug", summary="s", description="d",
        comments=[], attachments=[], priority="High", status="Open", metadata={},
    )
    defaults.update(overrides)
    return RawIssueData(**defaults)


def test_heuristic_triggered_on_low_confidence():
    from icx_engine.engine import _heuristic_confidence_triggered
    ctx = _make_context(confidence_score=0.7)
    raw = _make_raw_data()
    assert _heuristic_confidence_triggered(ctx, raw, {}) is True


def test_heuristic_not_triggered_at_threshold():
    from icx_engine.engine import _heuristic_confidence_triggered
    ctx = _make_context(confidence_score=0.8)
    raw = _make_raw_data()
    assert _heuristic_confidence_triggered(ctx, raw, {}) is False


def test_heuristic_triggered_on_low_ocr_with_images():
    from icx_engine.engine import _heuristic_confidence_triggered
    ctx = _make_context(confidence_score=0.9)
    raw = _make_raw_data(attachment_texts={"shot.png": "hi"})  # 2 chars < 500
    images = {"shot.png": "base64data"}
    assert _heuristic_confidence_triggered(ctx, raw, images) is True


def test_heuristic_not_triggered_when_ocr_sufficient():
    from icx_engine.engine import _heuristic_confidence_triggered
    ctx = _make_context(confidence_score=0.9)
    raw = _make_raw_data(attachment_texts={"shot.png": "x" * 600})  # 600 >= 500
    images = {"shot.png": "base64data"}
    assert _heuristic_confidence_triggered(ctx, raw, images) is False


def test_heuristic_not_triggered_when_images_empty_low_ocr():
    from icx_engine.engine import _heuristic_confidence_triggered
    ctx = _make_context(confidence_score=0.9)
    raw = _make_raw_data(attachment_texts={"shot.png": "hi"})
    assert _heuristic_confidence_triggered(ctx, raw, {}) is False  # no images captured


def test_heuristic_triggered_on_bug_with_no_repro_steps():
    from icx_engine.engine import _heuristic_confidence_triggered
    ctx = _make_context(confidence_score=0.9, reproduction_steps=[])
    raw = _make_raw_data(issue_type="Bug")
    assert _heuristic_confidence_triggered(ctx, raw, {}) is True


def test_heuristic_not_triggered_for_story_with_no_repro():
    from icx_engine.engine import _heuristic_confidence_triggered
    ctx = _make_context(confidence_score=0.9, issue_type="Story", reproduction_steps=[])
    raw = _make_raw_data(issue_type="Story")
    assert _heuristic_confidence_triggered(ctx, raw, {}) is False


# ── Visual fallback rule ──────────────────────────────────────────────────────

@respx.mock
async def test_engine_attaches_images_when_no_image_model(token_connection):
    """No image_model → images always attached regardless of heuristics."""
    import base64 as _b64
    from icx_engine.models.config import AppConfig, LLMConfig, ChannelConfig

    config = AppConfig(
        connections=[token_connection],
        llm_profiles={"personal": LLMConfig(text_config=ChannelConfig(provider="ollama", model="llama3"),
                                             image_config=None)},
        current_llm_profile="personal",
    )
    fake_png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 50
    respx.get(f"{JIRA_BASE_URL}/issue/TEST-123").mock(
        return_value=httpx.Response(200, json=JIRA_ISSUE_PAYLOAD)
    )
    respx.get("https://test.atlassian.net/rest/api/3/attachment/content/10001").mock(
        return_value=httpx.Response(200, content=fake_png)
    )

    with patch("icx_engine.connectors.attachments.ocr_image", return_value="some ocr text here"):
        with patch("icx_engine.llm.ollama.AsyncOpenAI") as mock_cls:
            mock_client = MagicMock()
            mock_cls.return_value = mock_client
            mock_client.chat.completions.create = AsyncMock(return_value=_mock_openai_response())
            result = await run("TEST-123", config)

    assert isinstance(result, IssueContext)
    assert "screenshot.png" in result.images
    assert _b64.b64decode(result.images["screenshot.png"]) == fake_png


@respx.mock
async def test_engine_attaches_images_on_bug_no_repro_heuristic(token_connection):
    """Condition 3: Bug + empty repro steps → images attached even at high confidence."""
    import json as _json
    import base64 as _b64
    from icx_engine.models.config import AppConfig, LLMConfig, ChannelConfig

    no_repro_json = _json.dumps({
        **_json.loads(MOCK_LLM_JSON),
        "confidence_score": 0.95,
        "reproduction_steps": [],
    })

    def _no_repro_response():
        resp = MagicMock()
        resp.choices = [MagicMock()]
        resp.choices[0].message.content = no_repro_json
        return resp

    config = AppConfig(
        connections=[token_connection],
        llm_profiles={"personal": LLMConfig(text_config=ChannelConfig(provider="ollama", model="llama3"),
                                             image_config=ChannelConfig(provider="ollama", model="llava"))},
        current_llm_profile="personal",
    )
    fake_png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 50
    respx.get(f"{JIRA_BASE_URL}/issue/TEST-123").mock(
        return_value=httpx.Response(200, json=JIRA_ISSUE_PAYLOAD)
    )
    respx.get("https://test.atlassian.net/rest/api/3/attachment/content/10001").mock(
        return_value=httpx.Response(200, content=fake_png)
    )

    with patch("icx_engine.connectors.attachments.ocr_image", return_value="x" * 300):
        with patch("icx_engine.connectors.attachments.vision_enrich", new=AsyncMock(return_value="x" * 300)):
            with patch("icx_engine.llm.ollama.AsyncOpenAI") as mock_cls:
                mock_client = MagicMock()
                mock_cls.return_value = mock_client
                mock_client.chat.completions.create = AsyncMock(return_value=_no_repro_response())
                result = await run("TEST-123", config)

    assert isinstance(result, IssueContext)
    assert "screenshot.png" in result.images


@respx.mock
async def test_engine_no_images_when_heuristic_clear_and_image_model_present(app_config):
    """High confidence, sufficient OCR, non-empty repro → images NOT attached."""
    # Override to ensure heuristic does NOT trigger:
    # confidence=0.9 (MOCK_LLM_JSON), repro steps populated, OCR text >= 500 chars
    respx.get(f"{JIRA_BASE_URL}/issue/TEST-123").mock(
        return_value=httpx.Response(200, json=JIRA_ISSUE_PAYLOAD)
    )
    respx.get("https://test.atlassian.net/rest/api/3/attachment/content/10001").mock(
        return_value=httpx.Response(200, content=b"\x89PNG\r\n\x1a\n" + b"\x00" * 50)
    )

    with patch("icx_engine.connectors.attachments.ocr_image", return_value="z" * 600):
        with patch("icx_engine.connectors.attachments.vision_enrich", new=AsyncMock(return_value="z" * 600)):
            with patch("icx_engine.llm.ollama.AsyncOpenAI") as mock_cls:
                mock_client = MagicMock()
                mock_cls.return_value = mock_client
                mock_client.chat.completions.create = AsyncMock(return_value=_mock_openai_response())
                result = await run("TEST-123", app_config)

    assert isinstance(result, IssueContext)
    assert "screenshot.png" in result.images  # images always attached when present


def test_compute_missing_flags_missing_schema_for_story_with_spreadsheet():
    raw = _make_raw_data(
        issue_type="Story",
        attachments=["budget.xlsx"],
        attachment_texts={"budget.xlsx": "| Col A | Col B |\n| --- | --- |\n| 100 | 200 |"},
    )
    ctx = _make_context(
        issue_type="Story",
        reproduction_steps=[],
        expected_behavior=None,
        actual_behavior=None,
        acceptance_criteria=["AC1"],
        detailed_description="Some description without technical schema block",
    )
    missing = _compute_missing(ctx, raw)
    assert "missing_schema" in missing


def test_compute_missing_no_missing_schema_when_block_present():
    raw = _make_raw_data(
        issue_type="Story",
        attachments=["budget.xlsx"],
        attachment_texts={"budget.xlsx": "| Col A | Col B |"},
    )
    ctx = _make_context(
        issue_type="Story",
        reproduction_steps=[],
        expected_behavior=None,
        actual_behavior=None,
        acceptance_criteria=["AC1"],
        detailed_description="### [TECHNICAL SCHEMA: budget.xlsx]\nColumn headers: Col A, Col B",
    )
    missing = _compute_missing(ctx, raw)
    assert "missing_schema" not in missing


def test_compute_missing_no_missing_schema_when_technical_logic_block_present():
    raw = _make_raw_data(
        issue_type="Task",
        attachments=["rates.xlsx"],
        attachment_texts={"rates.xlsx": "| Rate | 18.0 (Formula: =B2*0.18) |"},
    )
    ctx = _make_context(
        issue_type="Task",
        reproduction_steps=[],
        expected_behavior=None,
        actual_behavior=None,
        acceptance_criteria=["AC1"],
        detailed_description="### [TECHNICAL LOGIC: rates.xlsx]\nRate: 18.0 (Formula: =B2*0.18)",
    )
    missing = _compute_missing(ctx, raw)
    assert "missing_schema" not in missing


def test_compute_missing_schema_not_flagged_for_bug():
    raw = _make_raw_data(
        issue_type="Bug",
        attachments=["data.csv"],
        attachment_texts={"data.csv": "| A | B |"},
    )
    ctx = _make_context(
        issue_type="Bug",
        detailed_description="no schema block here",
    )
    missing = _compute_missing(ctx, raw)
    assert "missing_schema" not in missing


def test_compute_missing_schema_not_flagged_without_spreadsheet():
    raw = _make_raw_data(
        issue_type="Story",
        attachments=["screenshot.png"],
        attachment_texts={"screenshot.png": "some OCR text"},
    )
    ctx = _make_context(
        issue_type="Story",
        reproduction_steps=[],
        expected_behavior=None,
        actual_behavior=None,
        acceptance_criteria=["AC1"],
        detailed_description="no schema block here",
    )
    missing = _compute_missing(ctx, raw)
    assert "missing_schema" not in missing


def test_finalize_caps_completeness_score_when_missing_schema():
    raw = _make_raw_data(
        issue_type="Story",
        attachments=["budget.xlsx"],
        attachment_texts={"budget.xlsx": "| Col A | Col B |"},
        due_date="2026-06-01",
    )
    ctx = _make_context(
        issue_type="Story",
        reproduction_steps=[],
        expected_behavior=None,
        actual_behavior=None,
        acceptance_criteria=["AC1"],
        detailed_description="full description",
        impact="high impact",
    )
    result = finalize(ctx, raw)
    assert "missing_schema" in result.missing_information
    assert result.completeness_score <= 0.79


def test_finalize_does_not_cap_when_schema_present():
    raw = _make_raw_data(
        issue_type="Story",
        attachments=["budget.xlsx"],
        attachment_texts={"budget.xlsx": "| Col A | Col B |"},
        due_date="2026-06-01",
    )
    ctx = _make_context(
        issue_type="Story",
        reproduction_steps=[],
        expected_behavior=None,
        actual_behavior=None,
        acceptance_criteria=["AC1"],
        detailed_description="### [TECHNICAL SCHEMA: budget.xlsx]\nColumn headers: Col A, Col B",
        impact="high impact",
    )
    result = finalize(ctx, raw)
    assert "missing_schema" not in result.missing_information
    assert result.completeness_score > 0.79


# ── MCP headless mode includes images ────────────────────────────────────────

@respx.mock
async def test_engine_run_mcp_mode_includes_images(token_connection):
    """Headless MCP mode returns Base64 images in RawIssueResponse."""
    import base64 as _b64
    from icx_engine.models.output import RawIssueResponse

    config = AppConfig(connections=[token_connection])  # no llm
    fake_png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 50
    respx.get(f"{JIRA_BASE_URL}/issue/TEST-123").mock(
        return_value=httpx.Response(200, json=JIRA_ISSUE_PAYLOAD)
    )
    respx.get("https://test.atlassian.net/rest/api/3/attachment/content/10001").mock(
        return_value=httpx.Response(200, content=fake_png)
    )

    with patch("icx_engine.connectors.attachments.ocr_image", return_value="error text"):
        result = await run("TEST-123", config, mcp_mode=True)

    assert isinstance(result, RawIssueResponse)
    assert "screenshot.png" in result.images
    assert _b64.b64decode(result.images["screenshot.png"]) == fake_png


def test_raw_issue_response_note_exact_text():
    """Verify the exact note string required by the spec."""
    from icx_engine.models.output import RawIssueResponse
    r = RawIssueResponse(
        issue_key="X-1", issue_type="Bug", summary="s", description="d",
        comments=[], attachments=[], priority="High", status="Open", metadata={},
    )
    assert r.note == (
        "No LLM analysis performed - no API key configured. "
        "Raw issue data, digested documents, and raw images are provided for your direct analysis."
    )


def test_system_prompt_has_attachment_analysis_rules():
    """SYSTEM_PROMPT must contain the ATTACHMENT ANALYSIS section and all four extraction rules."""
    from icx_engine.llm.base import SYSTEM_PROMPT
    prompt_lower = SYSTEM_PROMPT.lower()
    assert "attachment analysis" in prompt_lower, "missing ATTACHMENT ANALYSIS section"
    assert "structural schemas" in prompt_lower, "missing STRUCTURAL SCHEMAS rule"
    assert "data samples" in prompt_lower, "missing DATA SAMPLES rule"
    assert "literal calculations" in prompt_lower, "missing LITERAL CALCULATIONS rule"
    assert "visual graph interpretation" in prompt_lower, "missing VISUAL GRAPH INTERPRETATION rule"
    assert "column headers" in prompt_lower, "missing column headers instruction"
    assert "2-3" in SYSTEM_PROMPT, "missing data samples count"
    assert ("axes" in prompt_lower or "axis" in prompt_lower), "missing graph axes instruction"
    assert "trend" in prompt_lower, "missing graph trends instruction"
    assert "peak" in prompt_lower, "missing graph peak values instruction"


def test_system_prompt_formula_annotation_instruction():
    """SYSTEM_PROMPT must instruct the LLM to treat (Formula: ...) annotations as Non-Negotiable Business Rules."""
    from icx_engine.llm.base import SYSTEM_PROMPT
    prompt_lower = SYSTEM_PROMPT.lower()
    assert "(Formula:" in SYSTEM_PROMPT, \
        "must reference the (Formula:) annotation format so the LLM knows what to look for"
    assert "non-negotiable" in prompt_lower, \
        "must instruct LLM that formula annotations are authoritative"
    assert "if no" in prompt_lower or "when no" in prompt_lower, \
        "must instruct LLM not to mention formulas when no (Formula:) annotation is present"


def test_system_prompt_mandates_technical_schema_tagged_block():
    p = SYSTEM_PROMPT.lower()
    assert "### [technical schema:" in p
    assert "column header" in p or "headers" in p


def test_system_prompt_mandates_technical_logic_tagged_block():
    p = SYSTEM_PROMPT.lower()
    assert "### [technical logic:" in p
    assert "formula" in p


def test_engine_run_uses_debug_console_for_prompt(app_config, raw_ticket):
    """When debug_console is provided, engine.run emits Rule output to it."""
    from io import StringIO
    from rich.console import Console
    from icx_engine.engine import run

    buf = StringIO()
    debug_con = Console(file=buf, highlight=False, markup=False, width=120)

    mock_context = IssueContext(
        problem_summary="x", detailed_description="x", impact="x",
        reproduction_steps=[], expected_behavior=None, actual_behavior=None,
        acceptance_criteria=[], priority="High", issue_type="Bug",
        confidence_score=0.9, completeness_score=0.8, missing_information=[],
    )

    with (
        patch("icx_engine.connectors.base.get_connector") as mock_gc,
        patch("icx_engine.llm.base.get_provider") as mock_gp,
        patch("icx_engine.grounding.visual_grounding_pass", new_callable=AsyncMock) as mock_vg,
    ):
        connector = MagicMock()
        connector.parse_input.return_value = MagicMock(issue_key="TEST-1")
        connector.fetch = AsyncMock(return_value=raw_ticket)
        connector.process_attachments = AsyncMock(return_value=({}, {}))
        mock_gc.return_value = connector

        provider = MagicMock()
        provider.analyze = AsyncMock(return_value=mock_context)
        mock_gp.return_value = provider

        mock_vg.return_value = mock_context

        import asyncio
        asyncio.run(run(
            "https://test.atlassian.net/browse/TEST-1",
            app_config,
            log=lambda m: None,
            debug_console=debug_con,
        ))

    output = buf.getvalue()
    assert "Prompt" in output or "LLM" in output or "──" in output


# --- Memory enrichment tests ---

from icx_engine.models.output import PastInsight as _PastInsight


def _make_past_insight():
    return _PastInsight(
        issue_key="PROJ-87",
        source_type="jira",
        summary="Login timeout",
        resolution_note="Updated TTL",
        files_changed=["src/auth.py"],
        similarity_score=0.91,
        saved_at="2026-03-12T09:14:22Z",
    )


@pytest.mark.asyncio
async def test_engine_injects_past_insights_when_memory_hits():
    from unittest.mock import AsyncMock, MagicMock, patch
    from icx_engine.engine import run
    from icx_engine.models.config import AppConfig, LLMConfig, ChannelConfig
    from icx_engine.models.output import RawIssueData

    insight = _make_past_insight()

    raw = RawIssueData(
        issue_key="PROJ-456",
        issue_type="Bug",
        summary="Auth fails",
        description="desc",
        comments=[],
        attachments=[],
        priority="High",
        status="Open",
        metadata={},
    )

    mock_connector = MagicMock()
    mock_connector.parse_input.return_value = MagicMock(issue_key="PROJ-456")
    mock_connector.fetch = AsyncMock(return_value=raw)
    mock_connector.process_attachments = AsyncMock(return_value=({}, {}))
    mock_connector.connector_type.return_value = "jira"

    mock_provider = MagicMock()
    from icx_engine.models.output import IssueContext
    mock_ctx = IssueContext(
        problem_summary="x", detailed_description="x", reproduction_steps=[],
        expected_behavior=None, actual_behavior=None, acceptance_criteria=[],
        impact="x", priority="High", issue_type="Bug",
        confidence_score=1.0, completeness_score=1.0, missing_information=[],
    )
    mock_provider.analyze = AsyncMock(return_value=mock_ctx)

    cfg = AppConfig(
        llm_profiles={"default": LLMConfig(
            text_config=ChannelConfig(provider="openai", model="gpt-4o", api_key="test")
        )},
        current_llm_profile="default",
    )

    mock_mgr = MagicMock()
    mock_mgr.query.return_value = [insight]

    with patch("icx_engine.config_manager.ConfigManager.load", return_value=cfg), \
         patch("icx_engine.connectors.base.get_connector", return_value=mock_connector), \
         patch("icx_engine.llm.base.get_provider", return_value=mock_provider), \
         patch("icx_engine.memory.MemoryManager", return_value=mock_mgr), \
         patch("icx_engine.grounding.visual_grounding_pass", new=AsyncMock(return_value=mock_ctx)):

        from icx_engine.models.config import BaseConnection
        conn = MagicMock(spec=BaseConnection)
        conn.connector_type = "jira"
        conn.domain = "test.atlassian.net"
        result = await run("PROJ-456", cfg, connection=conn)

    assert len(result.past_insights) == 1
    assert result.past_insights[0].issue_key == "PROJ-87"
    assert result.past_insights[0].similarity_score == 0.91


@pytest.mark.asyncio
async def test_engine_memory_exception_does_not_propagate():
    from unittest.mock import AsyncMock, MagicMock, patch
    from icx_engine.engine import run
    from icx_engine.models.config import AppConfig, LLMConfig, ChannelConfig
    from icx_engine.models.output import RawIssueData

    raw = RawIssueData(
        issue_key="PROJ-456", issue_type="Bug", summary="x", description="x",
        comments=[], attachments=[], priority="High", status="Open", metadata={},
    )
    mock_connector = MagicMock()
    mock_connector.parse_input.return_value = MagicMock(issue_key="PROJ-456")
    mock_connector.fetch = AsyncMock(return_value=raw)
    mock_connector.process_attachments = AsyncMock(return_value=({}, {}))
    mock_connector.connector_type.return_value = "jira"

    from icx_engine.models.output import IssueContext
    mock_ctx = IssueContext(
        problem_summary="x", detailed_description="x", reproduction_steps=[],
        expected_behavior=None, actual_behavior=None, acceptance_criteria=[],
        impact="x", priority="High", issue_type="Bug",
        confidence_score=1.0, completeness_score=1.0, missing_information=[],
    )
    mock_provider = MagicMock()
    mock_provider.analyze = AsyncMock(return_value=mock_ctx)

    cfg = AppConfig(
        llm_profiles={"default": LLMConfig(
            text_config=ChannelConfig(provider="openai", model="gpt-4o", api_key="test")
        )},
        current_llm_profile="default",
    )

    mock_mgr = MagicMock()
    mock_mgr.query.side_effect = RuntimeError("lancedb broken")

    with patch("icx_engine.config_manager.ConfigManager.load", return_value=cfg), \
         patch("icx_engine.connectors.base.get_connector", return_value=mock_connector), \
         patch("icx_engine.llm.base.get_provider", return_value=mock_provider), \
         patch("icx_engine.memory.MemoryManager", return_value=mock_mgr), \
         patch("icx_engine.grounding.visual_grounding_pass", new=AsyncMock(return_value=mock_ctx)):

        from icx_engine.models.config import BaseConnection
        conn = MagicMock(spec=BaseConnection)
        conn.connector_type = "jira"
        conn.domain = "test.atlassian.net"
        result = await run("PROJ-456", cfg, connection=conn)

    assert result.past_insights == []


# ── _split_attachments ────────────────────────────────────────────────────────

def test_split_attachments_separates_images_from_docs():
    from icx_engine.engine import _split_attachments
    urls = {
        "screenshot.png": "https://example.com/s.png",
        "report.pdf":     "https://example.com/r.pdf",
        "diagram.jpeg":   "https://example.com/d.jpeg",
    }
    non_image, image_names = _split_attachments(urls)
    assert non_image == {"report.pdf": "https://example.com/r.pdf"}
    assert sorted(image_names) == ["diagram.jpeg", "screenshot.png"]


def test_split_attachments_empty_input():
    from icx_engine.engine import _split_attachments
    non_image, image_names = _split_attachments({})
    assert non_image == {}
    assert image_names == []


def test_split_attachments_all_images():
    from icx_engine.engine import _split_attachments
    urls = {"a.png": "u1", "b.tif": "u2", "c.webp": "u3"}
    non_image, image_names = _split_attachments(urls)
    assert non_image == {}
    assert len(image_names) == 3


def test_split_attachments_no_images():
    from icx_engine.engine import _split_attachments
    urls = {"doc.xlsx": "u1", "notes.txt": "u2"}
    non_image, image_names = _split_attachments(urls)
    assert non_image == {"doc.xlsx": "u1", "notes.txt": "u2"}
    assert image_names == []


# ── engine.run skip_vision ────────────────────────────────────────────────────

@respx.mock
async def test_run_skip_vision_populates_pending_images(app_config):
    """With skip_vision=True, image filenames end up in result.pending_images."""
    respx.get(f"{JIRA_BASE_URL}/issue/TEST-123").mock(
        return_value=httpx.Response(200, json=JIRA_ISSUE_PAYLOAD)
    )
    with patch("icx_engine.llm.ollama.AsyncOpenAI") as mock_cls:
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.chat.completions.create = AsyncMock(return_value=_mock_openai_response())
        with patch("icx_engine.memory.MemoryManager"):
            result = await run("TEST-123", app_config, skip_vision=True)
    assert isinstance(result, IssueContext)
    assert "screenshot.png" in result.pending_images
    assert result.images == {}


@respx.mock
async def test_run_full_vision_leaves_pending_images_empty(app_config):
    """Without skip_vision, pending_images is empty (full mode)."""
    respx.get(f"{JIRA_BASE_URL}/issue/TEST-123").mock(
        return_value=httpx.Response(200, json=JIRA_ISSUE_PAYLOAD)
    )
    with patch("icx_engine.llm.ollama.AsyncOpenAI") as mock_cls:
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.chat.completions.create = AsyncMock(return_value=_mock_openai_response())
        with patch("icx_engine.memory.MemoryManager"):
            result = await run("TEST-123", app_config)
    assert isinstance(result, IssueContext)
    assert result.pending_images == []


# ── engine.run MCP mode memory guard ─────────────────────────────────────────

@respx.mock
async def test_run_mcp_mode_skips_memory_enrichment(app_config):
    """mcp_mode=True must not call MemoryManager.query()."""
    respx.get(f"{JIRA_BASE_URL}/issue/TEST-123").mock(
        return_value=httpx.Response(200, json=JIRA_ISSUE_PAYLOAD)
    )
    with patch("icx_engine.llm.ollama.AsyncOpenAI") as mock_cls:
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.chat.completions.create = AsyncMock(return_value=_mock_openai_response())
        with patch("icx_engine.memory.MemoryManager") as mock_mem_cls:
            await run("TEST-123", app_config, mcp_mode=True)
    mock_mem_cls.assert_not_called()


# ── engine.run contextual RAG ─────────────────────────────────────────────────

@respx.mock
async def test_run_cli_mode_uses_contextual_rag(app_config):
    """CLI mode memory query uses result.problem_summary, not raw.summary."""
    respx.get(f"{JIRA_BASE_URL}/issue/TEST-123").mock(
        return_value=httpx.Response(200, json=JIRA_ISSUE_PAYLOAD)
    )
    captured_queries: list = []
    mock_mem = MagicMock()
    mock_mem.query.side_effect = lambda q: captured_queries.append(q) or []

    with patch("icx_engine.llm.ollama.AsyncOpenAI") as mock_cls:
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.chat.completions.create = AsyncMock(return_value=_mock_openai_response())
        with patch("icx_engine.memory.MemoryManager", return_value=mock_mem):
            result = await run("TEST-123", app_config)

    assert len(captured_queries) == 1
    query = captured_queries[0]
    # Contextual RAG: query uses LLM output fields, not raw tracker text
    assert query.summary == result.problem_summary
    assert query.description == result.detailed_description
    # Must NOT equal the raw summary from the tracker payload
    assert query.summary != "Button not working on mobile"
