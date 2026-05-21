"""Tests for graph/storage.py"""
from __future__ import annotations

import json
import hashlib
from pathlib import Path
from unittest.mock import patch

import pytest

from icx_engine.graph.storage import (
    derive_project_id,
    normalize_name,
    validate_project_path,
    register_project,
    lookup_by_name,
    lookup_by_path,
    list_projects,
    remove_project,
    read_meta,
    write_meta,
    set_build_status,
    ProjectInfo,
    _is_relative_to,
    _normalize_issue_key,
    temp_images_dir,
    sweep_stale_temp_dirs,
)
from icx_engine.exceptions import GraphError


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolated_graphs(tmp_path, monkeypatch):
    """Redirect ~/.icx/graphs/ to a temp directory for every test."""
    graphs_root = tmp_path / "graphs"
    graphs_root.mkdir()
    monkeypatch.setattr("icx_engine.graph.storage._graphs_root", lambda: graphs_root)
    return graphs_root


# ---------------------------------------------------------------------------
# derive_project_id
# ---------------------------------------------------------------------------

def test_project_id_is_12_chars(tmp_path):
    pid = derive_project_id(tmp_path)
    assert len(pid) == 12
    assert pid.isalnum()


def test_project_id_is_sha256_prefix(tmp_path):
    expected = hashlib.sha256(str(tmp_path).encode()).hexdigest()[:12]
    assert derive_project_id(tmp_path) == expected


def test_project_id_stable_for_same_path(tmp_path):
    assert derive_project_id(tmp_path) == derive_project_id(tmp_path)


