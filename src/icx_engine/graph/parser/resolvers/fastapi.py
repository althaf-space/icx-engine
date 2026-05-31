"""FastAPI resolver: route decorators + Depends() dependency injection.

Emits `routes` edges for @app.get/@router.post/etc. and `depends_on`
edges for `Depends(target)` defaults. Uses jedi (when available) to
resolve cross-file Depends targets; falls back to same-file lookup.
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
    "get", "post", "put", "patch", "delete", "head", "options",
    "websocket", "add_api_route", "api_route",
})


def extract_fastapi_edges(
    files: Iterable[Path],
    project_root: Path,
    ast_extraction: dict,
) -> list[dict]:
    project_root = project_root.resolve()
    py_files = [Path(f).resolve() for f in files if str(f).endswith(".py")]
    if not py_files:
        return []

    try:
        import jedi
        project = jedi.Project(str(project_root))
    except Exception:
        jedi = None
        project = None

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
        except OSError:
            continue
        try:
            tree = ast.parse(code, filename=str(py))
        except SyntaxError:
            continue

        file_node_id = node_index["by_file"].get(rel)
        local_symbols = {
            sym: nid for (path, sym), nid in node_index["by_symbol"].items()
            if path == rel
        }

        script = None
        if jedi is not None:
            try:
                script = jedi.Script(code=code, path=str(py), project=project)
            except Exception:
                script = None

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                _handle_function(
                    node, rel, script, project_root, node_index,
                    local_symbols, file_node_id, seen, edges,
                )

    return edges


def _handle_function(
    func: ast.FunctionDef | ast.AsyncFunctionDef,
    rel: str,
    script,
    project_root: Path,
    node_index: dict,
    local_symbols: dict[str, str],
    file_node_id: str | None,
    seen: set,
    edges: list,
) -> None:
    func_node_id = local_symbols.get(func.name.lower()) or file_node_id
    if not func_node_id:
        return

    for deco in func.decorator_list:
        carrier, method = _decorator_carrier_and_method(deco)
        if method is None or method not in _ROUTE_METHODS:
            continue
        if not file_node_id:
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
            "source_location": f"L{deco.lineno}",
            "weight": 1.0,
            "carrier": (carrier or "app").lower(),
        }
        annotate_edge(edge, FRAMEWORK_RESOLVED, "fastapi_resolver")
        edges.append(edge)

    args = func.args
    defaults = list(args.defaults) + list(args.kw_defaults)
    for default in defaults:
        if default is None:
            continue
        dep_name = _extract_depends_target(default)
        if not dep_name:
            continue
        target_id = _resolve_depends_target(
            dep_name, default, script, project_root, rel, node_index, local_symbols,
        )
        if not target_id or target_id == func_node_id:
            continue
        key = (func_node_id, target_id, "depends_on")
        if key in seen:
            continue
        seen.add(key)
        edge = {
            "relation": "depends_on",
            "source": func_node_id,
            "target": target_id,
            "source_file": rel,
            "source_location": f"L{default.lineno}",
            "weight": 1.0,
        }
        annotate_edge(edge, FRAMEWORK_RESOLVED, "fastapi_resolver")
        edges.append(edge)


def _decorator_carrier_and_method(deco: ast.expr) -> tuple[str | None, str | None]:
    target = deco.func if isinstance(deco, ast.Call) else deco
    if isinstance(target, ast.Attribute):
        return _name_of(target.value), target.attr
    return None, None


def _name_of(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _extract_depends_target(default: ast.expr) -> str | None:
    if not isinstance(default, ast.Call):
        return None
    if _name_of(default.func) != "Depends":
        return None
    if not default.args:
        return None
    return _name_of(default.args[0])


def _resolve_depends_target(
    dep_name: str,
    default: ast.expr,
    script,
    project_root: Path,
    current_rel: str,
    node_index: dict,
    local_symbols: dict[str, str],
) -> str | None:
    if script is not None:
        try:
            defs = script.goto(
                line=default.lineno,
                column=_column_of_first_arg(default),
                follow_imports=True,
                follow_builtin_imports=False,
            )
        except Exception:
            defs = []
        for d in defs:
            if d.module_path is None:
                continue
            try:
                d_rel = Path(d.module_path).resolve().relative_to(project_root).as_posix()
            except ValueError:
                continue
            symbol_key = (d_rel, (d.name or "").lower())
            node_id = node_index["by_symbol"].get(symbol_key)
            if node_id:
                return node_id
            node_id = node_index["by_file"].get(d_rel)
            if node_id:
                return node_id

    return local_symbols.get(dep_name.lower())


def _column_of_first_arg(default: ast.Call) -> int:
    arg = default.args[0]
    col = getattr(arg, "col_offset", None)
    return col if col is not None else default.col_offset


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
