from pathlib import Path


class TestClangdConfig:
    def test_clangd_config_importable(self):
        from icx_engine.graph.parser.lsp_manager import CLANGD, cpp_runtime
        assert CLANGD.name == "clangd"
        assert callable(cpp_runtime)
        assert CLANGD.install_dir.name == "clangd"

    def test_clangd_binary_lookup_returns_none_when_absent(self, tmp_path):
        from icx_engine.graph.parser.lsp_manager import CLANGD
        assert CLANGD.binary_fn(tmp_path) is None

    def test_clangd_start_command(self, tmp_path):
        from icx_engine.graph.parser.lsp_manager import CLANGD
        binary = tmp_path / "clangd"
        binary.touch()
        cmd = CLANGD.start_fn(binary, "/usr/bin/clang++")
        assert cmd == [str(binary)]


class TestCppLspNoOp:
    def test_no_cpp_files_returns_empty(self, tmp_path):
        from icx_engine.graph.parser.resolvers.cpp_lsp import extract_cpp_lsp_edges
        assert extract_cpp_lsp_edges([], tmp_path, {"nodes": []}) == []

    def test_no_matching_nodes_returns_empty(self, tmp_path):
        from icx_engine.graph.parser.resolvers.cpp_lsp import extract_cpp_lsp_edges
        cpp_file = tmp_path / "main.cpp"
        cpp_file.write_text("int main() { return 0; }\n", encoding="utf-8")
        # No nodes reference main.cpp -> node_index["by_file"] is empty -> early return
        result = extract_cpp_lsp_edges([cpp_file], tmp_path, {"nodes": []})
        assert result == []


class TestCppLspPositionCollection:
    def test_collect_include_and_call_positions(self, tmp_path):
        pytest_mod = __import__("pytest")
        ts_cpp = pytest_mod.importorskip("tree_sitter_cpp")
        from tree_sitter import Language, Parser
        from icx_engine.graph.parser.resolvers.cpp_lsp import _collect_positions

        source = (
            b'#include "foo.h"\n\n'
            b"int main() {\n"
            b"    Foo f;\n"
            b"    f.run();\n"
            b"    helper();\n"
            b"    return 0;\n"
            b"}\n"
        )
        parser = Parser(Language(ts_cpp.language()))
        tree = parser.parse(source)
        positions = _collect_positions(tree.root_node)
        kinds = {p[2] for p in positions}
        assert "import" in kinds
        assert "call" in kinds
