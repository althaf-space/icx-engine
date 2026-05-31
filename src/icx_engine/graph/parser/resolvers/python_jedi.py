"""Python cross-file resolver backed by jedi.

Runs jedi.Script.get_names() + goto() on every Python file to bind
cross-file references to concrete definitions, complementing the
tree-sitter AST extractor with symbol-table-grade resolution.
"""
from __future__ import annotations

import ast
import logging
from pathlib import Path
from typing import Iterable

from icx_engine.graph.parser.confidence import (
    AST_DIRECT,
    LSP_RESOLVED,
    annotate_edge,
)

_log = logging.getLogger(__name__)

_TYPE_TO_RELATION: dict[str, str] = {
    "function": "calls",
    "method": "calls",
    # Class targets are usually constructor calls in idiomatic Python.
    "class": "calls",
    "instance": "uses",
    "statement": "uses",
    "param": "uses",
    "module": "imports",
}

_RELATION_PRIORITY: dict[str, int] = {
    "imports": 4,
    "inherits": 3,
    "calls": 2,
    "uses": 1,
}


def extract_python_edges(
    files: Iterable[Path],
    project_root: Path,
    ast_extraction: dict,
) -> list[dict]:
    """Resolve cross-file Python references against existing AST node IDs."""
    try:
        import jedi
    except ImportError:
        return []

    project_root = project_root.resolve()
    py_files = [Path(f).resolve() for f in files if str(f).endswith(".py")]
    if not py_files:
        return []

    node_index = _build_node_index(ast_extraction.get("nodes", []), project_root)
    if not node_index["by_symbol"] and not node_index["by_file"]:
        return []

    try:
        project = jedi.Project(str(project_root))
    except Exception as exc:
        _log.debug("jedi.Project init failed (%s)", type(exc).__name__)
        return []

    best_edge: dict[tuple[str, str], dict] = {}

    _ast_import_scan(py_files, project_root, node_index, best_edge)
    _extract_dynamic_imports(py_files, project_root, node_index, best_edge)

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
            script = jedi.Script(code=code, path=str(py), project=project)
            names = script.get_names(
                all_scopes=True, references=True, definitions=False,
            )
        except Exception:
            continue

        src_file_node = node_index["by_file"].get(rel)
        local_symbols = {
            sym: nid for (path, sym), nid in node_index["by_symbol"].items()
            if path == rel
        }

        for name in names:
            try:
                defs = name.goto(
                    follow_imports=True, follow_builtin_imports=False,
                )
            except Exception:
                continue
            if not defs:
                continue
            line_no = getattr(name, "line", None)
            for d in defs:
                if d.module_path is None:
                    continue
                try:
                    d_rel = Path(d.module_path).resolve().relative_to(project_root).as_posix()
                except ValueError:
                    continue

                target_type = (d.type or "").lower()
                # Same-file: AST extractor covers intra-file structure;
                # only forward class-target refs so ORM FK/cross-class
                # patterns in a single models.py still register as edges.
                if d_rel == rel and target_type != "class":
                    continue

                is_import_site = _is_import_line(code, line_no)
                is_class_header = _is_class_header_line(code, line_no)
                relation = _TYPE_TO_RELATION.get(target_type, "uses")

                # Module references inside `from x.y import z` are handled
                # by the AST import scan with deeper precision.
                if is_import_site and target_type == "module":
                    continue
                if is_import_site:
                    relation = "imports"
                elif is_class_header and target_type == "class":
                    relation = "inherits"

                if relation == "imports":
                    tgt_id = node_index["by_file"].get(d_rel)
                else:
                    tgt_id = _resolve_target_node_id(d_rel, d.name, node_index)

                src_id = _resolve_source_node_id(
                    rel, line_no, code, local_symbols, src_file_node,
                )
                if not src_id or not tgt_id or src_id == tgt_id:
                    continue

                pair_key = (src_id, tgt_id)
                existing = best_edge.get(pair_key)
                new_priority = _RELATION_PRIORITY.get(relation, 0)
                if existing is not None:
                    existing_priority = _RELATION_PRIORITY.get(
                        existing.get("relation", ""), 0,
                    )
                    if new_priority <= existing_priority:
                        continue

                edge = {
                    "relation": relation,
                    "source": src_id,
                    "target": tgt_id,
                    "source_file": rel,
                    "source_location": f"L{line_no}" if line_no else "",
                    "weight": 1.0,
                }
                annotate_edge(
                    edge,
                    AST_DIRECT if relation == "imports" else LSP_RESOLVED,
                    "python_jedi",
                )
                best_edge[pair_key] = edge

    return list(best_edge.values())


