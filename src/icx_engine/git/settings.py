"""Per-repo persisted git-workflow settings (currently just the chosen parent
branch) - `~/.icx/git/<repo_id>/settings.json`. Mirrors the meta.json pattern
already used by graph/storage.py: atomic tmp-then-replace write, owner-only
directory permissions, one JSON file per repo keyed by a stable hash of its
resolved path."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import stat
import sys
from pathlib import Path

_log = logging.getLogger(__name__)

_SETTINGS_FILE = "settings.json"


def _git_settings_root() -> Path:
    root = Path.home() / ".icx" / "git"
    if not root.exists():
        root.mkdir(parents=True, exist_ok=True)
        if sys.platform != "win32":
            try:
                root.chmod(stat.S_IRWXU)
            except OSError:
                pass
    return root


def _repo_id(repo_root: Path) -> str:
    digest = hashlib.sha256(repo_root.resolve().as_posix().encode("utf-8")).hexdigest()
    return digest[:12]


def _repo_dir_path(repo_root: Path) -> Path:
    """Return repo directory path without creating it. Use for read operations."""
    return _git_settings_root() / _repo_id(repo_root)


def _repo_dir(repo_root: Path) -> Path:
    """Return repo directory path, creating it if needed. Use for write operations only."""
    d = _git_settings_root() / _repo_id(repo_root)
    if not d.exists():
        d.mkdir(parents=True, exist_ok=True)
        if sys.platform != "win32":
            try:
                d.chmod(stat.S_IRWXU)
            except OSError:
                pass
    return d


def read_repo_settings(repo_root: Path) -> dict:
    path = _repo_dir_path(repo_root) / _SETTINGS_FILE
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError) as exc:
        _log.warning("Failed to parse git settings for repo (%s)", type(exc).__name__)
        return {}


def write_repo_settings(repo_root: Path, **updates) -> None:
    existing = read_repo_settings(repo_root)
    existing.update(updates)
    path = _repo_dir(repo_root) / _SETTINGS_FILE
    tmp = path.with_suffix(".tmp")
    try:
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0),
                     stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(existing, f, indent=2)
        tmp.replace(path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
