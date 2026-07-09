"""
Graph build pipeline.

Extraction strategy (hybrid):
  1. AST  - always runs. tree-sitter parse of every file. Zero API cost, zero misses.
            Produces nodes + intra-file edges. Louvain on AST graph = globally consistent
            community assignments.
  2. LLM  - runs when a model is configured. Sends file batches to the LLM to extract
            cross-file semantic edges (imports, calls, inheritance). Edges are merged into
            the AST result before community detection.

Community detection always uses Louvain (cluster()) on the final merged graph.
LLM-assigned per-chunk community IDs are intentionally discarded - they collide
across chunk boundaries and produce oversized incoherent clusters.
"""
from __future__ import annotations

import logging
from pathlib import Path

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# File collection: git-first, fallback to filtered rglob
# ---------------------------------------------------------------------------

_PARSER_EXTENSIONS = frozenset({
    ".py", ".js", ".jsx", ".mjs", ".ts", ".tsx", ".go", ".rs", ".java",
    ".groovy", ".gradle", ".c", ".h", ".cpp", ".cc", ".cxx", ".hpp",
    ".rb", ".cs", ".kt", ".kts", ".scala", ".php", ".swift", ".lua",
    ".zig", ".ps1", ".ex", ".exs", ".m", ".mm", ".jl", ".vue", ".svelte",
    ".dart", ".sql", ".md", ".mdx", ".html",
    ".jsp", ".jspx",
    ".erb",
    ".proto",
    ".tf", ".tfvars",
})

def _is_skip_dir(name: str) -> bool:
    from icx_engine.graph.parser.detect import _is_noise_dir
    return _is_noise_dir(name)

# Directories whose names end with these suffixes are expanded archive artifacts.
# Files inside .war/, .jar/, .ear/ etc. are compiled outputs - skip entirely.
_ARCHIVE_DIR_SUFFIXES = frozenset({".war", ".jar", ".ear", ".zip", ".tar", ".gz"})


def _is_inside_archive_dir(rel: Path) -> bool:
    """Return True if any directory component of rel ends with an archive suffix."""
    return any(
        Path(part).suffix.lower() in _ARCHIVE_DIR_SUFFIXES
        for part in rel.parts[:-1]
    )


def _filter_vendor_files(files: list[Path]) -> list[Path]:
    """
    Remove committed vendor/library files using a minified-file ratio heuristic.
    Any directory where >=50% of files contain '.min.' in the filename is vendor.
    Works universally without hardcoding library names.
    """
    from collections import defaultdict

    dir_files: dict[Path, list[Path]] = defaultdict(list)
    for f in files:
        dir_files[f.parent].append(f)

    vendor_dirs: set[Path] = set()
    for d, dfiles in dir_files.items():
        if len(dfiles) < 8:
            continue
        min_count = sum(1 for f in dfiles if ".min." in f.name.lower())
        if min_count / len(dfiles) >= 0.5:
            vendor_dirs.add(d)

    if not vendor_dirs:
        return files

    def _is_vendor(f: Path) -> bool:
        for d in vendor_dirs:
            try:
                f.relative_to(d)
                return True
            except ValueError:
                pass
        return False

    return [f for f in files if not _is_vendor(f)]


def _detect_llm_backend() -> tuple[str, str | None] | None:
    """
    Return (backend, api_key) if any LLM backend is configured via env vars.
    Fallback used when no ICX-configured LLM is passed to the subprocess.
    """
    import os

    _CHECKS = [
        ("claude", "ANTHROPIC_API_KEY"),
        ("openai", "OPENAI_API_KEY"),
        ("gemini", "GEMINI_API_KEY"),
        ("kimi", "KIMI_API_KEY"),
    ]
    for backend, env_var in _CHECKS:
        key = os.environ.get(env_var, "").strip()
        if key:
            return (backend, key)
    ollama_url = os.environ.get("OLLAMA_BASE_URL", "").strip()
    if ollama_url:
        return ("ollama", None)
    return None


def _collect_source_files(project_path: Path) -> list[Path]:
    """
    Collect source files for the graph parser.
    1. git ls-files (respects .gitignore - handles node_modules, dist, etc.)
    2. Fallback: rglob filtered by _is_noise_dir from detect.py
    Applies vendor-dir filter in both paths.
    """
    import subprocess

    try:
        r = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
            cwd=str(project_path),
            capture_output=True,
            text=True,
            timeout=30,
        )
        if r.returncode == 0:
            files = []
            for rel in r.stdout.splitlines():
                rel = rel.strip()
                if not rel:
                    continue
                rel_path = Path(rel)
                if _is_inside_archive_dir(rel_path):
                    continue
                p = project_path / rel_path
                if p.suffix in _PARSER_EXTENSIONS and p.is_file():
                    files.append(p)
            if files:
                return _filter_vendor_files(files)
    except Exception:
        pass

    # Fallback: rglob with skip-dir filter
    files = []
    for ext in _PARSER_EXTENSIONS:
        for p in project_path.rglob(f"*{ext}"):
            rel = p.relative_to(project_path)
            if _is_inside_archive_dir(rel):
                continue
            if any(_is_skip_dir(part) or part.startswith(".") for part in rel.parts[:-1]):
                continue
            if p.is_file():
                files.append(p)
    return _filter_vendor_files(sorted(set(files)))


