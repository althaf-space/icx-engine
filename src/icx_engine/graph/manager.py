"""
GraphManager: orchestrates build, query, status, register, list, remove.

Executors:
  _BUILD_EXECUTOR  ProcessPoolExecutor(max_workers=3)  - isolated subprocess per build

Module-level singleton initialised at first use and shut down via atexit.
"""
from __future__ import annotations

import atexit
import logging
import threading
from concurrent.futures import Future, ProcessPoolExecutor
from pathlib import Path
from typing import Literal

_log = logging.getLogger(__name__)

from icx_engine.exceptions import GraphError
from icx_engine.graph import storage
from icx_engine.graph.builder import _build_project_isolated, estimate_build_eta, _detect_llm_backend

# Provider names from ICX ChannelConfig → graphify backend names
_ICX_PROVIDER_TO_GRAPHIFY: dict[str, str] = {
    "anthropic": "claude",
    "openai": "openai",
    "nim": "openai",   # NIM is OpenAI-compatible
    "xai": "openai",   # xAI Grok is OpenAI-compatible
    "google": "gemini",
    "ollama": "ollama",
}


def _read_icx_llm_cfg() -> tuple[str, str | None, str | None] | None:
    """
    Read ICX's configured text model and return (graphify_backend, api_key, base_url).
    Returns None if no model configured or on any error.
    """
    try:
        from icx_engine.config_manager import ConfigManager
        cfg = ConfigManager.load()
        llm = cfg.active_llm
        if llm is None:
            return None
        ch = llm.text_config
        backend = _ICX_PROVIDER_TO_GRAPHIFY.get(ch.provider)
        if not backend:
            return None
        return (backend, ch.api_key, ch.base_url)
    except Exception:
        return None
from icx_engine.graph.change import current_git_commit
from icx_engine.graph.querier import generate_graph_report
from icx_engine.graph.storage import (
    BuildStatus,
    ProjectInfo,
    normalize_name,
    validate_project_path,
    report_path as _report_path,
)

# ---------------------------------------------------------------------------
# Module-level executor singletons
# ---------------------------------------------------------------------------

_executor_lock = threading.Lock()
_BUILD_EXECUTOR: ProcessPoolExecutor | None = None


def _get_build_executor() -> ProcessPoolExecutor:
    global _BUILD_EXECUTOR
    with _executor_lock:
        if _BUILD_EXECUTOR is None:
            import os
            _BUILD_EXECUTOR = ProcessPoolExecutor(max_workers=max(1, os.cpu_count() or 4))
    return _BUILD_EXECUTOR


def _shutdown_executors() -> None:
    global _BUILD_EXECUTOR
    with _executor_lock:
        if _BUILD_EXECUTOR is not None:
            _BUILD_EXECUTOR.shutdown(wait=False, cancel_futures=True)
            _BUILD_EXECUTOR = None


atexit.register(_shutdown_executors)


# ---------------------------------------------------------------------------
# Orphan detection
# ---------------------------------------------------------------------------

def _is_build_orphaned(meta: "ProjectInfo") -> bool:
    if not meta.build_started_at:
        return True
    from datetime import datetime, timezone
    try:
        started = datetime.fromisoformat(meta.build_started_at)
        elapsed = (datetime.now(timezone.utc) - started).total_seconds()
        return elapsed > 600
    except Exception:
        return False


# ---------------------------------------------------------------------------
# GraphManager
# ---------------------------------------------------------------------------

