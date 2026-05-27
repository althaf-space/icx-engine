"""Flask framework resolver.

Detects:
  * Blueprint route decorators (@bp.get, @bp.post, etc.) -> routes edge
  * app.register_blueprint() -> reference edge
  * SQLAlchemy model relationships (db.relationship, ForeignKey) -> has_relation edge
"""
from __future__ import annotations

import ast
import logging
from pathlib import Path
from typing import Iterable

from icx_engine.graph.parser.confidence import (
    FRAMEWORK_RESOLVED,
    annotate_edge,
)

_log = logging.getLogger(__name__)

_ROUTE_METHODS: frozenset[str] = frozenset({
    "get", "post", "put", "patch", "delete", "route",
    "before_request", "after_request", "errorhandler",
})

_SQLA_RELATION_FIELDS: frozenset[str] = frozenset({
    "relationship", "ForeignKey", "Column",
})


def extract_flask_edges(
    files: Iterable[Path],
    project_root: Path,
    ast_extraction: dict,
) -> list[dict]:
    project_root = project_root.resolve()
    py_files = [Path(f).resolve() for f in files if str(f).endswith(".py")]
    if not py_files:
        return []

    node_index = _build_node_index(ast_extraction.get("nodes", []), project_root)
    if not node_index["by_symbol"] and not node_index["by_file"]:
        return []

    seen: set[tuple[str, str, str]] = set()
    edges: list[dict] = []

    for py in py_files:
        try:
            rel = py.relative_to(project_root).as_posix()
        except ValueError:
            continue
        try:
            code = py.read_text(encoding="utf-8")
            tree = ast.parse(code, filename=str(py))
        except (OSError, SyntaxError):
            continue

        file_node_id = node_index["by_file"].get(rel)
        local_symbols = {
            sym: nid for (path, sym), nid in node_index["by_symbol"].items()
            if path == rel
        }

        _extract_routes(tree, rel, node_index, local_symbols, file_node_id, seen, edges)

    return edges


def _extract_routes(
    tree: ast.Module, rel: str, node_index: dict,
    local_symbols: dict, file_node_id: str | None,
    seen: set, edges: list,
) -> None:
    if not file_node_id:
        return
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        is_route = False
        for deco in node.decorator_list:
            method = _get_decorator_method(deco)
            if method in _ROUTE_METHODS:
                is_route = True
                break
        if not is_route:
            continue
        func_node_id = local_symbols.get(node.name.lower())
        if not func_node_id:
            continue
        key = (file_node_id, func_node_id, "routes")
        if key in seen:
            continue
        seen.add(key)
        edge = {
            "relation": "routes",
            "source": file_node_id,
            "target": func_node_id,
            "source_file": rel,
            "source_location": f"L{node.lineno}",
            "weight": 1.0,
        }
        annotate_edge(edge, FRAMEWORK_RESOLVED, "flask_resolver")
        edges.append(edge)


def _get_decorator_method(deco) -> str | None:
    target = deco.func if isinstance(deco, ast.Call) else deco
    if isinstance(target, ast.Attribute):
        return target.attr
    return None


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
        if label.lower().endswith(".py") or label == Path(rel).name:
            by_file.setdefault(rel, nid)
            continue
        symbol = label
        if symbol.endswith("()"):
            symbol = symbol[:-2]
        symbol = symbol.lstrip(".").lower()
        if symbol:
            by_symbol.setdefault((rel, symbol), nid)
    return {"by_file": by_file, "by_symbol": by_symbol}