# ---------------------------------------------------------------------------
# Incremental merge helper
# ---------------------------------------------------------------------------

def _rel_path(p: str, root_posix: str) -> str:
    """Normalize a path for stale-set comparison: backslash -> forward slash,
    and strip an absolute project-root prefix so paths are repo-relative.
    """
    if not p:
        return p
    p = p.replace("\\", "/")
    if root_posix and (p == root_posix or p.startswith(root_posix + "/")):
        p = p[len(root_posix):].lstrip("/")
    return p


def _merge_incremental(
    existing_graph: dict,
    new_extraction: dict,
    changed_files: list[str],
    deleted_files: list[str],
    root_posix: str = "",
) -> dict:
    """
    Removes stale nodes/edges (from changed/deleted files),
    appends newly extracted nodes/edges.
    Preserves fix_confidence_delta and resolution_weight on surviving edges.

    root_posix: absolute project root (POSIX form). Used to normalize
    absolute source_file/target_file values (e.g. from _abs_edges) back to
    repo-relative form so they compare correctly against changed_files/
    deleted_files, which are always repo-relative POSIX paths.
    """
    stale = {_rel_path(p, root_posix) for p in changed_files} | {
        _rel_path(p, root_posix) for p in deleted_files
    }

    surviving_nodes = [
        n for n in existing_graph.get("nodes", [])
        if _rel_path(n.get("file", n.get("source_file", "")), root_posix) not in stale
    ]
    surviving_edges = [
        e for e in existing_graph.get("links", existing_graph.get("edges", []))
        if _rel_path(e.get("source_file", ""), root_posix) not in stale
        and _rel_path(e.get("target_file", ""), root_posix) not in stale
    ]
    # Edges that reference a node removed above (e.g. file renamed/deleted)
    # become dangling even if the edge itself isn't tagged with that file.
    # A node id re-emitted by the fresh extraction is NOT removed - the changed
    # file was re-parsed and produced the same deterministic id (_make_id is
    # path/symbol based, no line numbers). Subtracting new ids keeps the changed
    # file's freshly-extracted edges instead of pruning them as dangling.
    existing_node_ids = {n.get("id") for n in existing_graph.get("nodes", [])}
    surviving_node_ids = {n.get("id") for n in surviving_nodes}
    new_node_ids = {n.get("id") for n in new_extraction.get("nodes", [])}
    removed_ids = existing_node_ids - surviving_node_ids - new_node_ids

    merged_links = surviving_edges + new_extraction.get(
        "links", new_extraction.get("edges", [])
    )
    if removed_ids:
        merged_links = [
            e for e in merged_links
            if e.get("source") not in removed_ids and e.get("target") not in removed_ids
        ]

    merged = dict(existing_graph)
    merged["nodes"] = surviving_nodes + new_extraction.get("nodes", [])
    merged["links"] = merged_links
    return merged


# ---------------------------------------------------------------------------
# Subprocess entry point (must be a top-level function for pickle on Windows)
# ---------------------------------------------------------------------------

