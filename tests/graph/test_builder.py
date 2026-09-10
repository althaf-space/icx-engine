"""Tests for graph/builder.py"""
from __future__ import annotations

import pytest

from icx_engine.graph.builder import estimate_build_eta, _build_project_isolated


# ---------------------------------------------------------------------------
# estimate_build_eta
# ---------------------------------------------------------------------------

def test_eta_minimum_is_15():
    assert estimate_build_eta(0) == 15
    assert estimate_build_eta(1) == 15


def test_eta_scales_with_file_count():
    small = estimate_build_eta(100)
    large = estimate_build_eta(10000)
    assert large > small


def test_eta_returns_int():
    assert isinstance(estimate_build_eta(500), int)


# ---------------------------------------------------------------------------
# _build_project_isolated - error handling
# ---------------------------------------------------------------------------

def test_build_returns_error_on_import_failure(tmp_path):
    """When graphifyy is not installed, returns error dict instead of raising."""
    import sys
    from unittest.mock import patch

    # Simulate graphifyy not installed
    with patch.dict(sys.modules, {"graphify": None, "graphify.cache": None}):
        result = _build_project_isolated(
            str(tmp_path),
            str(tmp_path / "graph.json.tmp"),
            str(tmp_path / "cache"),
        )
    # Should return an error dict, not raise
    assert isinstance(result, dict)
    assert result.get("error") is not None


def test_build_returns_error_on_empty_dir(tmp_path):
    """Empty directory with no source files returns error dict, not exception."""
    try:
        import graphify.extract  # noqa: F401
    except ImportError:
        pytest.skip("graphifyy not installed")

    result = _build_project_isolated(
        str(tmp_path),
        str(tmp_path / "graph.json.tmp"),
        str(tmp_path / "cache"),
    )
    assert isinstance(result, dict)
    # Empty dir: no git, no source files -> file_count == 0 or error
    assert result["file_count"] == 0 or result.get("error") is not None


# ---------------------------------------------------------------------------
# force=True must bypass the incremental "nothing changed, skip rebuild" shortcut
# ---------------------------------------------------------------------------
# Root cause of a real production bug: `icx graph build --force` never actually forced
# anything - GraphManager.build()/_run_build_subprocess() never passed `force` down to
# _build_project_isolated at all, so the file-hash-based incremental check
# (`graph_json_path.exists() and bool(stored_hashes)`) would trigger regardless of
# --force. When it found zero changed/deleted files (e.g. re-running build with no code
# changes), it returned a placeholder zero-valued result and never wrote graph.json.tmp
# at all - meaning a corrupted/incomplete graph.json could never be repaired by --force,
# since the skip shortcut fired before any real rebuild work ran, every single time.

def _write_java_project(root):
    (root / "A.java").write_text(
        "package com.example;\npublic class A {\n    public B makeB() { return new B(); }\n}\n",
        encoding="utf-8",
    )
    (root / "B.java").write_text(
        "package com.example;\npublic class B {\n}\n", encoding="utf-8",
    )


def test_no_force_rebuild_with_nothing_changed_takes_skip_shortcut(tmp_path):
    """Baseline/regression guard for the shortcut's own legitimate behavior: with no
    changes and no --force, the shortcut correctly fires (this is the intended fast
    path for e.g. a query-triggered background rebuild finding nothing new)."""
    try:
        import javalang  # noqa: F401
    except ImportError:
        pytest.skip("javalang not installed")

    project = tmp_path / "project"
    project.mkdir()
    _write_java_project(project)
    cache = tmp_path / "cache"
    cache.mkdir()
    graph_tmp = cache / "graph.json.tmp"

    result1 = _build_project_isolated(str(project), str(graph_tmp), str(cache))
    assert result1.get("edge_count", 0) > 0
    graph_tmp.replace(cache.parent / "graph.json")

    result2 = _build_project_isolated(str(project), str(graph_tmp), str(cache))
    assert result2.get("skipped") is True
    assert not graph_tmp.exists(), "skip shortcut must not write graph.json.tmp"


def test_force_true_bypasses_skip_shortcut_and_produces_real_edges(tmp_path):
    """The actual fix: force=True must always do a real rebuild, reproducing the true
    edge count, even when the file-hash check would otherwise find nothing changed."""
    try:
        import javalang  # noqa: F401
    except ImportError:
        pytest.skip("javalang not installed")

    project = tmp_path / "project"
    project.mkdir()
    _write_java_project(project)
    cache = tmp_path / "cache"
    cache.mkdir()
    graph_tmp = cache / "graph.json.tmp"

    result1 = _build_project_isolated(str(project), str(graph_tmp), str(cache))
    real_edge_count = result1.get("edge_count", 0)
    assert real_edge_count > 0
    graph_tmp.replace(cache.parent / "graph.json")

    result2 = _build_project_isolated(str(project), str(graph_tmp), str(cache), force=True)

    assert result2.get("skipped") is not True
    assert result2.get("incremental") is False
    assert result2.get("edge_count", 0) == real_edge_count
    assert graph_tmp.exists(), "force=True must write a real graph.json.tmp"
