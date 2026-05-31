"""Redux resolver.

Detects:
  * configureStore({ reducer: { key: reducerFn } }) -> has_relation edges to slice files
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

_CONFIGURE_STORE_RE = re.compile(
    r"configureStore\s*\(",
    re.MULTILINE,
)

_IMPORT_DEFAULT_RE = re.compile(
    r"""import\s+(?:type\s+)?([A-Za-z_$][\w$]*)\s+from\s+['"]([^'"]+)['"]""",
    re.MULTILINE,
)


def extract_redux_edges(
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

        if not _CONFIGURE_STORE_RE.search(code):
            continue

        file_node_id = node_index["by_file"].get(rel)
        if not file_node_id:
            continue

        import_map = _build_import_map(code, rel, project_root, jsts_files)
        reducer_names = _extract_reducer_value_names(code)

        for name in reducer_names:
            target_rel = import_map.get(name)
            if not target_rel:
                continue
            target_id = node_index["by_file"].get(target_rel)
            if not target_id or target_id == file_node_id:
                continue
            key = (file_node_id, target_id, "has_relation")
            if key in seen:
                continue
            seen.add(key)
            edge = {
                "relation": "has_relation",
                "source": file_node_id,
                "target": target_id,
                "source_file": rel,
                "source_location": "L1",
                "weight": 1.0,
            }
            annotate_edge(edge, FRAMEWORK_RESOLVED, "redux_resolver")
            edges.append(edge)

    return edges


def _extract_reducer_value_names(code: str) -> list[str]:
    m = re.search(r"reducer\s*:\s*\{([^}]+)\}", code, re.DOTALL)
    if not m:
        return []
    block = m.group(1)
    return re.findall(r":\s*([A-Za-z_$][\w$]*)", block)


def _build_import_map(
    code: str,
    rel: str,
    project_root: Path,
    all_files: list[Path],
) -> dict[str, str]:
    result: dict[str, str] = {}
    dir_path = (project_root / rel).parent
    for m in _IMPORT_DEFAULT_RE.finditer(code):
        identifier = m.group(1)
        import_path = m.group(2)
        if not import_path.startswith("."):
            continue
        for ext in ("", ".ts", ".tsx", ".js", ".jsx"):
            candidate = (dir_path / (import_path + ext)).resolve()
            try:
                candidate_rel = candidate.relative_to(project_root).as_posix()
                if (project_root / candidate_rel).is_file():
                    result[identifier] = candidate_rel
                    break
            except ValueError:
                continue
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
