"""JavaScript / TypeScript import edge resolver.

Parses ES module imports / re-exports / dynamic imports out of every
.js/.jsx/.mjs/.ts/.tsx file and resolves them to project files. Honors
tsconfig.json `compilerOptions.baseUrl` + `paths` aliases when present.
Emits imports edges with AST_DIRECT confidence; downstream tsserver
resolver upgrades call/inheritance edges separately.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Iterable

from icx_engine.graph.parser.confidence import AST_DIRECT, annotate_edge

_log = logging.getLogger(__name__)

_JS_EXTENSIONS: tuple[str, ...] = (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".vue", ".svelte")

# `import ... from '...';`, `import '...';`, `export ... from '...';`
_IMPORT_PATTERN = re.compile(
    r"""^\s*(?:import|export)\s+
        (?:
            [^'";]*?\s+from\s+
        )?
        ['"]([^'"]+)['"]""",
    re.VERBOSE | re.MULTILINE,
)

# `import('...')` dynamic + `require('...')` CJS.
_DYNAMIC_PATTERN = re.compile(
    r"""(?:^|[\s,;=({\[!?:])
        (?:import|require)\s*\(\s*['"]([^'"]+)['"]\s*\)""",
    re.VERBOSE,
)

# Single-line comment / block-comment stripper used before regex matching.
_LINE_COMMENT = re.compile(r"//[^\n]*")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)


def extract_jsts_imports(
    files: Iterable[Path],
    project_root: Path,
    ast_extraction: dict,
) -> list[dict]:
    project_root = project_root.resolve()
    js_files = [
        Path(f).resolve()
        for f in files
        if str(f).lower().endswith(_JS_EXTENSIONS)
    ]
    if not js_files:
        return []

    node_index = _build_node_index(ast_extraction.get("nodes", []), project_root)
    if not node_index["by_file"]:
        return []

    ts_paths_map = _load_tsconfig_paths(project_root)
    project_files = set(node_index["by_file"].keys())

    seen: set[tuple[str, str]] = set()
    edges: list[dict] = []

    for jf in js_files:
        try:
            rel = jf.relative_to(project_root).as_posix()
        except ValueError:
            continue
        try:
            source = jf.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        src_id = node_index["by_file"].get(rel)
        if not src_id:
            continue

        stripped = _strip_comments(source)

        for match in _IMPORT_PATTERN.finditer(stripped):
            _emit(
                match.group(1), rel, project_root, src_id,
                node_index, project_files, ts_paths_map,
                seen, edges, source, match,
            )
        for match in _DYNAMIC_PATTERN.finditer(stripped):
            _emit(
                match.group(1), rel, project_root, src_id,
                node_index, project_files, ts_paths_map,
                seen, edges, source, match,
            )

    return edges


def _emit(
    spec: str, rel: str, project_root: Path, src_id: str,
    node_index: dict, project_files: set[str], ts_paths_map: dict,
    seen: set, edges: list, source: str, match,
) -> None:
    target_rel = _resolve_spec(
        spec, current_rel=rel, project_root=project_root,
        project_files=project_files, ts_paths_map=ts_paths_map,
    )
    if not target_rel:
        return
    tgt_id = node_index["by_file"].get(target_rel)
    if not tgt_id or tgt_id == src_id:
        return
    pair = (src_id, tgt_id)
    if pair in seen:
        return
    seen.add(pair)
    line_no = source.count("\n", 0, match.start()) + 1
    edge = {
        "relation": "imports",
        "source": src_id,
        "target": tgt_id,
        "source_file": rel,
        "source_location": f"L{line_no}",
        "weight": 1.0,
    }
    annotate_edge(edge, AST_DIRECT, "jsts_imports")
    edges.append(edge)


def _strip_comments(src: str) -> str:
    src = _BLOCK_COMMENT.sub("", src)
    src = _LINE_COMMENT.sub("", src)
    return src


def _resolve_spec(
    spec: str, *, current_rel: str, project_root: Path,
    project_files: set[str], ts_paths_map: dict[str, list[str]],
) -> str | None:
    """Resolve an import specifier to a project file path, or None for externals."""
    if not spec:
        return None
    if spec.startswith("."):
        base = Path(current_rel).parent
        target = (project_root / base / spec).resolve()
        try:
            target_rel = target.relative_to(project_root).as_posix()
        except ValueError:
            return None
        return _match_extensionless(target_rel, project_files)

    if ts_paths_map:
        for alias, replacements in ts_paths_map.items():
            if _matches_alias(spec, alias):
                for replacement in replacements:
                    rebuilt = _apply_alias(spec, alias, replacement)
                    target_rel = _normalize(rebuilt)
                    resolved = _match_extensionless(target_rel, project_files)
                    if resolved:
                        return resolved

    return None


def _matches_alias(spec: str, alias: str) -> bool:
    if alias.endswith("/*"):
        return spec.startswith(alias[:-2])
    return spec == alias


def _apply_alias(spec: str, alias: str, replacement: str) -> str:
    if alias.endswith("/*") and replacement.endswith("/*"):
        suffix = spec[len(alias) - 2 :]
        return replacement[:-2] + suffix
    return replacement


def _normalize(p: str) -> str:
    return p.replace("\\", "/").lstrip("./")


def _match_extensionless(target_rel: str, project_files: set[str]) -> str | None:
    target_rel = target_rel.replace("\\", "/")
    if target_rel in project_files:
        return target_rel
    for ext in _JS_EXTENSIONS:
        candidate = target_rel + ext
        if candidate in project_files:
            return candidate
        index_candidate = f"{target_rel}/index{ext}"
        if index_candidate in project_files:
            return index_candidate
    return None


def _load_tsconfig_paths(project_root: Path) -> dict[str, list[str]]:
    """Parse tsconfig.json `compilerOptions.paths` + baseUrl into an alias map."""
    tsconfig = project_root / "tsconfig.json"
    if not tsconfig.is_file():
        return {}
    try:
        raw = tsconfig.read_text(encoding="utf-8")
        raw = re.sub(r"/\*.*?\*/", "", raw, flags=re.DOTALL)
        raw = re.sub(r"^\s*//[^\n]*", "", raw, flags=re.MULTILINE)
        cfg = json.loads(raw)
    except Exception:
        return {}
    opts = (cfg or {}).get("compilerOptions") or {}
    base_url = opts.get("baseUrl") or "."
    paths = opts.get("paths") or {}
    out: dict[str, list[str]] = {}
    for alias, targets in paths.items():
        out[alias] = [
            _join_base(base_url, t) for t in (targets or []) if isinstance(t, str)
        ]
    return out


def _join_base(base_url: str, target: str) -> str:
    base = base_url.strip().lstrip("./")
    target = target.strip()
    if not base or base == ".":
        return target
    if target.endswith("/*") and base:
        return f"{base.rstrip('/')}/{target.lstrip('./')}"
    return f"{base.rstrip('/')}/{target.lstrip('./')}"


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
            label.lower().endswith(ext) for ext in _JS_EXTENSIONS
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
