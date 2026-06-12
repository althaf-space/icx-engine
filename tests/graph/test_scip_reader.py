"""Tests for Phase 10: SCIP optional compiler-grade integration."""
import shutil
from pathlib import Path
from unittest.mock import patch, MagicMock
import tempfile
import pytest

import subprocess as _subprocess

from icx_engine.graph.parser.scip_reader import (
    detect_scip_indexers,
    run_scip_indexer,
    read_scip_edges,
    _read_varint,
    _iter_fields,
    _parse_scip_index,
    _SCIP_TIMEOUTS,
    _SCIP_DEFAULT_TIMEOUT,
    BACKGROUND_SPAWNED,
    _BACKGROUND_ON_LAUNCH,
)


# ---------------------------------------------------------------------------
# Helper: build minimal valid SCIP Index protobuf binary for testing
# ---------------------------------------------------------------------------

def _enc_varint(n: int) -> bytes:
    result = []
    while True:
        chunk = n & 0x7F
        n >>= 7
        if n:
            result.append(chunk | 0x80)
        else:
            result.append(chunk)
            break
    return bytes(result)


def _enc_ld(field_num: int, data: bytes) -> bytes:
    tag = (field_num << 3) | 2
    return _enc_varint(tag) + _enc_varint(len(data)) + data


def _enc_str(field_num: int, s: str) -> bytes:
    return _enc_ld(field_num, s.encode("utf-8"))


def _enc_int(field_num: int, v: int) -> bytes:
    return _enc_varint(field_num << 3) + _enc_varint(v)


def _make_scip_index(docs: list[tuple[str, list[tuple[str, int]]]]) -> bytes:
    """Build a minimal SCIP Index binary: list of (relative_path, [(symbol, roles)])."""
    index_bytes = b""
    for rel_path, occs in docs:
        doc_bytes = _enc_str(1, rel_path)
        for symbol, roles in occs:
            occ_bytes = _enc_str(2, symbol)
            if roles:
                occ_bytes += _enc_int(3, roles)
            doc_bytes += _enc_ld(2, occ_bytes)
        index_bytes += _enc_ld(2, doc_bytes)
    return index_bytes


class TestDetectScipIndexers:
    def test_empty_when_no_indexers_on_path(self):
        with patch("shutil.which", return_value=None):
            result = detect_scip_indexers(["python", "typescript", "go"])
        assert result == {}

    def test_detects_available_indexer(self):
        def mock_which(cmd):
            return "/usr/bin/" + cmd if cmd == "scip-python" else None
        with patch("shutil.which", side_effect=mock_which):
            result = detect_scip_indexers(["python"])
        assert "python" in result
        assert result["python"] == "scip-python"

    def test_unknown_language_not_included(self):
        with patch("shutil.which", return_value="/usr/bin/something"):
            result = detect_scip_indexers(["cobol"])
        assert result == {}


