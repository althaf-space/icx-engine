"""SvelteKit resolver.

Detects:
  * Component usage in Svelte templates -> renders edge
  * SvelteKit file-based route conventions (+page.server.ts load/actions) -> routes edge
  * Store imports in Svelte files -> calls edge
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

_SVELTE_EXTS: tuple[str, ...] = (".svelte", ".ts", ".js")

_COMPONENT_TAG = re.compile(r"<([A-Z][A-Za-z0-9_]*)")

_NAMED_IMPORT = re.compile(
    r"""import\s*(?:type\s+)?\{\s*([^}]+)\}\s*from\s*['"]([^'"]+)['"]""",
    re.VERBOSE,
)
_DEFAULT_IMPORT = re.compile(
    r"""import\s+(?:type\s+)?([A-Za-z_][A-Za-z0-9_]*)\s+from\s*['"]([^'"]+)['"]""",
    re.VERBOSE,
)

_LOAD_EXPORT = re.compile(
    r"export\s+(?:const\s+load|async\s+function\s+load|function\s+load)",
)
_ACTIONS_EXPORT = re.compile(
    r"export\s+const\s+actions",
)


def extract_svelte_edges(
    files: Iterable[Path],
    project_root: Path,
    ast_extraction: dict,
) -> list[dict]:
    project_root = project_root.resolve()
    all_files = [Path(f).resolve() for f in files if str(f).lower().endswith(_SVELTE_EXTS)]
    if not all_files:
        return []

    svelte_files = [f for f in all_files if str(f).lower().endswith(".svelte")]
    server_files = [f for f in all_files if _is_sveltekit_server_file(f)]
    if not svelte_files and not server_files:
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

    for sf in svelte_files:
        try:
            rel = sf.relative_to(project_root).as_posix()
        except ValueError:
            continue
        try:
            source = sf.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        file_node_id = node_index["by_file"].get(rel)
        if not file_node_id:
            continue

        symbol_to_file = _build_symbol_to_file(
            source, rel, project_root, project_files, ts_paths_map, _resolve_spec,
        )

        template = re.sub(r"<script[^>]*>.*?</script>", "", source, flags=re.DOTALL | re.IGNORECASE)
        for match in _COMPONENT_TAG.finditer(template):
            symbol = match.group(1)
            target_file = symbol_to_file.get(symbol)
            if not target_file:
                continue
            target_id = node_index["by_file"].get(target_file)
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
            annotate_edge(edge, FRAMEWORK_RESOLVED, "svelte_resolver")
            edges.append(edge)

    for sf in server_files:
        try:
            rel = sf.relative_to(project_root).as_posix()
        except ValueError:
            continue
        try:
            source = sf.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        file_node_id = node_index["by_file"].get(rel)
        if not file_node_id:
            continue
        local_symbols = {
            sym: nid for (path, sym), nid in node_index["by_symbol"].items()
            if path == rel
        }

        if _LOAD_EXPORT.search(source):
            load_id = local_symbols.get("load")
            if load_id:
                key = (file_node_id, load_id, "routes")
                if key not in seen:
                    seen.add(key)
                    edge = {
                        "relation": "routes",
                        "source": file_node_id,
                        "target": load_id,
                        "source_file": rel,
                        "source_location": "L1",
                        "weight": 1.0,
                    }
                    annotate_edge(edge, FRAMEWORK_RESOLVED, "sveltekit_resolver")
                    edges.append(edge)

        if _ACTIONS_EXPORT.search(source):
            actions_id = local_symbols.get("actions")
            if actions_id:
                key = (file_node_id, actions_id, "routes")
                if key not in seen:
                    seen.add(key)
                    edge = {
                        "relation": "routes",
                        "source": file_node_id,
                        "target": actions_id,
                        "source_file": rel,
                        "source_location": "L1",
                        "weight": 1.0,
                    }
                    annotate_edge(edge, FRAMEWORK_RESOLVED, "sveltekit_resolver")
                    edges.append(edge)

    return edges


def _is_sveltekit_server_file(f: Path) -> bool:
    name = f.name.lower()
    return "+page.server" in name or "+layout.server" in name or "+server" in name


def _build_symbol_to_file(
    source: str, current_rel: str, project_root: Path,
    project_files: set[str], ts_paths_map: dict, resolve_spec,
) -> dict[str, str]:
    out: dict[str, str] = {}
    for match in _DEFAULT_IMPORT.finditer(source):
        local_name, spec = match.group(1), match.group(2)
        target_rel = resolve_spec(
            spec, current_rel=current_rel, project_root=project_root,
            project_files=project_files, ts_paths_map=ts_paths_map,
        )
        if target_rel:
            out[local_name] = target_rel

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
            label.lower().endswith(ext) for ext in (".js", ".ts", ".svelte")
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
