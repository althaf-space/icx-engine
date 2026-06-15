from pathlib import Path

from icx_engine.graph.parser.resolvers.cpp_resolver import resolve_cpp


def _node(node_id, name, source_file):
    return {"id": node_id, "name": name, "source_file": source_file}


class TestCppResolverNoOp:
    def test_no_cpp_files_returns_empty(self, tmp_path):
        assert resolve_cpp([], tmp_path, {"nodes": []}) == []

    def test_no_matching_nodes_returns_empty(self, tmp_path):
        cpp_file = tmp_path / "main.cpp"
        cpp_file.write_text("int main() { return 0; }\n", encoding="utf-8")
        assert resolve_cpp([cpp_file], tmp_path, {"nodes": []}) == []


class TestCppInclude:
    def test_include_resolves_to_matching_header(self, tmp_path):
        header = tmp_path / "foo.h"
        header.write_text("class Foo {\npublic:\n    void run();\n};\n", encoding="utf-8")
        main = tmp_path / "main.cpp"
        main.write_text('#include "foo.h"\nint main() { return 0; }\n', encoding="utf-8")

        nodes = [
            _node("main_n", "main", "main.cpp"),
            _node("foo_n", "Foo", "foo.h"),
        ]
        edges = resolve_cpp([main, header], tmp_path, {"nodes": nodes})
        include_edges = [e for e in edges if e["relation"] == "cpp_include"]
        assert ("main_n", "foo_n") in [(e["source"], e["target"]) for e in include_edges]


class TestCppInherits:
    def test_class_inherits_from_base_in_other_file(self, tmp_path):
        base = tmp_path / "base.h"
        base.write_text("class Base {\npublic:\n    virtual void run();\n};\n", encoding="utf-8")
        derived = tmp_path / "derived.h"
        derived.write_text('#include "base.h"\nclass Derived : public Base {\npublic:\n    void run();\n};\n', encoding="utf-8")

        nodes = [
            _node("base_n", "Base", "base.h"),
            _node("derived_n", "Derived", "derived.h"),
        ]
        edges = resolve_cpp([base, derived], tmp_path, {"nodes": nodes})
        inherits_edges = [e for e in edges if e["relation"] == "cpp_inherits"]
        assert ("derived_n", "base_n") in [(e["source"], e["target"]) for e in inherits_edges]


class TestCppCalls:
    def test_intra_directory_function_call(self, tmp_path):
        helper = tmp_path / "helper.cpp"
        helper.write_text("int helper() {\n    return 42;\n}\n", encoding="utf-8")
        main = tmp_path / "main.cpp"
        main.write_text("int main() {\n    return helper();\n}\n", encoding="utf-8")

        nodes = [
            _node("main_n", "main", "main.cpp"),
            _node("helper_n", "helper", "helper.cpp"),
        ]
        edges = resolve_cpp([main, helper], tmp_path, {"nodes": nodes})
        call_edges = [e for e in edges if e["relation"] == "cpp_calls"]
        assert ("main_n", "helper_n") in [(e["source"], e["target"]) for e in call_edges]
