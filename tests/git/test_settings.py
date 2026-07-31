from __future__ import annotations
import logging
from pathlib import Path
from icx_engine.git.settings import read_repo_settings, write_repo_settings


def test_read_returns_empty_dict_when_nothing_stored(tmp_path, monkeypatch):
    monkeypatch.setattr("icx_engine.git.settings._git_settings_root", lambda: tmp_path)
    repo = tmp_path / "myrepo"
    repo.mkdir()
    assert read_repo_settings(repo) == {}


def test_write_then_read_round_trips(tmp_path, monkeypatch):
    monkeypatch.setattr("icx_engine.git.settings._git_settings_root", lambda: tmp_path)
    repo = tmp_path / "myrepo"
    repo.mkdir()
    write_repo_settings(repo, parent_branch="development")
    assert read_repo_settings(repo) == {"parent_branch": "development"}


def test_write_merges_with_existing_keys(tmp_path, monkeypatch):
    monkeypatch.setattr("icx_engine.git.settings._git_settings_root", lambda: tmp_path)
    repo = tmp_path / "myrepo"
    repo.mkdir()
    write_repo_settings(repo, parent_branch="development")
    write_repo_settings(repo, some_other_key="x")
    assert read_repo_settings(repo) == {"parent_branch": "development", "some_other_key": "x"}


def test_different_repo_paths_get_different_settings(tmp_path, monkeypatch):
    monkeypatch.setattr("icx_engine.git.settings._git_settings_root", lambda: tmp_path)
    repo_a = tmp_path / "a"; repo_a.mkdir()
    repo_b = tmp_path / "b"; repo_b.mkdir()
    write_repo_settings(repo_a, parent_branch="development")
    write_repo_settings(repo_b, parent_branch="main")
    assert read_repo_settings(repo_a) == {"parent_branch": "development"}
    assert read_repo_settings(repo_b) == {"parent_branch": "main"}


def test_read_does_not_create_repo_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("icx_engine.git.settings._git_settings_root", lambda: tmp_path)
    repo = tmp_path / "myrepo"
    repo.mkdir()
    read_repo_settings(repo)
    from icx_engine.git.settings import _repo_id
    assert not (tmp_path / _repo_id(repo)).exists()


def test_read_corrupt_json_logs_warning_and_returns_empty(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr("icx_engine.git.settings._git_settings_root", lambda: tmp_path)
    repo = tmp_path / "myrepo"
    repo.mkdir()
    write_repo_settings(repo, parent_branch="development")
    from icx_engine.git.settings import _repo_dir, _SETTINGS_FILE
    (_repo_dir(repo) / _SETTINGS_FILE).write_text("{not valid json", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        result = read_repo_settings(repo)
    assert result == {}
    assert any("git settings" in r.message.lower() for r in caplog.records)
