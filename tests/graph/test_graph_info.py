"""Tests for graph_info_for_path (manager.py) and paths.check_staleness timeout.

Covers the MCP performance fix: check_stale=False must never call check_staleness
or spawn git subprocesses. These would block the asyncio event loop on the MCP path.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from icx_engine.graph.manager import graph_info_for_path, GraphManager
from icx_engine.graph import storage
from icx_engine.graph.storage import ProjectInfo, write_meta, derive_project_id


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
def ready_project(tmp_path):
    """Registered, ready project with a fake graph file and populated meta."""
    project_dir = tmp_path / "myapp"
    project_dir.mkdir()
    mgr = GraphManager()
    pid = mgr.register("myapp", str(project_dir))
    storage.graph_path(pid).write_text("{}", encoding="utf-8")
    meta = storage.read_meta(pid)
    meta.build_status = "ready"
    meta.file_count = 100
    meta.git_commit = "abc123def456abc123def456abc123def456abc1"
    meta.last_built = "2026-05-25T10:00:00+00:00"
    write_meta(meta)
    return pid, project_dir


# ---------------------------------------------------------------------------
# check_stale=False: no staleness check, no subprocess
# ---------------------------------------------------------------------------

def test_check_stale_false_skips_check_staleness(ready_project):
    """check_stale=False must not invoke check_staleness."""
    _, project_dir = ready_project
    with patch("icx_engine.graph.change.check_staleness") as mock_cs:
        result = graph_info_for_path(str(project_dir), check_stale=False)
    mock_cs.assert_not_called()
    assert result["status"] == "ready"
    assert result["freshness"] == "not_checked"


def test_check_stale_false_no_subprocess(ready_project):
    """check_stale=False must not spawn any subprocess (no git calls on MCP path)."""
    _, project_dir = ready_project
    with patch("subprocess.run") as mock_run:
        result = graph_info_for_path(str(project_dir), check_stale=False)
    mock_run.assert_not_called()
    assert result["status"] == "ready"


def test_check_stale_false_includes_stored_metadata(ready_project):
    """check_stale=False includes last_built, git_commit, file_count from stored meta."""
    _, project_dir = ready_project
    result = graph_info_for_path(str(project_dir), check_stale=False)
    assert result["status"] == "ready"
    assert result["freshness"] == "not_checked"
    assert result["file_count"] == 100
    assert result["git_commit"] == "abc123def456abc123def456abc123def456abc1"
    assert result["last_built"] == "2026-05-25T10:00:00+00:00"


# ---------------------------------------------------------------------------
# check_stale=True: existing staleness behavior preserved
# ---------------------------------------------------------------------------

def test_check_stale_true_propagates_stale_note(ready_project):
    """check_stale=True calls check_staleness and surfaces stale_note when stale."""
    _, project_dir = ready_project
    from icx_engine.graph.change import ChangeResult
    stale = ChangeResult(is_stale=True, serve_existing=True, changed_files=["a.py", "b.py"])
    with patch("icx_engine.graph.change.check_staleness", return_value=stale):
        result = graph_info_for_path(str(project_dir), check_stale=True)
    assert result["status"] == "ready"
    assert "stale_note" in result
    assert "2 of" in result["stale_note"]


def test_check_stale_true_no_stale_note_when_fresh(ready_project):
    """check_stale=True with no changes: no stale_note, no freshness key."""
    _, project_dir = ready_project
    from icx_engine.graph.change import ChangeResult
    fresh = ChangeResult(is_stale=False, serve_existing=True)
    with patch("icx_engine.graph.change.check_staleness", return_value=fresh):
        result = graph_info_for_path(str(project_dir), check_stale=True)
    assert result["status"] == "ready"
    assert "stale_note" not in result
    assert "freshness" not in result


# ---------------------------------------------------------------------------
# Not registered / not built / building: return fast without blocking
# ---------------------------------------------------------------------------

def test_unknown_path_returns_not_registered_and_writes_nothing(tmp_path):
    """Unknown-but-valid dir must report not_registered and never auto-register."""
    unknown = tmp_path / "unknown_app"
    unknown.mkdir()
    before = len(storage.list_projects())
    result = graph_info_for_path(str(unknown), check_stale=False)
    assert result["status"] == "not_registered"
    assert result["path"] == str(unknown)
    assert len(storage.list_projects()) == before


def test_not_built_returns_status(tmp_path):
    """Registered but not built: returns not_built status."""
    project_dir = tmp_path / "unbuilt"
    project_dir.mkdir()
    GraphManager().register("unbuilt", str(project_dir))
    result = graph_info_for_path(str(project_dir), check_stale=False)
    assert result["status"] == "not_built"


def test_building_returns_eta(tmp_path):
    """Building project returns building status with eta_seconds set."""
    project_dir = tmp_path / "building_app"
    project_dir.mkdir()
    mgr = GraphManager()
    pid = mgr.register("buildingapp", str(project_dir))
    meta = storage.read_meta(pid)
    meta.build_status = "building"
    write_meta(meta)
    result = graph_info_for_path(str(project_dir), check_stale=False)
    assert result["status"] == "building"
    assert result.get("eta_seconds") is not None


def test_building_no_eta_caveat_when_no_llm_configured(tmp_path, monkeypatch):
    """Regression guard: estimate_build_eta's LLM portion is a best-case-only number
    (assumes every chunk succeeds first try - see LLM_ETA_CAVEAT) that must never be
    presented as precise. When no LLM is configured, the AST-only estimate is genuinely
    fast/predictable and needs no caveat."""
    project_dir = tmp_path / "building_app"
    project_dir.mkdir()
    mgr = GraphManager()
    pid = mgr.register("buildingapp", str(project_dir))
    meta = storage.read_meta(pid)
    meta.build_status = "building"
    write_meta(meta)

    monkeypatch.setattr("icx_engine.graph.manager._read_icx_llm_cfg", lambda: None)
    monkeypatch.setattr("icx_engine.graph.manager._detect_llm_backend", lambda: None)
    result = graph_info_for_path(str(project_dir), check_stale=False)

    assert result["status"] == "building"
    assert "eta_caveat" not in result
    assert "This estimate assumes" not in result["report_inline"]


def test_building_includes_eta_caveat_when_llm_configured(tmp_path, monkeypatch):
    """When an LLM IS configured, the number shown could be the semantic (LLM-inclusive)
    estimate, which has real, unmodelled rate-limit/retry risk - the caveat must be
    surfaced both as its own field and folded into report_inline (the text an agent
    actually reads)."""
    project_dir = tmp_path / "building_app"
    project_dir.mkdir()
    mgr = GraphManager()
    pid = mgr.register("buildingapp", str(project_dir))
    meta = storage.read_meta(pid)
    meta.build_status = "building"
    write_meta(meta)

    monkeypatch.setattr(
        "icx_engine.graph.manager._read_icx_llm_cfg", lambda: ("claude", "sk-x", None),
    )
    result = graph_info_for_path(str(project_dir), check_stale=False)

    assert result["status"] == "building"
    from icx_engine.graph.builder import LLM_ETA_CAVEAT
    assert result["eta_caveat"] == LLM_ETA_CAVEAT
    assert LLM_ETA_CAVEAT in result["report_inline"]


def test_estimate_eta_caveat_none_for_unregistered_project():
    from icx_engine.graph.manager import GraphManager
    mgr = GraphManager()
    assert mgr.estimate_eta_caveat("nonexistentidx") is None


# ---------------------------------------------------------------------------
# paths.check_staleness: git timeout returns freshness_unknown
# ---------------------------------------------------------------------------

def test_paths_check_staleness_git_timeout_returns_freshness_unknown(tmp_path):
    """git timeout in paths.check_staleness returns freshness_unknown, does not hang."""
    from icx_engine.graph.paths import check_staleness as paths_check_staleness

    project_dir = tmp_path / "gitrepo"
    project_dir.mkdir()
    pid = derive_project_id(project_dir)

    storage.graph_path(pid).parent.mkdir(parents=True, exist_ok=True)
    storage.graph_path(pid).write_text("{}", encoding="utf-8")
    storage.write_manifest(
        project_id=pid,
        project_root=str(project_dir),
        total_files=100,
        git_commit="abc123def456abc123def456abc123def456abc1",
        file_mtimes={},
    )

    with patch("icx_engine.graph.paths._is_git_repo", return_value=True):
        with patch("icx_engine.graph.paths.subprocess.run",
                   side_effect=subprocess.TimeoutExpired(cmd=["git"], timeout=2)):
            result = paths_check_staleness(pid, project_dir)

    assert result["status"] == "freshness_unknown"
