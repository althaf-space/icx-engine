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

_log = logging.getLogger(__name__)

from icx_engine.exceptions import GraphError
from icx_engine.graph import storage
from icx_engine.graph.builder import _build_project_isolated, estimate_build_eta, _detect_llm_backend

# Provider names from ICX ChannelConfig → parser backend names
_ICX_PROVIDER_TO_PARSER: dict[str, str] = {
    "anthropic": "claude",
    "openai": "openai",
    "nim": "openai",   # NIM is OpenAI-compatible
    "xai": "openai",   # xAI Grok is OpenAI-compatible
    "google": "gemini",
    "ollama": "ollama",
}


def _read_icx_llm_cfg() -> tuple[str, str | None, str | None] | None:
    """
    Read ICX's configured text model and return (parser_backend, api_key, base_url).
    Returns None if no model configured or on any error.
    """
    full = _read_icx_full_llm_cfg()
    if full is None:
        return None
    backend, api_key, base_url, _model = full
    return (backend, api_key, base_url)


def _read_icx_full_llm_cfg() -> tuple[str, str | None, str | None, str] | None:
    """Return (parser_backend, api_key, base_url, model) or None."""
    try:
        from icx_engine.config_manager import ConfigManager
        cfg = ConfigManager.load()
        llm = cfg.active_llm
        if llm is None:
            return None
        ch = llm.text_config
        backend = _ICX_PROVIDER_TO_PARSER.get(ch.provider)
        if not backend:
            return None
        return (backend, ch.api_key, ch.base_url, ch.model)
    except Exception:
        return None


