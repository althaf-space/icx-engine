from __future__ import annotations

import sys


def test_lock_file_persists_after_release(tmp_path, monkeypatch):
    """Regression, POSIX only: the POSIX branch used to unlink the lock file on
    release, a flock-then-unlink TOCTOU that let two processes both believe they
    held the exclusive lock. The lock file must now be a permanent sentinel.
    (Windows uses O_CREAT|O_EXCL file-existence as the lock itself, so removing
    it on release there is correct, not the same bug - not asserted here.)"""
    if sys.platform == "win32":
        return
    import icx_engine.config_manager as cm
    monkeypatch.setattr(cm, "CONFIG_PATH", tmp_path / "config.json")

    with cm._config_lock():
        pass

    lock_path = (tmp_path / "config.json").with_suffix(".lock")
    assert lock_path.exists()


def test_lock_can_be_reacquired_after_release(tmp_path, monkeypatch):
    """Repeated acquire/release cycles never hang or raise, on either platform."""
    import icx_engine.config_manager as cm
    monkeypatch.setattr(cm, "CONFIG_PATH", tmp_path / "config.json")

    for _ in range(5):
        with cm._config_lock():
            pass


def test_lock_reuses_same_inode_across_acquisitions(tmp_path, monkeypatch):
    """POSIX-only: proves the lock file is never removed and recreated between
    acquisitions (the actual TOCTOU vector) - the inode stays stable."""
    if sys.platform == "win32":
        return
    import os
    import icx_engine.config_manager as cm
    monkeypatch.setattr(cm, "CONFIG_PATH", tmp_path / "config.json")
    lock_path = (tmp_path / "config.json").with_suffix(".lock")

    with cm._config_lock():
        pass
    first_inode = os.stat(lock_path).st_ino

    with cm._config_lock():
        pass
    second_inode = os.stat(lock_path).st_ino

    assert first_inode == second_inode


def test_lock_body_executes_and_exceptions_propagate(tmp_path, monkeypatch):
    """The lock is still released (not left held) when the body raises."""
    import pytest
    import icx_engine.config_manager as cm
    monkeypatch.setattr(cm, "CONFIG_PATH", tmp_path / "config.json")

    with pytest.raises(ValueError):
        with cm._config_lock():
            raise ValueError("boom")

    # Lock must be released - a fresh acquisition must not hang/timeout.
    with cm._config_lock():
        pass
