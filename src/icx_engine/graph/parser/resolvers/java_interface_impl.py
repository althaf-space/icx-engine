"""Java interface-to-implementation resolver.

For every Spring DI edge (depends_on) whose target is an interface file,
emits a companion `injects` edge pointing to the concrete implementation class.

Uses pure regex - no javalang required.
"""
from __future__ import annotations

import re
import logging
from pathlib import Path
from typing import Iterable

from icx_engine.graph.parser.confidence import FRAMEWORK_RESOLVED, annotate_edge

_log = logging.getLogger(__name__)

_INTERFACE_DECL_RE = re.compile(r'\binterface\s+([A-Z]\w*)\b', re.MULTILINE)
_IMPLEMENTS_RE = re.compile(
    r'\bclass\s+\w+\b[^{]*\bimplements\s+([\w\s,<>]+?)(?:\{|extends)',
    re.MULTILINE,
)


def extract_java_interface_impl_edges(
    files: Iterable[Path],
    project_root: Path,
    ast_extraction: dict,
) -> list[dict]:
    project_root = Path(project_root).resolve()
    java_files = [Path(f).resolve() for f in files if str(f).endswith(".java")]
    if not java_files:
        return []

    node_index = _build_node_index(ast_extraction.get("nodes", []), project_root)
    if not node_index["by_file"]:
        return []

    # Pass 1: collect interface names and which files implement them.
    interface_names: set[str] = set()
    impl_map: dict[str, list[str]] = {}  # interface_simple_name -> [impl rel paths]
    iface_file_map: dict[str, str] = {}  # interface_simple_name -> rel path of declaring file

    for jf in java_files:
        try:
            rel = jf.relative_to(project_root).as_posix()
            source = jf.read_text(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            continue

        for m in _INTERFACE_DECL_RE.finditer(source):
            name = m.group(1)
            interface_names.add(name)
            iface_file_map.setdefault(name, rel)

        for m in _IMPLEMENTS_RE.finditer(source):
            for iface_name in _split_top_level(m.group(1)):
                if iface_name and re.match(r'^[A-Z]\w*$', iface_name):
                    impl_map.setdefault(iface_name, []).append(rel)

    # Only keep entries for interfaces actually declared in this project.
    impl_map = {k: v for k, v in impl_map.items() if k in interface_names}
    if not impl_map:
        return []

    # Pass 2: build interface_node_id -> [impl_node_ids] map.
    iface_node_to_impl_nodes: dict[str, list[str]] = {}
    for iface_name, impl_rels in impl_map.items():
        iface_rel = iface_file_map.get(iface_name)
        if not iface_rel:
            continue
        iface_node_id = node_index["by_file"].get(iface_rel)
        if not iface_node_id:
            continue
        impl_ids = [
            node_index["by_file"][r]
            for r in impl_rels
            if r in node_index["by_file"] and node_index["by_file"][r] != iface_node_id
        ]
        if impl_ids:
            iface_node_to_impl_nodes[iface_node_id] = impl_ids

    if not iface_node_to_impl_nodes:
        return []

    # Pass 3: emit injects edges for depends_on edges targeting interface nodes.
    seen: set[tuple[str, str]] = set()
    edges: list[dict] = []

    for existing_edge in ast_extraction.get("edges", []):
        if not isinstance(existing_edge, dict):
            continue
        if existing_edge.get("relation") != "depends_on":
            continue
        src_id = existing_edge.get("source", "")
        tgt_id = existing_edge.get("target", "")
        impl_ids = iface_node_to_impl_nodes.get(tgt_id)
        if not impl_ids:
            continue
        src_rel = existing_edge.get("source_file", "")
        for impl_id in impl_ids:
            key = (src_id, impl_id)
            if key in seen:
                continue
            seen.add(key)
            edge = {
                "relation": "injects",
                "source": src_id,
                "target": impl_id,
                "source_file": src_rel,
                "source_location": existing_edge.get("source_location", ""),
                "weight": 1.0,
            }
            annotate_edge(edge, FRAMEWORK_RESOLVED, "java_interface_impl")
            edges.append(edge)

    return edges


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
        if label.lower().endswith(".java") or label == Path(rel).name:
            by_file.setdefault(rel, nid)
        else:
            symbol = label.rstrip("()").lstrip(".").lower()
            if symbol:
                by_symbol.setdefault((rel, symbol), nid)
    return {"by_file": by_file, "by_symbol": by_symbol}


def _split_top_level(blob: str) -> list[str]:
    """Split a comma-separated list of Java types, ignoring commas inside <>."""
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for ch in blob:
        if ch == "<":
            depth += 1
            current.append(ch)
        elif ch == ">":
            depth = max(depth - 1, 0)
            current.append(ch)
        elif ch == "," and depth == 0:
            name = re.sub(r'<[^>]*>', '', "".join(current)).strip()
            parts.append(name)
            current = []
        else:
            current.append(ch)
    if current:
        name = re.sub(r'<[^>]*>', '', "".join(current)).strip()
        parts.append(name)
    return parts