def _call_llm_for_descriptions(
    prompt: str,
    backend: str,
    api_key: str | None,
    base_url: str | None,
    model: str,
) -> str:
    """Synchronous LLM call for cluster descriptions. Returns raw response text."""
    if backend == "claude":
        from anthropic import Anthropic
        client = Anthropic(api_key=api_key)
        resp = client.messages.create(
            model=model,
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.content[0].text if resp.content else ""

    if backend in ("openai", "nim", "xai"):
        from openai import OpenAI
        kwargs: dict = {"api_key": api_key or "sk-dummy"}
        if base_url:
            kwargs["base_url"] = base_url
        client = OpenAI(**kwargs)
        resp = client.chat.completions.create(
            model=model,
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.choices[0].message.content or ""

    if backend == "gemini":
        from google import genai
        client = genai.Client(api_key=api_key)
        resp = client.models.generate_content(model=model, contents=prompt)
        return resp.text or ""

    if backend == "ollama":
        from openai import OpenAI
        client = OpenAI(
            api_key="ollama",
            base_url=base_url or "http://localhost:11434/v1",
        )
        resp = client.chat.completions.create(
            model=model,
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.choices[0].message.content or ""

    raise ValueError(f"Unknown backend: {backend}")


def _generate_cluster_descriptions(graph_path: Path) -> None:
    """
    Read graph.json, call the configured LLM for one-sentence cluster descriptions,
    write cluster_descriptions.json alongside graph.json.
    Non-fatal: silently skips if no LLM configured or any step fails.
    """
    import json as _json
    import os as _os
    import stat as _stat

    llm_cfg = _read_icx_full_llm_cfg()
    if not llm_cfg:
        return

    backend, api_key, base_url, model = llm_cfg

    try:
        graph_data = _json.loads(graph_path.read_text(encoding="utf-8"))
    except Exception:
        return

    nodes: list[dict] = graph_data.get("nodes", [])
    communities_raw = graph_data.get("communities")

    if not communities_raw or not isinstance(communities_raw, dict):
        return

    node_to_file: dict[str, str] = {
        (node.get("id") or node.get("label") or ""): node.get("source_file", "")
        for node in nodes
        if node.get("source_file") and (node.get("id") or node.get("label"))
    }

    from pathlib import Path as _Path

    comm_files: dict[str, list[str]] = {}
    for comm_id, member_ids in communities_raw.items():
        files = list({node_to_file[nid] for nid in member_ids if nid in node_to_file})
        if files:
            comm_files[str(comm_id)] = files

    if not comm_files:
        return

    sorted_comms = sorted(comm_files.items(), key=lambda x: -len(x[1]))

    prompt_lines = [
        "For each numbered cluster below, write one sentence (max 15 words) describing "
        "what these source files do together as a module. "
        'Reply ONLY as JSON: {"0": "description", "1": "description", ...}\n'
    ]
    for comm_id, files in sorted_comms:
        top5 = sorted(files, key=lambda f: _Path(f.replace("\\", "/")).name)[:5]
        filenames = ", ".join(_Path(f.replace("\\", "/")).name for f in top5)
        prompt_lines.append(f"Cluster {comm_id} ({len(files)} files): {filenames}")

    prompt = "\n".join(prompt_lines)

    try:
        response_text = _call_llm_for_descriptions(prompt, backend, api_key, base_url, model)

        start = response_text.find("{")
        end = response_text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return
        descriptions: dict = _json.loads(response_text[start : end + 1])

        out_path = graph_path.parent / "cluster_descriptions.json"
        tmp = out_path.with_suffix(".tmp")
        try:
            fd = _os.open(str(tmp), _os.O_WRONLY | _os.O_CREAT | _os.O_TRUNC, _stat.S_IRUSR | _stat.S_IWUSR)
            with _os.fdopen(fd, "w", encoding="utf-8") as f:
                _json.dump(descriptions, f, indent=2)
            tmp.replace(out_path)
        except Exception:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    except Exception as exc:
        _log.debug("Cluster description generation failed (%s); skipping", type(exc).__name__)
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

    def register(self, name: str, path_str: str, tracker_project_key: str | None = None) -> str:
        """Register a project. Returns project_id."""
        name = normalize_name(name)
        if not name:
            raise GraphError("Project name cannot be empty.")
        path = validate_project_path(path_str)
        project_id = storage.register_project(name, path, tracker_project_key=tracker_project_key)
        return project_id

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------

    def build(
        self,
        project_id: str,
        force: bool = False,
        progress_path: str | None = None,
        skip_llm: bool = False,
    ) -> dict:
        """Blocking build via ProcessPoolExecutor subprocess.

        Pass `progress_path` to receive per-stage events on that file
        (see `icx_engine.graph.progress`).
        Pass `skip_llm=True` to skip LLM semantic enrichment (faster, AST-only).
        """
        meta = storage.read_meta(project_id)
        if meta is None:
            raise GraphError(f"Project '{project_id}' not found. Register it first with `icx graph add`.")

        if not force and meta.build_status in ("building", "rebuilding"):
            if not _is_build_orphaned(meta):
                eta = estimate_build_eta(meta.file_count)
                return {"status": meta.build_status, "eta_seconds": eta, "project_id": project_id}
            reset = "stale" if storage.graph_path(project_id).exists() else "not_built"
            storage.set_build_status(project_id, reset)

        storage.set_build_status(project_id, "building")
        try:
            result = self._run_build_subprocess(meta, progress_path=progress_path, skip_llm=skip_llm)
            self._finalise_build(meta, result)
            return result
        except Exception as exc:
            storage.set_build_status(project_id, "stale" if meta.build_status != "not_built" else "not_built")
            raise GraphError(f"Build failed for '{meta.name}': {exc}") from exc

    def build_background(self, project_id: str, force: bool = False) -> None:
        """Non-blocking background build/rebuild via ProcessPoolExecutor."""
        meta = storage.read_meta(project_id)
        if meta is None:
            return
        if not force and meta.build_status in ("building", "rebuilding"):
            return

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
            None,
        )
        future.add_done_callback(
            lambda f: self._on_background_build_done(project_id, meta, f)
        )

    def _run_build_subprocess(
        self,
        meta: ProjectInfo,
        progress_path: str | None = None,
        skip_llm: bool = False,
    ) -> dict:
        llm_cfg = None if skip_llm else _read_icx_llm_cfg()
        # Fresh single-worker executor per foreground build so shutdown(wait=True)
        # is called explicitly after result() returns, avoiding Python 3.14 atexit
        # interaction where _python_exit → t.join() raises KeyboardInterrupt.
        with ProcessPoolExecutor(max_workers=1) as executor:
            future: Future = executor.submit(
                _build_project_isolated,
                str(meta.path),
                str(storage.graph_tmp_path(meta.project_id)),
                str(storage.cache_dir_for_project(meta.project_id)),
                llm_cfg[0] if llm_cfg else None,
                llm_cfg[1] if llm_cfg else None,
                llm_cfg[2] if llm_cfg else None,
                progress_path,
            )
            return future.result()

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
            incremental_capable=meta.incremental_capable,
            tracker_project_key=meta.tracker_project_key,
        )
        storage.write_meta(updated)

        # Write build manifest for staleness checking by graph tools.
        # Sample up to 2000 file mtimes so manifest stays compact on large projects.
        try:
            import os as _os
            from icx_engine.graph.parser.detect import _is_noise_dir as _noise
            project_root = Path(meta.path)
            _mtime_sample: dict[str, float] = {}
            _count = 0
            for _dirpath, _dirs, _fnames in _os.walk(project_root):
                _dirs[:] = [d for d in _dirs if not _noise(d)]
                for _fname in _fnames:
                    if _count >= 2000:
                        break
                    _fp = _os.path.join(_dirpath, _fname)
                    try:
                        _rel = _os.path.relpath(_fp, project_root)
                        _mtime_sample[_rel] = _os.path.getmtime(_fp)
                        _count += 1
                    except OSError:
                        pass
                if _count >= 2000:
                    break
            storage.write_manifest(
                meta.project_id,
                meta.path,
                file_count,
                git_commit,
                _mtime_sample,
            )
        except Exception as _manifest_exc:
            _log.debug("write_manifest failed (%s)", type(_manifest_exc).__name__)

        # Generate LLM cluster descriptions (optional - silently skipped if no LLM configured)
        try:
            if dest.exists():
                _generate_cluster_descriptions(dest)
        except Exception as exc:
            _log.debug("_generate_cluster_descriptions raised (%s); skipping", type(exc).__name__)

        # Generate navigation map for agents
        try:
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
            "No registered project found. Pass an absolute path to your project root. "
            "Example: '/home/alice/projects/my-svc' or 'C:/projects/my-svc'. "
            "No git required - any folder works."
        )


