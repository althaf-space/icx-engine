"""Tests for upgrade_inferred_edges in java_symbols."""
from __future__ import annotations
from pathlib import Path
import pytest


def test_inferred_edge_promoted_when_target_is_known_file(tmp_path):
    from icx_engine.graph.parser.resolvers.java_symbols import upgrade_inferred_edges

    src = tmp_path / "src"
    src.mkdir()
    (src / "A.java").write_text("package src; class A {}", encoding="utf-8")
    (src / "B.java").write_text("package src; class B {}", encoding="utf-8")

    nodes = [
        {"id": "file_a", "label": "A.java", "source_file": str(src / "A.java"), "file_type": "code"},
        {"id": "file_b", "label": "B.java", "source_file": str(src / "B.java"), "file_type": "code"},
    ]
    edges = [
        {
            "source": "file_a",
            "target": "file_b",
            "relation": "uses",
            "source_file": str(src / "A.java"),
            "confidence_score": 0.55,
            "confidence": "INFERRED",
        }
    ]
    extraction = {"nodes": nodes, "edges": edges}

    upgrade_inferred_edges(extraction, tmp_path, [src / "A.java", src / "B.java"])

    upgraded = extraction["edges"][0]
    assert upgraded["confidence_score"] == 1.0
    assert upgraded["confidence_source"] == "java_symbols_validated"
    assert upgraded["confidence"] == "EXTRACTED"


def test_inferred_edge_not_promoted_for_unknown_target(tmp_path):
    from icx_engine.graph.parser.resolvers.java_symbols import upgrade_inferred_edges

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "A.java").write_text("package src; class A {}", encoding="utf-8")
    src_a = str(tmp_path / "src" / "A.java")

    nodes = [
        {"id": "file_a", "label": "A.java", "source_file": src_a, "file_type": "code"},
    ]
    edges = [
        {
            "source": "file_a",
            "target": "external_unknown_node",
            "relation": "uses",
            "source_file": src_a,
            "confidence_score": 0.55,
            "confidence": "INFERRED",
        }
    ]
    extraction = {"nodes": nodes, "edges": edges}

    upgrade_inferred_edges(extraction, tmp_path, [tmp_path / "src" / "A.java"])

    assert extraction["edges"][0]["confidence_score"] == 0.55


def test_extracted_edges_not_touched(tmp_path):
    from icx_engine.graph.parser.resolvers.java_symbols import upgrade_inferred_edges

    p = tmp_path / "src"
    p.mkdir(exist_ok=True)
    for name in ("A.java", "B.java"):
        (p / name).write_text(f"package src; class {name[:-5]} {{}}", encoding="utf-8")

    nodes = [
        {"id": "file_a", "label": "A.java", "source_file": str(p / "A.java"), "file_type": "code"},
        {"id": "file_b", "label": "B.java", "source_file": str(p / "B.java"), "file_type": "code"},
    ]
    edges = [
        {
            "source": "file_a",
            "target": "file_b",
            "relation": "imports",
            "source_file": str(p / "A.java"),
            "confidence_score": 1.0,
            "confidence": "EXTRACTED",
        }
    ]
    extraction = {"nodes": nodes, "edges": edges}

    upgrade_inferred_edges(extraction, tmp_path, [tmp_path / "src" / "A.java"])

    assert extraction["edges"][0]["confidence_score"] == 1.0
    assert extraction["edges"][0].get("confidence_source") != "java_symbols_validated"
