"""Tests for SCIP indexer auto-install manager."""
import os
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

import json

from icx_engine.graph.parser.scip_manager import (
    SCIPIndexerConfig,
    ensure_indexer,
    ensure_scip_indexers,
    write_ts_tsconfig,
    java_runtime,
    _find_coursier,
    _read_stored_version,
    _write_version,
    _CONFIGS,
)


def _make_config(
    tmp_path: Path,
    lang: str = "python",
    name: str = "scip-python",
    runtime=None,
    binary_exists: bool = False,
    install_ok: bool = True,
    cmd: list | None = None,
) -> SCIPIndexerConfig:
    install_dir = tmp_path / "scip" / lang
    install_dir.mkdir(parents=True)
    binary_path = install_dir / "main.js"
    if binary_exists:
        binary_path.write_text("// fake script")

    def _install(r, d):
        (d / "main.js").write_text("// installed")
        return install_ok

    return SCIPIndexerConfig(
        language=lang,
        name=name,
        get_runtime=runtime or (lambda: ("/usr/bin/node", "v20.0.0")),
        install_dir=install_dir,
        binary_fn=lambda d: (d / "main.js") if (d / "main.js").exists() else None,
        install_fn=_install,
        cmd_fn=lambda b, r: cmd or [r, str(b), "index", "."],
    )


class TestEnsureIndexer:
    def test_returns_none_when_runtime_absent(self, tmp_path):
        config = _make_config(tmp_path, runtime=lambda: None)
        assert ensure_indexer(config) is None

    def test_installs_and_returns_cmd_when_binary_missing(self, tmp_path):
        config = _make_config(tmp_path, binary_exists=False, install_ok=True)
        result = ensure_indexer(config)
        assert result is not None
        cmd, env = result
        assert isinstance(cmd, list)
        assert len(cmd) > 0
        assert env == {}

    def test_skips_install_when_version_matches(self, tmp_path):
        config = _make_config(tmp_path, binary_exists=True)
        _write_version(config.install_dir, "v20.0.0")
        install_called = []
        config.install_fn = lambda r, d: install_called.append(True) or True

        result = ensure_indexer(config)
        assert result is not None
        assert install_called == []

    def test_reinstalls_on_version_drift(self, tmp_path):
        config = _make_config(tmp_path, binary_exists=True)
        _write_version(config.install_dir, "v18.0.0")

        installed = []
        original = config.install_fn
        def tracking(r, d):
            installed.append(True)
            return original(r, d)
        config.install_fn = tracking

        result = ensure_indexer(config)
        assert result is not None
        assert installed == [True]

    def test_old_dir_removed_on_version_drift(self, tmp_path):
        config = _make_config(tmp_path, binary_exists=True)
        sentinel = config.install_dir / "old-sentinel.txt"
        sentinel.write_text("old")
        _write_version(config.install_dir, "v18.0.0")
        config.install_fn = lambda r, d: True  # succeeds but writes no binary
        config.binary_fn = lambda d: None

        ensure_indexer(config)
        assert not sentinel.exists()

    def test_returns_none_when_install_fails(self, tmp_path):
        config = _make_config(tmp_path, binary_exists=False)
        config.install_fn = lambda r, d: False
        assert ensure_indexer(config) is None

    def test_returns_none_when_binary_missing_after_install(self, tmp_path):
        config = _make_config(tmp_path, binary_exists=False)
        config.install_fn = lambda r, d: True
        config.binary_fn = lambda d: None
        assert ensure_indexer(config) is None

    def test_version_file_written_after_install(self, tmp_path):
        config = _make_config(tmp_path, binary_exists=False, install_ok=True)
        ensure_indexer(config)
        assert _read_stored_version(config.install_dir) == "v20.0.0"

    def test_cmd_contains_binary_path(self, tmp_path):
        config = _make_config(tmp_path, binary_exists=False, install_ok=True)
        result = ensure_indexer(config)
        assert result is not None
        cmd, _ = result
        assert any("main.js" in part for part in cmd)

    def test_extra_env_is_empty_dict(self, tmp_path):
        config = _make_config(tmp_path, binary_exists=True)
        _write_version(config.install_dir, "v20.0.0")
        result = ensure_indexer(config)
        assert result is not None
        _, env = result
        assert env == {}


