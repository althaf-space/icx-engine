from __future__ import annotations
import asyncio
import base64
import csv
import io
import logging
import os
import re
import shutil
import tempfile
import threading
from pathlib import Path
from typing import Callable

from icx_engine.models.config import LLMConfig, ChannelConfig
from icx_engine.models.output import RawIssueData

_log = logging.getLogger(__name__)

# -- Extension sets ------------------------------------------------------------

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".tif"}
DOCUMENT_EXTENSIONS = {".csv", ".xlsx", ".xls", ".pdf", ".docx", ".txt"}
AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".ogg", ".flac", ".aac", ".opus"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm"}

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
    "xai": "https://api.x.ai/v1",
}

_whisper_singleton: "WhisperManager | None" = None
_whisper_singleton_lock = threading.Lock()


def _get_whisper() -> "WhisperManager":
    global _whisper_singleton
    if _whisper_singleton is None:
        with _whisper_singleton_lock:
            if _whisper_singleton is None:
                from icx_engine.connectors.audio import WhisperManager
                _whisper_singleton = WhisperManager()
    return _whisper_singleton


def _is_image(filename: str) -> bool:
    return Path(filename).suffix.lower() in IMAGE_EXTENSIONS


def _is_document(filename: str) -> bool:
    return Path(filename).suffix.lower() in DOCUMENT_EXTENSIONS


def _is_audio(filename: str) -> bool:
    return Path(filename).suffix.lower() in AUDIO_EXTENSIONS


def _is_video(filename: str) -> bool:
    return Path(filename).suffix.lower() in VIDEO_EXTENSIONS


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
        _log.warning("LLM summarization failed (%s); truncating content", exc)
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


_VIDEO_FRAME_PROMPT = (
    "This is frame {frame_num} of {total_frames} from a screen recording showing a software "
    "issue or UI interaction. The following text was extracted via OCR from this frame:\n\n"
    "{ocr_text}\n\n"
    "Describe exactly what you see: which UI elements are visible, what data is shown in "
    "tables or lists, any filter values or date ranges selected, error messages, what the "
    "user appears to be doing, and any unexpected or incorrect behavior. "
    "Be specific about field values, column headers, visible records, and any visual "
    "discrepancies. Return only the description, nothing else."
)

_WHISPER_NOISE_RE = re.compile(r'^[\s\.…\,\!\?\-\_\(\)]*$')


def _is_empty_transcript(transcript: str) -> bool:
    """Return True if Whisper output is noise/silence rather than real speech."""
    return not transcript.strip() or bool(_WHISPER_NOISE_RE.fullmatch(transcript.strip()))


_SETUP_REQUIRED_MSG = (
    "[Audio transcription unavailable - run 'icx setup' in your terminal to download "
    "the Whisper model, then retry]"
)


def _is_setup_required_error(exc: Exception) -> bool:
    return isinstance(exc, RuntimeError) and "icx setup" in str(exc)


