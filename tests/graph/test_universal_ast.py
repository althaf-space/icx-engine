"""Tests for universal_ast resolver. Creates temp source files per language."""
from __future__ import annotations
import tempfile
from pathlib import Path
import pytest


def _run(source: str, suffix: str) -> list[dict]:
    """Write source to a temp file and run the universal_ast resolver on it."""
    from icx_engine.graph.parser.resolvers.universal_ast import extract_universal_ast_edges
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        f = root / f"sample{suffix}"
        f.write_text(source, encoding="utf-8")
        ast_ext = {"nodes": [], "edges": []}
        return extract_universal_ast_edges([f], root, ast_ext)


def test_skips_python_files():
    """Python files are handled by deep resolvers - universal_ast must skip them."""
    edges = _run("def foo(): pass\nimport os", ".py")
    assert edges == []


def test_skips_java_files():
    edges = _run("public class Foo {}", ".java")
    assert edges == []


def test_go_imports():
    """Go import statement produces an import edge."""
    pytest.importorskip("tree_sitter_go")
    src = '''package main
import "fmt"
func main() {}
'''
    edges = _run(src, ".go")
    import_edges = [e for e in edges if e["relation"] == "imports"]
    assert len(import_edges) >= 1
    targets = [e["target"] for e in import_edges]
    assert any("fmt" in t for t in targets)


def test_go_confidence():
    """All edges from universal_ast have confidence_score == 0.55."""
    pytest.importorskip("tree_sitter_go")
    src = 'package main\nimport "os"\nfunc run() {}'
    edges = _run(src, ".go")
    assert edges, "expected at least one edge"
    for e in edges:
        assert e["confidence_score"] == pytest.approx(0.55)
        assert e["confidence_source"] == "universal_ast"


def test_rust_imports():
    pytest.importorskip("tree_sitter_rust")
    src = "use std::io;\nfn main() {}"
    edges = _run(src, ".rs")
    import_edges = [e for e in edges if e["relation"] == "imports"]
    assert any("io" in e["target"] for e in import_edges)


def test_csharp_imports():
    pytest.importorskip("tree_sitter_c_sharp")
    src = "using System;\nclass Foo {}"
    edges = _run(src, ".cs")
    assert len(edges) >= 1


def test_ruby_imports():
    pytest.importorskip("tree_sitter_ruby")
    src = "require 'json'\nclass Foo; end"
    edges = _run(src, ".rb")
    import_edges = [e for e in edges if e["relation"] == "imports"]
    assert any("json" in e["target"] for e in import_edges)


def test_missing_grammar_silent_skip():
    """If a grammar package is not installed, should return [] not raise."""
    from icx_engine.graph.parser.resolvers.universal_ast import extract_universal_ast_edges
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        f = root / "sample.unknownlang"
        f.write_text("hello world", encoding="utf-8")
        result = extract_universal_ast_edges([f], root, {"nodes": [], "edges": []})
        assert result == []


def test_resolver_tag_is_universal_ast():
    src = 'package main\nimport "fmt"\nfunc main() {}'
    edges = _run(src, ".go")
    for e in edges:
        assert e.get("resolver_tag") == "universal_ast"