class TestEnsureScipIndexers:
    def test_empty_for_unsupported_languages(self):
        result = ensure_scip_indexers(["cobol", "pascal", "fortran", "rust"])
        assert result == {}

    def test_skips_when_node_runtime_absent(self, tmp_path):
        with patch("icx_engine.graph.parser.scip_manager.node_runtime", return_value=None), \
             patch("icx_engine.graph.parser.scip_manager.go_runtime", return_value=None):
            result = ensure_scip_indexers(["python", "typescript", "go"])
        assert result == {}

    def test_typescript_and_javascript_share_single_install(self, tmp_path):
        ensure_calls = []
        def counting_ensure(config):
            ensure_calls.append(str(config.install_dir))
            return None

        with patch("icx_engine.graph.parser.scip_manager.ensure_indexer",
                   side_effect=counting_ensure):
            ensure_scip_indexers(["typescript", "javascript"])

        # Both share the same install_dir -> only one ensure_indexer call
        assert len(ensure_calls) == 1

    def test_returns_cmd_for_mocked_available_language(self):
        fake_result = (["node", "/fake/main.js", "index", "."], {})
        with patch("icx_engine.graph.parser.scip_manager.ensure_indexer",
                   return_value=fake_result):
            result = ensure_scip_indexers(["python"])
        assert "python" in result
        cmd, env = result["python"]
        assert cmd[0] == "node"
        assert env == {}

    def test_unknown_language_excluded(self):
        with patch("icx_engine.graph.parser.scip_manager.ensure_indexer",
                   return_value=(["node", "/x"], {})):
            result = ensure_scip_indexers(["rust", "python"])
        assert "rust" not in result
        assert "python" in result

    def test_failed_indexer_excluded_from_result(self):
        with patch("icx_engine.graph.parser.scip_manager.ensure_indexer",
                   return_value=None):
            result = ensure_scip_indexers(["python"])
        assert result == {}


class TestConfigRegistry:
    def test_python_in_registry(self):
        assert "python" in _CONFIGS
        assert _CONFIGS["python"].name == "scip-python"

    def test_typescript_in_registry(self):
        assert "typescript" in _CONFIGS
        assert _CONFIGS["typescript"].name == "scip-typescript"

    def test_javascript_maps_to_typescript_config(self):
        assert _CONFIGS["javascript"] is _CONFIGS["typescript"]

    def test_go_in_registry(self):
        assert "go" in _CONFIGS
        assert _CONFIGS["go"].name == "scip-go"

    def test_java_auto_installed(self):
        assert "java" in _CONFIGS
        assert _CONFIGS["java"].name == "scip-java"

    def test_kotlin_auto_installed(self):
        assert "kotlin" in _CONFIGS
        assert _CONFIGS["kotlin"].name == "scip-java"

    def test_ruby_not_auto_installed(self):
        assert "ruby" not in _CONFIGS


class TestVersionHelpers:
    def test_read_stored_version_missing_file(self, tmp_path):
        assert _read_stored_version(tmp_path) is None

    def test_round_trip(self, tmp_path):
        _write_version(tmp_path, "v20.1.0")
        assert _read_stored_version(tmp_path) == "v20.1.0"

    def test_corrupted_json_returns_none(self, tmp_path):
        (tmp_path / "runtime-version.json").write_text("not valid json {{{")
        assert _read_stored_version(tmp_path) is None

    def test_missing_full_key_returns_none(self, tmp_path):
        (tmp_path / "runtime-version.json").write_text('{"other": "value"}')
        assert _read_stored_version(tmp_path) is None


