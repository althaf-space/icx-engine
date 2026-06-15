from pathlib import Path


class TestIntelephenseConfig:
    def test_intelephense_config_importable(self):
        from icx_engine.graph.parser.lsp_manager import INTELEPHENSE, node_runtime
        assert INTELEPHENSE.name == "intelephense"
        assert callable(node_runtime)
        assert INTELEPHENSE.install_dir.name == "intelephense"

    def test_intelephense_binary_lookup_returns_none_when_absent(self, tmp_path):
        from icx_engine.graph.parser.lsp_manager import INTELEPHENSE
        assert INTELEPHENSE.binary_fn(tmp_path) is None

    def test_intelephense_start_command(self, tmp_path):
        from icx_engine.graph.parser.lsp_manager import INTELEPHENSE
        binary = tmp_path / "intelephense.js"
        binary.touch()
        cmd = INTELEPHENSE.start_fn(binary, "/usr/bin/node")
        assert cmd == ["/usr/bin/node", str(binary), "--stdio"]


class TestPhpLspNoOp:
    def test_no_php_files_returns_empty(self, tmp_path):
        from icx_engine.graph.parser.resolvers.php_lsp import extract_php_lsp_edges
        assert extract_php_lsp_edges([], tmp_path, {"nodes": []}) == []

    def test_no_matching_nodes_returns_empty(self, tmp_path):
        from icx_engine.graph.parser.resolvers.php_lsp import extract_php_lsp_edges
        php_file = tmp_path / "index.php"
        php_file.write_text("<?php\nclass Foo {}\n", encoding="utf-8")
        # No nodes reference index.php -> node_index["by_file"] is empty -> early return
        result = extract_php_lsp_edges([php_file], tmp_path, {"nodes": []})
        assert result == []


class TestPhpLspPositionCollection:
    def test_collect_use_and_call_positions(self, tmp_path):
        pytest_mod = __import__("pytest")
        ts_php = pytest_mod.importorskip("tree_sitter_php")
        from tree_sitter import Language, Parser
        from icx_engine.graph.parser.resolvers.php_lsp import _collect_positions

        source = (
            b"<?php\n"
            b"use App\\Services\\Bar;\n\n"
            b"class Foo {\n"
            b"    function run() {\n"
            b"        helper();\n"
            b"        $this->doThing();\n"
            b"        Bar::staticCall();\n"
            b"    }\n"
            b"}\n"
        )
        parser = Parser(Language(ts_php.language_php()))
        tree = parser.parse(source)
        positions = _collect_positions(tree.root_node)
        kinds = {p[2] for p in positions}
        assert "use" in kinds
        assert "call" in kinds
