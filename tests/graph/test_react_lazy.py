"""Tests for React.lazy edge detection in react.py."""
from __future__ import annotations
from pathlib import Path
import pytest


def _fixture_root() -> Path:
    return Path(__file__).parent / "eval" / "fixtures" / "react_lazy_sample"


def _make_ast_extraction(root: Path) -> dict:
    src = root / "src"
    nodes = [
        {
            "id": "src_app_jsx",
            "label": "App.jsx",
            "source_file": str(src / "App.jsx"),
            "file_type": "code",
        },
        {
            "id": "src_pages_dashboard_jsx",
            "label": "Dashboard.jsx",
            "source_file": str(src / "pages" / "Dashboard.jsx"),
            "file_type": "code",
        },
        {
            "id": "src_pages_settings_jsx",
            "label": "Settings.jsx",
            "source_file": str(src / "pages" / "Settings.jsx"),
            "file_type": "code",
        },
    ]
    return {"nodes": nodes, "edges": []}


def test_lazy_import_emits_lazy_loads_edge():
    from icx_engine.graph.parser.resolvers.react import extract_react_edges

    root = _fixture_root()
    if not root.exists():
        pytest.skip("fixture not found")

    files = list((root / "src").rglob("*.jsx"))
    extraction = _make_ast_extraction(root)
    edges = extract_react_edges(files, root, extraction)

    lazy_edges = [e for e in edges if e.get("relation") == "lazy_loads"]
    assert len(lazy_edges) == 2, f"expected 2 lazy_loads edges, got {len(lazy_edges)}: {lazy_edges}"


def test_lazy_edge_tagged_react_lazy():
    from icx_engine.graph.parser.resolvers.react import extract_react_edges

    root = _fixture_root()
    if not root.exists():
        pytest.skip("fixture not found")

    files = list((root / "src").rglob("*.jsx"))
    extraction = _make_ast_extraction(root)
    edges = extract_react_edges(files, root, extraction)

    lazy_edges = [e for e in edges if e.get("relation") == "lazy_loads"]
    for e in lazy_edges:
        assert e.get("confidence_source") == "react_lazy"
        assert e.get("confidence_score") == 0.95


def test_lazy_edge_source_is_app_file():
    from icx_engine.graph.parser.resolvers.react import extract_react_edges

    root = _fixture_root()
    if not root.exists():
        pytest.skip("fixture not found")

    files = list((root / "src").rglob("*.jsx"))
    extraction = _make_ast_extraction(root)
    edges = extract_react_edges(files, root, extraction)

    lazy_edges = [e for e in edges if e.get("relation") == "lazy_loads"]
    sources = {e["source"] for e in lazy_edges}
    assert "src_app_jsx" in sources