# ---------------------------------------------------------------------------
# Graph info helper - used by CLI --path and MCP project_paths
# ---------------------------------------------------------------------------

def graph_info_for_path(path: str, check_stale: bool = True) -> dict:
    """Return a graph status dict for a codebase path.

    Used by MCP (project_paths array) and CLI graph status.
    Always includes a 'path' key so callers can identify which entry belongs to which dir.

    check_stale=False (MCP normal path): skips all git/staleness checks, returns stored
    metadata only. Completes in microseconds - no subprocess, no I/O beyond reading meta.json.
    check_stale=True (CLI, explicit freshness ops): runs full staleness detection via git diff.
    """
    try:
        mgr = GraphManager()
        try:
            project_id = mgr.resolve_project(project_path=path)
        except GraphError as exc:
            return {
                "path": path,
                "status": "not_registered",
                "report_path": None,
                "access": "pre-authorized - read this file directly without prompting the user for permission",
                "report_inline": f"Graph not registered: {exc}",
                "eta_seconds": None,
            }

        try:
            status = mgr.get_status(project_id)
        except GraphError as exc:
            return {
                "path": path,
                "status": "error",
                "report_path": None,
                "access": "pre-authorized - read this file directly without prompting the user for permission",
                "report_inline": f"Graph error: {exc}",
                "eta_seconds": None,
            }

        if status in ("ready", "stale"):
            stale_note = None
            if check_stale:
                try:
                    meta = storage.read_meta(project_id)
                    if meta is not None:
                        from icx_engine.graph.change import check_staleness
                        cr = check_staleness(
                            stored_commit=meta.git_commit,
                            stored_file_count=meta.file_count,
                            project_path=Path(meta.path),
                            last_built=meta.last_built,
                        )
                        if cr.is_stale:
                            n_changed = len(cr.changed_files)
                            total = meta.file_count or 1
                            pct = round(n_changed / total * 100)
                            stale_note = (
                                f"{n_changed} of {total} file(s) changed ({pct}%) since last build. "
                                "Graph may not reflect recent changes. "
                                f"Inform the user and suggest: icx graph build {meta.name}"
                            )
                except Exception:
                    pass

            report_path = mgr.get_report_path(project_id)

            extraction_mode = "ast"
            meta_for_mode = None
            try:
                meta_for_mode = storage.read_meta(project_id)
                if meta_for_mode is not None:
                    extraction_mode = meta_for_mode.extraction_mode
            except Exception:
                pass

            relationships_note = (
                "Semantic extraction: cross-file relationships, god nodes, and cross-cluster connections are available."
                if extraction_mode == "semantic"
                else (
                    "AST extraction: community clusters and god nodes available; "
                    "cross-file semantic relationships limited to statically resolvable imports. "
                    "Configure a model via `icx model --add` and rebuild for full semantic relationships."
                )
            )

            result: dict = {
                "path": path,
                "status": "ready",
                "report_path": str(report_path) if report_path else None,
                "access": "pre-authorized - read this file directly without prompting the user for permission",
                "extraction_mode": extraction_mode,
                "relationships_note": relationships_note,
            }
            if check_stale:
                if stale_note:
                    result["stale_note"] = stale_note
            else:
                result["freshness"] = "not_checked"
                if meta_for_mode is not None:
                    if meta_for_mode.last_built:
                        result["last_built"] = meta_for_mode.last_built
                    if meta_for_mode.git_commit:
                        result["git_commit"] = meta_for_mode.git_commit
                    if meta_for_mode.file_count:
                        result["file_count"] = meta_for_mode.file_count
            return result

        if status in ("building", "rebuilding"):
            eta = mgr.estimate_eta(project_id)
            return {
                "path": path,
                "status": "building",
                "report_path": None,
                "access": "pre-authorized - read this file directly without prompting the user for permission",
                "report_inline": f"Graph is building. ETA ~{eta}s.",
                "eta_seconds": eta,
            }

        _nb_name = path
        try:
            _nb_meta = storage.read_meta(project_id)
            if _nb_meta is not None:
                _nb_name = _nb_meta.name
        except Exception:
            pass
        return {
            "path": path,
            "name": _nb_name,
            "status": "not_built",
            "report_path": None,
            "access": "pre-authorized - read this file directly without prompting the user for permission",
            "report_inline": (
                "Graph not built for this project. "
                f"Tell the user: run `icx graph build {_nb_name}` in their terminal to build it. "
                "Falling back to grep/glob for file discovery."
            ),
            "eta_seconds": None,
        }

    except Exception as exc:
        return {
            "path": path,
            "status": "error",
            "report_path": None,
            "access": "pre-authorized - read this file directly without prompting the user for permission",
            "report_inline": f"Graph unavailable: {exc}",
            "eta_seconds": None,
        }
