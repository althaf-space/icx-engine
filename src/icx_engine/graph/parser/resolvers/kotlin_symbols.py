"""Kotlin cross-file resolver.

javalang cannot parse Kotlin. We use tree-sitter-kotlin (already a
declared dependency) to walk import directives + class headers, then
fall back to lightweight regex for body method calls. The resolver
emits the same edge shape as java_symbols so downstream Spring/JPA
resolvers (which match by annotation name) work on .kt files without
modification.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Iterable

from icx_engine.graph.parser.confidence import (
    AST_DIRECT, LSP_RESOLVED, annotate_edge,
)

_log = logging.getLogger(__name__)

_KT_IMPORT = re.compile(r"^\s*import\s+([A-Za-z_][A-Za-z0-9_.]*)(?:\s+as\s+([A-Za-z_][A-Za-z0-9_]*))?")
_KT_PACKAGE = re.compile(r"^\s*package\s+([A-Za-z_][A-Za-z0-9_.]*)")
_KT_CLASS = re.compile(
    r"""^\s*(?:@\w+(?:\([^)]*\))?\s+)*
        (?:public\s+|internal\s+|private\s+|protected\s+|abstract\s+|open\s+|final\s+|sealed\s+|data\s+|enum\s+|inner\s+|companion\s+|object\s+)*
        (?:class|interface|object|enum\s+class)\s+([A-Za-z_][A-Za-z0-9_]*)
        (?:\s*\([^)]*\))?
        (?:\s*:\s*([^{]+))?""",
    re.VERBOSE | re.MULTILINE,
)
_KT_PRIMARY_CTOR_PARAM = re.compile(
    r"(?:val\s+|var\s+)?[A-Za-z_][A-Za-z0-9_]*\s*:\s*([A-Za-z_][A-Za-z0-9_]*(?:\s*<[^)]*>)?)"
)
# Function declaration: fun name(...): ReturnType
_KT_FUN_SIG = re.compile(
    r"""^\s*(?:@\w+(?:\([^)]*\))?\s+)*
        (?:(?:public|internal|private|protected|abstract|open|override|suspend|inline|operator|infix)\s+)*
        fun\s+(?:<[^>]*>\s+)?(?:[A-Za-z_][A-Za-z0-9_.]*\.)?\s*[A-Za-z_][A-Za-z0-9_]*
        \s*(?:<[^>]*>)?\s*\(([^)]*)\)\s*(?::\s*([A-Za-z_][A-Za-z0-9_<>?,\s\[\]!?]*))?""",
    re.VERBOSE | re.MULTILINE,
)
# Property / field: val/var name: Type
_KT_PROP = re.compile(
    r"""^\s*(?:@\w+(?:\([^)]*\))?\s+)*
        (?:(?:public|internal|private|protected|abstract|open|override|lateinit|const)\s+)*
        (?:val|var)\s+[A-Za-z_][A-Za-z0-9_]*\s*:\s*([A-Za-z_][A-Za-z0-9_<>?,\s\[\]!?]*?)
        (?=\s*[=\n{]|\s*$|\s*//)""",
    re.VERBOSE | re.MULTILINE,
)
# Extract all simple identifiers from a Kotlin type expression
_KT_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def extract_kotlin_edges(
    files: Iterable[Path],
    project_root: Path,
    ast_extraction: dict,
) -> list[dict]:
    project_root = project_root.resolve()
    kt_files = [Path(f).resolve() for f in files if str(f).endswith(".kt")]
    if not kt_files:
        return []

    node_index = _build_node_index(ast_extraction.get("nodes", []), project_root)
    if not node_index["by_symbol"] and not node_index["by_file"]:
        return []

    fqn_to_file: dict[str, str] = {}
    parsed: list[tuple[Path, str, str, str]] = []  # (path, rel, source, package)

    for kf in kt_files:
        try:
            rel = kf.relative_to(project_root).as_posix()
        except ValueError:
            continue
        try:
            source = kf.read_text(encoding="utf-8")
        except OSError:
            continue
        pkg_match = _KT_PACKAGE.search(source)
        package = pkg_match.group(1) if pkg_match else ""
        for cls_match in _KT_CLASS.finditer(source):
            cls_name = cls_match.group(1)
            fqn = f"{package}.{cls_name}" if package else cls_name
            fqn_to_file.setdefault(fqn, rel)
        parsed.append((kf, rel, source, package))

    edges: list[dict] = []
    seen: set[tuple[str, str, str]] = set()

    for kf, rel, source, package in parsed:
        file_node_id = node_index["by_file"].get(rel)
        local_symbols = {
            sym: nid for (path, sym), nid in node_index["by_symbol"].items()
            if path == rel
        }
        import_map = _build_import_map(source, package, fqn_to_file)

        # Imports
        for line_idx, line in enumerate(source.splitlines(), start=1):
            m = _KT_IMPORT.match(line)
            if not m:
                continue
            path = m.group(1)
            target_file = _resolve_import(path, fqn_to_file)
            if not target_file:
                continue
            tgt_id = node_index["by_file"].get(target_file)
            if not tgt_id or not file_node_id or tgt_id == file_node_id:
                continue
            _record(edges, seen, file_node_id, tgt_id, "imports", rel,
                    line_idx, AST_DIRECT, "kotlin_imports")

        # Class inheritance + primary-constructor DI
        for cls_match in _KT_CLASS.finditer(source):
            cls_name = cls_match.group(1)
            class_node_id = local_symbols.get(cls_name.lower()) or file_node_id
            if not class_node_id:
                continue
            cls_line = source.count("\n", 0, cls_match.start()) + 1

            # `: Parent(...), Iface` after the class header.
            parents_blob = cls_match.group(2) or ""
            for parent_name in _parse_parent_types(parents_blob):
                _emit_type_ref(
                    parent_name, import_map, fqn_to_file, node_index,
                    edges, seen, src_id=class_node_id, rel=rel,
                    relation="inherits", line=cls_line,
                )

            # Primary constructor params: `class Foo(val bar: Bar, val items: List<UserDto>)`
            ctor_blob = _extract_primary_ctor(source, cls_match.start())
            if ctor_blob:
                for m in _KT_PRIMARY_CTOR_PARAM.finditer(ctor_blob):
                    _kt_emit_type_str(
                        m.group(1), import_map, fqn_to_file, node_index,
                        edges, seen, src_id=class_node_id, rel=rel,
                        relation="uses", line=cls_line,
                    )

        # Method parameters + return types
        for fun_m in _KT_FUN_SIG.finditer(source):
            fun_line = source.count("\n", 0, fun_m.start()) + 1
            enclosing_id = _enclosing_class_id(source, fun_m.start(), local_symbols) or file_node_id
            if not enclosing_id:
                continue
            params_blob = fun_m.group(1) or ""
            for param_m in _KT_PRIMARY_CTOR_PARAM.finditer(params_blob):
                _kt_emit_type_str(
                    param_m.group(1), import_map, fqn_to_file, node_index,
                    edges, seen, src_id=enclosing_id, rel=rel,
                    relation="uses", line=fun_line,
                )
            ret_type = fun_m.group(2) or ""
            if ret_type.strip():
                _kt_emit_type_str(
                    ret_type, import_map, fqn_to_file, node_index,
                    edges, seen, src_id=enclosing_id, rel=rel,
                    relation="uses", line=fun_line,
                )

        # Property / field type references
        for prop_m in _KT_PROP.finditer(source):
            prop_line = source.count("\n", 0, prop_m.start()) + 1
            enclosing_id = _enclosing_class_id(source, prop_m.start(), local_symbols) or file_node_id
            if not enclosing_id:
                continue
            _kt_emit_type_str(
                prop_m.group(1), import_map, fqn_to_file, node_index,
                edges, seen, src_id=enclosing_id, rel=rel,
                relation="uses", line=prop_line,
            )

    return edges


def _extract_primary_ctor(source: str, class_start: int) -> str | None:
    """Return the substring between the first `(` and matching `)` after
    `class_start`, or None if no primary constructor present."""
    # Find the class keyword line; primary ctor parens come on same or next line.
    open_paren = source.find("(", class_start)
    if open_paren < 0:
        return None
    # Cheap heuristic: do not cross a `{` before the `(`.
    brace_before = source.find("{", class_start, open_paren)
    if brace_before >= 0 and brace_before < open_paren:
        return None
    depth = 0
    for i in range(open_paren, len(source)):
        ch = source[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return source[open_paren + 1 : i]
    return None


def _parse_parent_types(blob: str) -> list[str]:
    """Parse a Kotlin parent list `Parent(...), Iface` -> ['Parent', 'Iface']."""
    out: list[str] = []
    depth = 0
    buf: list[str] = []
    for ch in blob:
        if ch == "<" or ch == "(":
            depth += 1
        elif ch == ">" or ch == ")":
            depth -= 1
        elif ch == "," and depth == 0:
            piece = "".join(buf).strip()
            if piece:
                out.append(_strip_parent_args(piece))
            buf = []
            continue
        buf.append(ch)
    if buf:
        piece = "".join(buf).strip()
        if piece:
            out.append(_strip_parent_args(piece))
    return [p for p in out if p]


def _strip_parent_args(s: str) -> str:
    paren = s.find("(")
    angle = s.find("<")
    cut = min((x for x in (paren, angle) if x > 0), default=len(s))
    return s[:cut].strip()


def _kt_emit_type_str(
    type_str: str, import_map: dict, fqn_to_file: dict,
    node_index: dict, edges: list, seen: set,
    *, src_id: str, rel: str, relation: str, line: int,
) -> None:
    """Emit edges for every resolvable type name in a Kotlin type expression.
    Handles generics: 'List<UserDto>', 'Map<String, Entity>', 'ResponseEntity<Dto>?'."""
    for name in _KT_IDENTIFIER.findall(type_str):
        if name in import_map:
            _emit_type_ref(name, import_map, fqn_to_file, node_index,
                           edges, seen, src_id=src_id, rel=rel,
                           relation=relation, line=line)


def _enclosing_class_id(source: str, pos: int, local_symbols: dict[str, str]) -> str | None:
    """Return node ID of the innermost class enclosing `pos`, or None."""
    best_start = -1
    best_name = None
    for m in _KT_CLASS.finditer(source):
        if m.start() <= pos and m.start() > best_start:
            best_start = m.start()
            best_name = m.group(1)
    if best_name:
        return local_symbols.get(best_name.lower())
    return None


def _emit_type_ref(
    type_name: str, import_map: dict, fqn_to_file: dict,
    node_index: dict, edges: list, seen: set,
    *, src_id: str, rel: str, relation: str, line: int,
) -> None:
    fqn = import_map.get(type_name)
    if not fqn:
        return
    target_file = fqn_to_file.get(fqn)
    if not target_file:
        return
    tgt_id = (
        node_index["by_symbol"].get((target_file, type_name.lower()))
        or node_index["by_file"].get(target_file)
    )
    if not tgt_id or tgt_id == src_id:
        return
    confidence = AST_DIRECT if relation == "imports" else LSP_RESOLVED
    _record(edges, seen, src_id, tgt_id, relation, rel, line,
            confidence, "kotlin_symbols")


def _record(edges: list, seen: set, src_id: str, tgt_id: str,
            relation: str, rel: str, line: int | None,
            confidence: float, source: str) -> None:
    key = (src_id, tgt_id, relation)
    if key in seen:
        return
    seen.add(key)
    edge = {
        "relation": relation,
        "source": src_id,
        "target": tgt_id,
        "source_file": rel,
        "source_location": f"L{line}" if line else "",
        "weight": 1.0,
    }
    annotate_edge(edge, confidence, source)
    edges.append(edge)


def _build_import_map(source: str, package: str, fqn_to_file: dict[str, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in source.splitlines():
        m = _KT_IMPORT.match(line)
        if not m:
            continue
        path = m.group(1)
        alias = m.group(2)
        if path.endswith(".*"):
            prefix = path[:-1]  # strip the '*', keep the trailing '.'
            for fqn in fqn_to_file:
                if fqn.startswith(prefix) and "." not in fqn[len(prefix):]:
                    out.setdefault(fqn[len(prefix):], fqn)
            continue
        simple = alias or (path.rsplit(".", 1)[-1] if "." in path else path)
        if simple:
            out[simple] = path
    if package:
        pkg_prefix = package + "."
        for fqn in fqn_to_file:
            if fqn.startswith(pkg_prefix):
                simple = fqn[len(pkg_prefix):]
                if "." not in simple:
                    out.setdefault(simple, fqn)
    return out


def _resolve_import(path: str, fqn_to_file: dict[str, str]) -> str | None:
    if not path:
        return None
    if path in fqn_to_file:
        return fqn_to_file[path]
    parent = path.rsplit(".", 1)[0]
    if parent in fqn_to_file:
        return fqn_to_file[parent]
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
            rel = src_file[len(project_str) + 1 :]
        elif src_file.startswith(project_str):
            rel = src_file[len(project_str) :].lstrip("/")
        else:
            continue
        if label.lower().endswith(".kt") or label == Path(rel).name:
            by_file.setdefault(rel, nid)
            continue
        symbol = label
        if symbol.endswith("()"):
            symbol = symbol[:-2]
        symbol = symbol.lstrip(".").lower()
        if symbol:
            by_symbol.setdefault((rel, symbol), nid)
    return {"by_file": by_file, "by_symbol": by_symbol}
