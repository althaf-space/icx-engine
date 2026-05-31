"""Path resolution and sub-project detection for ICX graph tools.

Handles:
- Resolving a raw path string to an actual project root
  (git repo: use as-is; non-git: scan for project marker files)
- Detecting multiple sub-projects inside a given directory
- Staleness checking via git diff or file mtime sampling
"""
from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

_log = logging.getLogger(__name__)

_GIT_SAFE_ENV: dict[str, str] = {
    "GIT_TERMINAL_PROMPT": "0",
    "GCM_INTERACTIVE": "never",
    "GCM_CREDENTIAL_STORE": "cache",
}

# Base git args prepended to every git command we spawn.
# -c credential.helper= disables all credential helpers for the invocation,
# preventing git-credential-manager from being spawned. Local read-only
# git commands (rev-parse, diff --name-only) never need credentials.
_GIT_BASE_CMD: tuple[str, ...] = (
    "git", "--no-pager",
    "-c", "credential.helper=",
    "-c", "gc.auto=0",
    "-c", "maintenance.auto=false",
)

# On Windows, close_fds=True prevents git subprocesses from inheriting
# the MCP server's overlapped I/O handles (IOCP) which can cause CreateProcess
# or pipe setup to hang when multiple concurrent calls are in-flight.
# CREATE_NO_WINDOW prevents a console window from being created.
_GIT_POPEN_KW: dict = {
    "close_fds": True,
    **({"creationflags": 0x08000000} if os.name == "nt" else {}),  # CREATE_NO_WINDOW
}


def _safe_git_env() -> dict[str, str]:
    env = dict(os.environ)
    env.update(_GIT_SAFE_ENV)
    return env


# Project marker files that indicate a project root.
# Checked in priority order - first match wins per directory.
_PROJECT_MARKERS: tuple[str, ...] = (
    "package.json",
    "pyproject.toml",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "Cargo.toml",
    "go.mod",
    "pubspec.yaml",
    "CMakeLists.txt",
)

def _is_skip_marker_dir(name: str) -> bool:
    """Return True if directory should be skipped during sub-project marker scan."""
    from icx_engine.graph.parser.detect import _is_noise_dir
    if _is_noise_dir(name):
        return True
    low = name.lower()
    # Also skip test/fixture dirs - they may have package.json but are not real projects
    return any(kw in low for kw in ("test", "spec", "fixture", "mock", "testdata"))

# Max sub-projects before we call a path "too broad" and reject it.
_MAX_SUB_PROJECTS = 20

# Staleness threshold: warn-and-continue if changed < this fraction of total files;
# at or above this fraction the tools hard-stop and tell the user to rebuild.
_STALE_THRESHOLD = 0.03


def _is_git_repo(path: Path) -> bool:
    try:
        r = subprocess.run(
            [*_GIT_BASE_CMD, "rev-parse", "--is-inside-work-tree"],
            cwd=str(path), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, timeout=2, env=_safe_git_env(), **_GIT_POPEN_KW,
        )
        return r.returncode == 0
    except Exception:
        return False


def _git_root(path: Path) -> Path | None:
    try:
        r = subprocess.run(
            [*_GIT_BASE_CMD, "rev-parse", "--show-toplevel"],
            cwd=str(path), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, timeout=2, env=_safe_git_env(), **_GIT_POPEN_KW,
        )
        if r.returncode == 0:
            return Path(r.stdout.decode("utf-8", errors="replace").strip()).resolve()
    except Exception:
        pass
    return None


def _has_marker(directory: Path) -> bool:
    return any((directory / m).exists() for m in _PROJECT_MARKERS)


def _skip_dir(name: str) -> bool:
    if name.startswith("."):
        return True
    return _is_skip_marker_dir(name)


def _scan_sub_projects(root: Path, max_depth: int = 2) -> list[Path]:
    """Return paths of immediate sub-project roots inside root (depth 1-max_depth).

    Does not recurse into a directory once a marker is found (stops per branch).
    """
    found: list[Path] = []

    def _walk(directory: Path, depth: int) -> None:
        if depth > max_depth:
            return
        try:
            entries = sorted(directory.iterdir())
        except (OSError, PermissionError):
            return
        for entry in entries:
            if not entry.is_dir():
                continue
            if _skip_dir(entry.name):
                continue
            if _has_marker(entry):
                found.append(entry.resolve())
                # Stop descending this branch - first marker wins
            else:
                _walk(entry, depth + 1)

    _walk(root, 1)
    return found


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def resolve_project_path(raw: str) -> Path:
    """Resolve a raw path string to a project root.

    If the path is inside a git repo: returns the git root.
    If not a git repo: scans inside for project marker files and returns
    the deepest matching root found at depth 0-3. Falls back to the given
    path if no markers found.

    Raises ValueError on invalid or non-existent paths.
    """
    from icx_engine.graph.storage import validate_project_path
    resolved = validate_project_path(raw)

    if _is_git_repo(resolved):
        git_root = _git_root(resolved)
        if git_root and git_root.exists():
            return git_root
        return resolved

    # Not a git repo - scan for marker files depth 0-3
    if _has_marker(resolved):
        return resolved

    for depth in range(1, 4):
        candidates = _scan_sub_projects(resolved, max_depth=depth)
        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            # Multiple projects - caller must handle ambiguity
            return resolved  # Return as-is; detect_sub_projects will expose them

    return resolved


