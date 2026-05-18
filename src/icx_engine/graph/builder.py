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

_GRAPHIFY_EXTENSIONS = frozenset({
    ".py", ".js", ".jsx", ".mjs", ".ts", ".tsx", ".go", ".rs", ".java",
    ".groovy", ".gradle", ".c", ".h", ".cpp", ".cc", ".cxx", ".hpp",
    ".rb", ".cs", ".kt", ".kts", ".scala", ".php", ".swift", ".lua",
    ".zig", ".ps1", ".ex", ".exs", ".m", ".mm", ".jl", ".vue", ".svelte",
    ".dart", ".sql", ".md", ".mdx",
})

_SKIP_DIRS = frozenset({
    "node_modules", "dist", "build", ".next", ".nuxt", "target", "vendor",
    "__pycache__", ".gradle", ".mvn", "out", ".output", "coverage",
    ".cache", "tmp", "temp", ".turbo", ".parcel-cache", "venv", ".venv",
    "env", ".env", "site-packages", ".tox", "buck-out", "bazel-out",
    ".dart_tool", "Pods", ".build", "DerivedData",
})

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
    Collect source files for graphify.
    1. git ls-files (respects .gitignore - handles node_modules, dist, etc.)
    2. Fallback: rglob filtered by _SKIP_DIRS
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
                if p.suffix in _GRAPHIFY_EXTENSIONS and p.is_file():
                    files.append(p)
            if files:
                return _filter_vendor_files(files)
    except Exception:
        pass

    # Fallback: rglob with skip-dir filter
    files = []
    for ext in _GRAPHIFY_EXTENSIONS:
        for p in project_path.rglob(f"*{ext}"):
            rel = p.relative_to(project_path)
            if _is_inside_archive_dir(rel):
                continue
            if any(part in _SKIP_DIRS or part.startswith(".") for part in rel.parts[:-1]):
                continue
            if p.is_file():
                files.append(p)
    return _filter_vendor_files(sorted(set(files)))


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

    try:
        import graphify.cache as _gcache
        from graphify.extract import extract
        from graphify.build import build_from_json
        from graphify.cluster import cluster
        from graphify.export import to_json

        icx_cache = _Path(icx_cache_path_str)
        icx_cache.mkdir(parents=True, exist_ok=True)

        # Move cwd away from project dir so graphify cannot create graphify-out/
        # or any other relative-path artifacts inside the user's project.
        os.chdir(str(icx_cache))

        # Redirect ALL cache writes to ~/.icx/graphs/<id>/cache/
        # Safe here because this is an isolated subprocess.
        def _redirected_cache_dir(root=_Path("."), kind="ast"):
            d = icx_cache / kind
            d.mkdir(parents=True, exist_ok=True)
            return d
        _gcache.cache_dir = _redirected_cache_dir

        project_path = _Path(project_path_str)
        files = _collect_source_files(project_path)
        result["file_count"] = len(files)

        if not files:
            result["error"] = "No source files found in project directory."
            return result

        # Cap at 4: we're already inside an isolated subprocess.
        # Spawning more sub-subprocesses on Windows causes exponential
        # Python startup overhead and can hang for several minutes.
        cpu = min(4, max(1, os.cpu_count() or 4))

        # ------------------------------------------------------------------
        # Step 1: AST extraction - always runs, covers every file, no API cost.
        # Produces nodes + intra-file call/import edges via tree-sitter.
        # This is the foundation: zero misses regardless of project size.
        # ------------------------------------------------------------------
        extraction = extract(files, parallel=True, max_workers=cpu)

        # ------------------------------------------------------------------
        # Step 2: LLM edge enrichment - runs only when a model is configured.
        # Sends file batches to the LLM to extract cross-file semantic edges.
        # Only edges are taken from the LLM output; nodes and LLM-assigned
        # community IDs are intentionally discarded.
        #
        # Why discard LLM communities?
        # Each LLM chunk independently assigns community IDs 0,1,2...
        # Community 0 in chunk A has no relation to community 0 in chunk B.
        # When merged, they collide into one oversized cluster. Louvain in
        # Step 3 derives communities from the full merged graph instead,
        # giving globally consistent assignments at any project scale.
        # ------------------------------------------------------------------
        resolved_backend = llm_backend
        resolved_key = llm_api_key
        if not resolved_backend:
            env_cfg = _detect_llm_backend()
            if env_cfg:
                resolved_backend, resolved_key = env_cfg

        if resolved_backend:
            if resolved_backend == "ollama" and llm_base_url:
                os.environ["OLLAMA_BASE_URL"] = llm_base_url
            try:
                from graphify.llm import extract_corpus_parallel as _llm_extract

                llm_result = _llm_extract(
                    files,
                    backend=resolved_backend,
                    api_key=resolved_key,
                    root=project_path,
                    # 20k token chunks: ~15-20 files each, fits any model's output window.
                    # max_concurrency=1: safe for all free-tier providers (Gemini=15 RPM,
                    # etc.). Graphify's adaptive retry handles 429s with backoff.
                    token_budget=20_000,
                    max_retry_depth=5,
                    max_concurrency=1,
                )

                llm_edges = llm_result.get("edges", [])
                if llm_edges:
                    # Merge LLM edges into the AST extraction.
                    # build_from_json normalizes node IDs and drops truly dangling
                    # edges (where LLM naming differs from AST naming), so no
                    # explicit filtering needed here.
                    extraction = {
                        **extraction,
                        "edges": list(extraction.get("edges", [])) + llm_edges,
                    }

                result["extraction_mode"] = "semantic"

            except Exception as llm_exc:
                # LLM step failed - AST result is still complete and usable.
                _log.debug("LLM edge enrichment failed (%s); using AST only", type(llm_exc).__name__)

        # ------------------------------------------------------------------
        # Step 3: Build NetworkX graph + Louvain community detection.
        # cluster() runs on the full merged graph (AST nodes + all edges).
        # With cross-file edges from LLM: rich communities reflecting real
        # module structure. Without edges: directory-based grouping fallback
        # in querier.py gives still-useful navigation clusters.
        # ------------------------------------------------------------------
        G = build_from_json(extraction)
        communities = cluster(G)

        result["node_count"] = G.number_of_nodes()
        result["edge_count"] = G.number_of_edges()
        result["community_count"] = len(communities) if isinstance(communities, dict) else 0

        to_json(G, communities, output_path=graph_tmp_path_str)

    except ImportError as exc:
        result["error"] = (
            f"graphify package not installed: {exc}. "
            "Run: pip install graphifyy"
        )
    except Exception as exc:
        result["error"] = str(exc)

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
