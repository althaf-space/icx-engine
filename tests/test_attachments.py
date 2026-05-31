import io
import pytest
import respx
import httpx
from unittest.mock import patch, MagicMock, AsyncMock

from icx_engine.connectors.attachments import (
    _is_image, _is_document, _convert_csv, _convert_txt, _rows_to_markdown,
    _convert_xlsx, _VISION_PROMPT, _SUMMARIZE_SYSTEM,
    ocr_image, vision_enrich, process_attachments,
    _convert_document,
)
from icx_engine.connectors.jira.client import JiraClient
from icx_engine.models.config import LLMConfig, ChannelConfig
from icx_engine.models.output import RawIssueData
from test_data import JIRA_BASE_URL, JIRA_AUTH_HEADER, JIRA_ALLOWED_HOSTS


# ── Extension detection ───────────────────────────────────────────────────────

def test_is_image_true_for_common_formats():
    assert _is_image("screenshot.png") is True
    assert _is_image("error.jpg") is True
    assert _is_image("ui.jpeg") is True
    assert _is_image("capture.webp") is True
    assert _is_image("diagram.gif") is True


def test_is_image_false_for_non_images():
    assert _is_image("log.txt") is False
    assert _is_image("report.pdf") is False
    assert _is_image("data.csv") is False
    assert _is_image("code.py") is False


def test_is_image_case_insensitive():
    assert _is_image("SCREEN.PNG") is True
    assert _is_image("Error.JPG") is True


def test_is_document_true_for_supported_types():
    assert _is_document("report.pdf") is True
    assert _is_document("data.csv") is True
    assert _is_document("sheet.xlsx") is True
    assert _is_document("doc.docx") is True
    assert _is_document("notes.txt") is True


def test_is_document_false_for_unsupported():
    assert _is_document("script.py") is False
    assert _is_document("archive.zip") is False
    assert _is_document("screenshot.png") is False


# ── OCR ───────────────────────────────────────────────────────────────────────

def test_ocr_image_returns_text():
    fake_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 50
    with patch("pytesseract.image_to_string", return_value="Error: null pointer"), \
         patch("PIL.Image.open", return_value=MagicMock()):
        result = ocr_image(fake_bytes)
    assert result == "Error: null pointer"


def test_ocr_image_returns_empty_on_tesseract_exception():
    fake_bytes = b"\x89PNG\r\n\x1a\n"
    with patch("pytesseract.image_to_string", side_effect=Exception("tesseract failed")), \
         patch("PIL.Image.open", return_value=MagicMock()):
        result = ocr_image(fake_bytes)
    assert result == ""


def test_ocr_image_returns_empty_on_import_error():
    fake_bytes = b"\x89PNG\r\n\x1a\n"
    with patch.dict("sys.modules", {"pytesseract": None}):
        result = ocr_image(fake_bytes)
    assert result == ""


def test_vision_prompt_includes_graph_analysis_sections():
    """_VISION_PROMPT must request graph/chart interpretation alongside text extraction."""
    prompt_lower = _VISION_PROMPT.lower()
    assert "{ocr_text}" in _VISION_PROMPT, \
        "placeholder must be preserved for OCR text injection"
    assert "graph" in prompt_lower or "chart" in prompt_lower, \
        "must request graph/chart analysis"
    assert "axes" in prompt_lower or "axis" in prompt_lower, \
        "must request axis labels and units"
    assert "trend" in prompt_lower, \
        "must request trend identification"
    assert "peak" in prompt_lower, \
        "must request peak value identification"
    assert "minimum" in prompt_lower or "min" in prompt_lower, \
        "must request minimum value identification"
    assert "average" in prompt_lower, \
        "must request average value identification"


# ── Vision enrichment ─────────────────────────────────────────────────────────

async def test_vision_enrich_anthropic_refines_ocr():
    config = ChannelConfig(provider="anthropic", model="claude-3-opus", api_key="sk-ant-test")
    fake_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 50

    mock_response = MagicMock()
    mock_response.content = [MagicMock()]
    mock_response.content[0].text = "Refined: NullPointerException at com.example.Foo:42"

    with patch("anthropic.AsyncAnthropic") as mock_cls:
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.messages.create = AsyncMock(return_value=mock_response)
        result = await vision_enrich(config, fake_bytes, "raw ocr text")

    assert result == "Refined: NullPointerException at com.example.Foo:42"


async def test_vision_enrich_openai_refines_ocr():
    config = ChannelConfig(provider="openai", model="gpt-4o", api_key="sk-test")
    fake_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 50

    mock_response = MagicMock()
    mock_response.choices = [MagicMock()]
    mock_response.choices[0].message.content = "Refined OpenAI output"

    with patch("openai.AsyncOpenAI") as mock_cls:
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.chat.completions.create = AsyncMock(return_value=mock_response)
        result = await vision_enrich(config, fake_bytes, "raw ocr text")

    assert result == "Refined OpenAI output"


async def test_vision_enrich_nim_vision_model_refines_ocr():
    config = ChannelConfig(provider="nim", model="meta/llama-3.2-11b-vision-instruct", api_key="nim-key")
    fake_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 50

    mock_response = MagicMock()
    mock_response.choices = [MagicMock()]
    mock_response.choices[0].message.content = "NIM vision output"

    with patch("openai.AsyncOpenAI") as mock_cls:
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.chat.completions.create = AsyncMock(return_value=mock_response)
        result = await vision_enrich(config, fake_bytes, "raw ocr text")

    assert result == "NIM vision output"


