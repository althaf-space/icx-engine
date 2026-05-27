"""Celery resolver.

Detects:
  * task_fn.delay(...) -> calls edge from caller function to task function
  * task_fn.apply_async(...) -> calls edge from caller function to task function
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

_CELERY_INVOKE_METHODS: frozenset[str] = frozenset({
    "delay", "apply_async", "s", "si", "signature",
})


def extract_celery_edges(
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

        parent_map = _build_parent_map(tree)
        _extract_delay_calls(
            tree, rel, node_index, local_symbols, file_node_id,
            parent_map, seen, edges,
        )

    return edges


def _extract_delay_calls(
    tree: ast.Module,
    rel: str,
    node_index: dict,
    local_symbols: dict,
    file_node_id: str | None,
    parent_map: dict,
    seen: set,
    edges: list,
) -> None:
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in _CELERY_INVOKE_METHODS:
            continue

        task_expr = node.func.value
        if not isinstance(task_expr, ast.Name):
            continue
        task_name = task_expr.id

        task_node_id = _find_symbol_node(task_name.lower(), node_index)
        if not task_node_id:
            continue

        caller_name = _find_containing_func(node, parent_map)
        if caller_name:
            caller_node_id = local_symbols.get(caller_name.lower())
        else:
            caller_node_id = None

        src_id = caller_node_id or file_node_id
        if not src_id or src_id == task_node_id:
            continue

        key = (src_id, task_node_id, "calls")
        if key in seen:
            continue
        seen.add(key)
        edge = {
            "relation": "calls",
            "source": src_id,
            "target": task_node_id,
            "source_file": rel,
            "source_location": f"L{node.lineno}",
            "weight": 1.0,
        }
        annotate_edge(edge, FRAMEWORK_RESOLVED, "celery_resolver")
        edges.append(edge)


def _build_parent_map(tree: ast.AST) -> dict[int, ast.AST]:
    parent: dict[int, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parent[id(child)] = node
    return parent


def _find_containing_func(node: ast.AST, parent_map: dict) -> str | None:
    cur = parent_map.get(id(node))
    while cur is not None:
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return cur.name
        cur = parent_map.get(id(cur))
    return None


def _find_symbol_node(symbol_lc: str, node_index: dict) -> str | None:
    for (_, sym), nid in node_index["by_symbol"].items():
        if sym == symbol_lc:
            return nid
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