def test_project_id_differs_for_different_paths(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    assert derive_project_id(a) != derive_project_id(b)


# ---------------------------------------------------------------------------
# normalize_name
# ---------------------------------------------------------------------------

def test_normalize_name_lowercases():
    assert normalize_name("MyApp") == "myapp"


def test_normalize_name_strips_whitespace():
    assert normalize_name("  myapp  ") == "myapp"


def test_normalize_name_mixed():
    assert normalize_name("  MyAPP  ") == "myapp"


# ---------------------------------------------------------------------------
# validate_project_path
# ---------------------------------------------------------------------------

def test_validate_path_returns_resolved(tmp_path):
    result = validate_project_path(str(tmp_path))
    assert result == tmp_path.resolve()


def test_validate_path_raises_if_not_exists():
    with pytest.raises(GraphError, match="does not exist"):
        validate_project_path("/nonexistent/path/xyz123")


def test_validate_path_raises_if_not_dir(tmp_path):
    f = tmp_path / "file.txt"
    f.write_text("x", encoding="utf-8")
    with pytest.raises(GraphError, match="not a directory"):
        validate_project_path(str(f))


def test_validate_path_raises_on_control_chars(tmp_path):
    with pytest.raises(GraphError, match="invalid characters"):
        validate_project_path(str(tmp_path) + "\x00bad")


# ---------------------------------------------------------------------------
# register / lookup
# ---------------------------------------------------------------------------

def test_register_creates_meta(tmp_path):
    pid = register_project("testapp", tmp_path)
    assert len(pid) == 12
    meta = read_meta(pid)
    assert meta is not None
    assert meta.name == "testapp"
    assert meta.build_status == "not_built"


def test_register_normalizes_name(tmp_path):
    pid = register_project("TestApp", tmp_path)
    meta = read_meta(pid)
    assert meta.name == "testapp"


def test_lookup_by_name_finds_project(tmp_path):
    register_project("myapp", tmp_path)
    info = lookup_by_name("myapp")
    assert info is not None
    assert info.name == "myapp"


def test_lookup_by_name_case_insensitive(tmp_path):
    register_project("myapp", tmp_path)
    assert lookup_by_name("MyApp") is not None
    assert lookup_by_name("MYAPP") is not None


def test_lookup_by_name_returns_none_if_missing():
    assert lookup_by_name("doesnotexist") is None


def test_lookup_by_path_finds_project(tmp_path):
    register_project("myapp", tmp_path)
    info = lookup_by_path(tmp_path)
    assert info is not None


def test_list_projects_returns_all(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    register_project("alpha", a)
    register_project("beta", b)
    projects = list_projects()
    names = {p.name for p in projects}
    assert "alpha" in names
    assert "beta" in names


# ---------------------------------------------------------------------------
# set_build_status / read_meta / write_meta
# ---------------------------------------------------------------------------

def test_set_build_status_updates(tmp_path):
    pid = register_project("app", tmp_path)
    set_build_status(pid, "building")
    meta = read_meta(pid)
    assert meta.build_status == "building"


def test_write_and_read_meta_roundtrip(tmp_path):
    pid = derive_project_id(tmp_path)
    info = ProjectInfo(
        name="app", path=str(tmp_path), project_id=pid,
        last_built="2026-05-16T10:00:00Z", git_commit="abc123",
        file_count=42, build_status="ready",
    )
    write_meta(info)
    loaded = read_meta(pid)
    assert loaded is not None
    assert loaded.file_count == 42
    assert loaded.git_commit == "abc123"
    assert loaded.build_status == "ready"


# ---------------------------------------------------------------------------
# remove_project
# ---------------------------------------------------------------------------

def test_remove_project_deletes_dir(tmp_path):
    pid = register_project("app", tmp_path)
    remove_project(pid, keep_cache=False)
    assert lookup_by_name("app") is None


def test_remove_project_keep_cache(tmp_path, isolated_graphs):
    pid = register_project("app", tmp_path)
    # Create a fake cache file
    cache_dir = isolated_graphs / pid / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "test.json").write_text("{}", encoding="utf-8")

    remove_project(pid, keep_cache=True)
    # Registration gone
    assert lookup_by_name("app") is None
    # Cache still present
    assert (cache_dir / "test.json").exists()


# ---------------------------------------------------------------------------
# _is_relative_to helper
# ---------------------------------------------------------------------------

def test_is_relative_to_child():
    parent = Path("/home/user/projects")
    child = Path("/home/user/projects/myapp/src")
    assert _is_relative_to(child, parent) is True


def test_is_relative_to_not_child():
    a = Path("/home/user/projects/a")
    b = Path("/home/user/projects/b")
    assert _is_relative_to(a, b) is False


def test_is_relative_to_same_path():
    p = Path("/home/user/projects/myapp")
    assert _is_relative_to(p, p) is True


# ---------------------------------------------------------------------------
# Temp image storage helpers
# ---------------------------------------------------------------------------

def test_normalize_issue_key_bare_key():
    assert _normalize_issue_key("PROJ-123") == "PROJ-123"

def test_normalize_issue_key_lowercase():
    assert _normalize_issue_key("proj-123") == "PROJ-123"

def test_normalize_issue_key_from_url():
    assert _normalize_issue_key("https://jira.company.com/browse/PROJ-123") == "PROJ-123"

def test_normalize_issue_key_url_with_query():
    assert _normalize_issue_key("https://jira.com/browse/PROJ-456?focusedCommentId=1") == "PROJ-456"

def test_normalize_issue_key_url_and_bare_produce_same_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("icx_engine.graph.storage.temp_root", lambda: tmp_path / "temp")
    d1 = temp_images_dir("PROJ-123")
    d2 = temp_images_dir("https://jira.company.com/browse/PROJ-123")
    assert d1 == d2

def test_normalize_issue_key_unknown_string():
    result = _normalize_issue_key("random-string")
    assert result == "random-string"  # hyphens are valid in filenames and preserved

def test_temp_images_dir_uses_normalized_key(tmp_path, monkeypatch):
    monkeypatch.setattr("icx_engine.graph.storage.temp_root", lambda: tmp_path / "temp")
    d = temp_images_dir("ABC-99")
    assert d.name == "ABC-99"

def test_sweep_stale_temp_dirs_removes_old_dirs(tmp_path):
    import time
    old_dir = tmp_path / "OLD-1"
    old_dir.mkdir()
    (old_dir / "img.png").write_bytes(b"fake")
    # Set mtime to 2 days ago
    old_mtime = time.time() - 172800
    import os
    os.utime(old_dir, (old_mtime, old_mtime))

    recent_dir = tmp_path / "NEW-2"
    recent_dir.mkdir()

    sweep_stale_temp_dirs(max_age_seconds=86400, _root=tmp_path)

    assert not old_dir.exists()
    assert recent_dir.exists()

def test_sweep_stale_temp_dirs_keeps_recent_dirs(tmp_path):
    recent = tmp_path / "RECENT-1"
    recent.mkdir()
    sweep_stale_temp_dirs(max_age_seconds=86400, _root=tmp_path)
    assert recent.exists()

def test_sweep_stale_temp_dirs_nonexistent_root_is_noop(tmp_path):
    missing = tmp_path / "does_not_exist"
    sweep_stale_temp_dirs(_root=missing)  # must not raise

def test_sweep_stale_temp_dirs_skips_files(tmp_path):
    (tmp_path / "loose_file.txt").write_text("hello", encoding="utf-8")
    sweep_stale_temp_dirs(max_age_seconds=0, _root=tmp_path)  # age=0 removes everything old
    # File should remain (sweep only targets dirs)
    assert (tmp_path / "loose_file.txt").exists()
