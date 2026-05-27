"""GraphQL resolver mapper.

Detects:
  * Resolver files (named *.resolver.ts or containing resolver map objects)
  * new ServiceClass() instantiation in resolver files -> depends_on edges to service files
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Iterable

from icx_engine.graph.parser.confidence import (
    FRAMEWORK_RESOLVED,
    annotate_edge,
)

_log = logging.getLogger(__name__)

_JSTS_EXTS: frozenset[str] = frozenset({".ts", ".tsx", ".js", ".jsx"})

_RESOLVER_OBJ_RE = re.compile(
    r"""(?:export\s+(?:const|default)\s+)?resolvers\s*=\s*\{""",
    re.MULTILINE,
)

_IMPORT_DEFAULT_RE = re.compile(
    r"""import\s+(?:type\s+)?([A-Za-z_$][\w$]*)\s+from\s+['"]([^'"]+)['"]""",
    re.MULTILINE,
)
_IMPORT_NAMED_RE = re.compile(
    r"""import\s+(?:type\s+)?\{\s*([^}]+)\}\s*from\s+['"]([^'"]+)['"]""",
    re.MULTILINE,
)

_NEW_INSTANCE_RE = re.compile(
    r"""new\s+([A-Z][A-Za-z0-9_]*)\s*\(""",
    re.MULTILINE,
)


def extract_graphql_edges(
    files: Iterable[Path],
    project_root: Path,
    ast_extraction: dict,
) -> list[dict]:
    project_root = project_root.resolve()
    jsts_files = [
        Path(f).resolve() for f in files
        if Path(f).suffix in _JSTS_EXTS
    ]
    if not jsts_files:
        return []

    node_index = _build_node_index(ast_extraction.get("nodes", []), project_root)

    seen: set[tuple[str, str, str]] = set()
    edges: list[dict] = []

    for f in jsts_files:
        try:
            rel = f.relative_to(project_root).as_posix()
        except ValueError:
            continue
        try:
            code = f.read_text(encoding="utf-8")
        except OSError:
            continue

        is_resolver_file = (
            "resolver" in f.name.lower()
            or _RESOLVER_OBJ_RE.search(code) is not None
        )
        if not is_resolver_file:
            continue

        file_node_id = node_index["by_file"].get(rel)
        if not file_node_id:
            continue

        import_map = _build_import_map(code, rel, project_root, jsts_files)

        for m in _NEW_INSTANCE_RE.finditer(code):
            class_name = m.group(1)
            target_rel = import_map.get(class_name)
            if not target_rel:
                continue
            target_id = node_index["by_file"].get(target_rel)
            if not target_id or target_id == file_node_id:
                continue
            key = (file_node_id, target_id, "depends_on")
            if key in seen:
                continue
            seen.add(key)
            lineno = code[: m.start()].count("\n") + 1
            edge = {
                "relation": "depends_on",
                "source": file_node_id,
                "target": target_id,
                "source_file": rel,
                "source_location": f"L{lineno}",
                "weight": 1.0,
            }
            annotate_edge(edge, FRAMEWORK_RESOLVED, "graphql_resolver")
            edges.append(edge)

    return edges


def _build_import_map(
    code: str,
    rel: str,
    project_root: Path,
    all_files: list[Path],
) -> dict[str, str]:
    result: dict[str, str] = {}
    dir_path = (project_root / rel).parent

    def _resolve(import_path: str, identifier: str) -> None:
        if not import_path.startswith("."):
            return
        for ext in ("", ".ts", ".tsx", ".js", ".jsx"):
            candidate = (dir_path / (import_path + ext)).resolve()
            try:
                candidate_rel = candidate.relative_to(project_root).as_posix()
                if (project_root / candidate_rel).is_file():
                    result[identifier] = candidate_rel
                    return
            except ValueError:
                continue

    for m in _IMPORT_DEFAULT_RE.finditer(code):
        _resolve(m.group(2), m.group(1))

    for m in _IMPORT_NAMED_RE.finditer(code):
        import_path = m.group(2)
        for name in re.split(r"[,\s]+", m.group(1)):
            name = name.strip().rstrip(",")
            if name:
                _resolve(import_path, name)

    return result


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
        ext = Path(rel).suffix
        if ext in {".ts", ".tsx", ".js", ".jsx", ".vue"} and label == Path(rel).name:
            by_file.setdefault(rel, nid)
            continue
        if label.lower().endswith((".ts", ".tsx", ".js", ".jsx", ".vue")):
            by_file.setdefault(rel, nid)
            continue
        symbol = label
        if symbol.endswith("()"):
            symbol = symbol[:-2]
        symbol = symbol.lstrip(".").lower()
        if symbol:
            by_symbol.setdefault((rel, symbol), nid)
    return {"by_file": by_file, "by_symbol": by_symbol}
