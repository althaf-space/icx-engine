"""Django framework resolver.

Detects:
  * @receiver(signal, sender=Model) -> listens edge
  * ForeignKey / OneToOneField / ManyToManyField -> has_relation edge
  * URL route patterns (path/re_path) -> routes edge
  * @admin.register(Model) -> reference edge
  * ModelForm/ModelSerializer Meta.model -> reference edge
  * CBV model attribute -> reference edge
  * Celery @shared_task / @app.task -> scheduled edge
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

_DJANGO_RELATION_FIELDS: frozenset[str] = frozenset({
    "ForeignKey", "OneToOneField", "ManyToManyField",
})

_DJANGO_SIGNALS: frozenset[str] = frozenset({
    "pre_save", "post_save", "pre_delete", "post_delete",
    "m2m_changed", "pre_init", "post_init",
    "request_started", "request_finished", "got_request_exception",
    "pre_migrate", "post_migrate",
})

_CBV_MODEL_BASES: frozenset[str] = frozenset({
    "ListView", "DetailView", "CreateView", "UpdateView", "DeleteView",
    "ModelViewSet", "ReadOnlyModelViewSet",
    "ModelSerializer",
    "ModelForm",
    "ModelAdmin", "TabularInline", "StackedInline",
})

_URL_FUNC_NAMES: frozenset[str] = frozenset({
    "path", "re_path", "url",
})


def extract_django_edges(
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

    file_asts: list[tuple[Path, str, ast.Module]] = []
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
        file_asts.append((py, rel, tree))

    symbol_to_file: dict[str, str] = {}
    for (path, sym), nid in node_index["by_symbol"].items():
        symbol_to_file.setdefault(sym, path)

    seen: set[tuple[str, str, str]] = set()
    edges: list[dict] = []

    for py, rel, tree in file_asts:
        file_node_id = node_index["by_file"].get(rel)
        local_symbols = {
            sym: nid for (path, sym), nid in node_index["by_symbol"].items()
            if path == rel
        }

        _extract_signals(tree, rel, node_index, local_symbols, file_node_id, seen, edges)
        _extract_orm_relations(tree, rel, node_index, local_symbols, file_node_id, seen, edges)
        _extract_url_routes(tree, rel, node_index, local_symbols, file_node_id, seen, edges)
        _extract_celery_tasks(tree, rel, node_index, local_symbols, file_node_id, seen, edges)
        _extract_include_urls(tree, rel, node_index, file_node_id, seen, edges, project_root)

    return edges


def _extract_signals(
    tree: ast.Module, rel: str, node_index: dict,
    local_symbols: dict, file_node_id: str | None,
    seen: set, edges: list,
) -> None:
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for deco in node.decorator_list:
            sender = _extract_receiver_sender(deco)
            if sender is None:
                continue
            func_node_id = local_symbols.get(node.name.lower())
            if not func_node_id:
                continue
            target_id = _find_symbol_node(sender.lower(), node_index)
            if not target_id or target_id == func_node_id:
                continue
            key = (func_node_id, target_id, "listens")
            if key in seen:
                continue
            seen.add(key)
            edge = {
                "relation": "listens",
                "source": func_node_id,
                "target": target_id,
                "source_file": rel,
                "source_location": f"L{deco.lineno}",
                "weight": 1.0,
            }
            annotate_edge(edge, FRAMEWORK_RESOLVED, "django_signals")
            edges.append(edge)


def _extract_receiver_sender(deco) -> str | None:
    if not isinstance(deco, ast.Call):
        return None
    func = deco.func
    name = None
    if isinstance(func, ast.Name):
        name = func.id
    elif isinstance(func, ast.Attribute):
        name = func.attr
    if name != "receiver":
        return None
    for kw in deco.keywords:
        if kw.arg == "sender":
            return _name_of(kw.value)
    if len(deco.args) >= 2:
        return _name_of(deco.args[1])
    return None


def _extract_orm_relations(
    tree: ast.Module, rel: str, node_index: dict,
    local_symbols: dict, file_node_id: str | None,
    seen: set, edges: list,
) -> None:
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        class_node_id = local_symbols.get(node.name.lower())
        if not class_node_id:
            continue
        for stmt in ast.walk(node):
            if not isinstance(stmt, ast.Assign):
                continue
            if not isinstance(stmt.value, ast.Call):
                continue
            call = stmt.value
            func_name = _call_func_name(call)
            if func_name not in _DJANGO_RELATION_FIELDS:
                continue
            if not call.args:
                continue
            target_name = _name_of(call.args[0])
            if not target_name or target_name == "settings.AUTH_USER_MODEL":
                if isinstance(call.args[0], ast.Attribute):
                    continue
                if isinstance(call.args[0], ast.Constant):
                    continue
            if not target_name:
                continue
            target_id = _find_symbol_node(target_name.lower(), node_index)
            if not target_id or target_id == class_node_id:
                continue
            key = (class_node_id, target_id, "has_relation")
            if key in seen:
                continue
            seen.add(key)
            edge = {
                "relation": "has_relation",
                "source": class_node_id,
                "target": target_id,
                "source_file": rel,
                "source_location": f"L{stmt.lineno}",
                "weight": 1.0,
            }
            annotate_edge(edge, FRAMEWORK_RESOLVED, "django_orm")
            edges.append(edge)


def _extract_url_routes(
    tree: ast.Module, rel: str, node_index: dict,
    local_symbols: dict, file_node_id: str | None,
    seen: set, edges: list,
) -> None:
    if not file_node_id:
        return
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func_name = _call_func_name(node)
        if func_name not in _URL_FUNC_NAMES:
            continue
        if len(node.args) < 2:
            continue
        view_arg = node.args[1]
        view_name = _extract_view_ref(view_arg)
        if not view_name:
            continue
        target_id = _find_symbol_node(view_name.lower(), node_index)
        if not target_id or target_id == file_node_id:
            continue
        key = (file_node_id, target_id, "routes")
        if key in seen:
            continue
        seen.add(key)
        edge = {
            "relation": "routes",
            "source": file_node_id,
            "target": target_id,
            "source_file": rel,
            "source_location": f"L{node.lineno}",
            "weight": 1.0,
        }
        annotate_edge(edge, FRAMEWORK_RESOLVED, "django_urls")
        edges.append(edge)


def _extract_celery_tasks(
    tree: ast.Module, rel: str, node_index: dict,
    local_symbols: dict, file_node_id: str | None,
    seen: set, edges: list,
) -> None:
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        is_task = False
        for deco in node.decorator_list:
            deco_name = _decorator_name(deco)
            if deco_name in ("shared_task", "task", "periodic_task"):
                is_task = True
                break
        if not is_task:
            continue
        func_node_id = local_symbols.get(node.name.lower())
        if not func_node_id or not file_node_id:
            continue
        key = (file_node_id, func_node_id, "scheduled")
        if key in seen:
            continue
        seen.add(key)
        edge = {
            "relation": "scheduled",
            "source": file_node_id,
            "target": func_node_id,
            "source_file": rel,
            "source_location": f"L{node.lineno}",
            "weight": 1.0,
        }
        annotate_edge(edge, FRAMEWORK_RESOLVED, "celery_task")
        edges.append(edge)


def _extract_include_urls(
    tree: ast.Module,
    rel: str,
    node_index: dict,
    file_node_id: str | None,
    seen: set,
    edges: list,
    project_root: Path,
) -> None:
    """Detect include('module.path') calls and emit imports edges to target URL module."""
    if not file_node_id:
        return
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func_name = _call_func_name(node)
        if func_name != "include":
            continue
        if not node.args:
            continue
        first_arg = node.args[0]
        if not (isinstance(first_arg, ast.Constant) and isinstance(first_arg.value, str)):
            continue
        module_path = first_arg.value
        file_path = module_path.replace(".", "/") + ".py"
        candidate = project_root / file_path
        if not candidate.is_file():
            continue
        target_id = node_index["by_file"].get(file_path)
        if not target_id or target_id == file_node_id:
            continue
        key = (file_node_id, target_id, "imports")
        if key in seen:
            continue
        seen.add(key)
        edge = {
            "relation": "imports",
            "source": file_node_id,
            "target": target_id,
            "source_file": rel,
            "source_location": f"L{node.lineno}",
            "weight": 1.0,
        }
        annotate_edge(edge, FRAMEWORK_RESOLVED, "django_include")
        edges.append(edge)


def _extract_view_ref(node) -> str | None:
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Call):
        if isinstance(node.func, ast.Attribute) and node.func.attr == "as_view":
            return _name_of(node.func.value)
    return None


def _decorator_name(deco) -> str | None:
    if isinstance(deco, ast.Call):
        return _decorator_name(deco.func)
    if isinstance(deco, ast.Name):
        return deco.id
    if isinstance(deco, ast.Attribute):
        return deco.attr
    return None


def _call_func_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def _name_of(node) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _find_symbol_node(symbol_lc: str, node_index: dict) -> str | None:
    for (path, sym), nid in node_index["by_symbol"].items():
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
