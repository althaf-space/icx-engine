"""Remix framework resolver.

Detects:
  * File-based routing: app/routes/*.tsx -> route edge to exported loader/action/default
  * loader/action function exports -> routes edge
  * JSX component renders in route default exports -> renders edge (delegated to react resolver)
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

_REMIX_EXTS: tuple[str, ...] = (".tsx", ".ts", ".jsx", ".js")

_ROUTE_EXPORTS = re.compile(
    r"export\s+(?:async\s+)?function\s+(loader|action|default)\s*\(",
)
_NAMED_ROUTE_EXPORTS = re.compile(
    r"export\s+(?:const|let)\s+(loader|action)\s*(?::\s*[^=]+)?\s*=",
)


def extract_remix_edges(
    files: Iterable[Path],
    project_root: Path,
    ast_extraction: dict,
) -> list[dict]:
    project_root = project_root.resolve()
    all_files = [Path(f).resolve() for f in files if str(f).lower().endswith(_REMIX_EXTS)]
    if not all_files:
        return []

    route_files = [f for f in all_files if _is_remix_route(f, project_root)]
    if not route_files:
        return []

    node_index = _build_node_index(ast_extraction.get("nodes", []), project_root)
    if not node_index["by_file"]:
        return []

    seen: set[tuple[str, str, str]] = set()
    edges: list[dict] = []

    for rf in route_files:
        try:
            rel = rf.relative_to(project_root).as_posix()
        except ValueError:
            continue
        try:
            source = rf.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        file_node_id = node_index["by_file"].get(rel)
        if not file_node_id:
            continue
        local_symbols = {
            sym: nid for (path, sym), nid in node_index["by_symbol"].items()
            if path == rel
        }

        for match in _ROUTE_EXPORTS.finditer(source):
            fn_name = match.group(1)
            if fn_name == "default":
                continue
            fn_node_id = local_symbols.get(fn_name.lower())
            if not fn_node_id:
                continue
            key = (file_node_id, fn_node_id, "routes")
            if key in seen:
                continue
            seen.add(key)
            line_no = source.count("\n", 0, match.start()) + 1
            edge = {
                "relation": "routes",
                "source": file_node_id,
                "target": fn_node_id,
                "source_file": rel,
                "source_location": f"L{line_no}",
                "weight": 1.0,
            }
            annotate_edge(edge, FRAMEWORK_RESOLVED, "remix_resolver")
            edges.append(edge)

        for match in _NAMED_ROUTE_EXPORTS.finditer(source):
            fn_name = match.group(1)
            fn_node_id = local_symbols.get(fn_name.lower())
            if not fn_node_id:
                continue
            key = (file_node_id, fn_node_id, "routes")
            if key in seen:
                continue
            seen.add(key)
            line_no = source.count("\n", 0, match.start()) + 1
            edge = {
                "relation": "routes",
                "source": file_node_id,
                "target": fn_node_id,
                "source_file": rel,
                "source_location": f"L{line_no}",
                "weight": 1.0,
            }
            annotate_edge(edge, FRAMEWORK_RESOLVED, "remix_resolver")
            edges.append(edge)

    return edges


def _is_remix_route(f: Path, project_root: Path) -> bool:
    try:
        rel = f.relative_to(project_root).as_posix()
    except ValueError:
        return False
    parts = rel.split("/")
    if "routes" in parts:
        return True
    return False


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
            rel = src_file[len(project_str) + 1:]
        elif src_file.startswith(project_str):
            rel = src_file[len(project_str):].lstrip("/")
        else:
            continue
        if label == Path(rel).name or any(
            label.lower().endswith(ext) for ext in _REMIX_EXTS
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
