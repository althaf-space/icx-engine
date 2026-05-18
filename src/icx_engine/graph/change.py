"""
Staleness detection: git-based diff with mtime fallback.
"""
from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

_log = logging.getLogger(__name__)


@dataclass
class ChangeResult:
    is_stale: bool
    serve_existing: bool      # True = small delta, use existing graph + background rebuild
    changed_files: list[str] = field(default_factory=list)


_SMALL_DELTA_MAX_FILES = 5
_SMALL_DELTA_MAX_RATIO = 0.03   # 3%
_MTIME_SAMPLE_SIZE = 50          # files sampled for mtime fallback
_GIT_TIMEOUT = 10               # seconds


def check_staleness(
    stored_commit: str | None,
    stored_file_count: int,
    project_path: Path,
    last_built: str | None = None,
) -> ChangeResult:
    """
    Determine if the graph is stale relative to current project state.
    Git diff first; falls back to mtime when git unavailable or project has no git repo.
    """
    # Never built at all
    if stored_commit is None and stored_file_count == 0:
        return ChangeResult(is_stale=True, serve_existing=False)

    if stored_commit is not None:
        changed = _git_changed_files(stored_commit, project_path)
        if changed is None:
            changed = _mtime_changed_files(project_path, stored_file_count, last_built)
    else:
        # Built previously but no git at project root - mtime fallback
        changed = _mtime_changed_files(project_path, stored_file_count, last_built)

    if not changed:
        return ChangeResult(is_stale=False, serve_existing=True)

    safe_count = max(stored_file_count, 1)
    ratio = len(changed) / safe_count
    small_delta = len(changed) <= _SMALL_DELTA_MAX_FILES or ratio < _SMALL_DELTA_MAX_RATIO

    return ChangeResult(
        is_stale=True,
        serve_existing=small_delta,
        changed_files=changed,
    )


def current_git_commit(project_path: Path) -> str | None:
    """Return HEAD commit hash, or None if git is unavailable."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(project_path),
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception as exc:
        _log.debug("git rev-parse failed (%s)", type(exc).__name__)
    return None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _git_changed_files(
    stored_commit: str,
    project_path: Path,
) -> list[str] | None:
    """
    Returns list of changed file paths since stored_commit, or None if git fails.
    """
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", stored_commit, "HEAD"],
            cwd=str(project_path),
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
        )
        if result.returncode != 0:
            return None
        lines = [f.strip() for f in result.stdout.splitlines() if f.strip()]
        return lines
    except Exception as exc:
        _log.debug("git diff failed (%s), falling back to mtime", type(exc).__name__)
        return None


def _mtime_changed_files(
    project_path: Path,
    stored_file_count: int,
    last_built: str | None = None,
) -> list[str]:
    """
    Staleness estimate when git is unavailable.
    Samples source files and returns those modified after last_built timestamp.
    Falls back to "last hour" if last_built not available.
    """
    try:
        from datetime import datetime, timezone

        if last_built:
            try:
                cutoff = datetime.fromisoformat(last_built).timestamp()
            except Exception:
                cutoff = time.time() - 3600
        else:
            cutoff = time.time() - 3600

        source_extensions = {
            ".py", ".js", ".ts", ".tsx", ".jsx",
            ".go", ".rs", ".java", ".kt", ".scala",
            ".c", ".cpp", ".h", ".cs", ".rb", ".php",
            ".vue", ".svelte",
        }
        files: list[Path] = []
        for ext in source_extensions:
            files.extend(project_path.rglob(f"*{ext}"))
            if len(files) > _MTIME_SAMPLE_SIZE * 3:
                break

        step = max(1, len(files) // _MTIME_SAMPLE_SIZE)
        sample = files[::step][:_MTIME_SAMPLE_SIZE]

        return [
            str(f.relative_to(project_path))
            for f in sample
            if f.exists() and f.stat().st_mtime > cutoff
        ]
    except Exception:
        return []
