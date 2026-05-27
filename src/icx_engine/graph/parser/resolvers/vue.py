"""Vue.js resolver.

Detects:
  * <script setup> component imports used in template -> renders edge
  * Pinia defineStore() composable usage -> calls edge
  * Vue Router route definitions -> routes edge
  * Composable calls (useXxx pattern) -> calls edge
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Iterable

from icx_engine.graph.parser.confidence import (
    FRAMEWORK_RESOLVED, annotate_edge,
)

_log = logging.getLogger(__name__)

_VUE_EXTS: tuple[str, ...] = (".vue", ".ts", ".tsx", ".js", ".jsx")

_COMPONENT_TAG = re.compile(r"<([A-Z][A-Za-z0-9_]*)")

_NAMED_IMPORT = re.compile(
    r"""import\s*(?:type\s+)?\{\s*([^}]+)\}\s*from\s*['"]([^'"]+)['"]""",
    re.VERBOSE,
)
_DEFAULT_IMPORT = re.compile(
    r"""import\s+(?:type\s+)?([A-Za-z_][A-Za-z0-9_]*)\s+from\s*['"]([^'"]+)['"]""",
    re.VERBOSE,
)

_COMPOSABLE_CALL = re.compile(r"\b(use[A-Z][A-Za-z0-9_]*)\s*\(")

_ROUTE_COMPONENT = re.compile(
    r"""component\s*:\s*([A-Za-z_][A-Za-z0-9_]*)""",
)

_DEFINE_STORE = re.compile(
    r"""defineStore\s*\(\s*['"][^'"]+['"]""",
)


def extract_vue_edges(
    files: Iterable[Path],
    project_root: Path,
    ast_extraction: dict,
) -> list[dict]:
    project_root = project_root.resolve()
    all_files = [Path(f).resolve() for f in files if str(f).lower().endswith(_VUE_EXTS)]
    if not all_files:
        return []

    vue_files = [f for f in all_files if str(f).lower().endswith(".vue")]
    if not vue_files and not any(_is_router_file(f) for f in all_files):
        return []

    node_index = _build_node_index(ast_extraction.get("nodes", []), project_root)
    if not node_index["by_file"]:
        return []

    project_files = set(node_index["by_file"].keys())

    try:
        from icx_engine.graph.parser.resolvers.jsts_imports import (
            _resolve_spec, _load_tsconfig_paths,
        )
        ts_paths_map = _load_tsconfig_paths(project_root)
    except ImportError:
        return []

    seen: set[tuple[str, str, str]] = set()
    edges: list[dict] = []

    for vf in all_files:
        try:
            rel = vf.relative_to(project_root).as_posix()
        except ValueError:
            continue
        try:
            source = vf.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        file_node_id = node_index["by_file"].get(rel)
        if not file_node_id:
            continue
        local_symbols = {
            sym: nid for (path, sym), nid in node_index["by_symbol"].items()
            if path == rel
        }

        symbol_to_file = _build_symbol_to_file(
            source, rel, project_root, project_files, ts_paths_map,
            _resolve_spec,
        )

        if str(vf).lower().endswith(".vue"):
            _extract_renders(
                source, rel, file_node_id, local_symbols,
                symbol_to_file, node_index, seen, edges,
            )

        _extract_composable_calls(
            source, rel, file_node_id, local_symbols,
            symbol_to_file, node_index, seen, edges,
        )

        if _is_router_file(vf):
            _extract_router_routes(
                source, rel, file_node_id, local_symbols,
                symbol_to_file, node_index, seen, edges,
            )

    return edges


def _is_router_file(f: Path) -> bool:
    name = f.name.lower()
    return name in ("index.ts", "index.js", "router.ts", "router.js") and "router" in str(f).lower()


def _extract_renders(
    source: str, rel: str, file_node_id: str,
    local_symbols: dict, symbol_to_file: dict,
    node_index: dict, seen: set, edges: list,
) -> None:
    template_match = re.search(r"<template[^>]*>(.*?)</template>", source, re.DOTALL)
    if not template_match:
        return
    template = template_match.group(1)

    for match in _COMPONENT_TAG.finditer(template):
        symbol = match.group(1)
        target_file = symbol_to_file.get(symbol)
        if not target_file:
            continue
        target_id = (
            node_index["by_symbol"].get((target_file, symbol.lower()))
            or node_index["by_file"].get(target_file)
        )
        if not target_id or target_id == file_node_id:
            continue
        key = (file_node_id, target_id, "renders")
        if key in seen:
            continue
        seen.add(key)
        edge = {
            "relation": "renders",
            "source": file_node_id,
            "target": target_id,
            "source_file": rel,
            "source_location": "",
            "weight": 1.0,
        }
        annotate_edge(edge, FRAMEWORK_RESOLVED, "vue_resolver")
        edges.append(edge)


def _extract_composable_calls(
    source: str, rel: str, file_node_id: str,
    local_symbols: dict, symbol_to_file: dict,
    node_index: dict, seen: set, edges: list,
) -> None:
    for match in _COMPOSABLE_CALL.finditer(source):
        hook_name = match.group(1)
        target_file = symbol_to_file.get(hook_name)
        if not target_file:
            continue
        target_id = (
            node_index["by_symbol"].get((target_file, hook_name.lower()))
            or node_index["by_file"].get(target_file)
        )
        if not target_id or target_id == file_node_id:
            continue
        key = (file_node_id, target_id, "calls")
        if key in seen:
            continue
        seen.add(key)
        line_no = source.count("\n", 0, match.start()) + 1
        edge = {
            "relation": "calls",
            "source": file_node_id,
            "target": target_id,
            "source_file": rel,
            "source_location": f"L{line_no}",
            "weight": 1.0,
        }
        annotate_edge(edge, FRAMEWORK_RESOLVED, "vue_resolver")
        edges.append(edge)


def _extract_router_routes(
    source: str, rel: str, file_node_id: str,
    local_symbols: dict, symbol_to_file: dict,
    node_index: dict, seen: set, edges: list,
) -> None:
    for match in _ROUTE_COMPONENT.finditer(source):
        symbol = match.group(1)
        target_file = symbol_to_file.get(symbol)
        if not target_file:
            continue
        target_id = (
            node_index["by_symbol"].get((target_file, symbol.lower()))
            or node_index["by_file"].get(target_file)
        )
        if not target_id or target_id == file_node_id:
            continue
        key = (file_node_id, target_id, "routes")
        if key in seen:
            continue
        seen.add(key)
        line_no = source.count("\n", 0, match.start()) + 1
        edge = {
            "relation": "routes",
            "source": file_node_id,
            "target": target_id,
            "source_file": rel,
            "source_location": f"L{line_no}",
            "weight": 1.0,
        }
        annotate_edge(edge, FRAMEWORK_RESOLVED, "vue_router")
        edges.append(edge)


def _build_symbol_to_file(
    source: str, current_rel: str, project_root: Path,
    project_files: set[str], ts_paths_map: dict, resolve_spec,
) -> dict[str, str]:
    out: dict[str, str] = {}
    for match in _NAMED_IMPORT.finditer(source):
        names_blob, spec = match.group(1), match.group(2)
        target_rel = resolve_spec(
            spec, current_rel=current_rel, project_root=project_root,
            project_files=project_files, ts_paths_map=ts_paths_map,
        )
        if not target_rel:
            continue
        for part in names_blob.split(","):
            part = part.strip()
            if not part:
                continue
            alias_match = re.match(
                r"([A-Za-z_][A-Za-z0-9_]*)\s+as\s+([A-Za-z_][A-Za-z0-9_]*)", part,
            )
            local_name = alias_match.group(2) if alias_match else part.split()[0]
            if local_name:
                out[local_name] = target_rel

    for match in _DEFAULT_IMPORT.finditer(source):
        local_name, spec = match.group(1), match.group(2)
        target_rel = resolve_spec(
            spec, current_rel=current_rel, project_root=project_root,
            project_files=project_files, ts_paths_map=ts_paths_map,
        )
        if target_rel:
            out[local_name] = target_rel
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
        if label == Path(rel).name or any(
            label.lower().endswith(ext) for ext in (".js", ".jsx", ".ts", ".tsx", ".vue")
        ):
            by_file.setdefault(rel, nid)
            continue
        symbol = label
        if symbol.endswith("()"):
            symbol = symbol[:-2]
        symbol = symbol.lstrip(".").lower()
        if symbol:
            by_symbol.setdefault((rel, symbol), nid)
    return {"by_file": by_file, "by_symbol": by_symbol}