class GraphManager:

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(self, name: str, path_str: str) -> str:
        """Register a project. Returns project_id."""
        name = normalize_name(name)
        if not name:
            raise GraphError("Project name cannot be empty.")
        path = validate_project_path(path_str)
        project_id = storage.register_project(name, path)
        return project_id

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------

    def build(self, project_id: str, force: bool = False) -> dict:
        """
        Blocking build. Runs in subprocess via ProcessPoolExecutor.
        Returns build stats dict.
        Raises GraphError on failure.
        """
        meta = storage.read_meta(project_id)
        if meta is None:
            raise GraphError(f"Project '{project_id}' not found. Register it first with `icx graph add`.")

        if not force and meta.build_status in ("building", "rebuilding"):
            if not _is_build_orphaned(meta):
                eta = estimate_build_eta(meta.file_count)
                return {"status": meta.build_status, "eta_seconds": eta, "project_id": project_id}
            # Orphaned build: reset and rebuild
            reset = "stale" if storage.graph_path(project_id).exists() else "not_built"
            storage.set_build_status(project_id, reset)

        storage.set_build_status(project_id, "building")
        try:
            result = self._run_build_subprocess(meta)
            self._finalise_build(meta, result)
            return result
        except Exception as exc:
            storage.set_build_status(project_id, "stale" if meta.build_status != "not_built" else "not_built")
            raise GraphError(f"Build failed for '{meta.name}': {exc}") from exc

    def build_background(self, project_id: str, force: bool = False) -> None:
        """
        Non-blocking background build/rebuild. Submits to ProcessPoolExecutor.
        Guards against duplicate spawns via build_status unless force=True.
        """
        meta = storage.read_meta(project_id)
        if meta is None:
            return
        if not force and meta.build_status in ("building", "rebuilding"):
            return  # already running

        storage.set_build_status(project_id, "rebuilding")
        llm_cfg = _read_icx_llm_cfg()
        future: Future = _get_build_executor().submit(
            _build_project_isolated,
            str(meta.path),
            str(storage.graph_tmp_path(project_id)),
            str(storage.cache_dir_for_project(project_id)),
            llm_cfg[0] if llm_cfg else None,
            llm_cfg[1] if llm_cfg else None,
            llm_cfg[2] if llm_cfg else None,
        )
        future.add_done_callback(
            lambda f: self._on_background_build_done(project_id, meta, f)
        )

    def _run_build_subprocess(self, meta: ProjectInfo) -> dict:
        llm_cfg = _read_icx_llm_cfg()
        future: Future = _get_build_executor().submit(
            _build_project_isolated,
            str(meta.path),
            str(storage.graph_tmp_path(meta.project_id)),
            str(storage.cache_dir_for_project(meta.project_id)),
            llm_cfg[0] if llm_cfg else None,
            llm_cfg[1] if llm_cfg else None,
            llm_cfg[2] if llm_cfg else None,
        )
        return future.result()  # blocks

    def _finalise_build(self, meta: ProjectInfo, result: dict) -> None:
        if result.get("error"):
            raise GraphError(result["error"])

        # Atomic rename: .tmp -> graph.json
        tmp = storage.graph_tmp_path(meta.project_id)
        dest = storage.graph_path(meta.project_id)
        if tmp.exists():
            tmp.replace(dest)

        git_commit = current_git_commit(Path(meta.path))
        file_count = result.get("file_count", 0)

        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()

        updated = ProjectInfo(
            name=meta.name,
            path=meta.path,
            project_id=meta.project_id,
            last_built=now,
            git_commit=git_commit,
            file_count=file_count,
            build_status="ready",
            extraction_mode=result.get("extraction_mode", "ast"),
        )
        storage.write_meta(updated)
        storage.update_registry_after_build(meta.project_id, file_count, git_commit)

        # Generate navigation map for agents
        try:
            dest = storage.graph_path(meta.project_id)
            report_out = _report_path(meta.project_id)
            if dest.exists():
                generate_graph_report(dest, report_out)
        except Exception as exc:
            _log.debug("generate_graph_report failed (%s)", type(exc).__name__)

    def _on_background_build_done(self, project_id: str, meta: ProjectInfo, future: Future) -> None:
        try:
            result = future.result()
            self._finalise_build(meta, result)
        except Exception as exc:
            _log.debug("Background build failed for project %s: %s", project_id, type(exc).__name__)
            storage.set_build_status(project_id, "stale")

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def get_status(self, project_id: str) -> BuildStatus:
        meta = storage.read_meta(project_id)
        if meta is None:
            raise GraphError(f"Project '{project_id}' not found.")
        return meta.build_status

    def estimate_eta(self, project_id: str) -> int:
        meta = storage.read_meta(project_id)
        if meta is None:
            return 0
        semantic = (_read_icx_llm_cfg() is not None) or (_detect_llm_backend() is not None)
        return estimate_build_eta(meta.file_count, semantic=semantic)

    def get_report_path(self, project_id: str) -> Path | None:
        """Return path to GRAPH_REPORT.md if graph is built, else None."""
        meta = storage.read_meta(project_id)
        if meta is None or meta.build_status not in ("ready", "stale"):
            return None
        rp = _report_path(project_id)
        return rp if rp.exists() else None

    # ------------------------------------------------------------------
    # List / Remove
    # ------------------------------------------------------------------

    def list_projects(self) -> list[ProjectInfo]:
        return storage.list_projects()

    def remove(self, project_id: str, keep_cache: bool = False) -> None:
        meta = storage.read_meta(project_id)
        if meta is None:
            raise GraphError(f"Project '{project_id}' not found.")
        storage.remove_project(project_id, keep_cache=keep_cache)

    # ------------------------------------------------------------------
    # Resolve project_id from name / path / cwd
    # ------------------------------------------------------------------

    def resolve_project(
        self,
        project_name: str | None = None,
        project_path: str | None = None,
        cwd: str | None = None,
    ) -> str:
        """
        Lookup priority:
          1. project_name -> registry lookup by name
          2. project_path -> validate + derive id
          3. cwd -> match against registered paths
          4. None of the above -> GraphError
        """
        if project_name:
            info = storage.lookup_by_name(project_name)
            if info is None:
                raise GraphError(
                    f"Project '{project_name}' not found. "
                    "Run `icx graph list` to see registered projects."
                )
            return info.project_id

        if project_path:
            path = validate_project_path(project_path)
            info = storage.lookup_by_path(path)
            if info is None:
                # Auto-register: writes both meta.json AND registry.json
                project_id = storage.register_project(path.name.lower(), path)
                return project_id
            return info.project_id

        if cwd:
            try:
                cwd_path = Path(cwd).resolve()
                info = storage.lookup_by_path(cwd_path)
                if info is not None:
                    return info.project_id
                info = storage.lookup_by_cwd()
                if info is not None:
                    return info.project_id
            except GraphError:
                raise
            except Exception as exc:
                _log.debug("cwd lookup failed: %s", type(exc).__name__)

        raise GraphError(
            "No registered project found. Pass project_path = absolute path to your workspace root. "
            "Example: project_path='E:\\\\my-project' or project_path='/home/user/my-project'. "
            "No git required - any folder works."
        )
