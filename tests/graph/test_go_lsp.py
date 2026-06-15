from pathlib import Path


class TestGoplsConfig:
    def test_gopls_config_importable(self):
        from icx_engine.graph.parser.lsp_manager import GOPLS, go_runtime
        assert GOPLS.name == "gopls"
        assert callable(go_runtime)
        assert GOPLS.install_dir.name == "gopls"

    def test_gopls_binary_lookup_returns_none_when_absent(self, tmp_path):
        from icx_engine.graph.parser.lsp_manager import GOPLS
        assert GOPLS.binary_fn(tmp_path) is None

    def test_gopls_start_command(self, tmp_path):
        from icx_engine.graph.parser.lsp_manager import GOPLS
        binary = tmp_path / "gopls"
        binary.touch()
        cmd = GOPLS.start_fn(binary, "/usr/bin/go")
        assert cmd == [str(binary), "serve"]


class TestGoLspNoOp:
    def test_no_go_files_returns_empty(self, tmp_path):
        from icx_engine.graph.parser.resolvers.go_lsp import extract_go_lsp_edges
        assert extract_go_lsp_edges([], tmp_path, {"nodes": []}) == []

    def test_no_matching_nodes_returns_empty(self, tmp_path):
        from icx_engine.graph.parser.resolvers.go_lsp import extract_go_lsp_edges
        go_file = tmp_path / "main.go"
        go_file.write_text("package main\n\nfunc main() {}\n", encoding="utf-8")
        # No nodes reference main.go -> node_index["by_file"] is empty -> early return
        result = extract_go_lsp_edges([go_file], tmp_path, {"nodes": []})
        assert result == []


class TestGoLspPositionCollection:
    def test_collect_import_and_call_positions(self, tmp_path):
        pytest_mod = __import__("pytest")
        ts_go = pytest_mod.importorskip("tree_sitter_go")
        from tree_sitter import Language, Parser
        from icx_engine.graph.parser.resolvers.go_lsp import _collect_positions

        source = b'package main\n\nimport "fmt"\n\nfunc main() {\n\tfmt.Println("hi")\n}\n'
        parser = Parser(Language(ts_go.language()))
        tree = parser.parse(source)
        positions = _collect_positions(tree.root_node)
        kinds = {p[2] for p in positions}
        assert "import" in kinds
        assert "call" in kinds
