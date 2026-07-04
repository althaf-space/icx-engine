"""Tests for graph/change.py"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from icx_engine.graph.change import check_staleness, current_git_commit, ChangeResult


# ---------------------------------------------------------------------------
# check_staleness - no stored commit
# ---------------------------------------------------------------------------

def test_never_built_is_stale(tmp_path):
    """No commit AND file_count == 0 -> never built -> stale, no existing to serve."""
    result = check_staleness(None, 0, tmp_path)
    assert result.is_stale is True
    assert result.serve_existing is False


def test_no_commit_but_has_files_falls_back_to_mtime(tmp_path):
    """No git commit but was built before (file_count > 0) -> mtime fallback path."""
    with patch("icx_engine.graph.change._mtime_changed_files", return_value=[]) as mock_mtime:
        result = check_staleness(None, 100, tmp_path)
    mock_mtime.assert_called_once()
    assert result.is_stale is False


# ---------------------------------------------------------------------------
# check_staleness - git path
# ---------------------------------------------------------------------------

def _mock_git_diff(changed_files: list[str], returncode: int = 0):
    """Return a mock for subprocess.run that returns a git diff output."""
    mock_result = MagicMock()
    mock_result.returncode = returncode
    mock_result.stdout = "\n".join(changed_files) + ("\n" if changed_files else "")
    return mock_result


def test_no_changes_is_not_stale(tmp_path):
    with patch("icx_engine.graph.change.subprocess.run", return_value=_mock_git_diff([])):
        result = check_staleness("abc123", 100, tmp_path)
    assert result.is_stale is False
    assert result.serve_existing is True


def test_small_delta_serves_existing(tmp_path):
    changed = ["src/foo.py", "src/bar.py"]
    with patch("icx_engine.graph.change.subprocess.run", return_value=_mock_git_diff(changed)):
        result = check_staleness("abc123", 100, tmp_path)
    assert result.is_stale is True
    assert result.serve_existing is True  # <=5 files
    assert result.changed_files == changed


def test_large_delta_by_file_count_blocks(tmp_path):
    changed = [f"src/f{i}.py" for i in range(10)]  # 10 files > 5
    with patch("icx_engine.graph.change.subprocess.run", return_value=_mock_git_diff(changed)):
        result = check_staleness("abc123", 20, tmp_path)  # 10/20 = 50% >= 3%
    assert result.is_stale is True
    assert result.serve_existing is False


def test_large_ratio_but_few_files_serves_existing(tmp_path):
    # 3 files out of 10 = 30% ratio, but <=5 files -> serve existing
    changed = ["a.py", "b.py", "c.py"]
    with patch("icx_engine.graph.change.subprocess.run", return_value=_mock_git_diff(changed)):
        result = check_staleness("abc123", 10, tmp_path)
    assert result.serve_existing is True  # <=5 files overrides ratio


def test_exactly_5_files_serves_existing(tmp_path):
    changed = [f"src/f{i}.py" for i in range(5)]
    with patch("icx_engine.graph.change.subprocess.run", return_value=_mock_git_diff(changed)):
        result = check_staleness("abc123", 1000, tmp_path)
    assert result.serve_existing is True


def test_6_files_large_ratio_blocks(tmp_path):
    changed = [f"src/f{i}.py" for i in range(6)]  # 6 files > 5, 6/10 = 60% >= 3%
    with patch("icx_engine.graph.change.subprocess.run", return_value=_mock_git_diff(changed)):
        result = check_staleness("abc123", 10, tmp_path)
    assert result.serve_existing is False


# ---------------------------------------------------------------------------
# check_staleness - git unavailable (fallback)
# ---------------------------------------------------------------------------

def test_git_failure_falls_back_to_mtime(tmp_path):
    mock_result = MagicMock()
    mock_result.returncode = 128  # git error
    with patch("icx_engine.graph.change.subprocess.run", return_value=mock_result):
        with patch("icx_engine.graph.change._mtime_changed_files", return_value=[]) as mock_mtime:
            result = check_staleness("abc123", 100, tmp_path)
    mock_mtime.assert_called_once()
    assert result.is_stale is False


def test_git_exception_falls_back_to_mtime(tmp_path):
    with patch("icx_engine.graph.change.subprocess.run", side_effect=FileNotFoundError("git not found")):
        with patch("icx_engine.graph.change._mtime_changed_files", return_value=["changed.py"]):
            result = check_staleness("abc123", 100, tmp_path)
    assert result.is_stale is True


def test_git_timeout_falls_back_to_mtime(tmp_path):
    """subprocess.TimeoutExpired must not propagate - falls back to mtime."""
    import subprocess as _sp
    with patch("icx_engine.graph.change.subprocess.run",
               side_effect=_sp.TimeoutExpired(cmd=["git"], timeout=2)):
        with patch("icx_engine.graph.change._mtime_changed_files", return_value=[]) as mock_mtime:
            result = check_staleness("abc123def456abc123def456abc123def456abc12", 100, tmp_path)
    mock_mtime.assert_called_once()
    assert result.is_stale is False


# ---------------------------------------------------------------------------
# current_git_commit
# ---------------------------------------------------------------------------

def test_current_git_commit_returns_hash(tmp_path):
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "abc123def456\n"
    with patch("icx_engine.graph.change.subprocess.run", return_value=mock_result):
        commit = current_git_commit(tmp_path)
    assert commit == "abc123def456"


def test_current_git_commit_returns_none_on_failure(tmp_path):
    with patch("icx_engine.graph.change.subprocess.run", side_effect=Exception("no git")):
        commit = current_git_commit(tmp_path)
    assert commit is None


def test_current_git_commit_returns_none_on_nonzero(tmp_path):
    mock_result = MagicMock()
    mock_result.returncode = 128
    with patch("icx_engine.graph.change.subprocess.run", return_value=mock_result):
        commit = current_git_commit(tmp_path)
    assert commit is None
