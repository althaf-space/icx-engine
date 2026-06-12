"""TypeScript / JavaScript LSP resolver.

Uses typescript-language-server to resolve cross-file references with
compiler-grade accuracy. Server lifecycle (install/version-track/kill/reinstall)
is managed by lsp_manager.py.

Supplements jsts_imports.py with:
  - Barrel re-export chains (A -> index.ts -> actual definition in B)
  - tsconfig path alias resolution verified by the real TS compiler
  - Cross-file call edges with type-accurate targets

If Node is not installed: silent no-op.
If typescript-language-server was installed for a different Node version: auto-reinstalls.
"""
from __future__ import annotations

import bisect
import importlib
import logging
from collections import defaultdict
from pathlib import Path
from typing import Iterable

from icx_engine.graph.parser.confidence import LSP_RESOLVED, annotate_edge
from icx_engine.graph.parser.lsp_manager import TS_LS, ensure_server, record_pid

_log = logging.getLogger(__name__)

_JS_EXTS: frozenset[str] = frozenset({
    ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".vue", ".svelte",
})

_MAX_POSITIONS_PER_FILE = 100
_CIRCUIT_BREAKER_LIMIT = 5


def _build_node_index(nodes: list[dict], project_root: Path) -> dict:
    proj_str = str(project_root).replace("\\", "/")
    by_file: dict[str, str] = {}
    by_symbol: dict[tuple[str, str], str] = {}

    for n in nodes:
        nid = n.get("id") or n.get("label")
        if not nid:
            continue
        src = (n.get("source_file") or "").replace("\\", "/").strip()
        label = (n.get("label") or "").strip()
        if not src:
            continue
        if src.startswith(proj_str + "/"):
            rel = src[len(proj_str) + 1:]
        elif src.startswith(proj_str):
            rel = src[len(proj_str):].lstrip("/")
        else:
            continue
        if label.lower().endswith(Path(rel).suffix) or label == Path(rel).name:
            by_file.setdefault(rel, nid)
            continue
        sym = label.rstrip("()").lstrip(".").lower()
        if sym:
            by_symbol.setdefault((rel, sym), nid)

    return {"by_file": by_file, "by_symbol": by_symbol}


def _build_pos_index(nodes: list[dict], project_root: Path) -> dict[str, list[tuple[int, str]]]:
    proj_str = str(project_root).replace("\\", "/")
    result: dict[str, list[tuple[int, str]]] = defaultdict(list)

    for n in nodes:
        nid = n.get("id")
        src = (n.get("source_file") or "").replace("\\", "/").strip()
        loc = n.get("source_location", "")
        if not nid or not src:
            continue
        if src.startswith(proj_str + "/"):
            rel = src[len(proj_str) + 1:]
        elif src.startswith(proj_str):
            rel = src[len(proj_str):].lstrip("/")
        else:
            continue
        try:
            line = int(loc.lstrip("L")) if loc.startswith("L") else 1
        except ValueError:
            line = 1
        result[rel].append((line, nid))

    return {k: sorted(v) for k, v in result.items()}


def _pos_to_node_id(pos_index: dict[str, list[tuple[int, str]]], rel_path: str, lsp_line: int) -> str | None:
    entries = pos_index.get(rel_path)
    if not entries:
        return None
    line_1 = lsp_line + 1
    idx = bisect.bisect_right(entries, (line_1, "\xff")) - 1
    return entries[idx][1] if idx >= 0 else None


def _norm(p: str) -> str:
    return p.replace("\\", "/")


def _collect_positions(tree_root) -> list[tuple[int, int, str]]:
    """Walk tree-sitter root and return (row, col, kind) to query via LSP.

    kind "import": named import specifier  kind "call": call expression callee.
    """
    positions: list[tuple[int, int, str]] = []

    def _walk(node) -> None:
        if len(positions) >= _MAX_POSITIONS_PER_FILE:
            return
        t = node.type
        if t == "import_specifier":
            name_node = node.child_by_field_name("name")
            if name_node is not None:
                row, col = name_node.start_point
                positions.append((row, col, "import"))
        elif t == "call_expression":
            func = node.child_by_field_name("function")
            if func is not None:
                if func.type == "member_expression":
                    prop = func.child_by_field_name("property")
                    if prop is not None:
                        row, col = prop.start_point
                        positions.append((row, col, "call"))
                elif func.type == "identifier":
                    row, col = func.start_point
                    positions.append((row, col, "call"))
        for child in node.children:
            _walk(child)

    _walk(tree_root)
    return positions


