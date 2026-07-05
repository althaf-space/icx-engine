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

    def test_duplicate_call_sites_are_deduped(self):
        # The same receiver.method() called many times -> queried once.
        import javalang
        from icx_engine.graph.parser.resolvers.java_lsp import _collect_positions

        source = (
            "package com.example;\n"
            "public class Main {\n"
            "    void run(Svc svc) {\n"
            "        svc.foo();\n"
            "        svc.foo();\n"
            "        svc.foo();\n"
            "    }\n"
            "}\n"
        )
        tree = javalang.parse.parse(source)
        calls = [p for p in _collect_positions(tree) if p[2] == "call"]
        assert len(calls) == 1  # three identical calls collapse to one query

    def test_distinct_receivers_not_deduped(self):
        # a.foo() and b.foo() resolve to different targets -> both queried.
        import javalang
        from icx_engine.graph.parser.resolvers.java_lsp import _collect_positions

        source = (
            "package com.example;\n"
            "public class Main {\n"
            "    void run(A a, B b) {\n"
            "        a.foo();\n"
            "        b.foo();\n"
            "    }\n"
            "}\n"
        )
        tree = javalang.parse.parse(source)
        calls = [p for p in _collect_positions(tree) if p[2] == "call"]
        assert len(calls) == 2  # distinct receivers kept separate


class TestJavaLspPersistentWorkspace:
    """Workspace persists per project; marker stores java version; wipe only on version change."""

    def _fake_client_cls(self):
        class _FakeClient:
            consecutive_timeouts = 0
            pid = None

            def start(self): return True
            def did_open(self, path, lang): pass
            def did_close(self, path): pass
            def definition(self, path, line, col): return []
            def __enter__(self): return self
            def __exit__(self, *a): pass

        return _FakeClient

    def _patches(self, monkeypatch, mod, lsp_mod, ws_path, java_ver="openjdk 17"):
        monkeypatch.setattr(lsp_mod, "LSPClient", lambda *a, **kw: self._fake_client_cls()())
        monkeypatch.setattr(mod, "ensure_server", lambda cfg: ["fake-java"])
        monkeypatch.setattr(mod, "record_pid", lambda *a: None)
        monkeypatch.setattr(mod, "kill_tracked", lambda *a: None)
        monkeypatch.setattr(mod, "_workspace_dir", lambda p: ws_path)
        monkeypatch.setattr(mod, "java_runtime", lambda: ("java", java_ver))

    def _node(self, java_file):
        return {
            "id": "Main", "label": "Main.java",
            "source_file": str(java_file).replace("\\", "/"), "source_location": "L1",
        }

    def test_marker_written_after_successful_build(self, tmp_path, monkeypatch):
        from icx_engine.graph.parser.resolvers import java_lsp as _mod
        import icx_engine.graph.parser.lsp_client as _lsp_mod

        ws = tmp_path / "ws"
        java_file = tmp_path / "Main.java"
        java_file.write_text("package com.example;\npublic class Main {}\n", encoding="utf-8")

        self._patches(monkeypatch, _mod, _lsp_mod, ws, java_ver="openjdk 17")
        _mod.extract_java_lsp_edges([java_file], tmp_path, {"nodes": [self._node(java_file)]})

        marker = ws / "icx-ready"
        assert marker.exists()
        assert marker.read_text(encoding="utf-8").strip() == "openjdk 17"

    def test_marker_not_written_when_start_fails(self, tmp_path, monkeypatch):
        from icx_engine.graph.parser.resolvers import java_lsp as _mod
        import icx_engine.graph.parser.lsp_client as _lsp_mod

        ws = tmp_path / "ws"
        java_file = tmp_path / "Main.java"
        java_file.write_text("public class Main {}\n", encoding="utf-8")

        class _FailClient:
            consecutive_timeouts = 0
            pid = None
            def start(self): return False
            def __enter__(self): return self
            def __exit__(self, *a): pass

        monkeypatch.setattr(_lsp_mod, "LSPClient", lambda *a, **kw: _FailClient())
        monkeypatch.setattr(_mod, "ensure_server", lambda cfg: ["fake-java"])
        monkeypatch.setattr(_mod, "record_pid", lambda *a: None)
        monkeypatch.setattr(_mod, "kill_tracked", lambda *a: None)
        monkeypatch.setattr(_mod, "_workspace_dir", lambda p: ws)
        monkeypatch.setattr(_mod, "java_runtime", lambda: ("java", "openjdk 17"))

        result = _mod.extract_java_lsp_edges([java_file], tmp_path, {"nodes": [self._node(java_file)]})
        assert result == []
        assert not (ws / "icx-ready").exists()

    def test_workspace_preserved_when_marker_missing(self, tmp_path, monkeypatch):
        """Existing workspace without marker is reused - preserves warm indexes from pre-marker builds."""
        from icx_engine.graph.parser.resolvers import java_lsp as _mod
        import icx_engine.graph.parser.lsp_client as _lsp_mod

        ws = tmp_path / "ws"
        ws.mkdir()
        sentinel = ws / "eclipse-index.db"
        sentinel.write_text("cached", encoding="utf-8")

        java_file = tmp_path / "Main.java"
        java_file.write_text("package com.example;\npublic class Main {}\n", encoding="utf-8")

        self._patches(monkeypatch, _mod, _lsp_mod, ws)
        _mod.extract_java_lsp_edges([java_file], tmp_path, {"nodes": [self._node(java_file)]})

        assert sentinel.exists(), "warm workspace without marker must be preserved"

    def test_workspace_wiped_when_java_version_changes(self, tmp_path, monkeypatch):
        from icx_engine.graph.parser.resolvers import java_lsp as _mod
        import icx_engine.graph.parser.lsp_client as _lsp_mod

        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "icx-ready").write_text("openjdk 11", encoding="utf-8")
        sentinel = ws / "old-index.db"
        sentinel.write_text("old", encoding="utf-8")

        java_file = tmp_path / "Main.java"
        java_file.write_text("package com.example;\npublic class Main {}\n", encoding="utf-8")

        self._patches(monkeypatch, _mod, _lsp_mod, ws, java_ver="openjdk 17")
        _mod.extract_java_lsp_edges([java_file], tmp_path, {"nodes": [self._node(java_file)]})

        assert not sentinel.exists(), "workspace must be wiped when Java version changed"

    def test_workspace_preserved_when_java_version_matches(self, tmp_path, monkeypatch):
        from icx_engine.graph.parser.resolvers import java_lsp as _mod
        import icx_engine.graph.parser.lsp_client as _lsp_mod

        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "icx-ready").write_text("openjdk 17", encoding="utf-8")
        sentinel = ws / "warm-index.db"
        sentinel.write_text("warm", encoding="utf-8")

        java_file = tmp_path / "Main.java"
        java_file.write_text("package com.example;\npublic class Main {}\n", encoding="utf-8")

        self._patches(monkeypatch, _mod, _lsp_mod, ws, java_ver="openjdk 17")
        _mod.extract_java_lsp_edges([java_file], tmp_path, {"nodes": [self._node(java_file)]})

        assert sentinel.exists(), "workspace must be preserved when Java version matches"
