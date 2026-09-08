"""Tests for graph/parser/ui_text.py - extracting literal, user-visible UI text so
graph_find_context can match a ticket description against what a user actually saw on
screen, not just code identifiers. No LLM calls, no embeddings - pure deterministic text
extraction."""
from __future__ import annotations
from pathlib import Path


def test_extracts_jsx_tag_text(tmp_path: Path):
    from icx_engine.graph.parser.ui_text import extract_ui_text

    f = tmp_path / "LoyaltyPoints.jsx"
    f.write_text(
        '<div className="title">Loyalty Points-Earned V/S Redemption</div>\n'
        '<h1>Start Date</h1>\n',
        encoding="utf-8",
    )
    result = extract_ui_text([f], tmp_path)
    assert "Loyalty Points-Earned V/S Redemption" in result["LoyaltyPoints.jsx"]
    assert "Start Date" in result["LoyaltyPoints.jsx"]


def test_extracts_accessibility_attribute_text(tmp_path: Path):
    from icx_engine.graph.parser.ui_text import extract_ui_text

    f = tmp_path / "Search.jsx"
    f.write_text(
        '<input placeholder="Search Reports" aria-label="Report search box" />\n',
        encoding="utf-8",
    )
    result = extract_ui_text([f], tmp_path)
    assert "Search Reports" in result["Search.jsx"]
    assert "Report search box" in result["Search.jsx"]


def test_excludes_jsx_expression_interpolation(tmp_path: Path):
    """`{someVariable}` is not literal text a user would recognize - must never be
    extracted as if it were visible UI copy."""
    from icx_engine.graph.parser.ui_text import extract_ui_text

    f = tmp_path / "Dynamic.jsx"
    f.write_text(
        '<div>{userName}</div>\n'
        '<span>{count}</span>\n',
        encoding="utf-8",
    )
    result = extract_ui_text([f], tmp_path)
    assert "Dynamic.jsx" not in result


def test_excludes_short_code_looking_tokens(tmp_path: Path):
    """A bare short lowercase-hyphen token between tags (CSS-class-shaped, not real
    prose) must not count as extracted UI text."""
    from icx_engine.graph.parser.ui_text import extract_ui_text

    f = tmp_path / "Icon.jsx"
    f.write_text('<i>close-icon</i>\n', encoding="utf-8")
    result = extract_ui_text([f], tmp_path)
    assert "Icon.jsx" not in result


def test_ignores_non_ui_file_extensions(tmp_path: Path):
    from icx_engine.graph.parser.ui_text import extract_ui_text

    f = tmp_path / "notes.txt"
    f.write_text("Loyalty Points-Earned V/S Redemption", encoding="utf-8")
    result = extract_ui_text([f], tmp_path)
    assert result == {}


def test_never_raises_on_unreadable_file(tmp_path: Path):
    from icx_engine.graph.parser.ui_text import extract_ui_text

    missing = tmp_path / "does_not_exist.jsx"
    result = extract_ui_text([missing], tmp_path)  # must not raise
    assert result == {}


def test_multiple_text_fragments_joined_and_deduplicated(tmp_path: Path):
    from icx_engine.graph.parser.ui_text import extract_ui_text

    f = tmp_path / "Form.jsx"
    f.write_text(
        '<label>Start Date</label>\n'
        '<label>End Date</label>\n'
        '<label>Start Date</label>\n',  # duplicate - must appear only once
        encoding="utf-8",
    )
    result = extract_ui_text([f], tmp_path)
    text = result["Form.jsx"]
    assert text.count("Start Date") == 1
    assert "End Date" in text