class TestWriteTsTsconfig:
    def test_creates_file_at_dest(self, tmp_path):
        project = tmp_path / "project"
        project.mkdir()
        (project / "src").mkdir()
        dest = tmp_path / "icx-tsconfig.json"
        write_ts_tsconfig(str(project), [str(project / "src" / "app.js")], dest)
        assert dest.exists()

    def test_sets_allowjs_true(self, tmp_path):
        project = tmp_path / "project"
        project.mkdir()
        dest = tmp_path / "icx-tsconfig.json"
        write_ts_tsconfig(str(project), [], dest)
        data = json.loads(dest.read_text())
        assert data["compilerOptions"]["allowJs"] is True

    def test_include_contains_source_dir(self, tmp_path):
        project = tmp_path / "project"
        (project / "src").mkdir(parents=True)
        dest = tmp_path / "icx-tsconfig.json"
        write_ts_tsconfig(str(project), [str(project / "src" / "app.js")], dest)
        data = json.loads(dest.read_text())
        assert any("src" in inc for inc in data["include"])

    def test_noise_dir_not_in_include(self, tmp_path):
        # Files only from node_modules should not add node_modules to include
        project = tmp_path / "proj"
        project.mkdir()
        dest = tmp_path / "icx-tsconfig.json"
        files = [str(project / "node_modules" / "pkg" / "index.js")]
        write_ts_tsconfig(str(project), files, dest)
        data = json.loads(dest.read_text())
        # include should be project root fallback, not a node_modules path
        assert not any(inc.rstrip("/**/*").endswith("node_modules") for inc in data["include"])

    def test_excludes_node_modules(self, tmp_path):
        project = tmp_path / "project"
        project.mkdir()
        dest = tmp_path / "icx-tsconfig.json"
        write_ts_tsconfig(str(project), [], dest)
        data = json.loads(dest.read_text())
        assert any("node_modules" in exc for exc in data["exclude"])

    def test_excludes_war_directory(self, tmp_path):
        project = tmp_path / "project"
        project.mkdir()
        (project / "Work_Order_UI.war").mkdir()
        dest = tmp_path / "icx-tsconfig.json"
        write_ts_tsconfig(str(project), [str(project / "src" / "app.js")], dest)
        data = json.loads(dest.read_text())
        assert any(".war" in exc for exc in data["exclude"])

    def test_fallback_when_no_source_files(self, tmp_path):
        project = tmp_path / "project"
        project.mkdir()
        dest = tmp_path / "icx-tsconfig.json"
        write_ts_tsconfig(str(project), [], dest)
        data = json.loads(dest.read_text())
        assert len(data["include"]) >= 1

    def test_multiple_source_dirs_all_included(self, tmp_path):
        project = tmp_path / "project"
        (project / "src").mkdir(parents=True)
        (project / "lib").mkdir()
        dest = tmp_path / "icx-tsconfig.json"
        files = [
            str(project / "src" / "app.js"),
            str(project / "lib" / "util.js"),
        ]
        write_ts_tsconfig(str(project), files, dest)
        data = json.loads(dest.read_text())
        includes = data["include"]
        assert any("src" in inc for inc in includes)
        assert any("lib" in inc for inc in includes)

    def test_baseurl_set_to_project_root(self, tmp_path):
        """baseUrl must equal the project root so bare-specifier imports resolve
        correctly when the tsconfig lives in a different directory (ICX cache)."""
        project = tmp_path / "project"
        project.mkdir()
        dest = tmp_path / "scip" / "icx-tsconfig.json"
        dest.parent.mkdir()
        write_ts_tsconfig(str(project), [], dest)
        data = json.loads(dest.read_text())
        base_url = data["compilerOptions"].get("baseUrl", "")
        assert base_url, "baseUrl must be set in compilerOptions"
        assert base_url.replace("\\", "/") == project.as_posix()

    def test_baseurl_is_project_not_cache_dir(self, tmp_path):
        """baseUrl must NOT be the tsconfig's own directory (the scip cache)."""
        project = tmp_path / "project"
        project.mkdir()
        cache = tmp_path / ".icx" / "scip"
        cache.mkdir(parents=True)
        dest = cache / "icx-tsconfig.json"
        write_ts_tsconfig(str(project), [], dest)
        data = json.loads(dest.read_text())
        base_url = data["compilerOptions"].get("baseUrl", "").replace("\\", "/")
        cache_posix = cache.as_posix()
        assert base_url != cache_posix, (
            f"baseUrl ({base_url}) must be the project root, not the scip cache dir ({cache_posix})"
        )

    def test_wrapper_mode_extends_project_tsconfig(self, tmp_path):
        """project_tsconfig provided: output must extend it and limit to .ts/.tsx files."""
        project = tmp_path / "project"
        project.mkdir()
        project_tc = project / "tsconfig.json"
        project_tc.write_text('{"compilerOptions":{"jsx":"react-jsx"}}')
        (project / "src").mkdir()
        ts_file = project / "src" / "app.ts"
        ts_file.write_text("export const x = 1;")
        jsx_file = project / "src" / "comp.jsx"
        jsx_file.write_text("export const C = () => null;")
        dest = tmp_path / "scip" / "icx-tsconfig.json"
        dest.parent.mkdir()
        write_ts_tsconfig(
            str(project), [str(ts_file), str(jsx_file)], dest,
            project_tsconfig=str(project_tc),
        )
        data = json.loads(dest.read_text())
        assert "extends" in data, "wrapper tsconfig must extend project tsconfig"
        assert project_tc.as_posix() in data["extends"].replace("\\", "/")
        files_list = data.get("files", [])
        assert any(f.endswith(".ts") or f.endswith(".tsx") for f in files_list)
        assert not any(f.endswith(".jsx") or f.endswith(".js") for f in files_list)

    def test_wrapper_mode_has_incremental_build_cache(self, tmp_path):
        """Wrapper tsconfig must enable incremental compilation."""
        project = tmp_path / "project"
        project.mkdir()
        project_tc = project / "tsconfig.json"
        project_tc.write_text('{}')
        dest = tmp_path / "scip" / "icx-tsconfig.json"
        dest.parent.mkdir()
        write_ts_tsconfig(str(project), [], dest, project_tsconfig=str(project_tc))
        data = json.loads(dest.read_text())
        assert data["compilerOptions"].get("incremental") is True
        assert "tsBuildInfoFile" in data["compilerOptions"]

    def test_standalone_mode_has_no_extends(self, tmp_path):
        """Without project_tsconfig: standalone tsconfig with allowJs, no extends."""
        project = tmp_path / "project"
        project.mkdir()
        dest = tmp_path / "icx-tsconfig.json"
        write_ts_tsconfig(str(project), [], dest, project_tsconfig=None)
        data = json.loads(dest.read_text())
        assert "extends" not in data
        assert data["compilerOptions"]["allowJs"] is True


