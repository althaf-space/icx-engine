from pathlib import Path


class TestOmniSharpConfig:
    def test_omnisharp_config_importable(self):
        from icx_engine.graph.parser.lsp_manager import OMNISHARP, dotnet_runtime
        assert OMNISHARP.name == "omnisharp"
        assert callable(dotnet_runtime)
        assert OMNISHARP.install_dir.name == "omnisharp"

    def test_omnisharp_binary_lookup_returns_none_when_absent(self, tmp_path):
        from icx_engine.graph.parser.lsp_manager import OMNISHARP
        assert OMNISHARP.binary_fn(tmp_path) is None

    def test_omnisharp_start_command(self, tmp_path):
        from icx_engine.graph.parser.lsp_manager import OMNISHARP
        binary = tmp_path / "OmniSharp"
        binary.touch()
        cmd = OMNISHARP.start_fn(binary, "/usr/bin/dotnet")
        assert cmd == [str(binary), "-lsp"]


class TestCsharpLspNoOp:
    def test_no_cs_files_returns_empty(self, tmp_path):
        from icx_engine.graph.parser.resolvers.csharp_lsp import extract_csharp_lsp_edges
        assert extract_csharp_lsp_edges([], tmp_path, {"nodes": []}) == []

    def test_no_matching_nodes_returns_empty(self, tmp_path):
        from icx_engine.graph.parser.resolvers.csharp_lsp import extract_csharp_lsp_edges
        cs_file = tmp_path / "Program.cs"
        cs_file.write_text("class Program {}\n", encoding="utf-8")
        # No nodes reference Program.cs -> node_index["by_file"] is empty -> early return
        result = extract_csharp_lsp_edges([cs_file], tmp_path, {"nodes": []})
        assert result == []


class TestCsharpLspPositionCollection:
    def test_collect_use_and_call_positions(self, tmp_path):
        pytest_mod = __import__("pytest")
        ts_cs = pytest_mod.importorskip("tree_sitter_c_sharp")
        from tree_sitter import Language, Parser
        from icx_engine.graph.parser.resolvers.csharp_lsp import _collect_positions

        source = (
            b"using MyApp.Services;\n\n"
            b"class Foo {\n"
            b"    void Run() {\n"
            b"        var x = new Bar();\n"
            b"        x.Helper();\n"
            b"        Util.Do();\n"
            b"    }\n"
            b"}\n"
        )
        parser = Parser(Language(ts_cs.language()))
        tree = parser.parse(source)
        positions = _collect_positions(tree.root_node)
        kinds = {p[2] for p in positions}
        assert "use" in kinds
        assert "call" in kinds
