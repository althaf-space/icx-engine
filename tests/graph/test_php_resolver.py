"""Tests for the PHP resolver: namespace/use, inheritance, and intra-namespace calls."""
import pytest
from pathlib import Path
from icx_engine.graph.parser.resolvers.php_resolver import resolve_php


def _node(node_id, source_file, name=None):
    return {"id": node_id, "source_file": source_file, "file": source_file, "name": name or node_id}


class TestNoPhpFiles:
    def test_no_php_files_returns_empty(self):
        result = resolve_php([], Path("."), {"nodes": []})
        assert result == []


class TestUseNamespace:
    def test_use_statement_creates_php_use_edge(self, tmp_path):
        user_file = tmp_path / "User.php"
        user_file.write_text("<?php\nnamespace App\\Models;\n\nclass User\n{\n}\n")
        controller_file = tmp_path / "UserController.php"
        controller_file.write_text("<?php\nnamespace App\\Controllers;\n\nuse App\\Models\\User;\n\nclass UserController\n{\n}\n")

        user_rel = str(user_file.relative_to(tmp_path).as_posix())
        controller_rel = str(controller_file.relative_to(tmp_path).as_posix())
        nodes = [_node("ctrl_n", controller_rel, "UserController"), _node("user_n", user_rel, "User")]
        edges = resolve_php([controller_file, user_file], tmp_path, {"nodes": nodes})

        types = [e["type"] for e in edges]
        assert "php_use" in types
        use_edge = [e for e in edges if e["type"] == "php_use"][0]
        assert use_edge["confidence"] == 0.90
        assert use_edge["source"] == "ctrl_n"
        assert use_edge["target"] == "user_n"

    def test_use_with_alias_resolves_by_fqcn(self, tmp_path):
        user_file = tmp_path / "User.php"
        user_file.write_text("<?php\nnamespace App\\Models;\n\nclass User\n{\n}\n")
        controller_file = tmp_path / "UserController.php"
        controller_file.write_text("<?php\nnamespace App\\Controllers;\n\nuse App\\Models\\User as UserModel;\n\nclass UserController\n{\n}\n")

        user_rel = str(user_file.relative_to(tmp_path).as_posix())
        controller_rel = str(controller_file.relative_to(tmp_path).as_posix())
        nodes = [_node("ctrl_n", controller_rel, "UserController"), _node("user_n", user_rel, "User")]
        edges = resolve_php([controller_file, user_file], tmp_path, {"nodes": nodes})

        use_edges = [e for e in edges if e["type"] == "php_use"]
        assert any(e["source"] == "ctrl_n" and e["target"] == "user_n" for e in use_edges)

    def test_unresolved_use_creates_no_edge(self, tmp_path):
        controller_file = tmp_path / "UserController.php"
        controller_file.write_text("<?php\nnamespace App\\Controllers;\n\nuse Illuminate\\Http\\Request;\n\nclass UserController\n{\n}\n")

        controller_rel = str(controller_file.relative_to(tmp_path).as_posix())
        nodes = [_node("ctrl_n", controller_rel, "UserController")]
        edges = resolve_php([controller_file], tmp_path, {"nodes": nodes})

        assert not any(e["type"] == "php_use" for e in edges)


