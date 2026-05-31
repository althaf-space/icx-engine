from __future__ import annotations
import base64
import json
from typing import Callable, Any

from icx_engine.models.config import ChannelConfig
from icx_engine.models.output import RawIssueData, IssueContext
from icx_engine.connectors.attachments import _mime_type as _media_type, _DEFAULT_BASE_URLS

_CONFIDENCE_THRESHOLD = 0.8

_VERIFY_USER_TEMPLATE = """\
You previously analyzed a Jira issue and produced this structured JSON:

{initial_json}

The original Jira description was:
{description}

Examine the image(s) attached above for any contradictions with the JSON analysis.

Visual evidence takes priority over text. Correct any contradictions found in the JSON.

Examples of contradictions to detect:
- Text says "Error 500" but screenshot shows "Error 404"
- Text says "Login button unresponsive" but screenshot shows a different UI element
- Text describes desktop behavior but screenshot shows a mobile interface

RULES:
1. Visual evidence takes priority over text when a conflict exists.
2. If no contradictions found, return the original JSON unchanged.
3. Correct only the fields where visual evidence is clearer or conflicting.
4. Return ONLY the JSON object - same schema as above, no prose, no markdown fences.
"""


async def visual_grounding_pass(
    initial: IssueContext,
    raw: RawIssueData,
    image_config: ChannelConfig | None,
    downloader: Any,
    log: Callable[[str], None] | None = None,
) -> IssueContext:
    if initial.confidence_score >= _CONFIDENCE_THRESHOLD:
        return initial
    if image_config is None:
        return initial

    from icx_engine.connectors.attachments import _is_image
    image_urls = {
        fname: url
        for fname, url in raw.attachment_content_urls.items()
        if _is_image(fname)
    }
    if not image_urls:
        return initial

    if log:
        log(
            f"  confidence_score={initial.confidence_score:.2f} < {_CONFIDENCE_THRESHOLD} "
            f"- running visual grounding verification..."
        )

    import asyncio
    download_tasks = [
        _safe_download(fname, url, downloader)
        for fname, url in image_urls.items()
    ]
    downloaded = await asyncio.gather(*download_tasks, return_exceptions=True)

    image_data: dict[str, bytes] = {}
    for item in downloaded:
        if isinstance(item, BaseException):
            if log:
                log(f"  attachment download error: {item}")
            continue
        fname, img_bytes = item
        if img_bytes:
            image_data[fname] = img_bytes
        elif log:
            log(f"  attachment '{fname}' download failed - skipping")

    if not image_data:
        return initial

    try:
        if image_config.provider == "anthropic":
            corrected = await _verify_anthropic(initial, raw, image_config, image_data)
        elif image_config.provider == "google":
            corrected = await _verify_google(initial, raw, image_config, image_data)
        else:
            corrected = await _verify_openai_compat(initial, raw, image_config, image_data)
        if log:
            log(
                f"  visual grounding complete - "
                f"confidence updated: {initial.confidence_score:.2f} to {corrected.confidence_score:.2f}"
            )
        return corrected
    except Exception as exc:
        if log:
            log(f"  visual grounding failed ({exc}) - returning original analysis")
        return initial


async def _safe_download(fname: str, url: str, downloader: Any) -> tuple[str, bytes]:
    try:
        data = await downloader.download_attachment(url)
        return fname, data
    except Exception:
        return fname, b""


def _build_verify_prompt(initial: IssueContext, raw: RawIssueData) -> str:
    return _VERIFY_USER_TEMPLATE.format(
        initial_json=initial.model_dump_json(indent=2),
        description=raw.description or "(no description provided)",
    )


async def _verify_anthropic(
    initial: IssueContext,
    raw: RawIssueData,
    image_config: ChannelConfig,
    image_data: dict[str, bytes],
) -> IssueContext:
    from anthropic import AsyncAnthropic
    from icx_engine.llm.base import SYSTEM_PROMPT, finalize, _strip_json_fencing

    client = AsyncAnthropic(api_key=image_config.api_key)

    content: list = []
    for fname, img_bytes in image_data.items():
        b64 = base64.b64encode(img_bytes).decode()
        content.append({"type": "text", "text": f"Image: {fname}"})
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": _media_type(fname), "data": b64},
        })
    content.append({"type": "text", "text": _build_verify_prompt(initial, raw)})

    response = await client.messages.create(
        model=image_config.model,
        max_tokens=4096,
        timeout=45.0,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": content}],
    )
    text = response.content[0].text.strip() if response.content else ""
    try:
        corrected = IssueContext.model_validate(json.loads(_strip_json_fencing(text)))
        return finalize(corrected, raw)
    except Exception:
        return initial


async def _verify_openai_compat(
    initial: IssueContext,
    raw: RawIssueData,
    image_config: ChannelConfig,
    image_data: dict[str, bytes],
) -> IssueContext:
    from openai import AsyncOpenAI
    from icx_engine.llm.base import SYSTEM_PROMPT, finalize, _strip_json_fencing

    kwargs: dict = {"api_key": image_config.api_key or "ollama"}
    base_url = image_config.base_url or _DEFAULT_BASE_URLS.get(image_config.provider)
    if base_url:
        kwargs["base_url"] = base_url
    client = AsyncOpenAI(**kwargs)

    content: list = []
    for fname, img_bytes in image_data.items():
        b64 = base64.b64encode(img_bytes).decode()
        content.append({"type": "text", "text": f"Image: {fname}"})
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:{_media_type(fname)};base64,{b64}"},
        })
    content.append({"type": "text", "text": _build_verify_prompt(initial, raw)})

    response = await client.chat.completions.create(
        model=image_config.model,
        max_tokens=4096,
        timeout=45.0,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": content},
        ],
    )
    text = (response.choices[0].message.content or "").strip()
    try:
        corrected = IssueContext.model_validate(json.loads(_strip_json_fencing(text)))
        return finalize(corrected, raw)
    except Exception:
        return initial


async def _verify_google(
    initial: IssueContext,
    raw: RawIssueData,
    image_config: ChannelConfig,
    image_data: dict[str, bytes],
) -> IssueContext:
    from google import genai
    from google.genai import types
    from icx_engine.llm.base import SYSTEM_PROMPT, finalize, _strip_json_fencing

    client = genai.Client(api_key=image_config.api_key)
    parts = []
    for fname, img_bytes in image_data.items():
        parts.append(types.Part.from_text(text=f"Image: {fname}"))
        parts.append(types.Part.from_bytes(data=img_bytes, mime_type=_media_type(fname)))
    parts.append(types.Part.from_text(text=_build_verify_prompt(initial, raw)))

    cfg = types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        temperature=0.0,
    )
    response = await client.aio.models.generate_content(
        model=image_config.model,
        contents=parts,
        config=cfg,
    )
    text = (response.text or "").strip()
    try:
        corrected = IssueContext.model_validate(json.loads(_strip_json_fencing(text)))
        return finalize(corrected, raw)
    except Exception:
        return initial