class TestRunScipIndexer:
    def test_returns_none_when_binary_not_found(self, tmp_path):
        with patch("shutil.which", return_value=None):
            result = run_scip_indexer("python", str(tmp_path), tmp_path)
        assert result is None

    def test_use_cache_true_returns_existing_without_running(self, tmp_path):
        """use_cache=True: if .scip exists, return it immediately, never run indexer."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()

        scip_file = cache_dir / "python.scip"
        scip_file.write_bytes(b"cached scip content")

        ran = []
        def mock_run(cmd, **kwargs):
            ran.append(True)
            m = MagicMock(); m.returncode = 0; return m

        with patch("shutil.which", return_value="/usr/bin/scip-python"), \
             patch("subprocess.run", side_effect=mock_run):
            result = run_scip_indexer("python", str(project_dir), cache_dir, use_cache=True)
        assert not ran, "subprocess must NOT run when use_cache=True and .scip exists"
        assert result == scip_file

    def test_use_cache_false_runs_indexer_even_when_file_exists(self, tmp_path):
        """use_cache=False: always run indexer regardless of cached .scip."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()

        scip_file = cache_dir / "python.scip"
        scip_file.write_bytes(b"stale scip content")

        ran = []
        def mock_run(cmd, **kwargs):
            ran.append(True)
            (project_dir / "index.scip").write_bytes(b"fresh scip content")
            m = MagicMock(); m.returncode = 0; return m

        with patch("shutil.which", return_value="/usr/bin/scip-python"), \
             patch("subprocess.run", side_effect=mock_run):
            result = run_scip_indexer("python", str(project_dir), cache_dir, use_cache=False)
        assert ran, "indexer must run when use_cache=False"
        assert result == scip_file

    def test_use_cache_true_no_existing_file_runs_indexer(self, tmp_path):
        """use_cache=True but no .scip yet: run indexer normally."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()

        ran = []
        def mock_run(cmd, **kwargs):
            ran.append(True)
            (project_dir / "index.scip").write_bytes(b"new scip content")
            m = MagicMock(); m.returncode = 0; return m

        with patch("shutil.which", return_value="/usr/bin/scip-python"), \
             patch("subprocess.run", side_effect=mock_run):
            result = run_scip_indexer("python", str(project_dir), cache_dir, use_cache=True)
        assert ran, "indexer must run when no cached .scip exists"
        assert result == cache_dir / "python.scip"

    def test_spawns_background_daemon_on_timeout(self, tmp_path):
        with patch("shutil.which", return_value="/usr/bin/scip-python"), \
             patch("subprocess.run", side_effect=_subprocess.TimeoutExpired("scip-python", 180)), \
             patch("icx_engine.graph.parser.scip_reader._spawn_background_scip") as mock_spawn:
            result = run_scip_indexer("python", str(tmp_path), tmp_path)
        assert result is BACKGROUND_SPAWNED
        assert mock_spawn.called

    def test_java_uses_180s_timeout(self, tmp_path):
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()

        captured = []
        def mock_run(cmd, **kwargs):
            captured.append(kwargs)
            m = MagicMock(); m.returncode = 1; return m

        with patch("shutil.which", return_value="/usr/bin/scip-java"), \
             patch("subprocess.run", side_effect=mock_run):
            run_scip_indexer("java", str(project_dir), cache_dir)
        assert captured and captured[0]["timeout"] == 180

    def test_python_uses_default_timeout(self, tmp_path):
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()

        captured = []
        def mock_run(cmd, **kwargs):
            captured.append(kwargs)
            m = MagicMock(); m.returncode = 1; return m

        with patch("shutil.which", return_value="/usr/bin/scip-python"), \
             patch("subprocess.run", side_effect=mock_run):
            run_scip_indexer("python", str(project_dir), cache_dir)
        assert captured and captured[0]["timeout"] == _SCIP_DEFAULT_TIMEOUT

    def test_typescript_always_spawns_background_no_sync_run(self, tmp_path):
        """TypeScript never runs synchronously - always spawns background daemon."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()

        popen_calls = []
        run_calls = []
        with patch("shutil.which", return_value="/usr/bin/scip-typescript"), \
             patch("subprocess.run", side_effect=lambda *a, **k: run_calls.append(a)), \
             patch("subprocess.Popen", side_effect=lambda *a, **k: popen_calls.append(a) or MagicMock()):
            result = run_scip_indexer("typescript", str(project_dir), cache_dir)

        assert not run_calls, "subprocess.run must NOT be called for TypeScript"
        assert popen_calls, "subprocess.Popen must be called (background daemon)"
        assert result is BACKGROUND_SPAWNED

    def test_javascript_always_spawns_background(self, tmp_path):
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()

        popen_calls = []
        with patch("shutil.which", return_value="/usr/bin/scip-typescript"), \
             patch("subprocess.Popen", side_effect=lambda *a, **k: popen_calls.append(a) or MagicMock()):
            result = run_scip_indexer("javascript", str(project_dir), cache_dir)

        assert popen_calls
        assert result is BACKGROUND_SPAWNED

    def test_typescript_cache_hit_returns_path_no_daemon(self, tmp_path):
        """Cache hit: return cached .scip (no lock) instantly, no daemon spawned."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        scip_file = cache_dir / "typescript.scip"
        scip_file.write_bytes(b"cached content")
        # No lock file = daemon not running = safe to return cache

        popen_calls = []
        with patch("shutil.which", return_value="/usr/bin/scip-typescript"), \
             patch("subprocess.Popen", side_effect=lambda *a, **k: popen_calls.append(a) or MagicMock()):
            result = run_scip_indexer("typescript", str(project_dir), cache_dir, use_cache=True)

        assert result == scip_file
        assert not popen_calls, "No daemon should spawn on cache hit"

    def test_lock_present_skips_spawn_returns_background_spawned(self, tmp_path):
        """Active lock: another daemon already running; skip spawn."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        lock = cache_dir / "typescript.building"
        lock.touch()

        popen_calls = []
        with patch("shutil.which", return_value="/usr/bin/scip-typescript"), \
             patch("subprocess.Popen", side_effect=lambda *a, **k: popen_calls.append(a) or MagicMock()):
            result = run_scip_indexer("typescript", str(project_dir), cache_dir)

        assert not popen_calls, "Must not spawn second daemon when lock exists"
        assert result is BACKGROUND_SPAWNED

    def test_stale_lock_removed_and_daemon_respawned(self, tmp_path):
        """Lock older than 2h = stale; remove it and spawn fresh daemon."""
        import time as _time
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        lock = cache_dir / "typescript.building"
        lock.touch()
        # Make lock appear 3 hours old
        old_time = _time.time() - 10800
        import os
        os.utime(str(lock), (old_time, old_time))

        popen_calls = []
        with patch("shutil.which", return_value="/usr/bin/scip-typescript"), \
             patch("subprocess.Popen", side_effect=lambda *a, **k: popen_calls.append(a) or MagicMock()):
            result = run_scip_indexer("typescript", str(project_dir), cache_dir)

        assert popen_calls, "Stale lock must be removed and daemon respawned"
        assert result is BACKGROUND_SPAWNED
        assert not lock.exists(), "Stale lock file must be removed before spawn"

    def test_cache_with_active_lock_spawns_new_daemon(self, tmp_path):
        """.scip exists but lock also exists = partial/stale file; spawn new daemon."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        scip_file = cache_dir / "typescript.scip"
        scip_file.write_bytes(b"partial content")
        lock = cache_dir / "typescript.building"
        lock.touch()

        popen_calls = []
        with patch("shutil.which", return_value="/usr/bin/scip-typescript"), \
             patch("subprocess.Popen", side_effect=lambda *a, **k: popen_calls.append(a) or MagicMock()):
            # use_cache=True but lock present -> don't return partial file
            result = run_scip_indexer("typescript", str(project_dir), cache_dir, use_cache=True)

        assert not popen_calls, "Must not spawn when lock exists (daemon still running)"
        assert result is BACKGROUND_SPAWNED

    def test_background_daemon_uses_tmp_output_path(self, tmp_path):
        """Daemon must write to .tmp via --output, not directly to .scip."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()

        script_args = []
        def mock_popen(args, **kwargs):
            script_args.append(args)
            return MagicMock()

        with patch("shutil.which", return_value="/usr/bin/node"), \
             patch("subprocess.Popen", side_effect=mock_popen):
            run_scip_indexer("typescript", str(project_dir), cache_dir)

        assert script_args
        # Popen is called with [python, "-c", script_code]
        script_code = script_args[0][2]  # third arg = inline Python script
        assert "typescript.scip.tmp" in script_code, "daemon must write to .tmp file"
        assert "shutil.move" in script_code, "daemon must rename .tmp -> .scip"
        assert "lock.touch()" in script_code, "daemon must create lock file"
        assert "lock.unlink" in script_code, "daemon must remove lock on completion"

    def test_uses_devnull_not_pipes(self, tmp_path):
        """stdout/stderr must be DEVNULL to prevent pipe-hold hang on Windows."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()

        captured = []
        def mock_run(cmd, **kwargs):
            captured.append(kwargs)
            m = MagicMock(); m.returncode = 1; return m

        with patch("shutil.which", return_value="/usr/bin/scip-python"), \
             patch("subprocess.run", side_effect=mock_run):
            run_scip_indexer("python", str(project_dir), cache_dir)
        assert captured
        kw = captured[0]
        assert kw.get("stdout") == _subprocess.DEVNULL
        assert kw.get("stderr") == _subprocess.DEVNULL
        assert not kw.get("capture_output", False)

    def test_cleanup_on_nonzero_exit(self, tmp_path):
        # Indexer fails but leaves partial index.scip - must be removed
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()

        def mock_run(cmd, **kwargs):
            (project_dir / "index.scip").write_bytes(b"partial")
            m = MagicMock()
            m.returncode = 1
            m.stdout = ""
            m.stderr = "error"
            return m

        with patch("shutil.which", return_value="/usr/bin/scip-python"), \
             patch("subprocess.run", side_effect=mock_run):
            result = run_scip_indexer("python", str(project_dir), cache_dir)
        assert result is None
        assert not (project_dir / "index.scip").exists()

    def test_cleanup_on_cross_drive_move_failure(self, tmp_path):
        # shutil.move raises OSError (Windows cross-drive) - index.scip must be removed
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()

        def mock_run(cmd, **kwargs):
            (project_dir / "index.scip").write_bytes(b"fake scip")
            m = MagicMock()
            m.returncode = 0
            m.stdout = ""
            m.stderr = ""
            return m

        with patch("shutil.which", return_value="/usr/bin/scip-python"), \
             patch("subprocess.run", side_effect=mock_run), \
             patch("icx_engine.graph.parser.scip_reader.shutil.move",
                   side_effect=OSError(17, "cross-drive move")):
            result = run_scip_indexer("python", str(project_dir), cache_dir)
        assert result is None
        assert not (project_dir / "index.scip").exists()


class TestRunScipIndexerWithCmd:
    def test_uses_provided_cmd_instead_of_shutil_which(self, tmp_path):
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()

        ran_cmds = []
        def mock_run(cmd, **kwargs):
            ran_cmds.append(cmd)
            (project_dir / "index.scip").write_bytes(b"fake")
            m = MagicMock()
            m.returncode = 0
            return m

        with patch("subprocess.run", side_effect=mock_run), \
             patch("shutil.which") as mock_which:
            run_scip_indexer(
                "python", str(project_dir), cache_dir,
                cmd=["/node", "/fake/main.js", "index", "."],
            )
        mock_which.assert_not_called()
        assert ran_cmds and ran_cmds[0][0] == "/node"

    def test_uses_extra_env_when_provided(self, tmp_path):
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()

        captured_env = []
        def mock_run(cmd, **kwargs):
            captured_env.append(kwargs.get("env", {}))
            (project_dir / "index.scip").write_bytes(b"fake")
            m = MagicMock()
            m.returncode = 0
            return m

        with patch("subprocess.run", side_effect=mock_run):
            run_scip_indexer(
                "python", str(project_dir), cache_dir,
                cmd=["/node", "/fake/main.js", "index", "."],
                extra_env={"MY_VAR": "my_val"},
            )
        assert captured_env and captured_env[0].get("MY_VAR") == "my_val"

    def test_no_extra_env_does_not_pass_env_kwarg(self, tmp_path):
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()

        run_kwargs = []
        def mock_run(cmd, **kwargs):
            run_kwargs.append(kwargs)
            (project_dir / "index.scip").write_bytes(b"fake")
            m = MagicMock()
            m.returncode = 0
            return m

        with patch("subprocess.run", side_effect=mock_run):
            run_scip_indexer(
                "python", str(project_dir), cache_dir,
                cmd=["/node", "/fake/main.js", "index", "."],
            )
        assert run_kwargs and "env" not in run_kwargs[0]


class TestParseScipIndex:
    def test_empty_bytes_returns_empty(self):
        assert _parse_scip_index(b"") == []

    def test_parses_single_document(self):
        raw = _make_scip_index([("src.py", [("pkg/Foo#bar().", 1)])])
        docs = _parse_scip_index(raw)
        assert len(docs) == 1
        rel_path, occs = docs[0]
        assert rel_path == "src.py"
        assert len(occs) == 1
        assert occs[0] == ("pkg/Foo#bar().", 1)

    def test_parses_multiple_documents(self):
        raw = _make_scip_index([
            ("tgt.py", [("sym#method().", 1)]),
            ("src.py", [("sym#method().", 0)]),
        ])
        docs = _parse_scip_index(raw)
        assert len(docs) == 2
        assert docs[0][0] == "tgt.py"
        assert docs[1][0] == "src.py"

    def test_symbol_roles_zero_omitted_field_defaults_to_zero(self):
        # roles=0 is the default, field may be omitted in encoding
        raw = _make_scip_index([("a.py", [("sym#", 0)])])
        docs = _parse_scip_index(raw)
        assert docs[0][1][0][1] == 0

    def test_malformed_bytes_returns_empty(self):
        assert _parse_scip_index(b"\xff\xff\xff\xff") == []


class TestReadScipEdges:
    def test_missing_file_returns_empty(self, tmp_path):
        result = read_scip_edges(tmp_path / "nonexistent.scip", [])
        assert result == []

    def test_malformed_protobuf_returns_empty(self, tmp_path):
        scip_file = tmp_path / "bad.scip"
        # Bytes that produce a malformed varint (incomplete sequence)
        scip_file.write_bytes(b"\x0a\xff\xff\xff\xff\xff")
        result = read_scip_edges(scip_file, [{"id": "n1", "source_file": "a.py"}])
        assert result == []

    def test_cross_file_reference_produces_edge(self, tmp_path):
        # tgt.py defines the symbol; src.py references it
        raw = _make_scip_index([
            ("tgt.py", [("pkg/Foo#bar().", 1)]),   # definition
            ("src.py", [("pkg/Foo#bar().", 0)]),    # reference
        ])
        scip_file = tmp_path / "python.scip"
        scip_file.write_bytes(raw)

        nodes = [
            {"id": "src_n", "source_file": "src.py"},
            {"id": "tgt_n", "source_file": "tgt.py"},
        ]
        edges = read_scip_edges(scip_file, nodes)
        assert len(edges) == 1
        e = edges[0]
        assert e["source"] == "src_n"
        assert e["target"] == "tgt_n"
        assert e["source_file"] == "src.py"
        assert e["target_file"] == "tgt.py"

    def test_definition_not_emitted_as_edge(self, tmp_path):
        # Only definitions present - no references - no edges
        raw = _make_scip_index([
            ("a.py", [("pkg/A#method().", 1)]),
            ("b.py", [("pkg/B#method().", 1)]),
        ])
        scip_file = tmp_path / "python.scip"
        scip_file.write_bytes(raw)
        nodes = [
            {"id": "a_n", "source_file": "a.py"},
            {"id": "b_n", "source_file": "b.py"},
        ]
        assert read_scip_edges(scip_file, nodes) == []

    def test_self_reference_not_emitted(self, tmp_path):
        # Symbol defined and referenced in same file - no cross-file edge
        raw = _make_scip_index([
            ("a.py", [("pkg/A#method().", 1), ("pkg/A#method().", 0)]),
        ])
        scip_file = tmp_path / "python.scip"
        scip_file.write_bytes(raw)
        nodes = [{"id": "a_n", "source_file": "a.py"}]
        assert read_scip_edges(scip_file, nodes) == []

    def test_edge_has_relation_field(self, tmp_path):
        raw = _make_scip_index([
            ("tgt.py", [("sym#x().", 1)]),
            ("src.py", [("sym#x().", 0)]),
        ])
        scip_file = tmp_path / "python.scip"
        scip_file.write_bytes(raw)
        nodes = [
            {"id": "src_n", "source_file": "src.py"},
            {"id": "tgt_n", "source_file": "tgt.py"},
        ]
        edges = read_scip_edges(scip_file, nodes)
        assert edges
        assert all("relation" in e for e in edges)
        assert all(e["relation"] == "scip_reference" for e in edges)

    def test_edge_confidence_is_0_95(self, tmp_path):
        raw = _make_scip_index([
            ("tgt.py", [("sym#x().", 1)]),
            ("src.py", [("sym#x().", 0)]),
        ])
        scip_file = tmp_path / "python.scip"
        scip_file.write_bytes(raw)
        nodes = [
            {"id": "src_n", "source_file": "src.py"},
            {"id": "tgt_n", "source_file": "tgt.py"},
        ]
        edges = read_scip_edges(scip_file, nodes)
        assert edges
        assert all(e["confidence"] == 0.95 for e in edges)

    def test_no_nodes_for_file_produces_no_edge(self, tmp_path):
        raw = _make_scip_index([
            ("tgt.py", [("sym#x().", 1)]),
            ("src.py", [("sym#x().", 0)]),
        ])
        scip_file = tmp_path / "python.scip"
        scip_file.write_bytes(raw)
        # Only provide tgt node, no src node
        nodes = [{"id": "tgt_n", "source_file": "tgt.py"}]
        assert read_scip_edges(scip_file, nodes) == []

    def test_absolute_node_paths_matched_via_project_path(self, tmp_path):
        # Nodes store absolute paths; SCIP emits relative paths.
        # read_scip_edges must strip the project prefix so lookups work.
        raw = _make_scip_index([
            ("tgt.py", [("sym#x().", 1)]),
            ("src.py", [("sym#x().", 0)]),
        ])
        scip_file = tmp_path / "python.scip"
        scip_file.write_bytes(raw)
        project = "/project/root"
        nodes = [
            {"id": "src_n", "source_file": f"{project}/src.py"},
            {"id": "tgt_n", "source_file": f"{project}/tgt.py"},
        ]
        edges = read_scip_edges(scip_file, nodes, project_path=project)
        assert len(edges) == 1
        assert edges[0]["source"] == "src_n"
        assert edges[0]["target"] == "tgt_n"

    def test_absolute_node_paths_windows_backslash(self, tmp_path):
        # Windows backslash paths in nodes with forward-slash project_path
        raw = _make_scip_index([
            ("tgt.py", [("sym#x().", 1)]),
            ("src.py", [("sym#x().", 0)]),
        ])
        scip_file = tmp_path / "python.scip"
        scip_file.write_bytes(raw)
        nodes = [
            {"id": "src_n", "source_file": "C:\\project\\src.py"},
            {"id": "tgt_n", "source_file": "C:\\project\\tgt.py"},
        ]
        edges = read_scip_edges(scip_file, nodes, project_path="C:/project")
        assert len(edges) == 1

    def test_no_project_path_relative_nodes_still_match(self, tmp_path):
        # If node paths are already relative, no project_path needed
        raw = _make_scip_index([
            ("tgt.py", [("sym#x().", 1)]),
            ("src.py", [("sym#x().", 0)]),
        ])
        scip_file = tmp_path / "python.scip"
        scip_file.write_bytes(raw)
        nodes = [
            {"id": "src_n", "source_file": "src.py"},
            {"id": "tgt_n", "source_file": "tgt.py"},
        ]
        edges = read_scip_edges(scip_file, nodes)
        assert len(edges) == 1
