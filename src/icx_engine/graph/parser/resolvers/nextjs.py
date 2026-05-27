"""Next.js convention-based route resolver.

Detects pages-router (`pages/...`) and app-router (`app/...`) file
conventions and emits `routes` edges from the file node to the
default-exported component (when identifiable). Also flags
`middleware.ts`, `_app.tsx`, and `_document.tsx` so cluster reports
can highlight framework entrypoints.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Iterable

from icx_engine.graph.parser.confidence import (
    FRAMEWORK_RESOLVED, annotate_edge,
)

_log = logging.getLogger(__name__)

_NEXT_EXTS: tuple[str, ...] = (".tsx", ".ts", ".jsx", ".js")

_DEFAULT_EXPORT_NAMED = re.compile(
    r"export\s+default\s+(?:function|class)\s+([A-Za-z_][A-Za-z0-9_]*)"
)
_DEFAULT_EXPORT_IDENT = re.compile(
    r"export\s+default\s+([A-Za-z_][A-Za-z0-9_]*)\s*;"
)

# Named function exports used by route.ts (GET/POST/...) and middleware.ts.
_NAMED_EXPORT_FUNCTION = re.compile(
    r"export\s+(?:async\s+)?function\s+([A-Za-z_][A-Za-z0-9_]*)\s*\("
)

_ROUTE_FILE_HANDLERS: frozenset[str] = frozenset({
    "GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS",
    "middleware",
})


def extract_nextjs_edges(
    files: Iterable[Path],
    project_root: Path,
    ast_extraction: dict,
) -> list[dict]:
    project_root = project_root.resolve()
    candidate_files = [
        Path(f).resolve()
        for f in files
        if str(f).lower().endswith(_NEXT_EXTS)
    ]
    if not candidate_files:
        return []

    node_index = _build_node_index(ast_extraction.get("nodes", []), project_root)
    if not node_index["by_file"]:
        return []

    seen: set[tuple[str, str, str]] = set()
    edges: list[dict] = []

    for nf in candidate_files:
        try:
            rel = nf.relative_to(project_root).as_posix()
        except ValueError:
            continue
        if not _is_next_route(rel):
            continue
        try:
            source = nf.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        file_node_id = node_index["by_file"].get(rel)
        if not file_node_id:
            continue

        route_str = _route_for_path(rel)

        # Default export: pages router + app/page/layout/error/etc.
        export_name = _find_default_export_name(source)
        if export_name:
            symbol_id = node_index["by_symbol"].get((rel, export_name.lower()))
            target_id = symbol_id or file_node_id
            key = (file_node_id, target_id, "routes")
            if key not in seen:
                seen.add(key)
                edge = {
                    "relation": "routes",
                    "source": file_node_id,
                    "target": target_id,
                    "source_file": rel,
                    "source_location": "L1",
                    "weight": 1.0,
                    "route": route_str,
                }
                annotate_edge(edge, FRAMEWORK_RESOLVED, "nextjs_resolver")
                edges.append(edge)

        # Named function exports: route.ts handlers + middleware.ts.
        for fn_match in _NAMED_EXPORT_FUNCTION.finditer(source):
            fn_name = fn_match.group(1)
            if fn_name not in _ROUTE_FILE_HANDLERS:
                continue
            symbol_id = node_index["by_symbol"].get((rel, fn_name.lower()))
            target_id = symbol_id or file_node_id
            if target_id == file_node_id:
                continue
            key = (file_node_id, target_id, "routes")
            if key in seen:
                continue
            seen.add(key)
            line_no = source.count("\n", 0, fn_match.start()) + 1
            edge = {
                "relation": "routes",
                "source": file_node_id,
                "target": target_id,
                "source_file": rel,
                "source_location": f"L{line_no}",
                "weight": 1.0,
                "route": route_str,
                "method": fn_name,
            }
            annotate_edge(edge, FRAMEWORK_RESOLVED, "nextjs_resolver")
            edges.append(edge)

    return edges


def _is_next_route(rel: str) -> bool:
    parts = rel.split("/")
    last = parts[-1].lower()
    # Project-root middleware.ts is a Next.js convention regardless of dir.
    if last in {"middleware.ts", "middleware.js"}:
        return True
    if "pages" in parts:
        return True
    if "app" in parts:
        if last in {
            "page.tsx", "page.ts", "page.jsx", "page.js",
            "layout.tsx", "layout.ts", "layout.jsx", "layout.js",
            "route.ts", "route.js", "route.tsx", "route.jsx",
            "error.tsx", "loading.tsx", "not-found.tsx",
        }:
            return True
    return False


def _find_default_export_name(source: str) -> str | None:
    match = _DEFAULT_EXPORT_NAMED.search(source)
    if match:
        return match.group(1)
    match = _DEFAULT_EXPORT_IDENT.search(source)
    if match:
        return match.group(1)
    return None


def _route_for_path(rel: str) -> str:
    parts = rel.split("/")
    try:
        anchor = parts.index("pages")
    except ValueError:
        try:
            anchor = parts.index("app")
        except ValueError:
            return rel
    tail = parts[anchor + 1 :]
    if not tail:
        return "/"
    last = tail[-1]
    for ext in _NEXT_EXTS:
        if last.lower().endswith(ext):
            last = last[: -len(ext)]
            break
    if last in {"index", "page", "route", "layout", "middleware"}:
        tail = tail[:-1]
    else:
        tail[-1] = last
    return "/" + "/".join(tail) if tail else "/"


def _build_node_index(nodes: list[dict], project_root: Path) -> dict[str, dict]:
    project_str = str(project_root).replace("\\", "/")
    by_file: dict[str, str] = {}
    by_symbol: dict[tuple[str, str], str] = {}
    for n in nodes:
        nid = n.get("id") or n.get("label")
        if not nid:
            continue
        src_file = (n.get("source_file") or "").replace("\\", "/").strip()
        label = (n.get("label") or "").strip()
        if not src_file:
            continue
        if src_file.startswith(project_str + "/"):
            rel = src_file[len(project_str) + 1 :]
        elif src_file.startswith(project_str):
            rel = src_file[len(project_str) :].lstrip("/")
        else:
            continue
        if label == Path(rel).name or any(
            label.lower().endswith(ext) for ext in _NEXT_EXTS
        ):
            by_file.setdefault(rel, nid)
            continue
        symbol = label
        if symbol.endswith("()"):
            symbol = symbol[:-2]
        symbol = symbol.lstrip(".").lower()
        if symbol:
            by_symbol.setdefault((rel, symbol), nid)
    return {"by_file": by_file, "by_symbol": by_symbol}
