"""Python structural pattern resolver.

Three sub-passes:
  1. __all__ barrel tracking: emits `exports` edges when __all__ lists symbols
     imported from other project files.
  2. Protocol/ABC implementors: emits `implements_protocol` edges when a class
     inherits from a Protocol or ABC subclass.
  3. Dataclass field types: emits `uses` edges for @dataclass fields whose
     type annotation resolves to a project file.
"""
from __future__ import annotations

import ast
import logging
from pathlib import Path
from typing import Iterable

from icx_engine.graph.parser.confidence import AST_DIRECT, FRAMEWORK_RESOLVED, annotate_edge

_log = logging.getLogger(__name__)


def extract_python_type_checking_edges(
    files: Iterable[Path],
    project_root: Path,
    ast_extraction: dict,
) -> list[dict]:
    project_root = Path(project_root).resolve()
    py_files = [Path(f).resolve() for f in files if str(f).endswith(".py")]
    if not py_files:
        return []

    node_index = _build_node_index(ast_extraction.get("nodes", []), project_root)
    if not node_index["by_file"]:
        return []

    module_to_rel: dict[str, str] = {}
    for rel in node_index["by_file"]:
        mod = _rel_to_module(rel)
        if mod:
            module_to_rel[mod] = rel

    protocol_file_ids: set[str] = _find_protocol_files(py_files, project_root, node_index)

    existing_imports: dict[str, list[str]] = {}
    for e in ast_extraction.get("edges", []):
        if isinstance(e, dict) and e.get("relation") == "imports":
            src = e.get("source", "")
            tgt = e.get("target", "")
            if src and tgt:
                existing_imports.setdefault(src, []).append(tgt)

    existing_inherits: list[dict] = [
        e for e in ast_extraction.get("edges", [])
        if isinstance(e, dict) and e.get("relation") == "inherits"
    ]

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

        src_file_id = node_index["by_file"].get(rel)
        if not src_file_id:
            continue

        _emit_barrel_edges(
            tree, rel, src_file_id, existing_imports, node_index, seen, edges,
        )

        _emit_dataclass_edges(
            tree, rel, src_file_id, project_root, node_index, module_to_rel, seen, edges,
        )

    _emit_protocol_edges(existing_inherits, protocol_file_ids, seen, edges)

    return edges


def _emit_barrel_edges(
    tree: ast.Module,
    rel: str,
    src_file_id: str,
    existing_imports: dict[str, list[str]],
    node_index: dict,
    seen: set,
    edges: list,
) -> None:
    imported_targets = set(existing_imports.get(src_file_id, []))
    if not imported_targets:
        return

    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == "__all__":
                if isinstance(node.value, (ast.List, ast.Tuple)):
                    for tgt_id in imported_targets:
                        key = (src_file_id, tgt_id, "exports")
                        if key in seen:
                            continue
                        seen.add(key)
                        edge = {
                            "relation": "exports",
                            "source": src_file_id,
                            "target": tgt_id,
                            "source_file": rel,
                            "source_location": f"L{node.lineno}",
                            "weight": 1.0,
                        }
                        annotate_edge(edge, AST_DIRECT, "python_exports")
                        edges.append(edge)
                    return


def _emit_protocol_edges(
    existing_inherits: list[dict],
    protocol_file_ids: set[str],
    seen: set,
    edges: list,
) -> None:
    for e in existing_inherits:
        tgt_id = e.get("target", "")
        if tgt_id not in protocol_file_ids:
            continue
        src_id = e.get("source", "")
        key = (src_id, tgt_id, "implements_protocol")
        if key in seen:
            continue
        seen.add(key)
        edge = {
            "relation": "implements_protocol",
            "source": src_id,
            "target": tgt_id,
            "source_file": e.get("source_file", ""),
            "source_location": e.get("source_location", ""),
            "weight": 1.0,
        }
        annotate_edge(edge, FRAMEWORK_RESOLVED, "python_protocol")
        edges.append(edge)


