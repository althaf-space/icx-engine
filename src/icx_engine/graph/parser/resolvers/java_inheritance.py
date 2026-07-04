"""Java inheritance resolver - pure regex, no javalang dependency.

Detects extends/implements for ALL Java files, including those that
exceed the javalang parse timeout. Emits `inherits` edges.

Supplements java_symbols.py which only processes successfully-parsed
files (javalang times out on ~5-15% of files in large projects, silently
dropping all inherits edges originating from or targeting those files).
"""
from __future__ import annotations

import re
import logging
from pathlib import Path
from typing import Iterable

from icx_engine.graph.parser.confidence import AST_DIRECT, annotate_edge

_log = logging.getLogger(__name__)

_PKG_RE = re.compile(r'^\s*package\s+([\w.]+)\s*;', re.MULTILINE)
_IMPORT_RE = re.compile(r'^\s*import\s+(?:static\s+)?([\w.]+)\s*;', re.MULTILINE)
_TYPE_DECL_RE = re.compile(
    r'\b(?:class|interface|enum|record)\s+([A-Z]\w*)\b',
    re.MULTILINE,
)


def extract_java_inheritance_edges(
    files: Iterable[Path],
    project_root: Path,
    ast_extraction: dict,
) -> list[dict]:
    project_root = project_root.resolve()
    java_files = [Path(f).resolve() for f in files if str(f).endswith(".java")]
    if not java_files:
        return []

    node_index = _build_node_index(ast_extraction.get("nodes", []), project_root)
    if not node_index["by_symbol"] and not node_index["by_file"]:
        return []

    from . import _java_fqn_map as _jfm
    fqn_to_file = _jfm.build_fqn_map(java_files, project_root)
    pkg_index = _build_pkg_member_index(fqn_to_file)

    seen: set[tuple[str, str, str]] = set()
    edges: list[dict] = []

    for jf in java_files:
        try:
            rel = jf.relative_to(project_root).as_posix()
        except ValueError:
            continue
        try:
            source = jf.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        file_node_id = node_index["by_file"].get(rel)
        local_symbols = {
            sym: nid for (path, sym), nid in node_index["by_symbol"].items()
            if path == rel
        }
        pkg_m = _PKG_RE.search(source)
        package = pkg_m.group(1) if pkg_m else ""
        import_map = _build_import_map(source, package, fqn_to_file, pkg_index)

        for decl_m in _TYPE_DECL_RE.finditer(source):
            type_name = decl_m.group(1)
            type_node_id = local_symbols.get(type_name.lower()) or file_node_id
            if not type_node_id:
                continue

            # Scan forward from end of class name to opening {, skipping generics
            chunk = source[decl_m.end(): decl_m.end() + 800]
            brace_pos = _find_open_brace(chunk)
            if brace_pos == -1:
                continue
            decl_text = chunk[:brace_pos]
            lineno = source[: decl_m.start()].count("\n") + 1

            # extends (one parent for class, multiple for interface)
            ext_m = re.search(
                r'\bextends\s+([\w\s,.<>?]+?)(?=\bimplements\b|\s*$)',
                decl_text, re.DOTALL,
            )
            if ext_m:
                for parent in _split_type_list(ext_m.group(1)):
                    _emit_inherits(
                        parent, import_map, fqn_to_file, node_index,
                        seen, edges, src_id=type_node_id, rel=rel, lineno=lineno,
                    )

            # implements (may be multiple)
            impl_m = re.search(r'\bimplements\s+([\w\s,.<>?]+?)$', decl_text, re.DOTALL)
            if impl_m:
                for iface in _split_type_list(impl_m.group(1)):
                    _emit_inherits(
                        iface, import_map, fqn_to_file, node_index,
                        seen, edges, src_id=type_node_id, rel=rel, lineno=lineno,
                    )

    return edges