async def _extract_frames_from_video(
    video_bytes: bytes,
    fname: str,
    fps: float = 0.5,
    max_frames: int = 8,
) -> list[bytes]:
    """Extract JPEG frames from video using ffmpeg. Returns list of JPEG bytes."""
    import imageio_ffmpeg
    suffix = Path(fname).suffix.lower() or ".mp4"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as vf:
        vf.write(video_bytes)
        video_path = vf.name
    frame_dir = tempfile.mkdtemp()
    try:
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        proc = await asyncio.create_subprocess_exec(
            ffmpeg, "-i", video_path,
            "-vf", f"fps={fps}",
            "-frames:v", str(max_frames),
            "-q:v", "5",
            os.path.join(frame_dir, "frame%04d.jpg"),
            "-y",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            await asyncio.wait_for(proc.communicate(), timeout=60.0)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()
            return []
        frames: list[bytes] = []
        for i in range(1, max_frames + 1):
            frame_path = Path(frame_dir) / f"frame{i:04d}.jpg"
            if frame_path.exists():
                frames.append(frame_path.read_bytes())
        return frames
    except Exception:
        return []
    finally:
        try:
            os.unlink(video_path)
        except OSError:
            pass
        try:
            shutil.rmtree(frame_dir, ignore_errors=True)
        except Exception:
            pass


async def _describe_video_frame(
    config: "ChannelConfig",
    frame_bytes: bytes,
    frame_num: int,
    total_frames: int,
) -> str:
    """OCR + vision-enrich a single video frame. Returns description string."""
    ocr_text = ocr_image(frame_bytes)
    prompt = _VIDEO_FRAME_PROMPT.format(
        frame_num=frame_num,
        total_frames=total_frames,
        ocr_text=ocr_text or "(no OCR output)",
    )
    try:
        b64 = base64.b64encode(frame_bytes).decode()
        if config.provider == "anthropic":
            from anthropic import AsyncAnthropic
            client = AsyncAnthropic(api_key=config.api_key)
            resp = await client.messages.create(
                model=config.model,
                max_tokens=512,
                timeout=90.0,
                messages=[{"role": "user", "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}},
                    {"type": "text", "text": prompt},
                ]}],
            )
            return resp.content[0].text.strip() if resp.content else ocr_text
        if config.provider == "google":
            from google import genai
            from google.genai import types
            client = genai.Client(api_key=config.api_key)
            resp = await asyncio.wait_for(
                client.aio.models.generate_content(
                    model=config.model,
                    contents=[
                        types.Part.from_bytes(data=frame_bytes, mime_type="image/jpeg"),
                        types.Part.from_text(text=prompt),
                    ],
                ),
                timeout=90.0,
            )
            return (resp.text or ocr_text).strip()
        from openai import AsyncOpenAI
        kwargs: dict = {"api_key": config.api_key or "ollama"}
        base_url = config.base_url or _DEFAULT_BASE_URLS.get(config.provider)
        if base_url:
            kwargs["base_url"] = base_url
        client = AsyncOpenAI(**kwargs)
        resp = await client.chat.completions.create(
            model=config.model,
            max_tokens=512,
            timeout=90.0,
            messages=[{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                {"type": "text", "text": prompt},
            ]}],
        )
        return (resp.choices[0].message.content or ocr_text).strip()
    except Exception:
        return ocr_text


async def _extract_audio_from_video(video_bytes: bytes, fname: str) -> bytes:
    """Extract audio track from video bytes as WAV (16 kHz mono) using imageio-ffmpeg."""
    import imageio_ffmpeg
    suffix = Path(fname).suffix.lower() or ".mp4"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as vf:
        vf.write(video_bytes)
        video_path = vf.name
    audio_fd, audio_path = tempfile.mkstemp(suffix=".wav")
    os.close(audio_fd)
    try:
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        proc = await asyncio.create_subprocess_exec(
            ffmpeg, "-i", video_path,
            "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
            audio_path, "-y",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=120.0)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()
            raise
        if proc.returncode != 0:
            msg = stderr.decode("utf-8", errors="replace").strip() if stderr else ""
            raise RuntimeError(
                f"ffmpeg exited with code {proc.returncode}: {msg[-500:]}"
            )
        with open(audio_path, "rb") as f:
            return f.read()
    finally:
        for p in (video_path, audio_path):
            try:
                os.unlink(p)
            except OSError:
                pass


async def _process_audio(
    filename: str,
    content_url: str,
    downloader,
    text_config: "ChannelConfig | None",
    whisper,
    log: Callable[[str], None] | None,
) -> tuple[str, str, str]:
    """Download audio, transcribe via LLM or local Whisper. Returns (filename, transcript, "")."""
    try:
        audio_bytes = await downloader.download_attachment(content_url)
    except Exception as exc:
        if log:
            log(f"    {filename}: download failed ({exc}) - skipped")
        return filename, "", ""
    try:
        from icx_engine.connectors.audio import transcribe as audio_transcribe
        transcript = await audio_transcribe(text_config, audio_bytes, filename, whisper)
    except Exception as exc:
        if _is_setup_required_error(exc):
            if log:
                log(f"    {filename}: Whisper model not installed - run 'icx setup'")
            return filename, _SETUP_REQUIRED_MSG, ""
        if log:
            log(f"    {filename}: transcription failed ({exc}) - skipped")
        return filename, "", ""
    if log:
        log(f"    {filename}: transcript: {len(transcript)} chars")
    return filename, transcript, ""