class TestJavaRuntime:
    def test_returns_none_when_java_absent(self):
        with patch("shutil.which", return_value=None):
            assert java_runtime() is None

    def test_returns_path_and_version(self):
        def mock_run(cmd, **kwargs):
            m = MagicMock()
            m.returncode = 0
            m.stdout = ""
            m.stderr = 'openjdk version "17.0.1" 2021-10-19\nOpenJDK Runtime Environment'
            return m
        with patch("shutil.which", return_value="/usr/bin/java"), \
             patch("subprocess.run", side_effect=mock_run):
            result = java_runtime()
        assert result is not None
        assert result[0] == "/usr/bin/java"
        assert result[1] == "17.0.1"

    def test_returns_none_on_timeout(self):
        import subprocess as _subprocess
        with patch("shutil.which", return_value="/usr/bin/java"), \
             patch("subprocess.run", side_effect=_subprocess.TimeoutExpired("java", 5)):
            assert java_runtime() is None

    def test_returns_none_when_no_version_in_output(self):
        def mock_run(cmd, **kwargs):
            m = MagicMock()
            m.returncode = 0
            m.stdout = ""
            m.stderr = "some unexpected output"
            return m
        with patch("shutil.which", return_value="/usr/bin/java"), \
             patch("subprocess.run", side_effect=mock_run):
            assert java_runtime() is None


class TestFindCoursier:
    def test_returns_none_when_absent_everywhere(self, tmp_path):
        with patch("shutil.which", return_value=None), \
             patch("icx_engine.graph.parser.scip_manager.ICX_HOME", tmp_path), \
             patch("icx_engine.graph.parser.scip_manager.Path") as mock_path:
            mock_path.return_value.resolve.return_value.parent.parent.parent.parent.parent = tmp_path
            result = _find_coursier()
        assert result is None

    def test_finds_on_path(self):
        with patch("shutil.which", side_effect=lambda n: "/usr/bin/coursier" if n == "coursier" else None):
            result = _find_coursier()
        assert result == "/usr/bin/coursier"

    def test_finds_in_icx_home(self, tmp_path):
        name = "coursier.bat" if os.name == "nt" else "coursier"
        binary = tmp_path / name
        binary.write_text("@echo off")
        with patch("shutil.which", return_value=None), \
             patch("icx_engine.graph.parser.scip_manager.ICX_HOME", tmp_path):
            result = _find_coursier()
        assert result == str(binary)


class TestJavaKotlinInConfigs:
    def test_java_in_configs(self):
        assert "java" in _CONFIGS

    def test_kotlin_in_configs(self):
        assert "kotlin" in _CONFIGS

    def test_java_kotlin_share_install_dir(self):
        assert _CONFIGS["java"].install_dir == _CONFIGS["kotlin"].install_dir

    def test_java_config_name(self):
        assert _CONFIGS["java"].name == "scip-java"

    def test_kotlin_config_name(self):
        assert _CONFIGS["kotlin"].name == "scip-java"


class TestScipPythonEnv:
    def test_python_extra_env_has_node_options(self):
        assert _CONFIGS["python"].extra_env_fn is not None
        env = _CONFIGS["python"].extra_env_fn(Path("."), "/usr/bin/node")
        assert "NODE_OPTIONS" in env
        assert "max-old-space-size" in env["NODE_OPTIONS"]

    def test_ensure_indexer_returns_node_options_for_python(self, tmp_path):
        config = _make_config(
            tmp_path, lang="python", binary_exists=True,
            cmd=["node", "main.js", "index", "."],
        )
        config = SCIPIndexerConfig(
            language="python",
            name="scip-python",
            get_runtime=lambda: ("/usr/bin/node", "v20.0.0"),
            install_dir=config.install_dir,
            binary_fn=config.binary_fn,
            install_fn=config.install_fn,
            cmd_fn=config.cmd_fn,
            extra_env_fn=lambda b, r: {"NODE_OPTIONS": "--max-old-space-size=4096"},
        )
        _write_version(config.install_dir, "v20.0.0")
        result = ensure_indexer(config)
        assert result is not None
        cmd, env = result
        assert env.get("NODE_OPTIONS") == "--max-old-space-size=4096"