def _build_node_index(
    nodes: list[dict], project_root: Path,
) -> dict[str, dict]:
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
        # Parser prefixes method labels with "." to distinguish from
        # module-level functions; strip so jedi name lookups match.
        symbol = symbol.lstrip(".").lower()
        if symbol:
            by_symbol.setdefault((rel, symbol), nid)

    return {"by_file": by_file, "by_symbol": by_symbol}


def _resolve_target_node_id(
    rel_path: str, target_name: str, node_index: dict,
) -> str | None:
    if target_name:
        key = (rel_path, target_name.lower())
        if key in node_index["by_symbol"]:
            return node_index["by_symbol"][key]
    return node_index["by_file"].get(rel_path)


def _resolve_source_node_id(
    rel_path: str,
    line_no: int | None,
    code: str,
    local_symbols: dict[str, str],
    file_node_id: str | None,
) -> str | None:
    if line_no is None:
        return file_node_id
    enclosing = _enclosing_definition(code, line_no)
    if enclosing:
        node = local_symbols.get(enclosing.lower())
        if node:
            return node
    return file_node_id


def _enclosing_definition(code: str, target_line: int) -> str | None:
    if target_line <= 0:
        return None
    lines = code.splitlines()
    if target_line > len(lines):
        return None
    for idx in range(target_line - 1, -1, -1):
        line = lines[idx]
        stripped = line.lstrip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("def ") or stripped.startswith("async def "):
            name = _parse_def_name(stripped)
            if name:
                return name
        if stripped.startswith("class "):
            name = _parse_class_name(stripped)
            if name:
                return name
    return None


def _parse_def_name(stripped: str) -> str | None:
    head = stripped
    if head.startswith("async def "):
        head = head[len("async def ") :]
    elif head.startswith("def "):
        head = head[len("def ") :]
    else:
        return None
    paren = head.find("(")
    if paren <= 0:
        return None
    return head[:paren].strip() or None


def _parse_class_name(stripped: str) -> str | None:
    head = stripped[len("class ") :]
    for end_char in ("(", ":"):
        pos = head.find(end_char)
        if pos > 0:
            return head[:pos].strip() or None
    return head.strip() or None


def _ast_import_scan(
    py_files: list[Path],
    project_root: Path,
    node_index: dict,
    best_edge: dict[tuple[str, str], dict],
) -> None:
    """Emit file-level + nested import edges via direct ast.parse().

    Handles cases jedi misses: imports inside function bodies, and
    multi-name `from pkg import a, b, c` where jedi only resolves one.
    """
    file_to_rel: dict[Path, str] = {}
    for py in py_files:
        try:
            file_to_rel[py] = py.relative_to(project_root).as_posix()
        except ValueError:
            pass

    module_to_file: dict[str, str] = {}
    for py, rel in file_to_rel.items():
        mod = _path_to_module(rel)
        if mod:
            module_to_file[mod] = node_index["by_file"].get(rel, "")

    for py, rel in file_to_rel.items():
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
        current_module = _path_to_module(rel)

        for src_id, import_node in _walk_imports_with_scope(
            tree, local_symbols, file_node_id,
        ):
            for target_module in _resolve_import_target_modules(
                import_node, current_module=current_module,
            ):
                tgt_id = module_to_file.get(target_module)
                if not tgt_id or not src_id or tgt_id == src_id:
                    continue
                pair_key = (src_id, tgt_id)
                existing = best_edge.get(pair_key)
                if existing and existing.get("relation") == "imports":
                    continue
                edge = {
                    "relation": "imports",
                    "source": src_id,
                    "target": tgt_id,
                    "source_file": rel,
                    "source_location": f"L{import_node.lineno}",
                    "weight": 1.0,
                }
                annotate_edge(edge, AST_DIRECT, "python_ast_imports")
                best_edge[pair_key] = edge


