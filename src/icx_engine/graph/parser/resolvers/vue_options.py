"""Vue Options API resolver.

Detects:
  * components: { ComponentName } -> renders edge to imported component file
  * extends: BaseComponent -> inherits edge
  * mixins: [MixinA, MixinB] -> calls edge to mixin files
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

_VUE_EXTS: frozenset[str] = frozenset({".vue", ".js", ".ts"})

_OPTIONS_EXPORT_RE = re.compile(
    r"""export\s+default\s*\{""",
    re.MULTILINE,
)

_COMPONENTS_RE = re.compile(
    r"""components\s*:\s*\{([^}]+)\}""",
    re.DOTALL | re.MULTILINE,
)

_EXTENDS_RE = re.compile(
    r"""extends\s*:\s*([A-Za-z_$][\w$]*)""",
    re.MULTILINE,
)

_MIXINS_RE = re.compile(
    r"""mixins\s*:\s*\[([^\]]+)\]""",
    re.DOTALL | re.MULTILINE,
)

_IMPORT_DEFAULT_RE = re.compile(
    r"""import\s+(?:type\s+)?([A-Za-z_$][\w$]*)\s+from\s+['"]([^'"]+)['"]""",
    re.MULTILINE,
)


def extract_vue_options_edges(
    files: Iterable[Path],
    project_root: Path,
    ast_extraction: dict,
) -> list[dict]:
    project_root = project_root.resolve()
    vue_files = [
        Path(f).resolve() for f in files
        if Path(f).suffix in _VUE_EXTS
    ]
    if not vue_files:
        return []

    node_index = _build_node_index(ast_extraction.get("nodes", []), project_root)

    seen: set[tuple[str, str, str]] = set()
    edges: list[dict] = []

    for f in vue_files:
        try:
            rel = f.relative_to(project_root).as_posix()
        except ValueError:
            continue
        try:
            code = f.read_text(encoding="utf-8")
        except OSError:
            continue

        if not _OPTIONS_EXPORT_RE.search(code):
            continue

        file_node_id = node_index["by_file"].get(rel)
        if not file_node_id:
            continue

        import_map = _build_import_map(code, rel, project_root, vue_files)

        # components: { Name } -> renders
        for m in _COMPONENTS_RE.finditer(code):
            block = m.group(1)
            names = re.findall(r"\b([A-Z][A-Za-z0-9_]*)\b", block)
            lineno = code[: m.start()].count("\n") + 1
            for name in names:
                target_rel = import_map.get(name)
                if not target_rel:
                    continue
                target_id = node_index["by_file"].get(target_rel)
                if not target_id or target_id == file_node_id:
                    continue
                _add_edge(file_node_id, target_id, "renders", rel, lineno,
                          seen, edges, "vue_options_resolver")

        # extends: Base -> inherits
        for m in _EXTENDS_RE.finditer(code):
            name = m.group(1)
            target_rel = import_map.get(name)
            if not target_rel:
                continue
            target_id = node_index["by_file"].get(target_rel)
            if not target_id or target_id == file_node_id:
                continue
            lineno = code[: m.start()].count("\n") + 1
            _add_edge(file_node_id, target_id, "inherits", rel, lineno,
                      seen, edges, "vue_options_resolver")

        # mixins: [...] -> calls
        for m in _MIXINS_RE.finditer(code):
            block = m.group(1)
            names = re.findall(r"\b([A-Z][A-Za-z0-9_]*)\b", block)
            lineno = code[: m.start()].count("\n") + 1
            for name in names:
                target_rel = import_map.get(name)
                if not target_rel:
                    continue
                target_id = node_index["by_file"].get(target_rel)
                if not target_id or target_id == file_node_id:
                    continue
                _add_edge(file_node_id, target_id, "calls", rel, lineno,
                          seen, edges, "vue_options_resolver")

    return edges


def _add_edge(
    src_id: str, tgt_id: str, relation: str, rel: str, lineno: int,
    seen: set, edges: list, tag: str,
) -> None:
    key = (src_id, tgt_id, relation)
    if key in seen:
        return
    seen.add(key)
    edge = {
        "relation": relation,
        "source": src_id,
        "target": tgt_id,
        "source_file": rel,
        "source_location": f"L{lineno}",
        "weight": 1.0,
    }
    annotate_edge(edge, FRAMEWORK_RESOLVED, tag)
    edges.append(edge)


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
        for ext in ("", ".vue", ".ts", ".tsx", ".js", ".jsx"):
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