async def _process_video(
    filename: str,
    content_url: str,
    downloader,
    text_config: "ChannelConfig | None",
    image_config: "ChannelConfig | None",
    whisper,
    log: Callable[[str], None] | None,
) -> tuple[str, str, str]:
    """Download video, extract audio + transcribe; fall back to frame analysis for screen recordings."""
    try:
        video_bytes = await downloader.download_attachment(content_url)
    except Exception as exc:
        if log:
            log(f"    {filename}: download failed ({exc}) - skipped")
        return filename, "", ""

    # --- Audio path ---
    transcript = ""
    _audio_setup_msg = ""
    try:
        audio_bytes = await _extract_audio_from_video(video_bytes, filename)
        if len(audio_bytes) >= 44:  # WAV header is 44 bytes minimum; less means no audio track
            from icx_engine.connectors.audio import transcribe as audio_transcribe
            raw_transcript = await audio_transcribe(
                text_config, audio_bytes, Path(filename).stem + ".wav", whisper
            )
            if not _is_empty_transcript(raw_transcript):
                transcript = raw_transcript
                if log:
                    log(f"    {filename}: transcript: {len(transcript)} chars")
        else:
            if log:
                log(f"    {filename}: no audio track detected")
    except Exception as exc:
        if _is_setup_required_error(exc):
            _audio_setup_msg = _SETUP_REQUIRED_MSG
            if log:
                log(f"    {filename}: Whisper model not installed - run 'icx setup'")
        else:
            if log:
                log(f"    {filename}: audio extraction/transcription failed ({exc})")

    if transcript:
        return filename, transcript, ""

    # --- Frame analysis fallback (screen recordings, silent videos) ---
    if not image_config and not _tesseract_available():
        if log:
            log(f"    {filename}: no speech detected and no vision model configured - skipped")
        return filename, _audio_setup_msg, ""

    if log:
        log(f"    {filename}: no speech detected - extracting frames for visual analysis")
    try:
        frames = await _extract_frames_from_video(video_bytes, filename)
    except Exception as exc:
        if log:
            log(f"    {filename}: frame extraction failed ({exc}) - skipped")
        return filename, _audio_setup_msg, ""

    if not frames:
        if log:
            log(f"    {filename}: no frames extracted - skipped")
        return filename, _audio_setup_msg, ""

    if log:
        log(f"    {filename}: analyzing {len(frames)} frame(s)")

    frame_descriptions: list[str] = []
    for i, frame_bytes in enumerate(frames, start=1):
        if image_config:
            try:
                desc = await _describe_video_frame(image_config, frame_bytes, i, len(frames))
            except Exception:
                desc = ocr_image(frame_bytes)
        else:
            desc = ocr_image(frame_bytes)
        if desc:
            frame_descriptions.append(f"[Frame {i}/{len(frames)}] {desc}")

    if not frame_descriptions:
        return filename, _audio_setup_msg, ""

    result = "\n\n".join(frame_descriptions)
    if _audio_setup_msg:
        result = _audio_setup_msg + "\n\n" + result
    if log:
        log(f"    {filename}: frame analysis: {len(result)} chars")
    return filename, result, ""


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

    _has_av = any(
        _is_audio(f) or _is_video(f)
        for f in raw.attachment_content_urls
    )
    _whisper = _get_whisper() if _has_av else None

    tasks = []
    for filename, content_url in raw.attachment_content_urls.items():
        if _is_image(filename):
            tasks.append(_process_image(filename, content_url, downloader, image_config, log))
        elif _is_document(filename):
            tasks.append(_process_document(filename, content_url, downloader, text_config, log))
        elif _is_audio(filename):
            tasks.append(_process_audio(filename, content_url, downloader, text_config, _whisper, log))
        elif _is_video(filename):
            tasks.append(_process_video(filename, content_url, downloader, text_config, image_config, _whisper, log))
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
