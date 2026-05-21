from __future__ import annotations
import asyncio
import base64
import csv
import io
import shutil
from pathlib import Path
from typing import Callable

from icx_engine.models.config import LLMConfig, ChannelConfig
from icx_engine.models.output import RawIssueData

# -- Extension sets ------------------------------------------------------------

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".tif"}
DOCUMENT_EXTENSIONS = {".csv", ".xlsx", ".xls", ".pdf", ".docx", ".txt"}

# -- Limits --------------------------------------------------------------------

_MAX_CSV_ROWS = 50
_EXTRACT_LIMIT = 100_000       # max chars extracted from PDF/DOCX/TXT before summarization
_SUMMARIZE_THRESHOLD = 20_000  # content longer than this triggers summarization (with LLM) or truncation
_TRUNCATION_NOTE = "\n\n[Content truncated. Request more data if required.]"

_MIME_TYPES: dict[str, str] = {
    ".png":  "image/png",
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif":  "image/gif",
    ".webp": "image/webp",
    ".bmp":  "image/bmp",
    ".tiff": "image/tiff",
    ".tif":  "image/tiff",
}


def _mime_type(filename: str) -> str:
    return _MIME_TYPES.get(Path(filename).suffix.lower(), "image/png")


_DEFAULT_BASE_URLS = {
    "nim": "https://integrate.api.nvidia.com/v1",
    "ollama": "http://localhost:11434/v1",
}


def _is_image(filename: str) -> bool:
    return Path(filename).suffix.lower() in IMAGE_EXTENSIONS


def _is_document(filename: str) -> bool:
    return Path(filename).suffix.lower() in DOCUMENT_EXTENSIONS


def _tesseract_available() -> bool:
    return shutil.which("tesseract") is not None


# -- OCR -----------------------------------------------------------------------

def ocr_image(image_bytes: bytes) -> str:
    """Extract text from image bytes using Pytesseract. Returns '' on any failure."""
    try:
        from PIL import Image
        import pytesseract
    except ImportError:
        return ""
    try:
        img = Image.open(io.BytesIO(image_bytes))
        return pytesseract.image_to_string(img).strip()
    except Exception:
        return ""


# -- Vision enrichment ---------------------------------------------------------

_VISION_PROMPT = (
    "The following text was extracted via OCR from the attached screenshot:\n\n"
    "{ocr_text}\n\n"
    "Please provide a complete extraction of ALL visible data:\n\n"
    "TEXT: Extract all error messages, stack traces, UI labels, and code snippets literally.\n\n"
    "GRAPHS/CHARTS: If the image contains a graph or chart, you MUST identify:\n"
    "  Axes: Labels and units for both X and Y axes.\n"
    "  Trends: Describe significant patterns (e.g., 'exponential growth', 'cyclic spikes', "
    "'latency plateau').\n"
    "  Values: List peak, minimum, and average data points visible.\n\n"
    "CORRECTION: Correct any OCR errors from the provided text. "
    "Return only the extracted information, nothing else."
)


async def _vision_enrich_anthropic(config: ChannelConfig, image_bytes: bytes, ocr_text: str, fname: str = "") -> str:
    from anthropic import AsyncAnthropic
    client = AsyncAnthropic(api_key=config.api_key)
    b64 = base64.b64encode(image_bytes).decode()
    response = await client.messages.create(
        model=config.model,
        max_tokens=512,
        timeout=90.0,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": _mime_type(fname), "data": b64},
                },
                {
                    "type": "text",
                    "text": _VISION_PROMPT.format(ocr_text=ocr_text or "(no OCR output)"),
                },
            ],
        }],
    )
    return response.content[0].text.strip() if response.content else ocr_text


async def _vision_enrich_openai_compat(config: ChannelConfig, image_bytes: bytes, ocr_text: str, fname: str = "") -> str:
    """OpenAI-compatible vision call - works for openai, nim, and ollama providers."""
    from openai import AsyncOpenAI
    kwargs: dict = {"api_key": config.api_key or "ollama"}
    base_url = config.base_url or _DEFAULT_BASE_URLS.get(config.provider)
    if base_url:
        kwargs["base_url"] = base_url
    client = AsyncOpenAI(**kwargs)
    b64 = base64.b64encode(image_bytes).decode()
    response = await client.chat.completions.create(
        model=config.model,
        max_tokens=512,
        timeout=90.0,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{_mime_type(fname)};base64,{b64}"},
                },
                {
                    "type": "text",
                    "text": _VISION_PROMPT.format(ocr_text=ocr_text or "(no OCR output)"),
                },
            ],
        }],
    )
    return (response.choices[0].message.content or ocr_text).strip()