def _find_open_brace(s: str) -> int:
    """Return index of first { not nested inside angle brackets, or -1."""
    depth = 0
    for i, c in enumerate(s):
        if c == '<':
            depth += 1
        elif c == '>' and depth > 0:
            depth -= 1
        elif c == '{' and depth == 0:
            return i
    return -1


def _split_type_list(s: str) -> list[str]:
    """Split 'Foo<Bar>, Baz' into simple names ['Foo', 'Baz']."""
    out: list[str] = []
    depth = 0
    buf: list[str] = []
    for c in s:
        if c == '<':
            depth += 1
            buf.append(c)
        elif c == '>':
            if depth > 0:
                depth -= 1
            buf.append(c)
        elif c == ',' and depth == 0:
            _flush(buf, out)
            buf = []
        else:
            buf.append(c)
    _flush(buf, out)
    return out


def _flush(buf: list[str], out: list[str]) -> None:
    raw = "".join(buf).strip()
    m = re.match(r"([\w.]+)", raw)
    if m:
        name = m.group(1).rsplit(".", 1)[-1]
        if name and name[0].isupper():
            out.append(name)


def _emit_inherits(
    type_name: str,
    import_map: dict[str, str],
    fqn_to_file: dict[str, str],
    node_index: dict,
    seen: set,
    edges: list,
    *,
    src_id: str,
    rel: str,
    lineno: int,
) -> None:
    fqn = import_map.get(type_name)
    if not fqn:
        return
    target_file = fqn_to_file.get(fqn)
    if not target_file:
        return
    target_id = (
        node_index["by_symbol"].get((target_file, type_name.lower()))
        or node_index["by_file"].get(target_file)
    )
    if not target_id or target_id == src_id:
        return
    key = (src_id, target_id, "inherits")
    if key in seen:
        return
    seen.add(key)
    edge = {
        "relation": "inherits",
        "source": src_id,
        "target": target_id,
        "source_file": rel,
        "source_location": f"L{lineno}",
        "weight": 1.0,
    }
    annotate_edge(edge, AST_DIRECT, "java_inheritance")
    edges.append(edge)


def _build_pkg_member_index(fqn_to_file: dict[str, str]) -> dict[str, dict[str, str]]:
    """Group FQNs by parent package: {package: {simple_name: fqn}}. Built once so
    _build_import_map does an O(1) lookup instead of scanning every FQN per file.
    Semantics mirror the original `startswith(package + '.')` / `'.' not in simple`
    direct-member condition, with first-FQN-wins over insertion order."""
    index: dict[str, dict[str, str]] = {}
    for fqn in fqn_to_file:
        dot = fqn.rfind(".")
        if dot <= 0:
            continue
        index.setdefault(fqn[:dot], {}).setdefault(fqn[dot + 1 :], fqn)
    return index


def _build_import_map(
    source: str,
    package: str,
    fqn_to_file: dict[str, str],
    pkg_index: dict[str, dict[str, str]] | None = None,
) -> dict[str, str]:
    out: dict[str, str] = {}
    for m in _IMPORT_RE.finditer(source):
        path = m.group(1)
        if path.endswith(".*"):
            continue
        simple = path.rsplit(".", 1)[-1] if "." in path else path
        if simple:
            out[simple] = path
    if package:
        if pkg_index is not None:
            for simple, fqn in pkg_index.get(package, {}).items():
                out.setdefault(simple, fqn)
        else:
            for fqn in fqn_to_file:
                if fqn.startswith(package + "."):
                    simple = fqn[len(package) + 1:]
                    if "." not in simple:
                        out.setdefault(simple, fqn)
    return out


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
        if label.lower().endswith(".java") or label == Path(rel).name:
            by_file.setdefault(rel, nid)
            continue
        symbol = label
        if symbol.endswith("()"):
            symbol = symbol[:-2]
        symbol = symbol.lstrip(".").lower()
        if symbol:
            by_symbol.setdefault((rel, symbol), nid)
    return {"by_file": by_file, "by_symbol": by_symbol}
