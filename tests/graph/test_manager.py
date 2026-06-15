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