def detect_sub_projects(root: Path) -> list[Path]:
    """Return list of sub-project paths inside root, or empty list if root itself is the project."""
    root = root.resolve()
    subs = _scan_sub_projects(root, max_depth=2)
    return subs


def check_staleness(project_id: str, project_root: Path) -> dict:
    """Check if the graph for project_id is stale.

    Returns a dict with status one of:
      ok, no_graph, no_manifest, incremental, stale, freshness_unknown
    """
    from icx_engine.graph import storage

    gpath = storage.graph_path(project_id)
    if not gpath.exists():
        return {"status": "no_graph"}

    manifest = storage.read_manifest(project_id)
    if manifest is None:
        return {"status": "no_manifest"}

    total_files: int = manifest.get("total_files", 0)
    if total_files == 0:
        return {"status": "no_manifest"}

    manifest_commit: str | None = manifest.get("git_commit")

    if _is_git_repo(project_root):
        try:
            head_r = subprocess.run(
                [*_GIT_BASE_CMD, "-c", "core.quotepath=false", "rev-parse", "HEAD"],
                cwd=str(project_root), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL, timeout=2, env=_safe_git_env(), **_GIT_POPEN_KW,
            )
            current_commit = head_r.stdout.decode("utf-8", errors="replace").strip() if head_r.returncode == 0 else None

            if current_commit and current_commit == manifest_commit:
                return {"status": "ok"}

            if manifest_commit and current_commit:
                diff_r = subprocess.run(
                    [*_GIT_BASE_CMD, "-c", "core.quotepath=false",
                     "diff", "--name-only", "--no-ext-diff", f"{manifest_commit}..HEAD"],
                    cwd=str(project_root), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    stdin=subprocess.DEVNULL, timeout=5, env=_safe_git_env(), **_GIT_POPEN_KW,
                )
                if diff_r.returncode == 0:
                    changed = len([l for l in diff_r.stdout.decode("utf-8", errors="replace").splitlines() if l.strip()])
                    pct = changed / total_files if total_files > 0 else 0.0
                    if pct <= _STALE_THRESHOLD:
                        return {"status": "incremental", "changed": changed, "total": total_files, "pct": round(pct * 100, 1)}
                    return {"status": "stale", "changed": changed, "total": total_files, "pct": round(pct * 100, 1)}
        except subprocess.TimeoutExpired:
            _log.debug("git staleness check timed out for %s; returning freshness_unknown", project_root)
            return {"status": "freshness_unknown"}
        except Exception:
            pass

    # Fallback: mtime sampling against manifest
    stored_mtimes: dict[str, float] = manifest.get("file_mtimes", {})
    if not stored_mtimes:
        return {"status": "no_manifest"}

    changed = 0
    sample = list(stored_mtimes.items())[:500]
    for rel_path, stored_mtime in sample:
        abs_path = project_root / rel_path
        try:
            current_mtime = os.path.getmtime(abs_path)
            if abs(current_mtime - stored_mtime) > 1.0:
                changed += 1
        except OSError:
            changed += 1

    if len(sample) > 0:
        changed_estimated = int(changed * total_files / len(sample))
    else:
        changed_estimated = 0

    pct = changed_estimated / total_files if total_files > 0 else 0.0
    if pct <= _STALE_THRESHOLD:
        return {"status": "incremental", "changed": changed_estimated, "total": total_files, "pct": round(pct * 100, 1)}
    return {"status": "stale", "changed": changed_estimated, "total": total_files, "pct": round(pct * 100, 1)}


def validate_and_resolve_paths(raw_paths: list[str]) -> tuple[list[Path], dict | None]:
    """Validate, resolve, and ambiguity-check a list of raw project path strings.

    Returns (resolved_paths, error_dict).
    error_dict is None on success, or a structured error dict on failure.
    Max 2 paths allowed.
    """
    if not raw_paths:
        return [], {
            "status": "error",
            "code": "NO_PATH",
            "message": "project_paths is required. Ask the user which project path to use.",
            "action_required": "ask_user_for_path",
        }

    if len(raw_paths) > 2:
        return [], {
            "status": "error",
            "code": "TOO_MANY_PATHS",
            "message": f"Maximum 2 project paths allowed, got {len(raw_paths)}. Pass at most 1 frontend + 1 backend path.",
            "action_required": "reduce_paths_to_max_2",
        }

    resolved: list[Path] = []
    for raw in raw_paths:
        try:
            r = resolve_project_path(raw)
            resolved.append(r)
        except Exception as exc:
            return [], {
                "status": "error",
                "code": "INVALID_PATH",
                "message": str(exc),
                "action_required": "ask_user_for_valid_path",
                "path": raw,
            }

    # Ambiguity check: if any resolved path contains multiple sub-projects,
    # the path is too broad - ask user to be specific.
    for r in resolved:
        subs = detect_sub_projects(r)
        if len(subs) > 1:
            if len(subs) > _MAX_SUB_PROJECTS:
                return [], {
                    "status": "error",
                    "code": "PATH_TOO_BROAD",
                    "message": f"Path '{r}' contains {len(subs)} sub-projects, which is too many. Provide a more specific path.",
                    "action_required": "ask_user_for_specific_path",
                    "path": str(r),
                }
            return [], {
                "status": "error",
                "code": "AMBIGUOUS_PATH",
                "message": (
                    f"Path '{r}' contains {len(subs)} projects. "
                    "Ask the user which specific project path(s) to use (max 2)."
                ),
                "action_required": "ask_user_to_pick_project_paths",
                "found_projects": [str(s) for s in subs],
                "path": str(r),
            }

    return resolved, None
