import io
import pytest
import respx
import httpx
from unittest.mock import patch, MagicMock, AsyncMock

from icx_engine.connectors.attachments import (
    _is_image, _is_document, _convert_csv, _convert_txt, _rows_to_markdown,
    _convert_xlsx, _VISION_PROMPT, _SUMMARIZE_SYSTEM,
    ocr_image, vision_enrich, process_attachments,
    _convert_document, _process_image, _process_document,
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

    # Data rows 1–3 (val_rows indices 1–3) must be annotated
    assert "(Formula: =A2*10)" in result
    assert "(Formula: =A3*10)" in result
    assert "(Formula: =A4*10)" in result
    # Data rows 4–5 (val_rows indices 4–5) must NOT be annotated
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