async def _vision_enrich_google(config: ChannelConfig, image_bytes: bytes, ocr_text: str, fname: str = "") -> str:
    """Google Gemini vision enrichment via google-genai SDK."""
    from google import genai
    from google.genai import types
    client = genai.Client(api_key=config.api_key)
    contents = [
        types.Part.from_bytes(data=image_bytes, mime_type=_mime_type(fname)),
        types.Part.from_text(text=_VISION_PROMPT.format(ocr_text=ocr_text or "(no OCR output)")),
    ]
    response = await asyncio.wait_for(
        client.aio.models.generate_content(
            model=config.model,
            contents=contents,
        ),
        timeout=90.0,
    )
    return (response.text or ocr_text).strip()


async def vision_enrich(config: ChannelConfig, image_bytes: bytes, ocr_text: str, fname: str = "") -> str:
    """
    Refine OCR text with the configured image channel.

    Raises ContextBuildError if the selected model does not support image input.
    """
    try:
        if config.provider == "anthropic":
            return await _vision_enrich_anthropic(config, image_bytes, ocr_text, fname)
        if config.provider == "google":
            return await _vision_enrich_google(config, image_bytes, ocr_text, fname)
        return await _vision_enrich_openai_compat(config, image_bytes, ocr_text, fname)
    except Exception as exc:
        from icx_engine.exceptions import ContextBuildError
        raise ContextBuildError(
            f"Selected model does not support image input: "
            f"image_model='{config.model}' (provider='{config.provider}'). "
            f"Configure a vision-capable model via `icx model --add`.",
            raw_output=str(exc),
        ) from exc


# -- Universal Attachment Engine (UAE) - document converters -------------------

def _rows_to_markdown(rows: list, max_rows: int = _MAX_CSV_ROWS) -> str:
    """Convert a list of row sequences to a Markdown table, capped at max_rows data rows."""
    if not rows:
        return ""
    header = [str(c) for c in rows[0]]
    data_rows = rows[1:]
    truncated = len(data_rows) > max_rows
    display = data_rows[:max_rows]

    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join("---" for _ in header) + " |",
    ]
    for row in display:
        cells = [str(c) if c is not None else "" for c in row]
        # Align cell count to header length
        cells = cells[:len(header)] + [""] * max(0, len(header) - len(cells))
        lines.append("| " + " | ".join(cells) + " |")

    result = "\n".join(lines)
    if truncated:
        total = len(data_rows)
        result += f"\n\n[Content truncated - showing first {max_rows} of {total} rows. Request more data if required.]"
    return result


def _convert_csv(data: bytes) -> str:
    text = data.decode("utf-8-sig", errors="replace")
    rows = list(csv.reader(io.StringIO(text)))
    return _rows_to_markdown(rows)


def _convert_xlsx(data: bytes) -> str:
    try:
        import openpyxl
    except ImportError:
        return "[Excel processing unavailable - install openpyxl]"

    _FORMULA_ANNOTATE_ROWS = 4  # header (index 0) + first 3 data rows (indices 1-3)

    # Pass 1: extract computed values, then close immediately to free memory.
    val_data: dict[str, list] = {}
    wb_val = openpyxl.load_workbook(io.BytesIO(data), data_only=True)
    try:
        for name in wb_val.sheetnames:
            val_data[name] = list(wb_val[name].iter_rows(values_only=True))
    finally:
        wb_val.close()

    # Pass 2: extract formula strings from the first few rows, then close.
    form_data: dict[str, list] = {}
    wb_form = openpyxl.load_workbook(io.BytesIO(data), data_only=False)
    try:
        for name in wb_form.sheetnames:
            form_data[name] = list(
                wb_form[name].iter_rows(max_row=_FORMULA_ANNOTATE_ROWS, values_only=False)
            )
    finally:
        wb_form.close()

    # Merge: annotate formula cells with their expression.
    parts: list[str] = []
    for name, val_rows in val_data.items():
        if not val_rows:
            continue
        form_rows = form_data.get(name, [])
        merged_rows: list[list] = []
        for row_idx, val_row in enumerate(val_rows):
            if row_idx < len(form_rows):
                form_row = form_rows[row_idx]
                merged: list = []
                for col_idx, val in enumerate(val_row):
                    form_cell_val = form_row[col_idx].value if col_idx < len(form_row) else None
                    if isinstance(form_cell_val, str) and form_cell_val.startswith("="):
                        prefix = f"{val} " if val is not None else ""
                        merged.append(f"{prefix}(Formula: {form_cell_val})")
                    else:
                        merged.append(val)
                merged_rows.append(merged)
            else:
                merged_rows.append(list(val_row))

        parts.append(f"**Sheet: {name}**")
        parts.append(_rows_to_markdown(merged_rows))

    return "\n\n".join(parts)