async def test_vision_enrich_google_refines_ocr():
    config = ChannelConfig(provider="google", model="gemini-1.5-flash", api_key="goog-test")
    fake_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 50

    mock_response = MagicMock()
    mock_response.text = "Google vision output"

    mock_client = MagicMock()
    mock_client.aio = MagicMock()
    mock_client.aio.models = MagicMock()
    mock_client.aio.models.generate_content = AsyncMock(return_value=mock_response)

    with patch("google.genai.Client", return_value=mock_client):
        result = await vision_enrich(config, fake_bytes, "raw ocr text")

    assert result == "Google vision output"


async def test_vision_enrich_raises_clear_error_when_model_is_text_only():
    from icx_engine.exceptions import ContextBuildError

    config = ChannelConfig(provider="ollama", model="llama3")
    fake_bytes = b"\x89PNG\r\n\x1a\n"

    with patch("openai.AsyncOpenAI") as mock_cls:
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.chat.completions.create = AsyncMock(
            side_effect=Exception("model does not support vision")
        )
        with pytest.raises(ContextBuildError, match="does not support image input"):
            await vision_enrich(config, fake_bytes, "ocr fallback text")


# ── SDK timeout parameters ────────────────────────────────────────────────────

async def test_vision_enrich_anthropic_passes_timeout_90s():
    """Anthropic vision call must include timeout=90.0 to prevent indefinite hang."""
    config = ChannelConfig(provider="anthropic", model="claude-3-haiku-20240307", api_key="sk-ant-test")
    fake_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 50

    mock_response = MagicMock()
    mock_response.content = [MagicMock()]
    mock_response.content[0].text = "result"

    with patch("anthropic.AsyncAnthropic") as mock_cls:
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.messages.create = AsyncMock(return_value=mock_response)
        await vision_enrich(config, fake_bytes, "ocr text", "shot.png")

    _, kwargs = mock_client.messages.create.call_args
    assert kwargs.get("timeout") == 90.0


async def test_vision_enrich_openai_passes_timeout_90s():
    """OpenAI-compat vision call must include timeout=90.0 to prevent indefinite hang."""
    config = ChannelConfig(provider="openai", model="gpt-4o", api_key="sk-test")
    fake_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 50

    mock_response = MagicMock()
    mock_response.choices = [MagicMock()]
    mock_response.choices[0].message.content = "result"

    with patch("openai.AsyncOpenAI") as mock_cls:
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.chat.completions.create = AsyncMock(return_value=mock_response)
        await vision_enrich(config, fake_bytes, "ocr text", "shot.png")

    _, kwargs = mock_client.chat.completions.create.call_args
    assert kwargs.get("timeout") == 90.0


async def test_vision_enrich_google_timeout_raises_context_build_error():
    """A Google vision call that hits asyncio.wait_for timeout surfaces as ContextBuildError."""
    import asyncio as _asyncio
    from icx_engine.exceptions import ContextBuildError

    config = ChannelConfig(provider="google", model="gemini-1.5-flash", api_key="goog-test")
    fake_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 50

    async def _timeout_wait_for(*args, **kwargs):
        raise _asyncio.TimeoutError

    mock_client = MagicMock()
    mock_client.aio.models.generate_content = MagicMock(return_value=MagicMock())

    with patch("google.genai.Client", return_value=mock_client):
        with patch("icx_engine.connectors.attachments.asyncio.wait_for", new=_timeout_wait_for):
            with pytest.raises(ContextBuildError):
                await vision_enrich(config, fake_bytes, "ocr text", "shot.png")


async def test_llm_summarize_anthropic_passes_timeout_90s():
    """Anthropic summarize call must include timeout=90.0."""
    from icx_engine.connectors.attachments import _llm_summarize

    config = ChannelConfig(provider="anthropic", model="claude-3-haiku-20240307", api_key="sk-ant-test")

    mock_response = MagicMock()
    mock_response.content = [MagicMock()]
    mock_response.content[0].text = "summary"

    with patch("anthropic.AsyncAnthropic") as mock_cls:
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.messages.create = AsyncMock(return_value=mock_response)
        await _llm_summarize(config, "doc.pdf", "long content here")

    _, kwargs = mock_client.messages.create.call_args
    assert kwargs.get("timeout") == 90.0


async def test_llm_summarize_openai_passes_timeout_90s():
    """OpenAI-compat summarize call must include timeout=90.0."""
    from icx_engine.connectors.attachments import _llm_summarize

    config = ChannelConfig(provider="openai", model="gpt-4o-mini", api_key="sk-test")

    mock_response = MagicMock()
    mock_response.choices = [MagicMock()]
    mock_response.choices[0].message.content = "summary"

    with patch("openai.AsyncOpenAI") as mock_cls:
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.chat.completions.create = AsyncMock(return_value=mock_response)
        await _llm_summarize(config, "doc.pdf", "long content here")

    _, kwargs = mock_client.chat.completions.create.call_args
    assert kwargs.get("timeout") == 90.0


# ── UAE: document converters ──────────────────────────────────────────────────

def test_convert_csv_returns_markdown_table():
    csv_data = b"Name,Status\nAlice,Active\nBob,Inactive"
    result = _convert_csv(csv_data)
    assert "| Name | Status |" in result
    assert "| Alice | Active |" in result
    assert "| Bob | Inactive |" in result


