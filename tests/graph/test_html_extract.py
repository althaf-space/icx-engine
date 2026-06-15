"""Tests for the minimal .html extractor used to anchor Angular template edges."""
from pathlib import Path

from icx_engine.graph.parser.extract import extract_html, _get_extractor


def test_extract_html_emits_single_file_node(tmp_path):
    html_file = tmp_path / "app.component.html"
    html_file.write_text("<h1>Hello</h1>\n")

    result = extract_html(html_file)

    assert result["edges"] == []
    assert len(result["nodes"]) == 1
    node = result["nodes"][0]
    assert node["name"] == "app.component.html"
    assert node["source_file"] == str(html_file)


def test_html_extension_dispatches_to_extract_html(tmp_path):
    html_file = tmp_path / "index.html"
    html_file.write_text("<div></div>\n")

    assert _get_extractor(html_file) is extract_html
