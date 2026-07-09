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
