import sys


class TestKotlinLsConfig:
    def test_kotlin_ls_config_importable(self):
        from icx_engine.graph.parser.lsp_manager import KOTLIN_LS, java_runtime
        assert KOTLIN_LS.name == "kotlin-language-server"
        assert callable(java_runtime)
        assert KOTLIN_LS.install_dir.name == "kotlin-language-server"

    def test_kotlin_ls_binary_lookup_returns_none_when_absent(self, tmp_path):
        from icx_engine.graph.parser.lsp_manager import KOTLIN_LS
        assert KOTLIN_LS.binary_fn(tmp_path) is None

    def test_kotlin_ls_binary_lookup_finds_script(self, tmp_path):
        from icx_engine.graph.parser.lsp_manager import KOTLIN_LS
        bin_dir = tmp_path / "server" / "bin"
        bin_dir.mkdir(parents=True)
        name = "kotlin-language-server.bat" if sys.platform == "win32" else "kotlin-language-server"
        script = bin_dir / name
        script.touch()
        assert KOTLIN_LS.binary_fn(tmp_path) == script

    def test_kotlin_ls_start_command_invokes_script(self, tmp_path):
        from icx_engine.graph.parser.lsp_manager import KOTLIN_LS
        bin_dir = tmp_path / "server" / "bin"
        bin_dir.mkdir(parents=True)
        name = "kotlin-language-server.bat" if sys.platform == "win32" else "kotlin-language-server"
        script = bin_dir / name
        script.touch()
        cmd = KOTLIN_LS.start_fn(script, "/usr/bin/java")
        assert cmd == [str(script)]


class TestKotlinLspNoOp:
    def test_no_kotlin_files_returns_empty(self, tmp_path):
        from icx_engine.graph.parser.resolvers.kotlin_lsp import extract_kotlin_lsp_edges
        assert extract_kotlin_lsp_edges([], tmp_path, {"nodes": []}) == []

    def test_no_matching_nodes_returns_empty(self, tmp_path):
        from icx_engine.graph.parser.resolvers.kotlin_lsp import extract_kotlin_lsp_edges
        kt_file = tmp_path / "Main.kt"
        kt_file.write_text(
            "package com.example\n\n"
            "import kotlin.collections.listOf\n\n"
            "fun main() {\n"
            "    println(listOf(1, 2, 3))\n"
            "}\n",
            encoding="utf-8",
        )
        result = extract_kotlin_lsp_edges([kt_file], tmp_path, {"nodes": []})
        assert result == []


class TestKotlinLspPositionCollection:
    def test_collect_import_and_call_positions(self):
        from icx_engine.graph.parser.resolvers.kotlin_lsp import _collect_positions

        source = (
            "package com.example\n\n"
            "import kotlin.collections.listOf\n\n"
            "fun main() {\n"
            "    println(listOf(1, 2, 3))\n"
            "}\n"
        )
        positions = _collect_positions(source)
        kinds = {p[2] for p in positions}
        assert "import" in kinds
        assert "call" in kinds

        import_pos = next(p for p in positions if p[2] == "import")
        assert import_pos[0] == 2  # 0-indexed line of "import kotlin.collections.listOf"
