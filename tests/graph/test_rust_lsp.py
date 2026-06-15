from pathlib import Path


class TestRustAnalyzerConfig:
    def test_rust_analyzer_config_importable(self):
        from icx_engine.graph.parser.lsp_manager import RUST_ANALYZER, rust_runtime
        assert RUST_ANALYZER.name == "rust-analyzer"
        assert callable(rust_runtime)
        assert RUST_ANALYZER.install_dir.name == "rust-analyzer"

    def test_rust_analyzer_binary_lookup_returns_none_when_absent(self, tmp_path):
        from icx_engine.graph.parser.lsp_manager import RUST_ANALYZER
        assert RUST_ANALYZER.binary_fn(tmp_path) is None

    def test_rust_analyzer_start_command(self, tmp_path):
        from icx_engine.graph.parser.lsp_manager import RUST_ANALYZER
        binary = tmp_path / "rust-analyzer"
        binary.touch()
        cmd = RUST_ANALYZER.start_fn(binary, "/usr/bin/rustc")
        assert cmd == [str(binary)]


class TestRustLspNoOp:
    def test_no_rust_files_returns_empty(self, tmp_path):
        from icx_engine.graph.parser.resolvers.rust_lsp import extract_rust_lsp_edges
        assert extract_rust_lsp_edges([], tmp_path, {"nodes": []}) == []

    def test_no_matching_nodes_returns_empty(self, tmp_path):
        from icx_engine.graph.parser.resolvers.rust_lsp import extract_rust_lsp_edges
        rs_file = tmp_path / "main.rs"
        rs_file.write_text("fn main() {}\n", encoding="utf-8")
        # No nodes reference main.rs -> node_index["by_file"] is empty -> early return
        result = extract_rust_lsp_edges([rs_file], tmp_path, {"nodes": []})
        assert result == []


class TestRustLspPositionCollection:
    def test_collect_use_and_call_positions(self, tmp_path):
        pytest_mod = __import__("pytest")
        ts_rust = pytest_mod.importorskip("tree_sitter_rust")
        from tree_sitter import Language, Parser
        from icx_engine.graph.parser.resolvers.rust_lsp import _collect_positions

        source = b"use crate::foo::Bar;\n\nfn main() {\n    Bar::baz();\n    helper();\n}\n"
        parser = Parser(Language(ts_rust.language()))
        tree = parser.parse(source)
        positions = _collect_positions(tree.root_node)
        kinds = {p[2] for p in positions}
        assert "use" in kinds
        assert "call" in kinds