def test_convert_csv_truncates_at_50_rows():
    header = "A,B\n"
    rows = "\n".join(f"{i},{i*2}" for i in range(1, 60))  # 59 data rows
    csv_data = (header + rows).encode()
    result = _convert_csv(csv_data)
    assert "truncated" in result.lower()
    assert "59" in result  # shows total row count


def test_convert_txt_returns_clean_text():
    from icx_engine.connectors.attachments import _convert_txt
    data = b"  Hello World  \nLine 2\n"
    result = _convert_txt(data)
    assert "Hello World" in result
    assert "Line 2" in result


def test_convert_txt_truncates_large_content():
    from icx_engine.connectors.attachments import _convert_txt, _SUMMARIZE_THRESHOLD, _TRUNCATION_NOTE
    # Create content larger than extract limit
    big_text = ("A" * 1000 + "\n") * 200   # 200_200 chars
    data = big_text.encode()
    result = _convert_txt(data)
    # Must not exceed extract limit
    from icx_engine.connectors.attachments import _EXTRACT_LIMIT
    assert len(result) <= _EXTRACT_LIMIT + len(_TRUNCATION_NOTE)


def test_rows_to_markdown_produces_valid_table():
    rows = [["Col1", "Col2"], ["A", "B"], ["C", "D"]]
    result = _rows_to_markdown(rows)
    assert "| Col1 | Col2 |" in result
    assert "| --- | --- |" in result
    assert "| A | B |" in result


def test_rows_to_markdown_truncates_at_50():
    rows = [["X"]] + [[str(i)] for i in range(60)]
    result = _rows_to_markdown(rows)
    assert "truncated" in result.lower()


def test_convert_xlsx_unavailable_returns_message():
    import unittest.mock
    with unittest.mock.patch.dict("sys.modules", {"openpyxl": None}):
        from icx_engine.connectors.attachments import _convert_xlsx
        result = _convert_xlsx(b"fake")
    assert "unavailable" in result.lower()


def test_convert_pdf_unavailable_returns_message():
    import unittest.mock
    with unittest.mock.patch.dict("sys.modules", {"pdfminer": None, "pdfminer.high_level": None}):
        from icx_engine.connectors.attachments import _convert_pdf
        result = _convert_pdf(b"fake")
    assert "unavailable" in result.lower()


def test_convert_docx_unavailable_returns_message():
    import unittest.mock
    with unittest.mock.patch.dict("sys.modules", {"docx": None}):
        from icx_engine.connectors.attachments import _convert_docx
        result = _convert_docx(b"fake")
    assert "unavailable" in result.lower()


def test_convert_document_returns_empty_for_unsupported_extension():
    result = _convert_document("script.py", b"print('hello')")
    assert result == ""


def test_convert_xlsx_annotates_formula_cells_in_sample_rows():
    """Formula cells in header + first 3 data rows are annotated as 'value (Formula: expr)'."""
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Prices"
    ws.append(["Item", "Price", "Tax"])
    ws.append(["Widget", 100, "=B2*0.18"])
    ws.append(["Gadget", 200, "=B3*0.18"])
    ws.append(["Doohickey", 300, "=B4*0.18"])
    buf = io.BytesIO()
    wb.save(buf)

    result = _convert_xlsx(buf.getvalue())

    assert "**Sheet: Prices**" in result
    assert "(Formula: =B2*0.18)" in result
    assert "(Formula: =B3*0.18)" in result
    assert "(Formula: =B4*0.18)" in result


def test_convert_xlsx_plain_value_cells_not_annotated():
    """Cells without formulas must appear as plain values with no (Formula: ...) annotation."""
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Data"
    ws.append(["Name", "Score"])
    ws.append(["Alice", 95])
    ws.append(["Bob", 87])
    buf = io.BytesIO()
    wb.save(buf)

    result = _convert_xlsx(buf.getvalue())

    assert "Formula:" not in result
    assert "Alice" in result
    assert "95" in result


def test_convert_xlsx_formula_annotation_skipped_beyond_row_3():
    """Formula annotation applies only to header + first 3 data rows; rows 4+ show plain values."""
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws.append(["Item", "Total"])
    for i in range(1, 6):
        ws.append([f"Item{i}", f"=A{i + 1}*10"])
    buf = io.BytesIO()
    wb.save(buf)

    result = _convert_xlsx(buf.getvalue())

    # Data rows 1-3 (val_rows indices 1-3) must be annotated
    assert "(Formula: =A2*10)" in result
    assert "(Formula: =A3*10)" in result
    assert "(Formula: =A4*10)" in result
    # Data rows 4-5 (val_rows indices 4-5) must NOT be annotated
    assert "(Formula: =A5*10)" not in result
    assert "(Formula: =A6*10)" not in result


def test_convert_xlsx_multi_sheet_formula_annotation():
    """Formula annotation works independently on each sheet in a multi-sheet workbook."""
    import openpyxl
    wb = openpyxl.Workbook()
    ws1 = wb.active
    ws1.title = "Sheet1"
    ws1.append(["Val", "Computed"])
    ws1.append([10, "=A2*2"])
    ws2 = wb.create_sheet("Sheet2")
    ws2.append(["Val", "Computed"])
    ws2.append([20, "=A2*3"])
    buf = io.BytesIO()
    wb.save(buf)

    result = _convert_xlsx(buf.getvalue())

    assert "**Sheet: Sheet1**" in result
    assert "**Sheet: Sheet2**" in result
    assert "(Formula: =A2*2)" in result
    assert "(Formula: =A2*3)" in result


# ── process_attachments - orchestrator ───────────────────────────────────────