def _build_project_isolated(
    project_path_str: str,
    graph_tmp_path_str: str,
    icx_cache_path_str: str,
    llm_backend: str | None = None,
    llm_api_key: str | None = None,
    llm_base_url: str | None = None,
    progress_path: str | None = None,
) -> dict:
    """
    Runs inside an isolated subprocess spawned by ProcessPoolExecutor.

    llm_backend/llm_api_key/llm_base_url are read from ICX's configured model by
    the manager before spawning. Falls back to env var detection if not provided.

    Returns a dict: {"file_count": int, "node_count": int, "edge_count": int,
                     "community_count": int, "extraction_mode": str, "error": str|None}
    """
    import os
    from pathlib import Path as _Path

    result: dict = {
        "file_count": 0,
        "node_count": 0,
        "edge_count": 0,
        "community_count": 0,
        "extraction_mode": "ast",
        "error": None,
    }

    from icx_engine.graph.progress import ProgressEmitter
    progress = ProgressEmitter(progress_path)

    try:
        from icx_engine.graph.parser import cache as _gcache
        from icx_engine.graph.parser.extract import extract
        from icx_engine.graph.parser.build import build_from_json
        from icx_engine.graph.parser.cluster import cluster
        from icx_engine.graph.parser.export import to_json

        icx_cache = _Path(icx_cache_path_str)
        icx_cache.mkdir(parents=True, exist_ok=True)

        # Move cwd away from project dir so the parser cannot create
        # icx-graph-out/ or other relative-path artifacts inside the user repo.
        os.chdir(str(icx_cache))

        def _redirected_cache_dir(root=_Path("."), kind="ast"):
            d = icx_cache / kind
            d.mkdir(parents=True, exist_ok=True)
            return d
        _gcache.cache_dir = _redirected_cache_dir

        project_path = _Path(project_path_str)

        files = _collect_source_files(project_path)

        # Apply .icxignore exclusions from ~/.icx/graphs/<project_id>/.icxignore
        try:
            from icx_engine.graph import storage as _storage
            from icx_engine.graph.parser.icxignore import load as _icx_load
            _pid = _storage.derive_project_id(project_path)
            _ipath = _storage.icxignore_path(_pid)
            if _ipath.exists():
                _icxignore = _icx_load(_ipath, project_path)
                files = [
                    f for f in files
                    if not _icxignore.matches(f.relative_to(project_path).as_posix())
                ]
        except Exception:
            pass

        # -- Incremental hash check ----------------------------------------------------------
        from icx_engine.graph.parser.file_cache import (
            load_hashes, save_hashes, compute_changed_files,
        )

        hash_cache_path = icx_cache / "file_hashes.json"
        stored_hashes = load_hashes(hash_cache_path)
        graph_json_path = icx_cache.parent / "graph.json"
        _incremental = graph_json_path.exists() and bool(stored_hashes)

        # Convert Path objects to relative POSIX strings for hashing
        _rel_files = [f.relative_to(project_path).as_posix() for f in files]

        if _incremental:
            _changed_rel, _deleted_rel, _new_hashes = compute_changed_files(
                str(project_path), _rel_files, stored_hashes
            )
            progress.emit(
                "incremental",
                current=len(_changed_rel),
                total=len(files),
                message=f"{len(_changed_rel)} changed, {len(_deleted_rel)} deleted",
            )
            if not _changed_rel and not _deleted_rel:
                progress.emit("complete", current=1, total=1, message="graph unchanged - skipping rebuild")
                save_hashes(hash_cache_path, _new_hashes)
                return {
                    **result,
                    "file_count": len(files),
                    "incremental": True,
                    "skipped": True,
                }
            # Only parse changed files through AST
            _changed_abs = {project_path / rel for rel in _changed_rel}
            files_to_parse = [f for f in files if f in _changed_abs]
        else:
            _changed_rel, _deleted_rel, _new_hashes = [], [], {}
            files_to_parse = files
        # -- End incremental check -----------------------------------------------------------

        result["file_count"] = len(files)
        progress.emit("scan", current=len(files), total=len(files),
                      message=f"{len(files)} files")

        if not files:
            result["error"] = "No source files found in project directory."
            return result

        # parallel=False: this function already runs in a ProcessPoolExecutor
        # subprocess; grandchild spawn under Windows deadlocks during import.
        _ast_total = len(files_to_parse)
        progress.emit("ast", current=0, total=_ast_total, message="parsing")

        _ast_msg = ["parsing"]

        def _ast_on_start(uncached: int, total: int) -> None:
            cached = total - uncached
            if uncached == 0:
                _ast_msg[0] = "all cached"
            elif cached == 0:
                _ast_msg[0] = "parsing"
            else:
                _ast_msg[0] = f"{uncached} to parse, {cached} cached"
            # Never emit completion here - post-processing still runs after extract().
            # Clamp to total-1 so the bar stays visible until the final emit below.
            display = min(cached, max(0, total - 1)) if total > 0 else cached
            progress.emit("ast", current=display, total=total, message=_ast_msg[0])

        def _ast_on_progress(done: int, total: int) -> None:
            # Clamp to total-1: post-processing hasn't run yet when on_progress fires.
            display = min(done, max(0, total - 1)) if total > 0 else done
            progress.emit("ast", current=display, total=total, message=_ast_msg[0])

        extraction = extract(
            files_to_parse, cache_root=icx_cache, parallel=False,
            on_progress=_ast_on_progress, on_start=_ast_on_start,
        )
        progress.emit("ast", current=_ast_total, total=_ast_total,
                      message=f"{len(extraction.get('nodes', []))} nodes")

        lsp_edge_count = 0

        _proj_posix = project_path.as_posix()

        def _abs_edges(edges: list[dict]) -> list[dict]:
            """Normalize resolver edges: relative source_file -> absolute."""
            result = []
            for e in edges:
                sf = e.get("source_file", "")
                if sf and not (sf.startswith("/") or (len(sf) > 1 and sf[1] == ":")):
                    e = {**e, "source_file": f"{_proj_posix}/{sf}"}
                result.append(e)
            return result

        # CO_CHANGED edges from git history (runs before LSP pass)
        try:
            from icx_engine.graph.parser.resolvers.cochange_resolver import resolve_cochange
            _cochange_edges = _abs_edges(resolve_cochange(files, project_path, extraction))
            if _cochange_edges:
                extraction = {
                    **extraction,
                    "edges": list(extraction.get("edges", [])) + _cochange_edges,
                }
            progress.emit(
                "cochange",
                current=len(_cochange_edges) // 2,
                total=len(_cochange_edges) // 2,
                message=f"{len(_cochange_edges) // 2} co-changed file pairs",
            )
        except Exception as _cochange_exc:
            _log.debug("cochange_resolver skipped (%s)", type(_cochange_exc).__name__)

        _exts = {f.suffix.lower() for f in files}
        _has_py = bool(_exts & {'.py'})
        _has_java = bool(_exts & {'.java'})
        _has_kotlin = bool(_exts & {'.kt', '.kts'})
        _has_jsts = bool(_exts & {'.js', '.jsx', '.ts', '.tsx', '.mjs', '.cjs', '.vue', '.svelte'})
        _has_go = bool(_exts & {'.go'})
        _has_rust = bool(_exts & {'.rs'})
        _has_csharp = bool(_exts & {'.cs'})
        _has_php = bool(_exts & {'.php'})
        _has_cpp = bool(_exts & {'.cpp', '.cc', '.cxx', '.h', '.hpp', '.hxx'})

        _LSP_STEPS = max(1, (
            (1 if _has_py else 0)        # python jedi
            + (1 if _has_py else 0)      # pyright LSP
            + (2 if _has_java else 0)    # lombok synth + java symbols
            + (1 if _has_kotlin else 0)  # kotlin symbols
            + (2 if _has_jsts else 0)    # jsts imports + typescript LSP
            + (1 if _has_go else 0)      # gopls LSP
            + (1 if _has_rust else 0)    # rust-analyzer LSP
            + (1 if _has_csharp else 0)  # OmniSharp LSP
            + (1 if _has_php else 0)     # intelephense LSP
            + (1 if _has_cpp else 0)     # clangd LSP
            + (1 if _has_kotlin else 0)  # kotlin-language-server LSP
            + 1                           # final edges summary
        ))
        _lsp_step = 0

        def _lsp_pre(label: str) -> None:
            progress.emit("lsp", current=_lsp_step, total=_LSP_STEPS, message=label)

        def _lsp_tick(label: str) -> None:
            nonlocal _lsp_step
            _lsp_step += 1
            progress.emit("lsp", current=_lsp_step, total=_LSP_STEPS, message=label)

        progress.emit("lsp", current=0, total=_LSP_STEPS, message="starting")

        if _has_py:
            _lsp_pre("python jedi")
            try:
                from icx_engine.graph.parser.resolvers.python_jedi import (
                    extract_python_edges,
                )
                jedi_edges = _abs_edges(extract_python_edges(files, project_path, extraction))
                if jedi_edges:
                    extraction = {
                        **extraction,
                        "edges": list(extraction.get("edges", [])) + jedi_edges,
                    }
                lsp_edge_count += len(jedi_edges)
            except Exception as jedi_exc:
                _log.debug("python_jedi resolver failed (%s)", type(jedi_exc).__name__)
            _lsp_tick("python jedi")

        if _has_java:
            # Lombok synth must run BEFORE java_symbols so the synthetic
            # getter/setter/toString nodes are in the node index when the
            # symbol resolver looks them up.
            _lsp_pre("lombok synth")
            try:
                from icx_engine.graph.parser.resolvers.lombok import apply_lombok_synth
                apply_lombok_synth(files, project_path, extraction)
            except Exception as lombok_exc:
                _log.debug("lombok resolver failed (%s)", type(lombok_exc).__name__)
            _lsp_tick("lombok synth")

            _lsp_pre("java symbols")
            try:
                from icx_engine.graph.parser.resolvers.java_symbols import (
                    extract_java_edges,
                )
                java_edges = _abs_edges(extract_java_edges(files, project_path, extraction))
                if java_edges:
                    extraction = {
                        **extraction,
                        "edges": list(extraction.get("edges", [])) + java_edges,
                    }
                lsp_edge_count += len(java_edges)
            except Exception as java_exc:
                _log.debug("java_symbols resolver failed (%s)", type(java_exc).__name__)
            _lsp_tick("java symbols")
            try:
                from icx_engine.graph.parser.resolvers.java_symbols import (
                    upgrade_inferred_edges,
                )
                upgrade_inferred_edges(extraction, project_path, files)
            except Exception as _uie_exc:
                _log.debug("upgrade_inferred_edges failed (%s)", type(_uie_exc).__name__)

        if _has_kotlin:
            _lsp_pre("kotlin symbols")
            try:
                from icx_engine.graph.parser.resolvers.kotlin_symbols import (
                    extract_kotlin_edges,
                )
                kotlin_edges = _abs_edges(extract_kotlin_edges(files, project_path, extraction))
                if kotlin_edges:
                    extraction = {
                        **extraction,
                        "edges": list(extraction.get("edges", [])) + kotlin_edges,
                    }
                lsp_edge_count += len(kotlin_edges)
            except Exception as kt_exc:
                _log.debug("kotlin_symbols resolver failed (%s)", type(kt_exc).__name__)
            _lsp_tick("kotlin symbols")

        if _has_kotlin:
            _lsp_pre("kotlin-language-server LSP")
            try:
                from icx_engine.graph.parser.resolvers.kotlin_lsp import extract_kotlin_lsp_edges
                kotlin_lsp_edges = _abs_edges(extract_kotlin_lsp_edges(files, project_path, extraction))
                if kotlin_lsp_edges:
                    extraction = {
                        **extraction,
                        "edges": list(extraction.get("edges", [])) + kotlin_lsp_edges,
                    }
                lsp_edge_count += len(kotlin_lsp_edges)
            except Exception as kotlin_lsp_exc:
                _log.debug("kotlin_lsp resolver failed (%s)", type(kotlin_lsp_exc).__name__)
            _lsp_tick("kotlin-language-server LSP")

        if _has_jsts:
            _lsp_pre("js/ts imports")
            try:
                from icx_engine.graph.parser.resolvers.jsts_imports import (
                    extract_jsts_imports,
                )
                jsts_edges = _abs_edges(extract_jsts_imports(files, project_path, extraction))
                if jsts_edges:
                    extraction = {
                        **extraction,
                        "edges": list(extraction.get("edges", [])) + jsts_edges,
                    }
                lsp_edge_count += len(jsts_edges)
            except Exception as jsts_exc:
                _log.debug("jsts_imports resolver failed (%s)", type(jsts_exc).__name__)
            _lsp_tick("js/ts imports")

            _lsp_pre("typescript LSP")
            try:
                from icx_engine.graph.parser.resolvers.ts_lsp import extract_ts_lsp_edges
                ts_lsp_edges = _abs_edges(extract_ts_lsp_edges(files, project_path, extraction))
                if ts_lsp_edges:
                    extraction = {
                        **extraction,
                        "edges": list(extraction.get("edges", [])) + ts_lsp_edges,
                    }
                lsp_edge_count += len(ts_lsp_edges)
            except Exception as ts_lsp_exc:
                _log.debug("ts_lsp resolver failed (%s)", type(ts_lsp_exc).__name__)
            _lsp_tick("typescript LSP")

        if _has_py:
            _lsp_pre("pyright LSP")
            try:
                from icx_engine.graph.parser.resolvers.pyright_lsp import extract_pyright_edges
                pyright_edges = _abs_edges(extract_pyright_edges(files, project_path, extraction))
                if pyright_edges:
                    extraction = {
                        **extraction,
                        "edges": list(extraction.get("edges", [])) + pyright_edges,
                    }
                lsp_edge_count += len(pyright_edges)
            except Exception as pyright_exc:
                _log.debug("pyright_lsp resolver failed (%s)", type(pyright_exc).__name__)
            _lsp_tick("pyright LSP")

        if _has_go:
            _lsp_pre("gopls LSP")
            try:
                from icx_engine.graph.parser.resolvers.go_lsp import extract_go_lsp_edges
                go_lsp_edges = _abs_edges(extract_go_lsp_edges(files, project_path, extraction))
                if go_lsp_edges:
                    extraction = {
                        **extraction,
                        "edges": list(extraction.get("edges", [])) + go_lsp_edges,
                    }
                lsp_edge_count += len(go_lsp_edges)
            except Exception as go_lsp_exc:
                _log.debug("go_lsp resolver failed (%s)", type(go_lsp_exc).__name__)
            _lsp_tick("gopls LSP")

        if _has_rust:
            _lsp_pre("rust-analyzer LSP")
            try:
                from icx_engine.graph.parser.resolvers.rust_lsp import extract_rust_lsp_edges
                rust_lsp_edges = _abs_edges(extract_rust_lsp_edges(files, project_path, extraction))
                if rust_lsp_edges:
                    extraction = {
                        **extraction,
                        "edges": list(extraction.get("edges", [])) + rust_lsp_edges,
                    }
                lsp_edge_count += len(rust_lsp_edges)
            except Exception as rust_lsp_exc:
                _log.debug("rust_lsp resolver failed (%s)", type(rust_lsp_exc).__name__)
            _lsp_tick("rust-analyzer LSP")

        if _has_csharp:
            _lsp_pre("OmniSharp LSP")
            try:
                from icx_engine.graph.parser.resolvers.csharp_lsp import extract_csharp_lsp_edges
                csharp_lsp_edges = _abs_edges(extract_csharp_lsp_edges(files, project_path, extraction))
                if csharp_lsp_edges:
                    extraction = {
                        **extraction,
                        "edges": list(extraction.get("edges", [])) + csharp_lsp_edges,
                    }
                lsp_edge_count += len(csharp_lsp_edges)
            except Exception as csharp_lsp_exc:
                _log.debug("csharp_lsp resolver failed (%s)", type(csharp_lsp_exc).__name__)
            _lsp_tick("OmniSharp LSP")

        if _has_php:
            _lsp_pre("intelephense LSP")
            try:
                from icx_engine.graph.parser.resolvers.php_lsp import extract_php_lsp_edges
                php_lsp_edges = _abs_edges(extract_php_lsp_edges(files, project_path, extraction))
                if php_lsp_edges:
                    extraction = {
                        **extraction,
                        "edges": list(extraction.get("edges", [])) + php_lsp_edges,
                    }
                lsp_edge_count += len(php_lsp_edges)
            except Exception as php_lsp_exc:
                _log.debug("php_lsp resolver failed (%s)", type(php_lsp_exc).__name__)
            _lsp_tick("intelephense LSP")

        if _has_cpp:
            _lsp_pre("clangd LSP")
            try:
                from icx_engine.graph.parser.resolvers.cpp_lsp import extract_cpp_lsp_edges
                cpp_lsp_edges = _abs_edges(extract_cpp_lsp_edges(files, project_path, extraction))
                if cpp_lsp_edges:
                    extraction = {
                        **extraction,
                        "edges": list(extraction.get("edges", [])) + cpp_lsp_edges,
                    }
                lsp_edge_count += len(cpp_lsp_edges)
            except Exception as cpp_lsp_exc:
                _log.debug("cpp_lsp resolver failed (%s)", type(cpp_lsp_exc).__name__)
            _lsp_tick("clangd LSP")

        _lsp_tick(f"{lsp_edge_count} edges")

        _RESOLVERS = (
            ("java_inheritance", "icx_engine.graph.parser.resolvers.java_inheritance", "extract_java_inheritance_edges"),
            ("fastapi", "icx_engine.graph.parser.resolvers.fastapi", "extract_fastapi_edges"),
            ("spring", "icx_engine.graph.parser.resolvers.spring", "extract_spring_edges"),
            ("java_interface_impl", "icx_engine.graph.parser.resolvers.java_interface_impl", "extract_java_interface_impl_edges"),
            ("jpa", "icx_engine.graph.parser.resolvers.jpa", "extract_jpa_edges"),
            ("kotlin_spring", "icx_engine.graph.parser.resolvers.kotlin_spring", "extract_kotlin_spring_edges"),
            ("react", "icx_engine.graph.parser.resolvers.react", "extract_react_edges"),
            ("nextjs", "icx_engine.graph.parser.resolvers.nextjs", "extract_nextjs_edges"),
            ("jsts_frameworks", "icx_engine.graph.parser.resolvers.jsts_frameworks", "extract_jsts_framework_edges"),
            ("jaxrs", "icx_engine.graph.parser.resolvers.jaxrs", "extract_jaxrs_edges"),
            ("build_deps", "icx_engine.graph.parser.resolvers.build_deps", "extract_build_dep_edges"),
            ("java_clients", "icx_engine.graph.parser.resolvers.java_clients", "extract_java_client_edges"),
            ("django", "icx_engine.graph.parser.resolvers.django", "extract_django_edges"),
            ("flask", "icx_engine.graph.parser.resolvers.flask", "extract_flask_edges"),
            ("vue", "icx_engine.graph.parser.resolvers.vue", "extract_vue_edges"),
            ("remix", "icx_engine.graph.parser.resolvers.remix", "extract_remix_edges"),
            ("svelte", "icx_engine.graph.parser.resolvers.svelte", "extract_svelte_edges"),
            ("sqlalchemy", "icx_engine.graph.parser.resolvers.sqlalchemy", "extract_sqlalchemy_edges"),
            ("celery", "icx_engine.graph.parser.resolvers.celery", "extract_celery_edges"),
            ("pytest_fixtures", "icx_engine.graph.parser.resolvers.pytest_fixtures", "extract_pytest_edges"),
            ("python_type_checking", "icx_engine.graph.parser.resolvers.python_type_checking", "extract_python_type_checking_edges"),
            ("redux", "icx_engine.graph.parser.resolvers.redux", "extract_redux_edges"),
            ("graphql_resolvers", "icx_engine.graph.parser.resolvers.graphql_resolvers", "extract_graphql_edges"),
            ("vue_options", "icx_engine.graph.parser.resolvers.vue_options", "extract_vue_options_edges"),
            ("spring_xml", "icx_engine.graph.parser.resolvers.spring_xml", "extract_spring_xml_edges"),
            ("jsp", "icx_engine.graph.parser.resolvers.jsp_resolver", "resolve_jsp"),
            ("go", "icx_engine.graph.parser.resolvers.go_resolver", "resolve_go"),
            ("csharp", "icx_engine.graph.parser.resolvers.csharp_resolver", "resolve_csharp"),
            ("php", "icx_engine.graph.parser.resolvers.php_resolver", "resolve_php"),
            ("rust", "icx_engine.graph.parser.resolvers.rust_resolver", "resolve_rust"),
            ("cpp", "icx_engine.graph.parser.resolvers.cpp_resolver", "resolve_cpp"),
            ("swift", "icx_engine.graph.parser.resolvers.swift_resolver", "resolve_swift"),
            ("elixir", "icx_engine.graph.parser.resolvers.elixir_resolver", "resolve_elixir"),
            ("scala", "icx_engine.graph.parser.resolvers.scala_resolver", "resolve_scala"),
            ("angular", "icx_engine.graph.parser.resolvers.angular_resolver", "resolve_angular"),
            ("rails", "icx_engine.graph.parser.resolvers.rails_resolver", "resolve_rails"),
            ("proto", "icx_engine.graph.parser.resolvers.proto_resolver", "resolve_proto"),
            ("terraform", "icx_engine.graph.parser.resolvers.terraform_resolver", "resolve_terraform"),
        )

        framework_edge_count = 0
        progress.emit("framework", current=0, total=len(_RESOLVERS), message="starting")

        for _i, (resolver_name, mod_path, fn_name) in enumerate(_RESOLVERS):
            progress.emit("framework", current=_i, total=len(_RESOLVERS), message=resolver_name)
            try:
                module = __import__(mod_path, fromlist=[fn_name])
                fn = getattr(module, fn_name)
                new_edges = fn(files, project_path, extraction)
                if new_edges:
                    new_edges = _abs_edges(new_edges)
                    extraction = {
                        **extraction,
                        "edges": list(extraction.get("edges", [])) + new_edges,
                    }
                framework_edge_count += len(new_edges)
            except Exception as exc:
                _log.debug("%s resolver failed (%s)", resolver_name, type(exc).__name__)

        progress.emit("framework", current=len(_RESOLVERS), total=len(_RESOLVERS),
                      message=f"{framework_edge_count} edges")

        try:
            from icx_engine.graph.parser.resolvers.universal_ast import (
                extract_universal_ast_edges,
            )
            universal_edges = extract_universal_ast_edges(files, project_path, extraction)
            if universal_edges:
                universal_edges = _abs_edges(universal_edges)
                extraction = {
                    **extraction,
                    "edges": list(extraction.get("edges", [])) + universal_edges,
                }
            _log.debug("universal_ast resolver: %d edges", len(universal_edges))
        except Exception as universal_exc:
            _log.debug("universal_ast resolver skipped (%s)", type(universal_exc).__name__)

        # Event-driven edge detection (Kafka, RabbitMQ, Redis, SQS, SNS, NATS, OpenAPI, AsyncAPI)
        try:
            from icx_engine.graph.parser.resolvers.event_resolver import resolve_events
            _event_edges = resolve_events(files, project_path, extraction)
            if _event_edges:
                _event_edges = _abs_edges(_event_edges)
                extraction = {
                    **extraction,
                    "edges": list(extraction.get("edges", [])) + _event_edges,
                }
            _log.debug("event_resolver: %d edges", len(_event_edges))
        except Exception as _event_exc:
            _log.debug("event_resolver skipped (%s)", type(_event_exc).__name__)

        try:
            from icx_engine.graph.parser.resolvers.cross_service_rest import (
                run_cross_service_linking,
            )
            from icx_engine.graph import storage as _xstorage
            _out_dir = _xstorage._graphs_root() / _xstorage.derive_project_id(project_path)
            run_cross_service_linking(files, project_path, extraction, _out_dir)
        except Exception as _csl_exc:
            _log.debug("cross_service_rest linker failed (%s)", type(_csl_exc).__name__)

        # LLM enrichment: per-chunk LLM call. Chunk IDs are intentionally
        # discarded; Louvain rederives communities globally so cluster IDs
        # are consistent across the whole graph.
        resolved_backend = llm_backend
        resolved_key = llm_api_key
        if not resolved_backend:
            env_cfg = _detect_llm_backend()
            if env_cfg:
                resolved_backend, resolved_key = env_cfg

        if resolved_backend:
            if resolved_backend == "ollama" and llm_base_url:
                os.environ["OLLAMA_BASE_URL"] = llm_base_url
            llm_chunks = max(1, len(files) // 20)
            progress.emit("llm", current=0, total=llm_chunks,
                          message=f"{resolved_backend}")
            try:
                from icx_engine.graph.parser.llm import (
                    extract_corpus_parallel as _llm_extract,
                    extract_corpus_two_pass as _llm_two_pass,
                    build_symbol_table_from_ast,
                )

                symbol_table = build_symbol_table_from_ast(extraction, project_path)

                # Two-pass consensus is opt-in (doubles LLM cost). Set
                # ICX_LLM_TWO_PASS=1 to enable; default is single pass.
                two_pass = os.environ.get("ICX_LLM_TWO_PASS", "").strip() == "1"
                keep_single = os.environ.get(
                    "ICX_LLM_KEEP_SINGLE_PASS", "1",
                ).strip() == "1"

                if two_pass:
                    llm_result = _llm_two_pass(
                        files,
                        backend=resolved_backend,
                        api_key=resolved_key,
                        root=project_path,
                        token_budget=20_000,
                        max_retry_depth=5,
                        max_concurrency=1,
                        symbol_table=symbol_table,
                        keep_single_pass=keep_single,
                    )
                else:
                    llm_result = _llm_extract(
                        files,
                        backend=resolved_backend,
                        api_key=resolved_key,
                        root=project_path,
                        token_budget=20_000,
                        max_retry_depth=5,
                        max_concurrency=1,
                        symbol_table=symbol_table,
                    )

                llm_edges = llm_result.get("edges", [])
                if llm_edges:
                    extraction = {
                        **extraction,
                        "edges": list(extraction.get("edges", [])) + llm_edges,
                    }

                # Embedding cross-check: reject LLM edges whose source and
                # target snippets are semantically unrelated. Opt-out via
                # ICX_LLM_EMBEDDING_FILTER=0. Drops rather than tags when
                # ICX_LLM_EMBEDDING_DROP=1.
                if os.environ.get("ICX_LLM_EMBEDDING_FILTER", "1").strip() != "0":
                    try:
                        from icx_engine.graph.parser.llm_embedding_filter import (
                            filter_llm_edges_by_embedding,
                        )
                        drop = os.environ.get(
                            "ICX_LLM_EMBEDDING_DROP", "0",
                        ).strip() == "1"
                        filter_llm_edges_by_embedding(
                            extraction, project_path, drop_rejected=drop,
                        )
                    except Exception as embed_exc:
                        _log.debug(
                            "embedding filter failed (%s)",
                            type(embed_exc).__name__,
                        )

                result["extraction_mode"] = "semantic"
                progress.emit("llm", current=llm_chunks, total=llm_chunks,
                              message=f"{len(llm_edges)} edges")

            except Exception as llm_exc:
                _log.debug("LLM edge enrichment failed (%s)", type(llm_exc).__name__)
                progress.emit("llm", current=llm_chunks, total=llm_chunks,
                              message="failed; AST result still usable")
        else:
            progress.emit("llm", current=0, total=0, message="no LLM configured")

        # Multi-source edge fusion: fuse edges where multiple resolvers agree
        from icx_engine.graph.parser.dedup import fuse_and_dedup as _fuse_dedup
        extraction = {**extraction, "edges": _fuse_dedup(extraction.get("edges", []))}

        progress.emit("louvain", current=0, total=1, message="community detect")
        # Directed graph preserves edge direction so opposite-direction
        # cross-file edges (e.g. JPA ManyToOne + inverse OneToMany on the
        # same entity pair) do not collapse into a single edge.
        G = build_from_json(extraction, directed=True)
        # exclude_hubs_percentile: barrel index.js / common utility files imported
        # by hundreds of components have very high degree and cause louvain to
        # thrash for hours. Excluding the top 1% hubs and reattaching them by
        # majority-vote after partitioning gives faster + cleaner communities.
        communities = cluster(G, exclude_hubs_percentile=99)
        progress.emit("louvain", current=1, total=1,
                      message=f"{len(communities) if isinstance(communities, dict) else 0} communities")

        result["node_count"] = G.number_of_nodes()
        result["edge_count"] = G.number_of_edges()
        result["community_count"] = len(communities) if isinstance(communities, dict) else 0

        progress.emit("export", current=0, total=1, message="write graph.json")
        to_json(G, communities, output_path=graph_tmp_path_str, skip_safety_check=True)
        progress.emit("export", current=1, total=1,
                      message=f"{result['node_count']} nodes, {result['edge_count']} edges")

        # -- Incremental merge (if applicable) --
        if _incremental:
            try:
                import json as _json
                existing_graph = _json.loads(graph_json_path.read_text(encoding="utf-8"))
                # Load what to_json just wrote to the tmp path
                fresh_graph = _json.loads(_Path(graph_tmp_path_str).read_text(encoding="utf-8"))
                merged = _merge_incremental(
                    existing_graph, fresh_graph, _changed_rel, _deleted_rel,
                    root_posix=project_path.as_posix(),
                )
                _Path(graph_tmp_path_str).write_text(
                    _json.dumps(merged, separators=(",", ":")), encoding="utf-8"
                )
                # The counts emitted above reflect only the freshly-parsed
                # changed files (pre-merge). Recompute from the merged graph so
                # the "Graph ready" summary shows the true total, not the small
                # incremental delta.
                _merged_nodes = merged.get("nodes", [])
                _merged_links = merged.get("links", merged.get("edges", []))
                result["node_count"] = len(_merged_nodes)
                result["edge_count"] = len(_merged_links)
                result["community_count"] = len({
                    n.get("community") for n in _merged_nodes
                    if n.get("community") is not None
                })
                progress.emit(
                    "export", current=1, total=1,
                    message=f"{result['node_count']} nodes, {result['edge_count']} edges (merged)",
                )
            except Exception as _merge_exc:
                _log.debug("incremental merge failed (%s), keeping full build", type(_merge_exc).__name__)

        # Save hashes after successful build
        _final_hashes = _new_hashes if _incremental else {}
        if not _final_hashes:
            # Full build: compute hashes now
            _final_hashes_list = compute_changed_files(str(project_path), _rel_files, {})
            _final_hashes = _final_hashes_list[2]
        save_hashes(hash_cache_path, _final_hashes)
        result["incremental"] = _incremental
        # -- End incremental merge -----------------------------------------------------------

    except ImportError as exc:
        result["error"] = (
            f"icx graph parser failed to import: {exc}. "
            "This indicates a broken icx-engine install - try `pip install --force-reinstall icx-engine`."
        )
    except BaseException as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        progress.close()

    return result


# ---------------------------------------------------------------------------
# ETA helper (used by manager before a build starts)
# ---------------------------------------------------------------------------

def estimate_build_eta(file_count: int, semantic: bool = False) -> int:
    """Estimated build time in seconds.

    Hybrid mode runs AST first then LLM sequentially:
      AST:  ~0.05s/file parallelised across cpu_count + 15s subprocess startup
      LLM:  ~20 files/chunk at 20k token budget, max_concurrency=1, ~15s/chunk
    """
    import os
    cpu = max(1, os.cpu_count() or 4)
    ast_time = max(15, int(file_count * 0.05 / cpu + 15))
    if not semantic:
        return ast_time
    chunks = max(1, file_count // 20)
    llm_time = chunks * 15  # sequential (max_concurrency=1)
    return ast_time + llm_time
