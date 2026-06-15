"""Tests for the Rust resolver: use-path, trait impl, and intra-directory calls."""
import pytest
from pathlib import Path
from icx_engine.graph.parser.resolvers.rust_resolver import resolve_rust


def _node(node_id, source_file, name=None):
    return {"id": node_id, "source_file": source_file, "file": source_file, "name": name or node_id}


class TestNoRustFiles:
    def test_no_rust_files_returns_empty(self):
        result = resolve_rust([], Path("."), {"nodes": []})
        assert result == []


class TestUsePath:
    def test_use_statement_creates_rust_use_edge(self, tmp_path):
        utils_file = tmp_path / "utils.rs"
        utils_file.write_text("pub fn helper() -> bool {\n    true\n}\n")
        main_file = tmp_path / "main.rs"
        main_file.write_text("use crate::utils::helper;\n\nfn main() {\n    helper();\n}\n")

        utils_rel = str(utils_file.relative_to(tmp_path).as_posix())
        main_rel = str(main_file.relative_to(tmp_path).as_posix())
        nodes = [_node("main_n", main_rel, "main"), _node("helper_n", utils_rel, "helper")]
        edges = resolve_rust([main_file, utils_file], tmp_path, {"nodes": nodes})

        types = [e["type"] for e in edges]
        assert "rust_use" in types
        use_edge = [e for e in edges if e["type"] == "rust_use"][0]
        assert use_edge["confidence"] == 0.90
        assert use_edge["source"] == "main_n"
        assert use_edge["target"] == "helper_n"

    def test_unresolved_use_creates_no_edge(self, tmp_path):
        main_file = tmp_path / "main.rs"
        main_file.write_text("use std::collections::HashMap;\n\nfn main() {\n}\n")

        main_rel = str(main_file.relative_to(tmp_path).as_posix())
        nodes = [_node("main_n", main_rel, "main")]
        edges = resolve_rust([main_file], tmp_path, {"nodes": nodes})

        assert not any(e["type"] == "rust_use" for e in edges)


class TestTraitImpl:
    def test_impl_trait_for_type_creates_rust_impl_edge(self, tmp_path):
        trait_file = tmp_path / "greet.rs"
        trait_file.write_text("pub trait Greet {\n    fn greet(&self) -> String;\n}\n")
        type_file = tmp_path / "dog.rs"
        type_file.write_text(
            "use crate::greet::Greet;\n\n"
            "pub struct Dog;\n\n"
            "impl Greet for Dog {\n"
            "    fn greet(&self) -> String {\n"
            "        String::from(\"Woof\")\n"
            "    }\n"
            "}\n"
        )

        trait_rel = str(trait_file.relative_to(tmp_path).as_posix())
        type_rel = str(type_file.relative_to(tmp_path).as_posix())
        nodes = [_node("greet_n", trait_rel, "Greet"), _node("dog_n", type_rel, "Dog")]
        edges = resolve_rust([trait_file, type_file], tmp_path, {"nodes": nodes})

        types = [e["type"] for e in edges]
        assert "rust_impl" in types
        impl_edge = [e for e in edges if e["type"] == "rust_impl"][0]
        assert impl_edge["confidence"] == 0.80
        assert impl_edge["source"] == "dog_n"
        assert impl_edge["target"] == "greet_n"

    def test_impl_trait_declared_in_same_file_creates_no_edge(self, tmp_path):
        combined_file = tmp_path / "lib.rs"
        combined_file.write_text(
            "pub trait Greet {\n    fn greet(&self) -> String;\n}\n\n"
            "pub struct Dog;\n\n"
            "impl Greet for Dog {\n"
            "    fn greet(&self) -> String {\n"
            "        String::from(\"Woof\")\n"
            "    }\n"
            "}\n"
        )

        combined_rel = str(combined_file.relative_to(tmp_path).as_posix())
        nodes = [_node("greet_n", combined_rel, "Greet"), _node("dog_n", combined_rel, "Dog")]
        edges = resolve_rust([combined_file], tmp_path, {"nodes": nodes})

        assert not any(e["type"] == "rust_impl" for e in edges)


class TestIntraDirectoryCalls:
    def test_intra_directory_call_creates_rust_calls_edge(self, tmp_path):
        handler_file = tmp_path / "handler.rs"
        handler_file.write_text("fn handle() {\n    validate();\n}\n")
        validator_file = tmp_path / "validator.rs"
        validator_file.write_text("pub fn validate() -> bool {\n    true\n}\n")

        handler_rel = str(handler_file.relative_to(tmp_path).as_posix())
        validator_rel = str(validator_file.relative_to(tmp_path).as_posix())
        nodes = [_node("handler_n", handler_rel, "handle"), _node("validator_n", validator_rel, "validate")]
        edges = resolve_rust([handler_file, validator_file], tmp_path, {"nodes": nodes})

        types = [e["type"] for e in edges]
        assert "rust_calls" in types
        call_edge = [e for e in edges if e["type"] == "rust_calls"][0]
        assert call_edge["confidence"] == 0.75
        assert call_edge["source"] == "handler_n"
        assert call_edge["target"] == "validator_n"

    def test_cross_directory_call_creates_no_edge(self, tmp_path):
        (tmp_path / "web").mkdir()
        (tmp_path / "core").mkdir()
        handler_file = tmp_path / "web" / "handler.rs"
        handler_file.write_text("fn handle() {\n    validate();\n}\n")
        validator_file = tmp_path / "core" / "validator.rs"
        validator_file.write_text("pub fn validate() -> bool {\n    true\n}\n")

        handler_rel = str(handler_file.relative_to(tmp_path).as_posix())
        validator_rel = str(validator_file.relative_to(tmp_path).as_posix())
        nodes = [_node("handler_n", handler_rel, "handle"), _node("validator_n", validator_rel, "validate")]
        edges = resolve_rust([handler_file, validator_file], tmp_path, {"nodes": nodes})

        assert not any(e["type"] == "rust_calls" for e in edges)


class TestRustEdgeSchema:
    def test_all_edges_have_relation_field(self, tmp_path):
        utils_file = tmp_path / "utils.rs"
        utils_file.write_text("pub fn helper() -> bool {\n    true\n}\n")
        main_file = tmp_path / "main.rs"
        main_file.write_text("use crate::utils::helper;\n\nfn main() {\n    helper();\n}\n")

        utils_rel = str(utils_file.relative_to(tmp_path).as_posix())
        main_rel = str(main_file.relative_to(tmp_path).as_posix())
        nodes = [_node("main_n", main_rel, "main"), _node("helper_n", utils_rel, "helper")]
        edges = resolve_rust([main_file, utils_file], tmp_path, {"nodes": nodes})

        assert edges
        assert all("relation" in e for e in edges)
        assert all(e["relation"] == e["type"] for e in edges)
        assert all(e["resolver"] == "rust" for e in edges)