def _make_raw(attachment_content_urls=None) -> RawIssueData:
    return RawIssueData(
        issue_key="X-1", issue_type="Bug", summary="s", description="d",
        comments=[], attachments=list((attachment_content_urls or {}).keys()),
        priority="High", status="Open", metadata={},
        attachment_content_urls=attachment_content_urls or {},
    )


@respx.mock
async def test_process_attachments_downloads_and_ocrs():
    content_url = "https://test.atlassian.net/rest/api/3/attachment/content/10001"
    fake_png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 50
    respx.get(content_url).mock(return_value=httpx.Response(200, content=fake_png))

    raw = _make_raw({"screenshot.png": content_url})
    client = JiraClient(JIRA_BASE_URL, JIRA_AUTH_HEADER, JIRA_ALLOWED_HOSTS)

    with patch("icx_engine.connectors.attachments.ocr_image", return_value="OCR result"):
        texts, images = await process_attachments(raw, client, llm_config=None)

    assert texts == {"screenshot.png": "OCR result"}
    assert "screenshot.png" in images
    import base64 as _b64
    assert _b64.b64decode(images["screenshot.png"]) == fake_png


@respx.mock
async def test_process_attachments_handles_pdf():
    content_url = "https://test.atlassian.net/rest/api/3/attachment/content/99"
    fake_pdf = b"fake pdf bytes"
    respx.get(content_url).mock(return_value=httpx.Response(200, content=fake_pdf))

    raw = _make_raw({"report.pdf": content_url})
    client = JiraClient(JIRA_BASE_URL, JIRA_AUTH_HEADER, JIRA_ALLOWED_HOSTS)

    with patch("icx_engine.connectors.attachments._convert_document", return_value="Extracted PDF text"):
        texts, images = await process_attachments(raw, client, llm_config=None)

    assert texts == {"report.pdf": "Extracted PDF text"}
    assert images == {}  # documents produce no Base64


async def test_process_attachments_skips_unsupported_extension():
    raw = _make_raw({"script.py": "https://test.atlassian.net/rest/api/3/attachment/content/100"})
    client = JiraClient(JIRA_BASE_URL, JIRA_AUTH_HEADER, JIRA_ALLOWED_HOSTS)
    texts, images = await process_attachments(raw, client, llm_config=None)
    assert texts == {}
    assert images == {}


@respx.mock
async def test_process_attachments_skips_on_download_error():
    content_url = "https://test.atlassian.net/rest/api/3/attachment/content/10001"
    respx.get(content_url).mock(return_value=httpx.Response(403))
    raw = _make_raw({"screenshot.png": content_url})
    client = JiraClient(JIRA_BASE_URL, JIRA_AUTH_HEADER, JIRA_ALLOWED_HOSTS)
    texts, images = await process_attachments(raw, client, llm_config=None)
    assert texts == {}
    assert images == {}


@respx.mock
async def test_process_attachments_skips_vision_when_image_model_is_none():
    content_url = "https://test.atlassian.net/rest/api/3/attachment/content/10001"
    fake_png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 50
    respx.get(content_url).mock(return_value=httpx.Response(200, content=fake_png))

    raw = _make_raw({"screenshot.png": content_url})
    client = JiraClient(JIRA_BASE_URL, JIRA_AUTH_HEADER, JIRA_ALLOWED_HOSTS)
    llm = LLMConfig(text_config=ChannelConfig(provider="ollama", model="llama3"), image_config=None)

    with patch("icx_engine.connectors.attachments.ocr_image", return_value="raw ocr"):
        with patch("icx_engine.connectors.attachments.vision_enrich", new=AsyncMock()) as mock_vision:
            texts, images = await process_attachments(raw, client, llm_config=llm)

    mock_vision.assert_not_called()
    assert texts == {"screenshot.png": "raw ocr"}
    assert "screenshot.png" in images


@respx.mock
async def test_process_attachments_calls_vision_when_llm_config_provided():
    content_url = "https://test.atlassian.net/rest/api/3/attachment/content/10001"
    fake_png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 50
    respx.get(content_url).mock(return_value=httpx.Response(200, content=fake_png))

    raw = _make_raw({"screenshot.png": content_url})
    client = JiraClient(JIRA_BASE_URL, JIRA_AUTH_HEADER, JIRA_ALLOWED_HOSTS)
    llm = LLMConfig(text_config=ChannelConfig(provider="ollama", model="llama3"),
                    image_config=ChannelConfig(provider="ollama", model="llava"))

    with patch("icx_engine.connectors.attachments.ocr_image", return_value="raw ocr"):
        with patch("icx_engine.connectors.attachments.vision_enrich", new=AsyncMock(return_value="vision refined")) as mock_vision:
            texts, images = await process_attachments(raw, client, llm_config=llm)

    mock_vision.assert_called_once()
    assert texts == {"screenshot.png": "vision refined"}
    assert "screenshot.png" in images  # Base64 always captured regardless of vision model


@respx.mock
async def test_process_attachments_processes_multiple_in_parallel():
    url1 = "https://test.atlassian.net/rest/api/3/attachment/content/10001"
    url2 = "https://test.atlassian.net/rest/api/3/attachment/content/10002"
    fake_png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 50
    fake_csv = b"A,B\n1,2\n"
    respx.get(url1).mock(return_value=httpx.Response(200, content=fake_png))
    respx.get(url2).mock(return_value=httpx.Response(200, content=fake_csv))

    raw = _make_raw({"screen.png": url1, "data.csv": url2})
    client = JiraClient(JIRA_BASE_URL, JIRA_AUTH_HEADER, JIRA_ALLOWED_HOSTS)

    with patch("icx_engine.connectors.attachments.ocr_image", return_value="OCR text"):
        texts, images = await process_attachments(raw, client, llm_config=None)

    assert "screen.png" in texts
    assert "data.csv" in texts
    assert "OCR text" in texts["screen.png"]
    assert "| A | B |" in texts["data.csv"]
    assert "screen.png" in images   # image captured as Base64
    assert "data.csv" not in images  # document has no Base64