def _convert_pdf(data: bytes) -> str:
    try:
        from pdfminer.high_level import extract_text
    except ImportError:
        return "[PDF processing unavailable - install pdfminer.six]"
    text = extract_text(io.BytesIO(data)).strip()
    if len(text) > _EXTRACT_LIMIT:
        text = text[:_EXTRACT_LIMIT]
    return text


def _convert_docx(data: bytes) -> str:
    try:
        from docx import Document
    except ImportError:
        return "[DOCX processing unavailable - install python-docx]"
    doc = Document(io.BytesIO(data))
    parts: list[str] = []
    for para in doc.paragraphs:
        text = para.text.strip()
        if not text:
            continue
        style = para.style.name if para.style else ""
        if style.startswith("Heading"):
            try:
                level = int(style.split()[-1])
            except (ValueError, IndexError):
                level = 1
            parts.append("#" * min(level, 6) + " " + text)
        else:
            parts.append(text)
    result = "\n\n".join(parts)
    if len(result) > _EXTRACT_LIMIT:
        result = result[:_EXTRACT_LIMIT]
    return result


def _convert_txt(data: bytes) -> str:
    text = data.decode("utf-8", errors="replace").strip()
    if len(text) > _EXTRACT_LIMIT:
        text = text[:_EXTRACT_LIMIT]
    return text


def _convert_document(filename: str, data: bytes, log=None) -> str:
    """Dispatch to the appropriate converter. Returns '' for unsupported or on error."""
    ext = Path(filename).suffix.lower()
    try:
        if ext == ".csv":
            return _convert_csv(data)
        if ext in (".xlsx", ".xls"):
            return _convert_xlsx(data)
        if ext == ".pdf":
            return _convert_pdf(data)
        if ext == ".docx":
            return _convert_docx(data)
        if ext in (".txt",):
            return _convert_txt(data)
    except Exception as exc:
        if log:
            log(f"    {filename}: conversion error ({exc}) - skipped")
        return ""
    return ""


# -- LLM summarization for large documents ------------------------------------

_SUMMARIZE_SYSTEM = (
    "You are a technical summarizer. Summarize the provided document content concisely, "
    "preserving ALL of the following verbatim - never paraphrase or drop them:\n"
    "  - Column headers and sheet names from every spreadsheet table.\n"
    "  - Every formula annotation in the form 'VALUE (Formula: EXPR)' - the EXPR is a "
    "Non-Negotiable Business Rule and must appear exactly as written.\n"
    "  - Any block prefixed ### [TECHNICAL SCHEMA: <filename>] - reproduce the entire block.\n"
    "  - Any block prefixed ### [TECHNICAL LOGIC: <filename>] - reproduce the entire block.\n"
    "For all other content: condense to the key insights, error messages, data points, and "
    "code references. Return only the summary."
)


async def _llm_summarize(config: ChannelConfig, filename: str, content: str) -> str:
    """Summarize large document content via the configured text LLM. Falls back to truncation."""
    prompt = f"Summarize this content from attachment '{filename}':\n\n{content}"
    try:
        if config.provider == "anthropic":
            from anthropic import AsyncAnthropic
            client = AsyncAnthropic(api_key=config.api_key)
            resp = await client.messages.create(
                model=config.model,
                max_tokens=1024,
                timeout=90.0,
                system=_SUMMARIZE_SYSTEM,
                messages=[{"role": "user", "content": prompt}],
            )
            return resp.content[0].text.strip() if resp.content else content[:_SUMMARIZE_THRESHOLD]
        if config.provider == "google":
            from google import genai
            from google.genai import types
            client = genai.Client(api_key=config.api_key)
            cfg = types.GenerateContentConfig(
                system_instruction=_SUMMARIZE_SYSTEM,
                temperature=0.0,
            )
            resp = await asyncio.wait_for(
                client.aio.models.generate_content(
                    model=config.model,
                    contents=prompt,
                    config=cfg,
                ),
                timeout=90.0,
            )
            return (resp.text or content[:_SUMMARIZE_THRESHOLD]).strip()
        from openai import AsyncOpenAI
        kwargs: dict = {"api_key": config.api_key or "ollama"}
        base_url = config.base_url or _DEFAULT_BASE_URLS.get(config.provider)
        if base_url:
            kwargs["base_url"] = base_url
        client = AsyncOpenAI(**kwargs)
        resp = await client.chat.completions.create(
            model=config.model,
            max_tokens=1024,
            timeout=90.0,
            messages=[
                {"role": "system", "content": _SUMMARIZE_SYSTEM},
                {"role": "user", "content": prompt},
            ],
        )
        return (resp.choices[0].message.content or content[:_SUMMARIZE_THRESHOLD]).strip()
    except Exception as exc:
        import sys
        print(f"    [summarize] LLM summarization failed ({exc}) - truncating content", file=sys.stderr)
        return content[:_SUMMARIZE_THRESHOLD] + _TRUNCATION_NOTE


