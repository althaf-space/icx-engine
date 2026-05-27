"""Tests for graph/parser/validate.py - schema validation."""
from __future__ import annotations

import pytest
from icx_engine.graph.parser.validate import validate_extraction, assert_valid, REQUIRED_EDGE_FIELDS, REQUIRED_NODE_FIELDS


# ---------------------------------------------------------------------------
# REQUIRED_EDGE_FIELDS contract
# ---------------------------------------------------------------------------

def test_required_edge_fields_does_not_include_source_file():
    """source_file is optional on edges - nodes already carry it."""
    assert "source_file" not in REQUIRED_EDGE_FIELDS


def test_required_edge_fields_includes_core_relationship_fields():
    assert {"source", "target", "relation", "confidence"} == REQUIRED_EDGE_FIELDS


def test_required_node_fields_includes_source_file():
    assert "source_file" in REQUIRED_NODE_FIELDS


# ---------------------------------------------------------------------------
# validate_extraction - valid cases
# ---------------------------------------------------------------------------

def _minimal_valid() -> dict:
    return {
        "nodes": [
            {"id": "a", "label": "A", "file_type": "code", "source_file": "a.py"},
        ],
        "edges": [
            {"source": "a", "target": "a", "relation": "calls", "confidence": "EXTRACTED"},
        ],
    }


def test_valid_extraction_returns_no_errors():
    assert validate_extraction(_minimal_valid()) == []


def test_edge_without_source_file_is_valid():
    data = _minimal_valid()
    errors = validate_extraction(data)
    assert errors == []


def test_edge_with_source_file_is_also_valid():
    data = _minimal_valid()
    data["edges"][0]["source_file"] = "a.py"
    errors = validate_extraction(data)
    assert errors == []


def test_links_key_accepted_as_edge_list():
    data = {
        "nodes": [{"id": "n", "label": "N", "file_type": "code", "source_file": "n.py"}],
        "links": [{"source": "n", "target": "n", "relation": "uses", "confidence": "INFERRED"}],
    }
    assert validate_extraction(data) == []


# ---------------------------------------------------------------------------
# validate_extraction - invalid cases
# ---------------------------------------------------------------------------

def test_missing_nodes_key():
    errors = validate_extraction({"edges": []})
    assert any("nodes" in e for e in errors)


def test_missing_edges_key():
    errors = validate_extraction({"nodes": []})
    assert any("edges" in e for e in errors)


def test_edge_missing_source_field():
    data = _minimal_valid()
    del data["edges"][0]["source"]
    errors = validate_extraction(data)
    assert any("source" in e for e in errors)


def test_edge_missing_target_field():
    data = _minimal_valid()
    del data["edges"][0]["target"]
    errors = validate_extraction(data)
    assert any("target" in e for e in errors)


def test_edge_missing_relation_field():
    data = _minimal_valid()
    del data["edges"][0]["relation"]
    errors = validate_extraction(data)
    assert any("relation" in e for e in errors)


def test_edge_missing_confidence_field():
    data = _minimal_valid()
    del data["edges"][0]["confidence"]
    errors = validate_extraction(data)
    assert any("confidence" in e for e in errors)


def test_edge_invalid_confidence_value():
    data = _minimal_valid()
    data["edges"][0]["confidence"] = "CERTAIN"
    errors = validate_extraction(data)
    assert any("CERTAIN" in e for e in errors)


def test_node_missing_source_file():
    data = _minimal_valid()
    del data["nodes"][0]["source_file"]
    errors = validate_extraction(data)
    assert any("source_file" in e for e in errors)


def test_node_invalid_file_type():
    data = _minimal_valid()
    data["nodes"][0]["file_type"] = "spreadsheet"
    errors = validate_extraction(data)
    assert any("spreadsheet" in e for e in errors)


# ---------------------------------------------------------------------------
# assert_valid
# ---------------------------------------------------------------------------

def test_assert_valid_passes_on_clean_data():
    assert_valid(_minimal_valid())


def test_assert_valid_raises_on_invalid():
    bad = {"nodes": [], "edges": [{"source": "x", "target": "y"}]}
    with pytest.raises(ValueError, match="error"):
        assert_valid(bad)
