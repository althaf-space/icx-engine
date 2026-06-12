"""Tests for Phase 4: Go resolver with implicit interface matching."""
import pytest
from pathlib import Path
from icx_engine.graph.parser.resolvers.go_resolver import (
    resolve_go,
    _extract_interfaces,
    _extract_struct_methods,
    _read_module_name,
)


def _node(node_id, source_file, name=None):
    return {"id": node_id, "source_file": source_file, "file": source_file, "name": name or node_id}


class TestReadModuleName:
    def test_reads_module_from_go_mod(self, tmp_path):
        (tmp_path / "go.mod").write_text("module github.com/user/myrepo\n\ngo 1.21\n")
        assert _read_module_name(tmp_path) == "github.com/user/myrepo"

    def test_missing_go_mod_returns_empty(self, tmp_path):
        assert _read_module_name(tmp_path) == ""


class TestImplicitInterface:
    def test_struct_with_all_methods_implements_interface(self, tmp_path):
        iface_file = tmp_path / "reader.go"
        iface_file.write_text("package main\ntype Reader interface {\n\tRead(p []byte) (int, error)\n\tClose() error\n}")
        struct_file = tmp_path / "file.go"
        struct_file.write_text("package main\ntype File struct{}\nfunc (f *File) Read(p []byte) (int, error) { return 0, nil }\nfunc (f *File) Close() error { return nil }")

        iface_rel = str(iface_file.relative_to(tmp_path).as_posix())
        struct_rel = str(struct_file.relative_to(tmp_path).as_posix())
        nodes = [_node("iface_n", iface_rel, "Reader"), _node("struct_n", struct_rel, "File")]
        edges = resolve_go([iface_file, struct_file], tmp_path, {"nodes": nodes})
        types = [e["type"] for e in edges]
        assert "go_implements" in types
        impl = [e for e in edges if e["type"] == "go_implements"][0]
        assert impl["confidence"] == 0.75

    def test_struct_missing_method_no_implements(self, tmp_path):
        iface_file = tmp_path / "reader.go"
        iface_file.write_text("package main\ntype Reader interface {\n\tRead(p []byte) (int, error)\n\tClose() error\n}")
        struct_file = tmp_path / "partial.go"
        struct_file.write_text("package main\ntype Partial struct{}\nfunc (p *Partial) Read(b []byte) (int, error) { return 0, nil }")

        iface_rel = str(iface_file.relative_to(tmp_path).as_posix())
        struct_rel = str(struct_file.relative_to(tmp_path).as_posix())
        nodes = [_node("iface_n", iface_rel, "Reader"), _node("struct_n", struct_rel, "Partial")]
        edges = resolve_go([iface_file, struct_file], tmp_path, {"nodes": nodes})
        assert not any(e["type"] == "go_implements" for e in edges)


class TestGoImport:
    def test_single_import_creates_go_import_edge(self, tmp_path):
        (tmp_path / "go.mod").write_text("module github.com/user/repo\ngo 1.21\n")
        (tmp_path / "pkg").mkdir()
        pkg_file = tmp_path / "pkg" / "util.go"
        pkg_file.write_text("package pkg\nfunc Helper() {}")
        main_file = tmp_path / "main.go"
        main_file.write_text('package main\nimport "github.com/user/repo/pkg"\nfunc main() { pkg.Helper() }')

        pkg_rel = str(pkg_file.relative_to(tmp_path).as_posix())
        main_rel = str(main_file.relative_to(tmp_path).as_posix())
        nodes = [_node("main_n", main_rel), _node("pkg_n", pkg_rel)]
        edges = resolve_go([main_file, pkg_file], tmp_path, {"nodes": nodes})
        types = [e["type"] for e in edges]
        assert "go_import" in types
        imp = [e for e in edges if e["type"] == "go_import"][0]
        assert imp["confidence"] == 0.90

    def test_no_go_files_returns_empty(self):
        result = resolve_go([], Path("."), {"nodes": []})
        assert result == []


class TestGoCalls:
    def test_intra_package_function_call_creates_edge(self, tmp_path):
        # Two files in same package: fileA calls function defined in fileB
        file_a = tmp_path / "handler.go"
        file_a.write_text("package main\nfunc Handler() { validate() }")
        file_b = tmp_path / "validator.go"
        file_b.write_text("package main\nfunc validate() bool { return true }")

        a_rel = str(file_a.relative_to(tmp_path).as_posix())
        b_rel = str(file_b.relative_to(tmp_path).as_posix())
        nodes = [_node("handler_n", a_rel), _node("validator_n", b_rel)]
        edges = resolve_go([file_a, file_b], tmp_path, {"nodes": nodes})
        types = [e["type"] for e in edges]
        assert "go_calls" in types
        call_edge = [e for e in edges if e["type"] == "go_calls"][0]
        assert call_edge["confidence"] == 0.85


class TestGoEdgeSchema:
    def test_all_edges_have_relation_field(self, tmp_path):
        (tmp_path / "go.mod").write_text("module github.com/user/repo\ngo 1.21\n")
        (tmp_path / "pkg").mkdir()
        pkg_file = tmp_path / "pkg" / "util.go"
        pkg_file.write_text("package pkg\nfunc Helper() {}")
        main_file = tmp_path / "main.go"
        main_file.write_text('package main\nimport "github.com/user/repo/pkg"\nfunc main() { pkg.Helper() }')
        pkg_rel = str(pkg_file.relative_to(tmp_path).as_posix())
        main_rel = str(main_file.relative_to(tmp_path).as_posix())
        nodes = [_node("main_n", main_rel), _node("pkg_n", pkg_rel)]
        edges = resolve_go([main_file, pkg_file], tmp_path, {"nodes": nodes})
        assert edges
        assert all("relation" in e for e in edges)
        assert all(e["relation"] == e["type"] for e in edges)
