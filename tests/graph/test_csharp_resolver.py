"""Tests for the C# resolver: namespace/using resolution."""
import pytest
from pathlib import Path
from icx_engine.graph.parser.resolvers.csharp_resolver import resolve_csharp


def _node(node_id, source_file, name=None):
    return {"id": node_id, "source_file": source_file, "file": source_file, "name": name or node_id}


class TestNoCsFiles:
    def test_no_cs_files_returns_empty(self):
        result = resolve_csharp([], Path("."), {"nodes": []})
        assert result == []


class TestUsingNamespace:
    def test_using_directive_creates_csharp_using_edge(self, tmp_path):
        util_file = tmp_path / "Util.cs"
        util_file.write_text("namespace MyApp.Helpers\n{\n    public class Util {}\n}\n")
        main_file = tmp_path / "Main.cs"
        main_file.write_text("using MyApp.Helpers;\n\nnamespace MyApp\n{\n    public class Program {}\n}\n")

        util_rel = str(util_file.relative_to(tmp_path).as_posix())
        main_rel = str(main_file.relative_to(tmp_path).as_posix())
        nodes = [_node("main_n", main_rel, "Program"), _node("util_n", util_rel, "Util")]
        edges = resolve_csharp([main_file, util_file], tmp_path, {"nodes": nodes})

        types = [e["type"] for e in edges]
        assert "csharp_using" in types
        using_edge = [e for e in edges if e["type"] == "csharp_using"][0]
        assert using_edge["confidence"] == 0.90
        assert using_edge["source"] == "main_n"
        assert using_edge["target"] == "util_n"

    def test_using_static_is_ignored(self, tmp_path):
        util_file = tmp_path / "Util.cs"
        util_file.write_text("namespace MyApp.Helpers\n{\n    public static class MathUtil {}\n}\n")
        main_file = tmp_path / "Main.cs"
        main_file.write_text("using static MyApp.Helpers.MathUtil;\n\nnamespace MyApp\n{\n    public class Program {}\n}\n")

        util_rel = str(util_file.relative_to(tmp_path).as_posix())
        main_rel = str(main_file.relative_to(tmp_path).as_posix())
        nodes = [_node("main_n", main_rel, "Program"), _node("util_n", util_rel, "MathUtil")]
        edges = resolve_csharp([main_file, util_file], tmp_path, {"nodes": nodes})

        assert not any(e["type"] == "csharp_using" for e in edges)

    def test_unresolved_using_creates_no_edge(self, tmp_path):
        main_file = tmp_path / "Main.cs"
        main_file.write_text("using System.Collections.Generic;\n\nnamespace MyApp\n{\n    public class Program {}\n}\n")

        main_rel = str(main_file.relative_to(tmp_path).as_posix())
        nodes = [_node("main_n", main_rel, "Program")]
        edges = resolve_csharp([main_file], tmp_path, {"nodes": nodes})

        assert not any(e["type"] == "csharp_using" for e in edges)


class TestInheritance:
    def test_class_extends_base_in_project_creates_csharp_extends_edge(self, tmp_path):
        base_file = tmp_path / "Animal.cs"
        base_file.write_text("namespace MyApp\n{\n    public class Animal\n    {\n        public virtual void Speak() {}\n    }\n}\n")
        derived_file = tmp_path / "Dog.cs"
        derived_file.write_text("namespace MyApp\n{\n    public class Dog : Animal\n    {\n        public override void Speak() {}\n    }\n}\n")

        base_rel = str(base_file.relative_to(tmp_path).as_posix())
        derived_rel = str(derived_file.relative_to(tmp_path).as_posix())
        nodes = [_node("animal_n", base_rel, "Animal"), _node("dog_n", derived_rel, "Dog")]
        edges = resolve_csharp([base_file, derived_file], tmp_path, {"nodes": nodes})

        types = [e["type"] for e in edges]
        assert "csharp_extends" in types
        extends_edge = [e for e in edges if e["type"] == "csharp_extends"][0]
        assert extends_edge["confidence"] == 0.80
        assert extends_edge["source"] == "dog_n"
        assert extends_edge["target"] == "animal_n"

    def test_interface_implementation_creates_csharp_extends_edge(self, tmp_path):
        iface_file = tmp_path / "IShape.cs"
        iface_file.write_text("namespace MyApp\n{\n    public interface IShape\n    {\n        double Area();\n    }\n}\n")
        impl_file = tmp_path / "Circle.cs"
        impl_file.write_text("namespace MyApp\n{\n    public class Circle : IShape\n    {\n        public double Area() => 0;\n    }\n}\n")

        iface_rel = str(iface_file.relative_to(tmp_path).as_posix())
        impl_rel = str(impl_file.relative_to(tmp_path).as_posix())
        nodes = [_node("ishape_n", iface_rel, "IShape"), _node("circle_n", impl_rel, "Circle")]
        edges = resolve_csharp([iface_file, impl_file], tmp_path, {"nodes": nodes})

        extends_edges = [e for e in edges if e["type"] == "csharp_extends"]
        assert any(e["source"] == "circle_n" and e["target"] == "ishape_n" for e in extends_edges)

    def test_base_not_in_project_creates_no_edge(self, tmp_path):
        derived_file = tmp_path / "MyController.cs"
        derived_file.write_text("namespace MyApp\n{\n    public class MyController : ControllerBase\n    {\n    }\n}\n")

        derived_rel = str(derived_file.relative_to(tmp_path).as_posix())
        nodes = [_node("ctrl_n", derived_rel, "MyController")]
        edges = resolve_csharp([derived_file], tmp_path, {"nodes": nodes})

        assert not any(e["type"] == "csharp_extends" for e in edges)


