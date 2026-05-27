"""SQLAlchemy resolver.

Detects:
  * relationship("TargetModel", back_populates="field") string refs -> has_relation edge
  * Handles both positional string arg and direct Name references
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

_RELATIONSHIP_FUNCS: frozenset[str] = frozenset({
    "relationship", "relation",
})


def extract_sqlalchemy_edges(
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

        local_symbols = {
            sym: nid for (path, sym), nid in node_index["by_symbol"].items()
            if path == rel
        }

        _extract_relationships(tree, rel, node_index, local_symbols, seen, edges)

    return edges


def _extract_relationships(
    tree: ast.Module,
    rel: str,
    node_index: dict,
    local_symbols: dict,
    seen: set,
    edges: list,
) -> None:
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        class_node_id = local_symbols.get(node.name.lower())
        if not class_node_id:
            continue

        for stmt in ast.walk(node):
            if not isinstance(stmt, (ast.Assign, ast.AnnAssign)):
                continue
            value = (
                stmt.value if isinstance(stmt, ast.Assign)
                else getattr(stmt, "value", None)
            )
            if not isinstance(value, ast.Call):
                continue
            func_name = _call_func_name(value)
            if func_name not in _RELATIONSHIP_FUNCS:
                continue

            target_name: str | None = None
            if value.args:
                first_arg = value.args[0]
                if isinstance(first_arg, ast.Constant) and isinstance(first_arg.value, str):
                    target_name = first_arg.value
                elif isinstance(first_arg, ast.Name):
                    target_name = first_arg.id

            if not target_name:
                continue

            target_id = _find_symbol_node(target_name.lower(), node_index)
            if not target_id or target_id == class_node_id:
                continue

            key = (class_node_id, target_id, "has_relation")
            if key in seen:
                continue
            seen.add(key)
            lineno = getattr(stmt, "lineno", 0)
            edge = {
                "relation": "has_relation",
                "source": class_node_id,
                "target": target_id,
                "source_file": rel,
                "source_location": f"L{lineno}",
                "weight": 1.0,
            }
            annotate_edge(edge, FRAMEWORK_RESOLVED, "sqlalchemy_resolver")
            edges.append(edge)


def _call_func_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
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
