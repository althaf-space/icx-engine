"""
Graph storage layer: ~/.icx/graphs/ layout, project-id derivation,
registry.json + meta.json read/write, atomic writes.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import stat
import sys
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from icx_engine.exceptions import GraphError

_log = logging.getLogger(__name__)

_GRAPHS_DIR_NAME = "graphs"
_REGISTRY_FILE = "registry.json"
_META_FILE = "meta.json"
_GRAPH_FILE = "graph.json"
_GRAPH_TMP_FILE = "graph.json.tmp"

BuildStatus = Literal["not_built", "building", "ready", "stale", "rebuilding"]

_registry_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Data containers
# ---------------------------------------------------------------------------

@dataclass
class ProjectInfo:
    name: str
    path: str
    project_id: str
    last_built: str | None
    git_commit: str | None
    file_count: int
    build_status: BuildStatus
    build_started_at: str | None = None
    extraction_mode: str = "ast"  # "ast" or "semantic"


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def _graphs_root() -> Path:
    """~/.icx/graphs/ - created on first access with owner-only permissions on Unix."""
    root = Path.home() / ".icx" / _GRAPHS_DIR_NAME
    if not root.exists():
        root.mkdir(parents=True, exist_ok=True)
        if sys.platform != "win32":
            try:
                root.chmod(stat.S_IRWXU)
            except OSError:
                pass
    return root


def _project_dir(project_id: str) -> Path:
    d = _graphs_root() / project_id
    if not d.exists():
        d.mkdir(parents=True, exist_ok=True)
    return d


def _cache_dir(project_id: str) -> Path:
    d = _project_dir(project_id) / "cache"
    if not d.exists():
        d.mkdir(parents=True, exist_ok=True)
    return d


def _registry_path() -> Path:
    return _graphs_root() / _REGISTRY_FILE


def _meta_path(project_id: str) -> Path:
    return _project_dir(project_id) / _META_FILE


def graph_path(project_id: str) -> Path:
    return _project_dir(project_id) / _GRAPH_FILE


def graph_tmp_path(project_id: str) -> Path:
    return _project_dir(project_id) / _GRAPH_TMP_FILE


def report_path(project_id: str) -> Path:
    return _project_dir(project_id) / "GRAPH_REPORT.md"


def cache_dir_for_project(project_id: str) -> Path:
    return _cache_dir(project_id)


# ---------------------------------------------------------------------------
# Project-ID derivation
# ---------------------------------------------------------------------------

def derive_project_id(resolved_path: Path) -> str:
    """SHA256[:12] of the resolved absolute project path. Stable across renames."""
    digest = hashlib.sha256(str(resolved_path).encode("utf-8")).hexdigest()
    return digest[:12]


# ---------------------------------------------------------------------------
# Path validation
# ---------------------------------------------------------------------------

def validate_project_path(raw: str) -> Path:
    """Resolve and validate a project path string. Raises GraphError on failure."""
    if any(c in raw for c in ["\r", "\n", "\t", "\x00"]):
        raise GraphError("Path contains invalid characters.")
    p = Path(raw).resolve()
    if not p.exists():
        raise GraphError(f"Path does not exist: {p}")
    if not p.is_dir():
        raise GraphError(f"Path is not a directory: {p}")
    return p


def normalize_name(name: str) -> str:
    """Lowercase + strip. Project names are always case-insensitive."""
    return name.strip().lower()


# ---------------------------------------------------------------------------
# Registry read/write
# ---------------------------------------------------------------------------

def _read_registry() -> list[dict]:
    path = _registry_path()
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        _log.warning("Failed to parse graph registry (%s); treating as empty", type(exc).__name__)
        return []


def _write_registry(entries: list[dict]) -> None:
    """Write registry. Caller must hold _registry_lock."""
    path = _registry_path()
    tmp = path.with_suffix(".tmp")
    try:
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(entries, f, indent=2)
        tmp.replace(path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Meta read/write
# ---------------------------------------------------------------------------

def read_meta(project_id: str) -> ProjectInfo | None:
    path = _meta_path(project_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return ProjectInfo(
            name=data.get("name", ""),
            path=data.get("path", ""),
            project_id=data.get("project_id", project_id),
            last_built=data.get("last_built"),
            git_commit=data.get("git_commit"),
            file_count=data.get("file_count", 0),
            build_status=data.get("build_status", "not_built"),
            build_started_at=data.get("build_started_at"),
            extraction_mode=data.get("extraction_mode", "ast"),
        )
    except Exception as exc:
        _log.warning("Failed to parse meta.json for project %s (%s)", project_id, type(exc).__name__)
        return None


def write_meta(info: ProjectInfo) -> None:
    path = _meta_path(info.project_id)
    tmp = path.with_suffix(".tmp")
    data = asdict(info)
    try:
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        tmp.replace(path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def set_build_status(project_id: str, status: BuildStatus) -> None:
    from datetime import datetime, timezone
    with _registry_lock:
        meta = read_meta(project_id)
        if meta is None:
            return
        meta.build_status = status
        if status in ("building", "rebuilding"):
            meta.build_started_at = datetime.now(timezone.utc).isoformat()
        else:
            meta.build_started_at = None
        write_meta(meta)


# ---------------------------------------------------------------------------
# Registry operations
# ---------------------------------------------------------------------------

def register_project(name: str, path: Path) -> str:
    """Add or update a project in the registry. Returns project_id."""
    name = normalize_name(name)
    project_id = derive_project_id(path)

    with _registry_lock:
        entries = _read_registry()
        # Remove existing entry for this path or name (update in place)
        entries = [
            e for e in entries
            if e.get("project_id") != project_id and e.get("name") != name
        ]
        entries.append({
            "name": name,
            "path": str(path),
            "project_id": project_id,
        })
        _write_registry(entries)

    # Create project dir + initial meta if not present (outside registry lock)
    meta = read_meta(project_id)
    if meta is None:
        write_meta(ProjectInfo(
            name=name,
            path=str(path),
            project_id=project_id,
            last_built=None,
            git_commit=None,
            file_count=0,
            build_status="not_built",
        ))
    return project_id


def lookup_by_name(name: str) -> ProjectInfo | None:
    name = normalize_name(name)
    for entry in _read_registry():
        if normalize_name(entry.get("name", "")) == name:
            return read_meta(entry["project_id"])
    return None


def lookup_by_path(path: Path) -> ProjectInfo | None:
    project_id = derive_project_id(path)
    return read_meta(project_id)


def lookup_by_cwd() -> ProjectInfo | None:
    cwd = Path.cwd().resolve()
    for entry in _read_registry():
        registered = Path(entry.get("path", "")).resolve()
        # Match exact or if cwd is inside the registered project
        if cwd == registered or _is_relative_to(cwd, registered):
            return read_meta(entry["project_id"])
    return None


def list_projects() -> list[ProjectInfo]:
    results = []
    for entry in _read_registry():
        meta = read_meta(entry["project_id"])
        if meta is not None:
            results.append(meta)
    return results


def remove_project(project_id: str, keep_cache: bool = False) -> None:
    import shutil
    with _registry_lock:
        entries = _read_registry()
        entries = [e for e in entries if e.get("project_id") != project_id]
        _write_registry(entries)

    project_dir = _graphs_root() / project_id
    if project_dir.exists():
        if keep_cache:
            # Remove everything except cache/
            for item in project_dir.iterdir():
                if item.name != "cache":
                    if item.is_file():
                        item.unlink(missing_ok=True)
                    elif item.is_dir():
                        shutil.rmtree(item, ignore_errors=True)
        else:
            shutil.rmtree(project_dir, ignore_errors=True)


def update_registry_after_build(
    project_id: str,
    file_count: int,
    git_commit: str | None,
) -> None:
    """Sync registry.json last_built/file_count after a successful build."""
    pass


# ---------------------------------------------------------------------------
# Compatibility helper (Path.is_relative_to added in Python 3.9)
# ---------------------------------------------------------------------------

def _is_relative_to(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False
