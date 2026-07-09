from icx_engine.connectors.attachments import (
    _rows_to_markdown, _convert_csv, _convert_document,
)


def _rows(n):
    return [["h1", "h2"]] + [[f"a{i}", f"b{i}"] for i in range(n)]


def test_rows_to_markdown_default_caps_at_50():
    md = _rows_to_markdown(_rows(120))
    assert md.count("\n| a") <= 50  # 50 data rows shown (approx via cell prefix)
    assert "Content truncated" in md


def test_rows_to_markdown_none_is_uncapped():
    md = _rows_to_markdown(_rows(120), max_rows=None)
    assert "Content truncated" not in md
    assert "a119" in md  # last row present


def test_convert_csv_uncapped_has_all_rows():
    data = ("h1,h2\n" + "\n".join(f"a{i},b{i}" for i in range(100))).encode()
    capped = _convert_csv(data)
    full = _convert_csv(data, max_rows=None)
    assert "Content truncated" in capped
    assert "Content truncated" not in full
    assert "a99" in full


def test_convert_document_passes_max_rows_for_csv():
    data = ("h1,h2\n" + "\n".join(f"a{i},b{i}" for i in range(100))).encode()
    full_text, imgs = _convert_document("data.csv", data, max_rows=None)
    assert "a99" in full_text
    assert imgs == []


import asyncio
import base64
from icx_engine.models.output import RawIssueData
from icx_engine.connectors.attachments import process_attachments


class _FakeDownloader:
    def __init__(self, blobs):
        self._blobs = blobs

    async def download_attachment(self, url):
        return self._blobs[url]


def _raw_with(urls):
    return RawIssueData(
        issue_key="P-1", issue_type="Bug", summary="s", description="d",
        comments=[], attachments=list(urls), priority="High", status="Open",
        metadata={}, attachment_content_urls={f: f for f in urls},
    )


def test_process_attachments_returns_four_maps_csv_and_html():
    csv_bytes = ("h1,h2\n" + "\n".join(f"a{i},b{i}" for i in range(80))).encode()
    html_bytes = b"<html><body>hi</body></html>"
    raw = _raw_with(["data.csv", "page.html"])
    dl = _FakeDownloader({"data.csv": csv_bytes, "page.html": html_bytes})
    texts, images, full_texts, raw_map = asyncio.run(
        process_attachments(raw, dl, None)
    )
    # csv: capped inline truncated, full uncapped complete, raw present
    assert "Content truncated" in texts["data.csv"]
    assert "a79" in full_texts["data.csv"]
    assert "Content truncated" not in full_texts["data.csv"]
    assert base64.b64decode(raw_map["data.csv"]) == csv_bytes
    # html: full_text + raw present
    assert "page.html" in full_texts
    assert base64.b64decode(raw_map["page.html"]) == html_bytes
    assert images == {}


def test_process_attachments_image_has_no_full_text_or_raw():
    # Any bytes work: with no image_config and likely no tesseract, OCR returns "",
    # and images never produce a full_text sidecar or raw entry regardless.
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 32
    raw = _raw_with(["shot.png"])
    dl = _FakeDownloader({"shot.png": png})
    texts, images, full_texts, raw_map = asyncio.run(
        process_attachments(raw, dl, None)
    )
    assert "shot.png" not in full_texts
    assert "shot.png" not in raw_map