def _emit_dataclass_edges(
    tree: ast.Module,
    rel: str,
    src_file_id: str,
    project_root: Path,
    node_index: dict,
    module_to_rel: dict[str, str],
    seen: set,
    edges: list,
) -> None:
    # Build a name -> source_module map from the file's own import statements.
    # This lets us resolve annotation type names like `Notifier` to the module
    # that exports them (e.g. `myapp.protocols`), even when symbol-level nodes
    # are absent from the extraction index.
    name_to_module: dict[str, str] = {}
    for stmt in ast.walk(tree):
        if isinstance(stmt, ast.ImportFrom) and stmt.module:
            for alias in stmt.names:
                imported_name = alias.asname if alias.asname else alias.name
                name_to_module[imported_name] = stmt.module
        elif isinstance(stmt, ast.Import):
            for alias in stmt.names:
                imported_name = alias.asname if alias.asname else alias.name
                name_to_module[imported_name] = alias.name

    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        if not _has_dataclass_decorator(node):
            continue
        for item in node.body:
            if not isinstance(item, ast.AnnAssign):
                continue
            ann = item.annotation
            type_name = _extract_annotation_name(ann)
            if not type_name:
                continue
            # Try symbol-level resolution via module_to_rel first.
            tgt_rel = _resolve_name_to_file(type_name, module_to_rel, node_index)
            # Fall back: look up the module that exported this name in the
            # file's own imports, then find the corresponding project file.
            if not tgt_rel:
                mod = name_to_module.get(type_name)
                if mod:
                    tgt_rel = module_to_rel.get(mod)
            if not tgt_rel:
                continue
            tgt_id = node_index["by_file"].get(tgt_rel)
            if not tgt_id or tgt_id == src_file_id:
                continue
            key = (src_file_id, tgt_id, "uses")
            if key in seen:
                continue
            seen.add(key)
            lineno = getattr(item, "lineno", 0)
            edge = {
                "relation": "uses",
                "source": src_file_id,
                "target": tgt_id,
                "source_file": rel,
                "source_location": f"L{lineno}" if lineno else "",
                "weight": 1.0,
            }
            annotate_edge(edge, AST_DIRECT, "python_dataclass")
            edges.append(edge)


def _find_protocol_files(
    py_files: list[Path],
    project_root: Path,
    node_index: dict,
) -> set[str]:
    protocol_ids: set[str] = set()
    for py in py_files:
        try:
            rel = py.relative_to(project_root).as_posix()
            code = py.read_text(encoding="utf-8")
            tree = ast.parse(code, filename=str(py))
        except (ValueError, OSError, SyntaxError):
            continue
        file_id = node_index["by_file"].get(rel)
        if not file_id:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            for base in node.bases:
                base_name = _extract_annotation_name(base)
                if base_name in ("Protocol", "ABC", "ABCMeta"):
                    protocol_ids.add(file_id)
                    break
    return protocol_ids


def _has_dataclass_decorator(node: ast.ClassDef) -> bool:
    for dec in node.decorator_list:
        if isinstance(dec, ast.Name) and dec.id == "dataclass":
            return True
        if isinstance(dec, ast.Attribute) and dec.attr == "dataclass":
            return True
    return False


def _extract_annotation_name(ann: ast.expr) -> str | None:
    if isinstance(ann, ast.Name):
        return ann.id
    if isinstance(ann, ast.Attribute):
        return ann.attr
    if isinstance(ann, ast.Subscript):
        return _extract_annotation_name(ann.value)
    return None


def _resolve_name_to_file(
    type_name: str,
    module_to_rel: dict[str, str],
    node_index: dict,
) -> str | None:
    for mod, rel in module_to_rel.items():
        if mod.endswith(f".{type_name.lower()}") or mod == type_name.lower():
            if rel in node_index["by_file"]:
                return rel
    return None


def _rel_to_module(rel: str) -> str:
    if not rel.endswith(".py"):
        return ""
    mod = rel[:-3].replace("/", ".").replace("\\", ".")
    if mod.endswith(".__init__"):
        mod = mod[:-9]
    return mod


def _build_node_index(nodes: list[dict], project_root: Path) -> dict:
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
        else:
            symbol = label.rstrip("()").lstrip(".").lower()
            if symbol:
                by_symbol.setdefault((rel, symbol), nid)
    return {"by_file": by_file, "by_symbol": by_symbol}
