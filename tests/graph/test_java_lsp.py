import sys
from pathlib import Path


class TestJdtlsConfig:
    def test_jdtls_config_importable(self):
        from icx_engine.graph.parser.lsp_manager import JDTLS, java_runtime
        assert JDTLS.name == "jdtls"
        assert callable(java_runtime)
        assert JDTLS.install_dir.name == "jdtls"

    def test_jdtls_binary_lookup_returns_none_when_absent(self, tmp_path):
        from icx_engine.graph.parser.lsp_manager import JDTLS
        assert JDTLS.binary_fn(tmp_path) is None

    def test_jdtls_binary_lookup_finds_launcher_jar(self, tmp_path):
        from icx_engine.graph.parser.lsp_manager import JDTLS
        plugins = tmp_path / "plugins"
        plugins.mkdir()
        jar = plugins / "org.eclipse.equinox.launcher_1.6.900.v20240613-2009.jar"
        jar.touch()
        assert JDTLS.binary_fn(tmp_path) == jar

    def test_jdtls_start_command_includes_configuration(self, tmp_path):
        from icx_engine.graph.parser.lsp_manager import JDTLS
        plugins = tmp_path / "plugins"
        plugins.mkdir()
        jar = plugins / "org.eclipse.equinox.launcher_1.6.900.jar"
        jar.touch()
        cmd = JDTLS.start_fn(jar, "/usr/bin/java")
        assert cmd[0] == "/usr/bin/java"
        assert "-jar" in cmd
        assert str(jar) in cmd
        assert "-configuration" in cmd
        config_idx = cmd.index("-configuration")
        expected_config_name = "config_win" if sys.platform == "win32" else (
            "config_mac" if sys.platform == "darwin" else "config_linux"
        )
        assert cmd[config_idx + 1] == str(tmp_path / expected_config_name)


class TestJavaLspNoOp:
    def test_no_java_files_returns_empty(self, tmp_path):
        from icx_engine.graph.parser.resolvers.java_lsp import extract_java_lsp_edges
        assert extract_java_lsp_edges([], tmp_path, {"nodes": []}) == []

    def test_no_matching_nodes_returns_empty(self, tmp_path):
        from icx_engine.graph.parser.resolvers.java_lsp import extract_java_lsp_edges
        java_file = tmp_path / "Main.java"
        java_file.write_text(
            "package com.example;\n\nimport java.util.List;\n\n"
            "public class Main {\n    public static void main(String[] args) {\n"
            "        List.of();\n    }\n}\n",
            encoding="utf-8",
        )
        result = extract_java_lsp_edges([java_file], tmp_path, {"nodes": []})
        assert result == []


class TestJavaLspPositionCollection:
    def test_collect_import_and_call_positions(self):
        import javalang
        from icx_engine.graph.parser.resolvers.java_lsp import _collect_positions

        source = (
            "package com.example;\n\n"
            "import java.util.List;\n\n"
            "public class Main {\n"
            "    public static void main(String[] args) {\n"
            "        List.of();\n"
            "    }\n"
            "}\n"
        )
        tree = javalang.parse.parse(source)
        positions = _collect_positions(tree)
        kinds = {p[2] for p in positions}
        assert "import" in kinds
        assert "call" in kinds
