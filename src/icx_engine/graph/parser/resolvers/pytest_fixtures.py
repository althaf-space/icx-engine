"""Pytest fixture resolver.

Detects:
  * @pytest.fixture definitions in conftest.py -> builds fixture registry
  * Test functions and fixture functions with params matching registry -> depends_on edge
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

_SKIP_PARAMS: frozenset[str] = frozenset({"self", "cls", "request"})


def extract_pytest_edges(
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

    # Step 1: build fixture registry from all conftest.py files
    fixture_registry: dict[str, str] = {}  # fixture_name_lc -> node_id

    for py in py_files:
        if py.name != "conftest.py":
            continue
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

        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not _is_fixture(node):
                continue
            name_lc = node.name.lower()
            nid = local_symbols.get(name_lc)
            if nid:
                fixture_registry[name_lc] = nid

    if not fixture_registry:
        return []

    # Step 2: scan test files and conftest files for fixture param usage
    seen: set[tuple[str, str, str]] = set()
    edges: list[dict] = []

    for py in py_files:
        is_conftest = py.name == "conftest.py"
        is_test_file = py.name.startswith("test_") or py.stem.endswith("_test")
        if not is_conftest and not is_test_file:
            continue
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

        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            is_test = node.name.startswith("test_")
            is_fix = _is_fixture(node)
            if not is_test and not is_fix:
                continue

            func_node_id = local_symbols.get(node.name.lower())
            if not func_node_id:
                continue

            for arg in node.args.args:
                param_name = arg.arg.lower()
                if param_name in _SKIP_PARAMS:
                    continue
                fixture_node_id = fixture_registry.get(param_name)
                if not fixture_node_id or fixture_node_id == func_node_id:
                    continue
                key = (func_node_id, fixture_node_id, "depends_on")
                if key in seen:
                    continue
                seen.add(key)
                edge = {
                    "relation": "depends_on",
                    "source": func_node_id,
                    "target": fixture_node_id,
                    "source_file": rel,
                    "source_location": f"L{node.lineno}",
                    "weight": 1.0,
                }
                annotate_edge(edge, FRAMEWORK_RESOLVED, "pytest_resolver")
                edges.append(edge)

    return edges


def _is_fixture(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for deco in node.decorator_list:
        name = _decorator_name(deco)
        if name in ("fixture", "pytest.fixture"):
            return True
        if isinstance(deco, ast.Attribute) and deco.attr == "fixture":
            return True
    return False


def _decorator_name(deco) -> str | None:
    if isinstance(deco, ast.Call):
        return _decorator_name(deco.func)
    if isinstance(deco, ast.Name):
        return deco.id
    if isinstance(deco, ast.Attribute):
        return f"{_decorator_name(deco.value)}.{deco.attr}"
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