class TestIntraNamespaceCalls:
    def test_intra_namespace_method_call_creates_csharp_calls_edge(self, tmp_path):
        handler_file = tmp_path / "Handler.cs"
        handler_file.write_text("namespace MyApp\n{\n    public class Handler\n    {\n        public void Handle()\n        {\n            Validate();\n        }\n    }\n}\n")
        validator_file = tmp_path / "Validator.cs"
        validator_file.write_text("namespace MyApp\n{\n    public class Validator\n    {\n        public bool Validate()\n        {\n            return true;\n        }\n    }\n}\n")

        handler_rel = str(handler_file.relative_to(tmp_path).as_posix())
        validator_rel = str(validator_file.relative_to(tmp_path).as_posix())
        nodes = [_node("handler_n", handler_rel, "Handler"), _node("validator_n", validator_rel, "Validator")]
        edges = resolve_csharp([handler_file, validator_file], tmp_path, {"nodes": nodes})

        types = [e["type"] for e in edges]
        assert "csharp_calls" in types
        call_edge = [e for e in edges if e["type"] == "csharp_calls"][0]
        assert call_edge["confidence"] == 0.75
        assert call_edge["source"] == "handler_n"
        assert call_edge["target"] == "validator_n"

    def test_cross_namespace_method_call_creates_no_edge(self, tmp_path):
        handler_file = tmp_path / "Handler.cs"
        handler_file.write_text("namespace MyApp.Web\n{\n    public class Handler\n    {\n        public void Handle()\n        {\n            Validate();\n        }\n    }\n}\n")
        validator_file = tmp_path / "Validator.cs"
        validator_file.write_text("namespace MyApp.Core\n{\n    public class Validator\n    {\n        public bool Validate()\n        {\n            return true;\n        }\n    }\n}\n")

        handler_rel = str(handler_file.relative_to(tmp_path).as_posix())
        validator_rel = str(validator_file.relative_to(tmp_path).as_posix())
        nodes = [_node("handler_n", handler_rel, "Handler"), _node("validator_n", validator_rel, "Validator")]
        edges = resolve_csharp([handler_file, validator_file], tmp_path, {"nodes": nodes})

        assert not any(e["type"] == "csharp_calls" for e in edges)


class TestCsharpEdgeSchema:
    def test_all_edges_have_relation_field(self, tmp_path):
        util_file = tmp_path / "Util.cs"
        util_file.write_text("namespace MyApp.Helpers\n{\n    public class Util {}\n}\n")
        main_file = tmp_path / "Main.cs"
        main_file.write_text("using MyApp.Helpers;\n\nnamespace MyApp\n{\n    public class Program {}\n}\n")

        util_rel = str(util_file.relative_to(tmp_path).as_posix())
        main_rel = str(main_file.relative_to(tmp_path).as_posix())
        nodes = [_node("main_n", main_rel, "Program"), _node("util_n", util_rel, "Util")]
        edges = resolve_csharp([main_file, util_file], tmp_path, {"nodes": nodes})

        assert edges
        assert all("relation" in e for e in edges)
        assert all(e["relation"] == e["type"] for e in edges)
        assert all(e["resolver"] == "csharp" for e in edges)