class TestInheritance:
    def test_class_extends_base_in_project_creates_php_extends_edge(self, tmp_path):
        base_file = tmp_path / "Model.php"
        base_file.write_text("<?php\nnamespace App\\Models;\n\nclass Model\n{\n}\n")
        derived_file = tmp_path / "User.php"
        derived_file.write_text("<?php\nnamespace App\\Models;\n\nclass User extends Model\n{\n}\n")

        base_rel = str(base_file.relative_to(tmp_path).as_posix())
        derived_rel = str(derived_file.relative_to(tmp_path).as_posix())
        nodes = [_node("model_n", base_rel, "Model"), _node("user_n", derived_rel, "User")]
        edges = resolve_php([base_file, derived_file], tmp_path, {"nodes": nodes})

        types = [e["type"] for e in edges]
        assert "php_extends" in types
        extends_edge = [e for e in edges if e["type"] == "php_extends"][0]
        assert extends_edge["confidence"] == 0.80
        assert extends_edge["source"] == "user_n"
        assert extends_edge["target"] == "model_n"

    def test_interface_implementation_creates_php_extends_edge(self, tmp_path):
        iface_file = tmp_path / "Arrayable.php"
        iface_file.write_text("<?php\nnamespace App\\Contracts;\n\ninterface Arrayable\n{\n    public function toArray(): array;\n}\n")
        impl_file = tmp_path / "User.php"
        impl_file.write_text("<?php\nnamespace App\\Models;\n\nuse App\\Contracts\\Arrayable;\n\nclass User implements Arrayable\n{\n    public function toArray(): array { return []; }\n}\n")

        iface_rel = str(iface_file.relative_to(tmp_path).as_posix())
        impl_rel = str(impl_file.relative_to(tmp_path).as_posix())
        nodes = [_node("arrayable_n", iface_rel, "Arrayable"), _node("user_n", impl_rel, "User")]
        edges = resolve_php([iface_file, impl_file], tmp_path, {"nodes": nodes})

        extends_edges = [e for e in edges if e["type"] == "php_extends"]
        assert any(e["source"] == "user_n" and e["target"] == "arrayable_n" for e in extends_edges)

    def test_base_not_in_project_creates_no_edge(self, tmp_path):
        derived_file = tmp_path / "UserController.php"
        derived_file.write_text("<?php\nnamespace App\\Controllers;\n\nclass UserController extends Controller\n{\n}\n")

        derived_rel = str(derived_file.relative_to(tmp_path).as_posix())
        nodes = [_node("ctrl_n", derived_rel, "UserController")]
        edges = resolve_php([derived_file], tmp_path, {"nodes": nodes})

        assert not any(e["type"] == "php_extends" for e in edges)


class TestIntraNamespaceCalls:
    def test_intra_namespace_method_call_creates_php_calls_edge(self, tmp_path):
        handler_file = tmp_path / "Handler.php"
        handler_file.write_text("<?php\nnamespace App\\Services;\n\nclass Handler\n{\n    public function handle()\n    {\n        validate();\n    }\n}\n")
        validator_file = tmp_path / "Validator.php"
        validator_file.write_text("<?php\nnamespace App\\Services;\n\nclass Validator\n{\n    public function validate()\n    {\n        return true;\n    }\n}\n")

        handler_rel = str(handler_file.relative_to(tmp_path).as_posix())
        validator_rel = str(validator_file.relative_to(tmp_path).as_posix())
        nodes = [_node("handler_n", handler_rel, "Handler"), _node("validator_n", validator_rel, "Validator")]
        edges = resolve_php([handler_file, validator_file], tmp_path, {"nodes": nodes})

        types = [e["type"] for e in edges]
        assert "php_calls" in types
        call_edge = [e for e in edges if e["type"] == "php_calls"][0]
        assert call_edge["confidence"] == 0.75
        assert call_edge["source"] == "handler_n"
        assert call_edge["target"] == "validator_n"

    def test_cross_namespace_method_call_creates_no_edge(self, tmp_path):
        handler_file = tmp_path / "Handler.php"
        handler_file.write_text("<?php\nnamespace App\\Web;\n\nclass Handler\n{\n    public function handle()\n    {\n        validate();\n    }\n}\n")
        validator_file = tmp_path / "Validator.php"
        validator_file.write_text("<?php\nnamespace App\\Core;\n\nclass Validator\n{\n    public function validate()\n    {\n        return true;\n    }\n}\n")

        handler_rel = str(handler_file.relative_to(tmp_path).as_posix())
        validator_rel = str(validator_file.relative_to(tmp_path).as_posix())
        nodes = [_node("handler_n", handler_rel, "Handler"), _node("validator_n", validator_rel, "Validator")]
        edges = resolve_php([handler_file, validator_file], tmp_path, {"nodes": nodes})

        assert not any(e["type"] == "php_calls" for e in edges)


class TestPhpEdgeSchema:
    def test_all_edges_have_relation_field(self, tmp_path):
        user_file = tmp_path / "User.php"
        user_file.write_text("<?php\nnamespace App\\Models;\n\nclass User\n{\n}\n")
        controller_file = tmp_path / "UserController.php"
        controller_file.write_text("<?php\nnamespace App\\Controllers;\n\nuse App\\Models\\User;\n\nclass UserController\n{\n}\n")

        user_rel = str(user_file.relative_to(tmp_path).as_posix())
        controller_rel = str(controller_file.relative_to(tmp_path).as_posix())
        nodes = [_node("ctrl_n", controller_rel, "UserController"), _node("user_n", user_rel, "User")]
        edges = resolve_php([controller_file, user_file], tmp_path, {"nodes": nodes})

        assert edges
        assert all("relation" in e for e in edges)
        assert all(e["relation"] == e["type"] for e in edges)
        assert all(e["resolver"] == "php" for e in edges)