@respx.mock
async def test_process_attachments_summarizes_large_document_with_llm():
    content_url = "https://test.atlassian.net/rest/api/3/attachment/content/99"
    big_content = "A" * 25_000  # exceeds _SUMMARIZE_THRESHOLD
    respx.get(content_url).mock(return_value=httpx.Response(200, content=big_content.encode()))

    raw = _make_raw({"notes.txt": content_url})
    client = JiraClient(JIRA_BASE_URL, JIRA_AUTH_HEADER, JIRA_ALLOWED_HOSTS)
    llm = LLMConfig(text_config=ChannelConfig(provider="ollama", model="llama3"), image_config=None)

    with patch("icx_engine.connectors.attachments._llm_summarize", new=AsyncMock(return_value="summary")) as mock_sum:
        texts, images = await process_attachments(raw, client, llm_config=llm)

    mock_sum.assert_called_once()
    assert texts == {"notes.txt": "summary"}
    assert images == {}


@respx.mock
async def test_process_attachments_truncates_large_document_without_llm():
    from icx_engine.connectors.attachments import _SUMMARIZE_THRESHOLD, _TRUNCATION_NOTE
    content_url = "https://test.atlassian.net/rest/api/3/attachment/content/99"
    big_content = "B" * 25_000
    respx.get(content_url).mock(return_value=httpx.Response(200, content=big_content.encode()))

    raw = _make_raw({"notes.txt": content_url})
    client = JiraClient(JIRA_BASE_URL, JIRA_AUTH_HEADER, JIRA_ALLOWED_HOSTS)

    texts, images = await process_attachments(raw, client, llm_config=None)
    assert "notes.txt" in texts
    assert len(texts["notes.txt"]) <= _SUMMARIZE_THRESHOLD + len(_TRUNCATION_NOTE)
    assert "truncated" in texts["notes.txt"].lower()
    assert images == {}


@respx.mock
async def test_process_attachments_returns_empty_tuple_when_no_urls():
    raw = _make_raw({})
    client = JiraClient(JIRA_BASE_URL, JIRA_AUTH_HEADER, JIRA_ALLOWED_HOSTS)
    texts, images = await process_attachments(raw, client, llm_config=None)
    assert texts == {}
    assert images == {}


@respx.mock
async def test_process_attachments_captures_base64_for_images():
    """Base64 is captured even when OCR returns no text."""
    content_url = "https://test.atlassian.net/rest/api/3/attachment/content/10001"
    fake_png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
    respx.get(content_url).mock(return_value=httpx.Response(200, content=fake_png))

    raw = _make_raw({"error_screenshot.png": content_url})
    client = JiraClient(JIRA_BASE_URL, JIRA_AUTH_HEADER, JIRA_ALLOWED_HOSTS)

    with patch("icx_engine.connectors.attachments.ocr_image", return_value=""):
        texts, images = await process_attachments(raw, client, llm_config=None)

    assert "error_screenshot.png" not in texts   # no OCR text → not in texts
    assert "error_screenshot.png" in images       # but Base64 still captured
    import base64 as _b64
    assert _b64.b64decode(images["error_screenshot.png"]) == fake_png


def test_summarize_system_preserves_structured_data():
    p = _SUMMARIZE_SYSTEM.lower()
    assert "column headers" in p
    assert "formula" in p
    assert "non-negotiable business rule" in p
    assert "[technical schema:" in p
    assert "[technical logic:" in p
    assert "verbatim" in p