def _walk_imports_with_scope(tree, local_symbols, file_node_id):
    def visit(node, scope_stack):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            src = file_node_id
            for scope_name in reversed(scope_stack):
                node_id = local_symbols.get(scope_name.lower())
                if node_id:
                    src = node_id
                    break
            yield src, node
            return
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            scope_stack = scope_stack + [node.name]
            for child in ast.iter_child_nodes(node):
                yield from visit(child, scope_stack)
            return
        for child in ast.iter_child_nodes(node):
            yield from visit(child, scope_stack)

    yield from visit(tree, [])


def _resolve_import_target_modules(import_node, current_module: str) -> list[str]:
    if isinstance(import_node, ast.Import):
        return [alias.name for alias in import_node.names if alias.name]

    if isinstance(import_node, ast.ImportFrom):
        base = import_node.module or ""
        level = import_node.level or 0
        if level > 0:
            base = _resolve_relative_module(current_module, level, base)
        if not base:
            return []
        targets = [base]
        for alias in import_node.names:
            if alias.name == "*":
                continue
            targets.append(f"{base}.{alias.name}")
        return targets

    return []


def _resolve_relative_module(current_module: str, level: int, base: str) -> str:
    parts = current_module.split(".") if current_module else []
    if level > len(parts):
        return base
    parent = parts[: len(parts) - level]
    if base:
        return ".".join(parent + [base])
    return ".".join(parent)


def _path_to_module(rel: str) -> str:
    parts = rel.split("/")
    if parts[-1] == "__init__.py":
        return ".".join(parts[:-1])
    if parts[-1].endswith(".py"):
        return ".".join(parts[:-1] + [parts[-1][:-3]])
    return ""


def _is_import_line(code: str, line_no: int | None) -> bool:
    if line_no is None:
        return False
    lines = code.splitlines()
    if 0 < line_no <= len(lines):
        stripped = lines[line_no - 1].lstrip()
        return stripped.startswith("from ") or stripped.startswith("import ")
    return False


def _is_class_header_line(code: str, line_no: int | None) -> bool:
    if line_no is None:
        return False
    lines = code.splitlines()
    if 0 < line_no <= len(lines):
        return lines[line_no - 1].lstrip().startswith("class ")
    return False


def _extract_dynamic_imports(
    py_files: list[Path],
    project_root: Path,
    node_index: dict,
    best_edge: dict[tuple[str, str], dict],
) -> None:
    """Emit import edges for importlib.import_module() and __import__() calls."""
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

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            is_import_module = (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "import_module"
            )
            is_dunder_import = (
                isinstance(node.func, ast.Name)
                and node.func.id == "__import__"
            )
            if not (is_import_module or is_dunder_import):
                continue
            if not node.args:
                continue
            first_arg = node.args[0]
            if not (isinstance(first_arg, ast.Constant) and isinstance(first_arg.value, str)):
                continue
            module_str = first_arg.value
            if not module_str:
                continue
            candidate_rel = module_str.replace(".", "/") + ".py"
            target_id = node_index["by_file"].get(candidate_rel)
            if not target_id or target_id == src_file_id:
                continue
            pair_key = (src_file_id, target_id)
            existing = best_edge.get(pair_key)
            new_pri = _RELATION_PRIORITY.get("imports", 0)
            if existing is not None:
                if _RELATION_PRIORITY.get(existing.get("relation", ""), 0) >= new_pri:
                    continue
            lineno = getattr(node, "lineno", 0)
            edge = {
                "relation": "imports",
                "source": src_file_id,
                "target": target_id,
                "source_file": rel,
                "source_location": f"L{lineno}",
                "weight": 1.0,
            }
            annotate_edge(edge, AST_DIRECT, "dynamic_import")
            best_edge[pair_key] = edge
