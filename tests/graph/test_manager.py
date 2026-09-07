"""Tests for graph/manager.py (GraphManager integration)."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from icx_engine.graph.manager import GraphManager
from icx_engine.graph import storage
from icx_engine.graph.storage import ProjectInfo, write_meta, derive_project_id
from icx_engine.exceptions import GraphError


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolated_graphs(tmp_path, monkeypatch):
    graphs_root = tmp_path / "graphs"
    graphs_root.mkdir()
    monkeypatch.setattr("icx_engine.graph.storage._graphs_root", lambda: graphs_root)
    monkeypatch.setattr("icx_engine.graph.manager.storage._graphs_root", lambda: graphs_root)
    return graphs_root


@pytest.fixture
def project_dir(tmp_path):
    d = tmp_path / "myapp"
    d.mkdir()
    return d


@pytest.fixture
def registered(project_dir):
    mgr = GraphManager()
    pid = mgr.register("myapp", str(project_dir))
    return pid, project_dir


# ---------------------------------------------------------------------------
# register
# ---------------------------------------------------------------------------

def test_register_returns_project_id(project_dir):
    mgr = GraphManager()
    pid = mgr.register("myapp", str(project_dir))
    assert len(pid) == 12


def test_register_empty_name_raises(project_dir):
    mgr = GraphManager()
    with pytest.raises(GraphError, match="cannot be empty"):
        mgr.register("", str(project_dir))


def test_register_invalid_path_raises():
    mgr = GraphManager()
    with pytest.raises(GraphError):
        mgr.register("app", "/nonexistent/path/xyz")


def test_register_normalizes_name(project_dir):
    mgr = GraphManager()
    pid = mgr.register("MyApp", str(project_dir))
    meta = storage.read_meta(pid)
    assert meta.name == "myapp"


# ---------------------------------------------------------------------------
# get_status
# ---------------------------------------------------------------------------

def test_get_status_not_built(registered):
    pid, _ = registered
    mgr = GraphManager()
    assert mgr.get_status(pid) == "not_built"


def test_get_status_unknown_raises():
    mgr = GraphManager()
    with pytest.raises(GraphError):
        mgr.get_status("nonexistentid1")


# ---------------------------------------------------------------------------
# list_projects
# ---------------------------------------------------------------------------

def test_list_projects_returns_registered(registered, project_dir, tmp_path):
    pid, _ = registered
    projects = GraphManager().list_projects()
    assert any(p.project_id == pid for p in projects)


# ---------------------------------------------------------------------------
# remove
# ---------------------------------------------------------------------------

def test_remove_project(registered):
    pid, _ = registered
    mgr = GraphManager()
    mgr.remove(pid)
    assert storage.lookup_by_name("myapp") is None


def test_remove_unknown_raises():
    mgr = GraphManager()
    with pytest.raises(GraphError):
        mgr.remove("nonexistentidx")


# ---------------------------------------------------------------------------
# resolve_project
# ---------------------------------------------------------------------------

def test_resolve_by_name(registered):
    pid, _ = registered
    mgr = GraphManager()
    assert mgr.resolve_project(project_name="myapp") == pid


def test_resolve_by_name_case_insensitive(registered):
    pid, _ = registered
    mgr = GraphManager()
    assert mgr.resolve_project(project_name="MyApp") == pid


def test_resolve_by_name_not_found_raises():
    mgr = GraphManager()
    with pytest.raises(GraphError, match="not found"):
        mgr.resolve_project(project_name="doesnotexist")


def test_resolve_by_path(registered, project_dir):
    pid, _ = registered
    mgr = GraphManager()
    assert mgr.resolve_project(project_path=str(project_dir)) == pid


def test_resolve_unregistered_path_raises_and_writes_nothing(tmp_path):
    """An unregistered valid dir must raise - never silently auto-register."""
    unknown = tmp_path / "guessed_root"
    unknown.mkdir()
    mgr = GraphManager()
    before = len(storage.list_projects())
    with pytest.raises(GraphError, match="not registered"):
        mgr.resolve_project(project_path=str(unknown))
    assert len(storage.list_projects()) == before


def test_resolve_no_args_raises():
    mgr = GraphManager()
    with pytest.raises(GraphError):
        mgr.resolve_project()


# ---------------------------------------------------------------------------
# estimate_eta
# ---------------------------------------------------------------------------

def test_estimate_eta_returns_int(registered):
    pid, _ = registered
    mgr = GraphManager()
    eta = mgr.estimate_eta(pid)
    assert isinstance(eta, int)
    assert eta >= 15


# ---------------------------------------------------------------------------
# build - mocked subprocess
# ---------------------------------------------------------------------------

def test_build_success_sets_status_ready(registered):
    pid, project_dir = registered

    fake_result = {
        "file_count": 10, "node_count": 50, "edge_count": 100,
        "community_count": 3, "error": None,
    }

    # Write a fake tmp file to simulate the subprocess writing it
    def _fake_submit(fn, *args, **kwargs):
        # Simulate the subprocess writing graph.json.tmp
        storage.graph_tmp_path(pid).write_text("{}", encoding="utf-8")
        future = MagicMock()
        future.result.return_value = fake_result
        return future

    with patch("icx_engine.graph.manager.current_git_commit", return_value="abc123"):
        with patch.object(GraphManager, "_run_build_subprocess") as mock_build:
            mock_build.return_value = fake_result
            storage.graph_tmp_path(pid).write_text("{}", encoding="utf-8")
            mgr = GraphManager()
            result = mgr.build(pid)

    assert result.get("error") is None
    meta = storage.read_meta(pid)
    assert meta.build_status == "ready"
    assert meta.file_count == 10


def test_build_passes_force_through_to_run_build_subprocess(registered):
    """Plumbing regression guard: GraphManager.build(force=True) must forward force all
    the way to _run_build_subprocess - this is the wiring that was previously missing
    entirely, meaning --force never actually reached the incremental-skip check."""
    pid, project_dir = registered
    fake_result = {
        "file_count": 10, "node_count": 50, "edge_count": 100,
        "community_count": 3, "error": None,
    }

    with patch("icx_engine.graph.manager.current_git_commit", return_value="abc123"):
        with patch.object(GraphManager, "_run_build_subprocess") as mock_build:
            mock_build.return_value = fake_result
            storage.graph_tmp_path(pid).write_text("{}", encoding="utf-8")
            mgr = GraphManager()
            mgr.build(pid, force=True)

    _, kwargs = mock_build.call_args
    assert kwargs.get("force") is True


def test_build_default_force_is_false(registered):
    pid, project_dir = registered
    fake_result = {
        "file_count": 10, "node_count": 50, "edge_count": 100,
        "community_count": 3, "error": None,
    }

    with patch("icx_engine.graph.manager.current_git_commit", return_value="abc123"):
        with patch.object(GraphManager, "_run_build_subprocess") as mock_build:
            mock_build.return_value = fake_result
            storage.graph_tmp_path(pid).write_text("{}", encoding="utf-8")
            mgr = GraphManager()
            mgr.build(pid)

    _, kwargs = mock_build.call_args
    assert kwargs.get("force") is False


def test_run_build_subprocess_passes_force_as_final_positional_arg(registered):
    """_run_build_subprocess itself must submit force as the 8th positional argument to
    _build_project_isolated (after progress_path) - the actual subprocess boundary."""
    pid, project_dir = registered
    meta = storage.read_meta(pid)
    captured: dict = {}

    class _FakeFuture:
        def result(self):
            return {"file_count": 0, "node_count": 0, "edge_count": 0, "community_count": 0, "error": None}

    class _FakeExecutor:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def submit(self, fn, *args, **kwargs):
            captured["args"] = args
            return _FakeFuture()

    with patch("icx_engine.graph.manager.ProcessPoolExecutor", return_value=_FakeExecutor()):
        mgr = GraphManager()
        mgr._run_build_subprocess(meta, force=True)

    assert captured["args"][-1] is True


def test_build_preserves_tracker_project_key(project_dir):
    mgr = GraphManager()
    pid = mgr.register("myapp", str(project_dir), tracker_project_key="PROJ")

    fake_result = {
        "file_count": 10, "node_count": 50, "edge_count": 100,
        "community_count": 3, "error": None,
    }

    with patch("icx_engine.graph.manager.current_git_commit", return_value="abc123"):
        with patch.object(GraphManager, "_run_build_subprocess") as mock_build:
            mock_build.return_value = fake_result
            storage.graph_tmp_path(pid).write_text("{}", encoding="utf-8")
            mgr.build(pid)

    meta = storage.read_meta(pid)
    assert meta.tracker_project_key == "PROJ"


def test_build_unknown_project_raises():
    mgr = GraphManager()
    with pytest.raises(GraphError, match="not found"):
        mgr.build("nonexistentidx")


# ---------------------------------------------------------------------------
# build_background - must never run LLM enrichment, regardless of configuration
# ---------------------------------------------------------------------------

def test_build_background_never_passes_llm_config_even_when_configured(registered):
    """Regression guard: build_background is the unattended, query-triggered auto-rebuild
    path (small-delta staleness) - it must always be AST-only, never reading an LLM config,
    even when the user has a model configured (icx model --add). Only the interactive CLI
    build (icx graph build --llm) may opt in to LLM enrichment."""
    pid, project_dir = registered
    captured: dict = {}

    class _FakeFuture:
        def add_done_callback(self, cb):
            captured["callback"] = cb

    class _FakeExecutor:
        def submit(self, fn, *args, **kwargs):
            captured["fn"] = fn
            captured["args"] = args
            return _FakeFuture()

    with patch("icx_engine.graph.manager._get_build_executor", return_value=_FakeExecutor()):
        with patch(
            "icx_engine.graph.manager._read_icx_llm_cfg",
            return_value=("claude", "sk-real-key", None),
        ) as mock_read_cfg:
            mgr = GraphManager()
            mgr.build_background(pid)

    # llm_backend, llm_api_key, llm_base_url are positional args 4-6 to
    # _build_project_isolated (after project_path, graph_tmp_path, icx_cache_path).
    assert captured["args"][3] is None
    assert captured["args"][4] is None
    assert captured["args"][5] is None
    # And the LLM config lookup itself must never even be consulted - background rebuilds
    # have no code path that reads it at all.
    mock_read_cfg.assert_not_called()


def test_build_background_sets_status_rebuilding(registered):
    pid, project_dir = registered

    class _FakeFuture:
        def add_done_callback(self, cb):
            pass

    class _FakeExecutor:
        def submit(self, fn, *args, **kwargs):
            return _FakeFuture()

    with patch("icx_engine.graph.manager._get_build_executor", return_value=_FakeExecutor()):
        mgr = GraphManager()
        mgr.build_background(pid)

    meta = storage.read_meta(pid)
    assert meta.build_status == "rebuilding"


def test_build_background_passes_force_as_final_positional_arg(registered):
    pid, project_dir = registered
    captured: dict = {}

    class _FakeFuture:
        def add_done_callback(self, cb):
            pass

    class _FakeExecutor:
        def submit(self, fn, *args, **kwargs):
            captured["args"] = args
            return _FakeFuture()

    with patch("icx_engine.graph.manager._get_build_executor", return_value=_FakeExecutor()):
        mgr = GraphManager()
        mgr.build_background(pid, force=True)

    assert captured["args"][-1] is True
