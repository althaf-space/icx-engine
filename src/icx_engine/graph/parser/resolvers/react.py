"""React / JSX render-edge resolver.

Walks `.jsx` and `.tsx` files and emits edges for:
  * `<Component .../>` JSX elements -> renders edges from the enclosing
    function/class component to the referenced component
  * `useFoo(...)` hook calls inside React components -> calls edges

The resolver depends on the jsts_imports resolver having already emitted
file-level imports, since render targets are matched to the components
the importing file actually pulled in.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Iterable

from icx_engine.graph.parser.confidence import (
    FRAMEWORK_RESOLVED, annotate_edge,
)
from icx_engine.graph.parser.resolvers.jsts_imports import (
    _resolve_spec,
    _load_tsconfig_paths,
)

_log = logging.getLogger(__name__)

_REACT_EXTS: tuple[str, ...] = (".jsx", ".tsx", ".js")

# JSX opening tag with PascalCase tag name. Excludes lowercase (HTML).
_JSX_TAG = re.compile(r"<([A-Z][A-Za-z0-9_]*)")

# `import { Foo, Bar as Baz } from './x'` -> capture symbol names.
_NAMED_IMPORT = re.compile(
    r"""import\s*
        (?:type\s+)?
        \{\s*([^}]+)\}\s*
        from\s*['"]([^'"]+)['"]""",
    re.VERBOSE,
)

# `import Foo from './x'` -> default import.
_DEFAULT_IMPORT = re.compile(
    r"""import\s+(?:type\s+)?([A-Za-z_][A-Za-z0-9_]*)\s+from\s*['"]([^'"]+)['"]""",
    re.VERBOSE,
)

# `import * as Foo from './x'`.
_NAMESPACE_IMPORT = re.compile(
    r"""import\s*\*\s*as\s+([A-Za-z_][A-Za-z0-9_]*)\s+from\s*['"]([^'"]+)['"]""",
    re.VERBOSE,
)

# Hook call: `useFoo(` at expression position.
_HOOK_CALL = re.compile(r"\b(use[A-Z][A-Za-z0-9_]*)\s*\(")

# `const Foo = lazy(() => import('./Bar'))` or `React.lazy(() => import('./Bar'))`.
_LAZY_IMPORT = re.compile(
    r"""(?:const|let|var)\s+([A-Z][A-Za-z0-9_]*)\s*=\s*
        (?:React\.)?lazy\s*\(\s*\(\s*\)\s*=>\s*
        import\s*\(\s*['"]([^'"]+)['"]\s*\)\s*\)""",
    re.VERBOSE,
)


def extract_react_edges(
    files: Iterable[Path],
    project_root: Path,
    ast_extraction: dict,
) -> list[dict]:
    project_root = project_root.resolve()
    jsx_files = [
        Path(f).resolve()
        for f in files
        if str(f).lower().endswith(_REACT_EXTS)
    ]
    if not jsx_files:
        return []

    node_index = _build_node_index(ast_extraction.get("nodes", []), project_root)
    if not node_index["by_file"]:
        return []

    project_files = set(node_index["by_file"].keys())
    ts_paths_map = _load_tsconfig_paths(project_root)

    seen: set[tuple[str, str, str]] = set()
    edges: list[dict] = []

    for jf in jsx_files:
        try:
            rel = jf.relative_to(project_root).as_posix()
        except ValueError:
            continue
        try:
            source = jf.read_text(encoding="utf-8", errors="replace")
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
            source, rel, project_root, project_files,
        )

        for match in _JSX_TAG.finditer(source):
            symbol = match.group(1)
            target_file = symbol_to_file.get(symbol)
            if not target_file:
                continue
            target_id = (
                node_index["by_symbol"].get((target_file, symbol.lower()))
                or node_index["by_file"].get(target_file)
            )
            src_id = _enclosing_function_node(
                source, match.start(), local_symbols, file_node_id,
            )
            if not target_id or not src_id or target_id == src_id:
                continue
            key = (src_id, target_id, "renders")
            if key in seen:
                continue
            seen.add(key)
            line_no = source.count("\n", 0, match.start()) + 1
            edge = {
                "relation": "renders",
                "source": src_id,
                "target": target_id,
                "source_file": rel,
                "source_location": f"L{line_no}",
                "weight": 1.0,
            }
            annotate_edge(edge, FRAMEWORK_RESOLVED, "react_resolver")
            edges.append(edge)

        for match in _HOOK_CALL.finditer(source):
            hook_name = match.group(1)
            target_file = symbol_to_file.get(hook_name)
            if not target_file:
                continue
            target_id = (
                node_index["by_symbol"].get((target_file, hook_name.lower()))
                or node_index["by_file"].get(target_file)
            )
            src_id = _enclosing_function_node(
                source, match.start(), local_symbols, file_node_id,
            )
            if not target_id or not src_id or target_id == src_id:
                continue
            key = (src_id, target_id, "calls")
            if key in seen:
                continue
            seen.add(key)
            line_no = source.count("\n", 0, match.start()) + 1
            edge = {
                "relation": "calls",
                "source": src_id,
                "target": target_id,
                "source_file": rel,
                "source_location": f"L{line_no}",
                "weight": 1.0,
            }
            annotate_edge(edge, FRAMEWORK_RESOLVED, "react_resolver")
            edges.append(edge)

        for match in _LAZY_IMPORT.finditer(source):
            spec = match.group(2)
            target_rel = _resolve_spec(
                spec, current_rel=rel, project_root=project_root,
                project_files=project_files, ts_paths_map=ts_paths_map,
            )
            if not target_rel:
                continue
            tgt_id = node_index["by_file"].get(target_rel)
            if not tgt_id or tgt_id == file_node_id:
                continue
            key = (file_node_id, tgt_id, "lazy_loads")
            if key in seen:
                continue
            seen.add(key)
            line_no = source.count("\n", 0, match.start()) + 1
            edge = {
                "relation": "lazy_loads",
                "source": file_node_id,
                "target": tgt_id,
                "source_file": rel,
                "source_location": f"L{line_no}",
                "weight": 1.0,
            }
            annotate_edge(edge, FRAMEWORK_RESOLVED, "react_lazy")
            edges.append(edge)

    return edges


_BARREL_NAMES: frozenset[str] = frozenset({"index.js", "index.jsx", "index.ts", "index.tsx"})

_REEXPORT_NAMED = re.compile(
    r"export\s*\{([^}]+)\}\s*from\s*[\"']([^\"']+)[\"']"
)
_REEXPORT_DEFAULT_AS = re.compile(
    r"export\s*\{\s*default\s+as\s+(\w+)\s*\}\s*from\s*[\"']([^\"']+)[\"']"
)


def _trace_barrel(
    symbol: str,
    barrel_rel: str,
    project_root: Path,
    project_files: set[str],
    ts_paths_map: dict,
    depth: int = 0,
) -> str | None:
    """Follow re-export chains one level to find the actual file for symbol."""
    if depth > 2:
        return None
    barrel_path = project_root / barrel_rel
    try:
        src = barrel_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    from icx_engine.graph.parser.resolvers.jsts_imports import _resolve_spec
    for m in _REEXPORT_NAMED.finditer(src):
        names_blob, spec = m.group(1), m.group(2)
        names = [p.strip().split()[0] for p in names_blob.split(",") if p.strip()]
        if symbol not in names:
            continue
        resolved = _resolve_spec(
            spec, current_rel=barrel_rel, project_root=project_root,
            project_files=project_files, ts_paths_map=ts_paths_map,
        )
        if not resolved:
            continue
        if Path(resolved).name in _BARREL_NAMES:
            deeper = _trace_barrel(symbol, resolved, project_root, project_files, ts_paths_map, depth + 1)
            return deeper or resolved
        return resolved
    for m in _REEXPORT_DEFAULT_AS.finditer(src):
        local_name, spec = m.group(1), m.group(2)
        if local_name != symbol:
            continue
        from icx_engine.graph.parser.resolvers.jsts_imports import _resolve_spec
        resolved = _resolve_spec(
            spec, current_rel=barrel_rel, project_root=project_root,
            project_files=project_files, ts_paths_map=ts_paths_map,
        )
        if resolved:
            return resolved
    return None


def _build_symbol_to_file(
    source: str, current_rel: str, project_root: Path, project_files: set[str],
) -> dict[str, str]:
    """Walk the file's imports and produce {imported_name: relative_path} when
    the path resolves to a project file."""
    from icx_engine.graph.parser.resolvers.jsts_imports import (
        _resolve_spec, _load_tsconfig_paths,
    )
    ts_paths_map = _load_tsconfig_paths(project_root)

    out: dict[str, str] = {}
    for match in _NAMED_IMPORT.finditer(source):
        names_blob, spec = match.group(1), match.group(2)
        target_rel = _resolve_spec(
            spec, current_rel=current_rel, project_root=project_root,
            project_files=project_files, ts_paths_map=ts_paths_map,
        )
        if not target_rel:
            continue
        is_barrel = Path(target_rel).name in _BARREL_NAMES
        for part in names_blob.split(","):
            part = part.strip()
            if not part:
                continue
            alias_match = re.match(r"([A-Za-z_][A-Za-z0-9_]*)\s+as\s+([A-Za-z_][A-Za-z0-9_]*)", part)
            if alias_match:
                local_name = alias_match.group(2)
                original_name = alias_match.group(1)
            else:
                local_name = part.split()[0]
                original_name = local_name
            if not local_name:
                continue
            if is_barrel and original_name and original_name[0].isupper():
                traced = _trace_barrel(original_name, target_rel, project_root, project_files, ts_paths_map)
                out[local_name] = traced or target_rel
            else:
                out[local_name] = target_rel

    for match in _DEFAULT_IMPORT.finditer(source):
        if "* as" in source[max(0, match.start() - 16) : match.end()]:
            continue
        local_name, spec = match.group(1), match.group(2)
        target_rel = _resolve_spec(
            spec, current_rel=current_rel, project_root=project_root,
            project_files=project_files, ts_paths_map=ts_paths_map,
        )
        if target_rel:
            out[local_name] = target_rel

    for match in _NAMESPACE_IMPORT.finditer(source):
        local_name, spec = match.group(1), match.group(2)
        target_rel = _resolve_spec(
            spec, current_rel=current_rel, project_root=project_root,
            project_files=project_files, ts_paths_map=ts_paths_map,
        )
        if target_rel:
            out[local_name] = target_rel

    return out


def _enclosing_function_node(
    source: str, offset: int, local_symbols: dict[str, str], file_node_id: str,
) -> str | None:
    """Best-effort: scan backwards for the nearest `function Foo`,
    `const Foo =`, `export default function`, or class declaration whose
    name matches a known local symbol; fall back to the file node."""
    prefix = source[:offset]
    # Patterns ordered from most specific to least.
    patterns = (
        re.compile(r"function\s+([A-Z][A-Za-z0-9_]*)\b"),
        re.compile(r"const\s+([A-Z][A-Za-z0-9_]*)\s*=\s*\("),
        re.compile(r"const\s+([A-Z][A-Za-z0-9_]*)\s*:\s*[^=]+=\s*\("),
        re.compile(r"export\s+default\s+function\s+([A-Z][A-Za-z0-9_]*)"),
        re.compile(r"class\s+([A-Z][A-Za-z0-9_]*)\b"),
        re.compile(r"function\s+(use[A-Z][A-Za-z0-9_]*)\b"),
        re.compile(r"const\s+(use[A-Z][A-Za-z0-9_]*)\s*=\s*\("),
    )
    best: tuple[int, str] | None = None
    for pattern in patterns:
        for match in pattern.finditer(prefix):
            if best is None or match.start() > best[0]:
                best = (match.start(), match.group(1))
    if best is not None:
        symbol = best[1].lower()
        if symbol in local_symbols:
            return local_symbols[symbol]
    return file_node_id


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
        if label == Path(rel).name or any(
            label.lower().endswith(ext) for ext in (".js", ".jsx", ".ts", ".tsx", ".vue", ".svelte")
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
