from pathlib import Path

from icx_engine.graph.parser.resolvers.scala_resolver import resolve_scala


def _node(node_id, name, source_file):
    return {"id": node_id, "name": name, "source_file": source_file}


class TestScalaResolverNoOp:
    def test_no_scala_files_returns_empty(self, tmp_path):
        assert resolve_scala([], tmp_path, {"nodes": []}) == []

    def test_no_matching_nodes_returns_empty(self, tmp_path):
        scala_file = tmp_path / "Foo.scala"
        scala_file.write_text("class Foo {}\n", encoding="utf-8")
        assert resolve_scala([scala_file], tmp_path, {"nodes": []}) == []


class TestScalaImport:
    def test_simple_import_resolves_to_declaring_file(self, tmp_path):
        bar_file = tmp_path / "Bar.scala"
        bar_file.write_text("package com.foo\n\nclass Bar {\n  def run(): Unit = ()\n}\n", encoding="utf-8")
        main_file = tmp_path / "Main.scala"
        main_file.write_text(
            "package com.foo\n\nimport com.foo.Bar\n\nclass Main {\n  def run(): Unit = ()\n}\n",
            encoding="utf-8",
        )

        nodes = [
            _node("main_n", "Main", "Main.scala"),
            _node("bar_n", "Bar", "Bar.scala"),
        ]
        edges = resolve_scala([main_file, bar_file], tmp_path, {"nodes": nodes})
        import_edges = [e for e in edges if e["relation"] == "scala_import"]
        assert ("main_n", "bar_n") in [(e["source"], e["target"]) for e in import_edges]

    def test_braced_import_resolves_each_name(self, tmp_path):
        bar_file = tmp_path / "Bar.scala"
        bar_file.write_text("class Bar {}\n", encoding="utf-8")
        baz_file = tmp_path / "Baz.scala"
        baz_file.write_text("class Baz {}\n", encoding="utf-8")
        main_file = tmp_path / "Main.scala"
        main_file.write_text("import com.foo.{Bar, Baz}\n\nclass Main {}\n", encoding="utf-8")

        nodes = [
            _node("main_n", "Main", "Main.scala"),
            _node("bar_n", "Bar", "Bar.scala"),
            _node("baz_n", "Baz", "Baz.scala"),
        ]
        edges = resolve_scala([main_file, bar_file, baz_file], tmp_path, {"nodes": nodes})
        targets = {(e["source"], e["target"]) for e in edges if e["relation"] == "scala_import"}
        assert ("main_n", "bar_n") in targets
        assert ("main_n", "baz_n") in targets


class TestScalaExtends:
    def test_class_extends_and_with_trait(self, tmp_path):
        base_file = tmp_path / "Base.scala"
        base_file.write_text("class Base {}\n", encoding="utf-8")
        mixin_file = tmp_path / "Mixin.scala"
        mixin_file.write_text("trait Mixin {}\n", encoding="utf-8")
        derived_file = tmp_path / "Derived.scala"
        derived_file.write_text("class Derived extends Base with Mixin {}\n", encoding="utf-8")

        nodes = [
            _node("derived_n", "Derived", "Derived.scala"),
            _node("base_n", "Base", "Base.scala"),
            _node("mixin_n", "Mixin", "Mixin.scala"),
        ]
        edges = resolve_scala([derived_file, base_file, mixin_file], tmp_path, {"nodes": nodes})
        targets = {(e["source"], e["target"]) for e in edges if e["relation"] == "scala_extends"}
        assert ("derived_n", "base_n") in targets
        assert ("derived_n", "mixin_n") in targets


class TestScalaCalls:
    def test_intra_directory_function_call(self, tmp_path):
        helper_file = tmp_path / "Helper.scala"
        helper_file.write_text("object Helper {\n  def help(): Int = 42\n}\n", encoding="utf-8")
        main_file = tmp_path / "Main.scala"
        main_file.write_text("object Main {\n  def run(): Int = help()\n}\n", encoding="utf-8")

        nodes = [
            _node("main_n", "Main", "Main.scala"),
            _node("helper_n", "Helper", "Helper.scala"),
        ]
        edges = resolve_scala([main_file, helper_file], tmp_path, {"nodes": nodes})
        call_edges = [e for e in edges if e["relation"] == "scala_calls"]
        assert ("main_n", "helper_n") in [(e["source"], e["target"]) for e in call_edges]
