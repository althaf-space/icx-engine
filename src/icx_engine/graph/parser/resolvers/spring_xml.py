"""Spring XML config resolver.

Detects bean definitions in Spring XML config files:
  * <bean id="..." class="com.example.ClassName"> -> provides edge (file -> class node)
  * <property name="..." ref="beanId"/> -> depends_on edge (bean -> referenced bean)
  * <constructor-arg ref="beanId"/> -> depends_on edge
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

_XML_EXTS: frozenset[str] = frozenset({".xml"})

_BEAN_RE = re.compile(
    r"""<bean\s[^>]*?id\s*=\s*["']([^"']+)["'][^>]*?class\s*=\s*["']([^"']+)["'][^>]*/?>""",
    re.DOTALL | re.MULTILINE,
)
_BEAN_RE2 = re.compile(
    r"""<bean\s[^>]*?class\s*=\s*["']([^"']+)["'][^>]*?id\s*=\s*["']([^"']+)["'][^>]*/?>""",
    re.DOTALL | re.MULTILINE,
)

_PROPERTY_REF_RE = re.compile(
    r"""<property\s[^>]*?ref\s*=\s*["']([^"']+)["']""",
    re.MULTILINE,
)
_CTOR_REF_RE = re.compile(
    r"""<constructor-arg\s[^>]*?ref\s*=\s*["']([^"']+)["']""",
    re.MULTILINE,
)

_IMPORT_RE = re.compile(
    r"""<import\s+resource\s*=\s*["']([^"']+)["']""",
    re.MULTILINE,
)

_SPRING_XML_MARKERS = (
    "springframework.org/schema/beans",
    "xmlns:context=",
    "<beans ",
)


def extract_spring_xml_edges(
    files: Iterable[Path],
    project_root: Path,
    ast_extraction: dict,
) -> list[dict]:
    project_root = project_root.resolve()
    xml_files = [
        Path(f).resolve() for f in files
        if Path(f).suffix in _XML_EXTS
    ]
    if not xml_files:
        return []

    node_index = _build_node_index(ast_extraction.get("nodes", []), project_root)

    seen: set[tuple[str, str, str]] = set()
    edges: list[dict] = []

    for xml in xml_files:
        try:
            rel = xml.relative_to(project_root).as_posix()
        except ValueError:
            continue
        try:
            code = xml.read_text(encoding="utf-8")
        except OSError:
            continue

        if not any(marker in code for marker in _SPRING_XML_MARKERS):
            continue

        file_node_id = node_index["by_file"].get(rel)

        # Parse bean definitions: id -> class FQN
        bean_id_to_class: dict[str, str] = {}
        bean_id_to_node: dict[str, str] = {}

        for m in _BEAN_RE.finditer(code):
            bean_id, class_fqn = m.group(1), m.group(2)
            bean_id_to_class[bean_id] = class_fqn
        for m in _BEAN_RE2.finditer(code):
            class_fqn, bean_id = m.group(1), m.group(2)
            bean_id_to_class[bean_id] = class_fqn

        for bean_id, class_fqn in bean_id_to_class.items():
            simple_name = class_fqn.rsplit(".", 1)[-1]
            class_node_id = _find_symbol_node(simple_name.lower(), node_index)
            if class_node_id:
                bean_id_to_node[bean_id] = class_node_id
            if file_node_id and class_node_id:
                key = (file_node_id, class_node_id, "provides")
                if key not in seen:
                    seen.add(key)
                    edge = {
                        "relation": "provides",
                        "source": file_node_id,
                        "target": class_node_id,
                        "source_file": rel,
                        "source_location": "L1",
                        "weight": 1.0,
                    }
                    annotate_edge(edge, FRAMEWORK_RESOLVED, "spring_xml_resolver")
                    edges.append(edge)

        # Parse property/constructor-arg refs inside bean blocks
        for pattern, label in ((_PROPERTY_REF_RE, "property"), (_CTOR_REF_RE, "ctor")):
            for m in pattern.finditer(code):
                ref_id = m.group(1)
                target_node_id = bean_id_to_node.get(ref_id)
                if not target_node_id:
                    continue
                # Find which bean this property belongs to by scanning backwards
                preceding = code[: m.start()]
                bean_starts = list(_BEAN_RE.finditer(preceding)) + list(_BEAN_RE2.finditer(preceding))
                if bean_starts:
                    last_bean = bean_starts[-1]
                    bid = last_bean.group(1) if last_bean.re is _BEAN_RE else last_bean.group(2)
                    src_node = bean_id_to_node.get(bid) or file_node_id
                else:
                    src_node = file_node_id
                if not src_node or src_node == target_node_id:
                    continue
                key = (src_node, target_node_id, "depends_on")
                if key in seen:
                    continue
                seen.add(key)
                lineno = code[: m.start()].count("\n") + 1
                edge = {
                    "relation": "depends_on",
                    "source": src_node,
                    "target": target_node_id,
                    "source_file": rel,
                    "source_location": f"L{lineno}",
                    "weight": 1.0,
                }
                annotate_edge(edge, FRAMEWORK_RESOLVED, "spring_xml_resolver")
                edges.append(edge)

    return edges


def _find_symbol_node(symbol_lc: str, node_index: dict) -> str | None:
    for (_, sym), nid in node_index["by_symbol"].items():
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
        if label.lower().endswith(".xml") or label == Path(rel).name:
            by_file.setdefault(rel, nid)
            continue
        symbol = label
        if symbol.endswith("()"):
            symbol = symbol[:-2]
        symbol = symbol.lstrip(".").lower()
        if symbol:
            by_symbol.setdefault((rel, symbol), nid)
    return {"by_file": by_file, "by_symbol": by_symbol}