# -- Per-attachment coroutines -------------------------------------------------

async def _process_image(
    filename: str,
    content_url: str,
    downloader,
    image_config: ChannelConfig | None,
    log: Callable[[str], None] | None,
) -> tuple[str, str, str]:
    """Download an image, OCR it, optionally vision-enrich. Returns (filename, text, base64)."""
    try:
        image_bytes = await downloader.download_attachment(content_url)
    except Exception as exc:
        if log:
            log(f"    {filename}: download failed ({exc}) - skipped")
        return filename, "", ""
    b64 = base64.b64encode(image_bytes).decode()
    text = ocr_image(image_bytes)
    if log:
        log(f"    {filename}: OCR: {len(text)} chars")
    if image_config:
        text = await vision_enrich(image_config, image_bytes, text, filename)
        if log:
            log(f"    {filename}: vision: {len(text)} chars")
    return filename, text, b64


async def _process_document(
    filename: str,
    content_url: str,
    downloader,
    text_config: ChannelConfig | None,
    log: Callable[[str], None] | None,
) -> tuple[str, str, str]:
    """Download a document, convert to text/markdown, optionally summarize. Returns (filename, text, "")."""
    try:
        data = await downloader.download_attachment(content_url)
    except Exception as exc:
        if log:
            log(f"    {filename}: download failed ({exc}) - skipped")
        return filename, "", ""
    text = _convert_document(filename, data, log=log)
    if not text:
        return filename, "", ""
    if log:
        log(f"    {filename}: {Path(filename).suffix}: {len(text)} chars")
    if len(text) > _SUMMARIZE_THRESHOLD:
        if text_config:
            text = await _llm_summarize(text_config, filename, text)
            if log:
                log(f"    {filename}: summarized: {len(text)} chars")
        else:
            text = text[:_SUMMARIZE_THRESHOLD] + _TRUNCATION_NOTE
    return filename, text, ""


# -- Main entry point ----------------------------------------------------------

async def process_attachments(
    raw: RawIssueData,
    downloader,
    llm_config: LLMConfig | None,
    log: Callable[[str], None] | None = None,
) -> tuple[dict[str, str], dict[str, str]]:
    """
    Download and process all attachments concurrently via asyncio.gather.

    Images   -> OCR + optional vision enrichment + Base64 capture.
    Documents -> UAE conversion + optional LLM summarization for large content.
    Unknown file types -> silently skipped.

    downloader - any object with async download_attachment(url) -> bytes
    Returns (attachment_texts, images):
      attachment_texts: filename -> extracted text
      images: filename -> Base64 (ALL image attachments, regardless of OCR result)
    """
    if not raw.attachment_content_urls:
        return {}, {}

    image_config = llm_config.image_config if llm_config else None
    text_config = llm_config.text_config if llm_config else None

    image_names = [f for f in raw.attachment_content_urls if _is_image(f)]
    if image_names and not _tesseract_available() and not image_config and log:
        log(
            "  Tesseract OCR binary not found - text will not be extracted from images.\n"
            "    Install: brew install tesseract (macOS)"
            " | apt install tesseract-ocr (Linux)"
            " | winget install UB-Mannheim.TesseractOCR (Windows)"
        )

    tasks = []
    for filename, content_url in raw.attachment_content_urls.items():
        if _is_image(filename):
            tasks.append(_process_image(filename, content_url, downloader, image_config, log))
        elif _is_document(filename):
            tasks.append(_process_document(filename, content_url, downloader, text_config, log))
        # Unsupported types are silently skipped - no task created

    if not tasks:
        return {}, {}

    if log:
        from rich.console import Console
        _con = Console(stderr=True)
        with _con.status(f"  processing {len(tasks)} attachment(s)...", spinner="dots"):
            results = await asyncio.gather(*tasks, return_exceptions=True)
    else:
        results = await asyncio.gather(*tasks, return_exceptions=True)

    output_texts: dict[str, str] = {}
    output_images: dict[str, str] = {}
    for item in results:
        if isinstance(item, BaseException):
            if log:
                log(f"    attachment error: {item}")
            continue
        fname, text, b64 = item
        if text:
            output_texts[fname] = text
        if b64:
            output_images[fname] = b64
    return output_texts, output_images