def test_process_attachments_spinner_shown_when_log_set():
    """When log is provided, a Rich status spinner is shown during processing."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock, patch
    from icx_engine.connectors.attachments import process_attachments
    from icx_engine.models.output import RawIssueData

    raw = RawIssueData(
        issue_key="T-1", issue_type="Bug", summary="s", description="d",
        comments=[], attachments=["report.txt"],
        priority="High", status="Open", metadata={},
        attachment_content_urls={"report.txt": "https://x.atlassian.net/content/1"},
        attachment_texts={},
    )

    downloader = MagicMock()
    downloader.download_attachment = AsyncMock(return_value=b"hello world content")

    status_context = MagicMock()
    status_context.__enter__ = MagicMock(return_value=None)
    status_context.__exit__ = MagicMock(return_value=False)

    with patch("rich.console.Console") as mock_console_cls:
        mock_con_instance = MagicMock()
        mock_con_instance.status.return_value = status_context
        mock_console_cls.return_value = mock_con_instance

        asyncio.run(process_attachments(raw, downloader, llm_config=None, log=list().append))

    mock_con_instance.status.assert_called_once()
    call_msg = mock_con_instance.status.call_args[0][0]
    assert "attachment" in call_msg.lower() or "processing" in call_msg.lower()


# ── Extension detection (audio/video) ─────────────────────────────────────────

def test_is_audio_true_for_audio_formats():
    from icx_engine.connectors.attachments import _is_audio
    for fname in ["note.mp3", "clip.wav", "record.m4a", "sound.ogg", "track.flac", "audio.aac", "voice.opus"]:
        assert _is_audio(fname) is True, f"Expected True for {fname}"


def test_is_audio_false_for_non_audio():
    from icx_engine.connectors.attachments import _is_audio
    assert _is_audio("report.pdf") is False
    assert _is_audio("screen.png") is False
    assert _is_audio("video.mp4") is False


def test_is_audio_case_insensitive():
    from icx_engine.connectors.attachments import _is_audio
    assert _is_audio("RECORDING.MP3") is True
    assert _is_audio("Clip.WAV") is True


def test_is_video_true_for_video_formats():
    from icx_engine.connectors.attachments import _is_video
    for fname in ["demo.mp4", "screen.mov", "capture.avi", "video.mkv", "clip.webm"]:
        assert _is_video(fname) is True, f"Expected True for {fname}"


def test_is_video_false_for_non_video():
    from icx_engine.connectors.attachments import _is_video
    assert _is_video("audio.mp3") is False
    assert _is_video("report.pdf") is False
    assert _is_video("screen.png") is False


# ── _process_audio ─────────────────────────────────────────────────────────────

@respx.mock
async def test_process_audio_downloads_and_transcribes():
    from icx_engine.connectors.attachments import _process_audio
    from icx_engine.connectors.audio import WhisperManager

    content_url = "https://test.atlassian.net/rest/api/3/attachment/content/200"
    respx.get(content_url).mock(return_value=httpx.Response(200, content=b"fake mp3 bytes"))

    client = JiraClient(JIRA_BASE_URL, JIRA_AUTH_HEADER, JIRA_ALLOWED_HOSTS)
    whisper = WhisperManager()

    with patch("icx_engine.connectors.audio.transcribe", new=AsyncMock(return_value="Transcribed audio text.")):
        fname, text, b64 = await _process_audio("meeting.mp3", content_url, client, None, whisper, None)

    assert fname == "meeting.mp3"
    assert text == "Transcribed audio text."
    assert b64 == ""


@respx.mock
async def test_process_audio_returns_empty_on_download_error():
    from icx_engine.connectors.attachments import _process_audio
    from icx_engine.connectors.audio import WhisperManager

    content_url = "https://test.atlassian.net/rest/api/3/attachment/content/201"
    respx.get(content_url).mock(return_value=httpx.Response(403))

    client = JiraClient(JIRA_BASE_URL, JIRA_AUTH_HEADER, JIRA_ALLOWED_HOSTS)
    whisper = WhisperManager()

    fname, text, b64 = await _process_audio("meeting.mp3", content_url, client, None, whisper, None)

    assert text == ""
    assert b64 == ""


@respx.mock
async def test_process_audio_returns_empty_on_transcription_error():
    from icx_engine.connectors.attachments import _process_audio
    from icx_engine.connectors.audio import WhisperManager

    content_url = "https://test.atlassian.net/rest/api/3/attachment/content/202"
    respx.get(content_url).mock(return_value=httpx.Response(200, content=b"bytes"))

    client = JiraClient(JIRA_BASE_URL, JIRA_AUTH_HEADER, JIRA_ALLOWED_HOSTS)
    whisper = WhisperManager()
    log_messages: list[str] = []

    with patch("icx_engine.connectors.audio.transcribe",
               new=AsyncMock(side_effect=Exception("boom"))):
        fname, text, b64 = await _process_audio(
            "meeting.mp3", content_url, client, None, whisper, log_messages.append
        )

    assert fname == "meeting.mp3"
    assert text == ""
    assert b64 == ""
    assert any("transcription failed" in m for m in log_messages), (
        f"Expected 'transcription failed' log message but got: {log_messages}"
    )
    assert any("boom" in m for m in log_messages), (
        f"Expected exception message 'boom' in log but got: {log_messages}"
    )


@respx.mock
async def test_process_audio_returns_setup_msg_when_whisper_not_installed():
    """_process_audio must surface _SETUP_REQUIRED_MSG when Whisper raises setup-required RuntimeError."""
    from icx_engine.connectors.attachments import _process_audio, _SETUP_REQUIRED_MSG
    from icx_engine.connectors.audio import WhisperManager

    content_url = "https://test.atlassian.net/rest/api/3/attachment/content/203"
    respx.get(content_url).mock(return_value=httpx.Response(200, content=b"bytes"))

    client = JiraClient(JIRA_BASE_URL, JIRA_AUTH_HEADER, JIRA_ALLOWED_HOSTS)
    whisper = WhisperManager()
    log_messages: list[str] = []

    with patch("icx_engine.connectors.audio.transcribe",
               new=AsyncMock(side_effect=RuntimeError(
                   "Whisper model not found. Run `icx setup` to download it."
               ))):
        fname, text, b64 = await _process_audio(
            "meeting.mp3", content_url, client, None, whisper, log_messages.append
        )

    assert fname == "meeting.mp3"
    assert text == _SETUP_REQUIRED_MSG
    assert b64 == ""
    assert any("icx setup" in m for m in log_messages)


# ── _process_video ─────────────────────────────────────────────────────────────

@respx.mock
async def test_process_video_extracts_audio_then_transcribes():
    from icx_engine.connectors.attachments import _process_video
    from icx_engine.connectors.audio import WhisperManager

    content_url = "https://test.atlassian.net/rest/api/3/attachment/content/300"
    respx.get(content_url).mock(return_value=httpx.Response(200, content=b"fake mp4 bytes"))

    client = JiraClient(JIRA_BASE_URL, JIRA_AUTH_HEADER, JIRA_ALLOWED_HOSTS)
    whisper = WhisperManager()

    with patch("icx_engine.connectors.attachments._extract_audio_from_video", new=AsyncMock(return_value=b"\x00" * 44)), \
         patch("icx_engine.connectors.audio.transcribe", new=AsyncMock(return_value="Video transcript.")):
        fname, text, b64 = await _process_video("demo.mp4", content_url, client, None, None, whisper, None)

    assert fname == "demo.mp4"
    assert text == "Video transcript."
    assert b64 == ""


@respx.mock
async def test_process_video_returns_empty_on_extraction_error():
    from icx_engine.connectors.attachments import _process_video
    from icx_engine.connectors.audio import WhisperManager

    content_url = "https://test.atlassian.net/rest/api/3/attachment/content/301"
    respx.get(content_url).mock(return_value=httpx.Response(200, content=b"bytes"))

    client = JiraClient(JIRA_BASE_URL, JIRA_AUTH_HEADER, JIRA_ALLOWED_HOSTS)
    whisper = WhisperManager()

    with patch("icx_engine.connectors.attachments._extract_audio_from_video",
               new=AsyncMock(side_effect=Exception("ffmpeg failed"))), \
         patch("icx_engine.connectors.attachments._extract_frames_from_video",
               new=AsyncMock(return_value=[])):
        fname, text, b64 = await _process_video("demo.mp4", content_url, client, None, None, whisper, None)

    assert text == ""


@respx.mock
async def test_process_video_passes_wav_fname_without_double_extension():
    """transcribe must receive 'demo.wav', not 'demo.mp4.wav'."""
    from icx_engine.connectors.attachments import _process_video
    from icx_engine.connectors.audio import WhisperManager

    content_url = "https://test.atlassian.net/rest/api/3/attachment/content/302"
    respx.get(content_url).mock(return_value=httpx.Response(200, content=b"mp4 bytes"))

    client = JiraClient(JIRA_BASE_URL, JIRA_AUTH_HEADER, JIRA_ALLOWED_HOSTS)
    whisper = WhisperManager()

    transcribe_mock = AsyncMock(return_value="Video transcript.")
    with patch("icx_engine.connectors.attachments._extract_audio_from_video",
               new=AsyncMock(return_value=b"\x00" * 44)), \
         patch("icx_engine.connectors.audio.transcribe", new=transcribe_mock):
        await _process_video("demo.mp4", content_url, client, None, None, whisper, None)

    # transcribe is called as: transcribe(text_config, audio_bytes, fname, whisper)
    fname_arg = transcribe_mock.call_args[0][2]
    assert fname_arg == "demo.wav", f"Expected 'demo.wav' but got '{fname_arg}'"


@respx.mock
async def test_process_video_returns_empty_when_no_audio_track():
    """Videos with no audio track produce very small WAV bytes; must be caught and logged."""
    from icx_engine.connectors.attachments import _process_video
    from icx_engine.connectors.audio import WhisperManager

    content_url = "https://test.atlassian.net/rest/api/3/attachment/content/303"
    respx.get(content_url).mock(return_value=httpx.Response(200, content=b"mp4 bytes"))

    client = JiraClient(JIRA_BASE_URL, JIRA_AUTH_HEADER, JIRA_ALLOWED_HOSTS)
    whisper = WhisperManager()
    log_messages: list[str] = []

    # ffmpeg exits 0 but returns minimal/empty WAV (< 44 bytes = no real audio)
    empty_wav = b"\x00" * 10
    with patch("icx_engine.connectors.attachments._extract_audio_from_video",
               new=AsyncMock(return_value=empty_wav)), \
         patch("icx_engine.connectors.attachments._extract_frames_from_video",
               new=AsyncMock(return_value=[])):
        fname, text, b64 = await _process_video(
            "silent.mp4", content_url, client, None, None, whisper, log_messages.append
        )

    assert fname == "silent.mp4"
    assert text == ""
    assert any("no audio" in m.lower() for m in log_messages), (
        f"Expected 'no audio' log message but got: {log_messages}"
    )


@respx.mock
async def test_process_video_returns_setup_msg_when_whisper_not_installed():
    """_process_video must surface _SETUP_REQUIRED_MSG when Whisper raises setup-required RuntimeError.

    Frame extraction still runs (it uses vision/OCR, not Whisper). When no frames
    are available, only the setup message is returned.
    """
    from icx_engine.connectors.attachments import _process_video, _SETUP_REQUIRED_MSG
    from icx_engine.connectors.audio import WhisperManager

    content_url = "https://test.atlassian.net/rest/api/3/attachment/content/311"
    respx.get(content_url).mock(return_value=httpx.Response(200, content=b"fake mp4 bytes"))

    client = JiraClient(JIRA_BASE_URL, JIRA_AUTH_HEADER, JIRA_ALLOWED_HOSTS)
    whisper = WhisperManager()
    log_messages: list[str] = []

    with patch("icx_engine.connectors.attachments._extract_audio_from_video",
               new=AsyncMock(return_value=b"\x00" * 100)), \
         patch("icx_engine.connectors.audio.transcribe",
               new=AsyncMock(side_effect=RuntimeError(
                   "Whisper model not found. Run `icx setup` to download it."
               ))), \
         patch("icx_engine.connectors.attachments._extract_frames_from_video",
               new=AsyncMock(return_value=[])):
        fname, text, b64 = await _process_video(
            "demo.mp4", content_url, client, None, None, whisper, log_messages.append
        )

    assert fname == "demo.mp4"
    assert text == _SETUP_REQUIRED_MSG
    assert b64 == ""
    assert any("icx setup" in m for m in log_messages)


# ── _extract_audio_from_video safety: timeout + returncode ────────────────────

async def test_extract_audio_from_video_kills_ffmpeg_on_timeout():
    """asyncio.wait_for timeout must kill the ffmpeg subprocess, not orphan it."""
    import asyncio as _asyncio
    from icx_engine.connectors.attachments import _extract_audio_from_video

    proc = MagicMock()
    proc.communicate = AsyncMock(side_effect=_asyncio.TimeoutError)
    proc.kill = MagicMock()
    proc.wait = AsyncMock(return_value=0)

    async def _make_subproc(*args, **kwargs):
        return proc

    with patch("imageio_ffmpeg.get_ffmpeg_exe", return_value="ffmpeg"), \
         patch("asyncio.create_subprocess_exec", new=_make_subproc):
        with pytest.raises(_asyncio.TimeoutError):
            await _extract_audio_from_video(b"fake video", "demo.mp4")

    proc.kill.assert_called_once()
    proc.wait.assert_awaited()


async def test_extract_audio_from_video_raises_on_nonzero_returncode():
    """Non-zero ffmpeg exit code must raise RuntimeError, not silently return empty bytes."""
    from icx_engine.connectors.attachments import _extract_audio_from_video

    proc = MagicMock()
    proc.communicate = AsyncMock(return_value=(b"", b"bad codec error"))
    proc.returncode = 1

    async def _make_subproc(*args, **kwargs):
        return proc

    with patch("imageio_ffmpeg.get_ffmpeg_exe", return_value="ffmpeg"), \
         patch("asyncio.create_subprocess_exec", new=_make_subproc):
        with pytest.raises(RuntimeError, match="ffmpeg exited with code 1"):
            await _extract_audio_from_video(b"fake video", "demo.mp4")


# ── process_attachments with audio/video ──────────────────────────────────────

@respx.mock
async def test_process_attachments_transcribes_audio_attachment():
    content_url = "https://test.atlassian.net/rest/api/3/attachment/content/400"
    respx.get(content_url).mock(return_value=httpx.Response(200, content=b"mp3 bytes"))

    raw = _make_raw({"meeting.mp3": content_url})
    client = JiraClient(JIRA_BASE_URL, JIRA_AUTH_HEADER, JIRA_ALLOWED_HOSTS)

    with patch("icx_engine.connectors.audio.transcribe", new=AsyncMock(return_value="Meeting transcript.")):
        texts, images = await process_attachments(raw, client, llm_config=None)

    assert texts == {"meeting.mp3": "Meeting transcript."}
    assert images == {}


@respx.mock
async def test_process_attachments_transcribes_video_attachment():
    content_url = "https://test.atlassian.net/rest/api/3/attachment/content/401"
    respx.get(content_url).mock(return_value=httpx.Response(200, content=b"mp4 bytes"))

    raw = _make_raw({"demo.mp4": content_url})
    client = JiraClient(JIRA_BASE_URL, JIRA_AUTH_HEADER, JIRA_ALLOWED_HOSTS)

    with patch("icx_engine.connectors.attachments._extract_audio_from_video", new=AsyncMock(return_value=b"\x00" * 44)), \
         patch("icx_engine.connectors.audio.transcribe", new=AsyncMock(return_value="Demo video transcript.")):
        texts, images = await process_attachments(raw, client, llm_config=None)

    assert texts == {"demo.mp4": "Demo video transcript."}
    assert images == {}


@respx.mock
async def test_process_attachments_handles_mixed_image_audio_video_doc():
    img_url = "https://test.atlassian.net/rest/api/3/attachment/content/500"
    audio_url = "https://test.atlassian.net/rest/api/3/attachment/content/501"
    video_url = "https://test.atlassian.net/rest/api/3/attachment/content/502"
    doc_url = "https://test.atlassian.net/rest/api/3/attachment/content/503"

    fake_png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 50
    respx.get(img_url).mock(return_value=httpx.Response(200, content=fake_png))
    respx.get(audio_url).mock(return_value=httpx.Response(200, content=b"mp3"))
    respx.get(video_url).mock(return_value=httpx.Response(200, content=b"mp4"))
    respx.get(doc_url).mock(return_value=httpx.Response(200, content=b"A,B\n1,2\n"))

    raw = _make_raw({
        "screen.png": img_url,
        "note.mp3": audio_url,
        "demo.mp4": video_url,
        "data.csv": doc_url,
    })
    client = JiraClient(JIRA_BASE_URL, JIRA_AUTH_HEADER, JIRA_ALLOWED_HOSTS)

    with patch("icx_engine.connectors.attachments.ocr_image", return_value="OCR"), \
         patch("icx_engine.connectors.attachments._extract_audio_from_video", new=AsyncMock(return_value=b"\x00" * 44)), \
         patch("icx_engine.connectors.audio.transcribe", new=AsyncMock(return_value="audio text")):
        texts, images = await process_attachments(raw, client, llm_config=None)

    assert "screen.png" in texts
    assert "note.mp3" in texts
    assert texts["note.mp3"] == "audio text"
    assert "demo.mp4" in texts
    assert texts["demo.mp4"] == "audio text"
    assert "data.csv" in texts
    assert "screen.png" in images
    assert "note.mp3" not in images
    assert "demo.mp4" not in images
