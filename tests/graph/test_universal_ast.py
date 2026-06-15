"""Tests for universal_ast resolver. Creates temp source files per language."""
from __future__ import annotations
import sys
import tempfile
from pathlib import Path
import pytest

from icx_engine.graph.parser.resolvers.universal_ast import _walk


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


class _FakeNode:
    """Minimal stand-in for a tree-sitter Node: has .type and .children."""

    def __init__(self, node_type: str, children: list["_FakeNode"] | None = None) -> None:
        self.type = node_type
        self.children = children or []


class TestWalkOrder:
    def test_preorder_visit_order_matches_recursive(self):
        root = _FakeNode("root", [
            _FakeNode("a", [_FakeNode("a1"), _FakeNode("a2")]),
            _FakeNode("b", [_FakeNode("b1")]),
        ])
        visited: list[tuple[str, int]] = []
        _walk(root, lambda node, depth: visited.append((node.type, depth)))
        assert visited == [
            ("root", 0),
            ("a", 1),
            ("a1", 2),
            ("a2", 2),
            ("b", 1),
            ("b1", 2),
        ]


class TestWalkDeepTree:
    def test_deep_chain_does_not_raise_recursion_error(self):
        """A chain deeper than Python's default recursion limit must not raise."""
        depth = sys.getrecursionlimit() + 500
        root = _FakeNode("leaf")
        for _ in range(depth):
            root = _FakeNode("node", [root])

        visited_types = []
        _walk(root, lambda node, depth: visited_types.append(node.type))

        assert len(visited_types) == depth + 1
        assert visited_types[0] == "node"
        assert visited_types[-1] == "leaf"