def extract_ts_lsp_edges(
    files: Iterable[Path],
    project_root: Path,
    ast_extraction: dict,
) -> list[dict]:
    """Resolve TS/JS cross-file references using typescript-language-server."""
    project_root = project_root.resolve()
    js_files = [Path(f).resolve() for f in files if Path(f).suffix.lower() in _JS_EXTS]
    if not js_files:
        return []

    nodes_list = ast_extraction.get("nodes", [])
    node_index = _build_node_index(nodes_list, project_root)
    if not node_index["by_file"]:
        return []

    pos_index = _build_pos_index(nodes_list, project_root)

    cmd = ensure_server(TS_LS)
    if cmd is None:
        return []

    try:
        ts_mod = importlib.import_module("tree_sitter_typescript")
        from tree_sitter import Language, Parser
        ts_parser = Parser(Language(ts_mod.language_typescript()))
        tsx_parser = Parser(Language(ts_mod.language_tsx()))
    except Exception as exc:
        _log.debug("tree-sitter unavailable for ts_lsp: %s", exc)
        return []

    from icx_engine.graph.parser.lsp_client import LSPClient

    proj_str_norm = _norm(str(project_root))
    seen: set[tuple[str, str, str]] = set()
    edges: list[dict] = []

    try:
        with LSPClient(cmd, project_root) as client:
            if not client.start():
                return []
            if client.pid:
                record_pid(TS_LS.install_dir, client.pid)

            # Phase 1: parse all files and batch-open them before any queries.
            # Opening all files first lets the server index the full workspace
            # once, avoiding N repeated re-analysis cycles that cause timeouts.
            file_tasks: list[tuple[Path, str, str, list[tuple[int, int, str]]]] = []
            for js_file in js_files:
                try:
                    rel = _norm(str(js_file.relative_to(project_root)))
                except ValueError:
                    continue

                src_id = node_index["by_file"].get(rel)
                if not src_id:
                    continue

                ext = js_file.suffix.lower()
                lang_id = "typescript" if ext in (".ts", ".tsx") else "javascript"
                parser = tsx_parser if ext == ".tsx" else ts_parser

                try:
                    source_bytes = js_file.read_bytes()
                    tree = parser.parse(source_bytes)
                except Exception:
                    continue

                positions = _collect_positions(tree.root_node)
                if not positions:
                    continue

                client.did_open(js_file, lang_id)
                file_tasks.append((js_file, rel, src_id, positions))

            # Phase 2: query definitions with circuit breaker.
            # After _CIRCUIT_BREAKER_LIMIT consecutive timeouts the server is
            # overloaded; stop querying rather than burning timeout budget on
            # every remaining file.
            for js_file, rel, src_id, positions in file_tasks:
                if client.consecutive_timeouts >= _CIRCUIT_BREAKER_LIMIT:
                    _log.debug(
                        "ts_lsp: circuit breaker triggered (%d consecutive timeouts), "
                        "aborting remaining queries",
                        _CIRCUIT_BREAKER_LIMIT,
                    )
                    break

                for row, col, kind in positions:
                    if client.consecutive_timeouts >= _CIRCUIT_BREAKER_LIMIT:
                        break
                    for loc in client.definition(js_file, row, col):
                        loc_norm = _norm(loc.path)
                        if not loc_norm.startswith(proj_str_norm):
                            continue
                        rel_tgt = loc_norm[len(proj_str_norm):].lstrip("/")
                        if rel_tgt == rel:
                            continue

                        if kind == "import":
                            tgt_id = (
                                _pos_to_node_id(pos_index, rel_tgt, loc.line)
                                or node_index["by_file"].get(rel_tgt)
                            )
                            relation = "imports"
                        else:
                            tgt_id = _pos_to_node_id(pos_index, rel_tgt, loc.line)
                            if not tgt_id:
                                tgt_id = node_index["by_file"].get(rel_tgt)
                            relation = "calls"

                        if not tgt_id or tgt_id == src_id:
                            continue

                        key = (src_id, tgt_id, relation)
                        if key in seen:
                            continue
                        seen.add(key)

                        edge = {
                            "relation": relation,
                            "source": src_id,
                            "target": tgt_id,
                            "source_file": rel,
                            "source_location": f"L{row + 1}",
                            "weight": 1.0,
                        }
                        annotate_edge(edge, LSP_RESOLVED, "ts_lsp")
                        edges.append(edge)

                client.did_close(js_file)

    except Exception as exc:
        _log.debug("ts_lsp error: %s", type(exc).__name__)

    return edges
