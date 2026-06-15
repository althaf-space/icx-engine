"""Rust LSP resolver.

Uses rust-analyzer (Rust's official language server) to resolve cross-file
references with compiler-grade accuracy. Server lifecycle (install/version-
track/kill/reinstall) is managed by lsp_manager.py.

Supplements rust_resolver.py with:
  - `use` path resolution verified by rust-analyzer's type/macro-aware resolver
    (handles re-exports and glob `use` that the regex-based resolver misses)
  - Cross-file call edges with accurate targets, including trait method
    dispatch that rust_resolver.py's directory-scoped heuristic cannot follow

If Rust is not installed: silent no-op.
If tree-sitter-rust is not installed (graph-extended optional dependency): silent no-op.
If rust-analyzer was installed for a different rustc version: auto-reinstalls.
"""
from __future__ import annotations

import bisect
import importlib
import logging
from collections import defaultdict
from pathlib import Path
from typing import Iterable

from icx_engine.graph.parser.confidence import LSP_RESOLVED, annotate_edge
from icx_engine.graph.parser.lsp_manager import RUST_ANALYZER, ensure_server, record_pid

_log = logging.getLogger(__name__)

_MAX_POSITIONS_PER_FILE = 100
_CIRCUIT_BREAKER_LIMIT = 5


def _build_node_index(nodes: list[dict], project_root: Path) -> dict:
    proj_str = str(project_root).replace("\\", "/")
    by_file: dict[str, str] = {}

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
        if label.lower().endswith(".rs") or label == Path(rel).name:
            by_file.setdefault(rel, nid)

    return {"by_file": by_file}


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


def _last_identifier(node):
    """Descend through scoped_identifier/use_as_clause nodes to the trailing identifier."""
    target = node
    while target is not None and target.type in ("scoped_identifier", "use_as_clause"):
        nxt = target.child_by_field_name("name") or target.child_by_field_name("path")
        if nxt is None:
            return target if target.type == "identifier" else None
        target = nxt
    return target if target is not None and target.type == "identifier" else None


def _collect_positions(tree_root) -> list[tuple[int, int, str]]:
    """Walk tree-sitter Rust root and return (row, col, kind) to query via LSP.

    kind "use": trailing identifier of a `use` path  kind "call": call expression callee.
    """
    positions: list[tuple[int, int, str]] = []

    def _walk(node) -> None:
        if len(positions) >= _MAX_POSITIONS_PER_FILE:
            return
        t = node.type
        if t == "use_declaration":
            arg = node.child_by_field_name("argument")
            if arg is not None:
                target = _last_identifier(arg) if arg.type != "identifier" else arg
                if target is not None:
                    row, col = target.start_point
                    positions.append((row, col, "use"))
        elif t == "call_expression":
            func = node.child_by_field_name("function")
            if func is not None:
                if func.type == "field_expression":
                    field = func.child_by_field_name("field")
                    if field is not None:
                        row, col = field.start_point
                        positions.append((row, col, "call"))
                elif func.type == "identifier":
                    row, col = func.start_point
                    positions.append((row, col, "call"))
                elif func.type == "scoped_identifier":
                    target = _last_identifier(func)
                    if target is not None:
                        row, col = target.start_point
                        positions.append((row, col, "call"))
        for child in node.children:
            _walk(child)

    _walk(tree_root)
    return positions


def extract_rust_lsp_edges(
    files: Iterable[Path],
    project_root: Path,
    ast_extraction: dict,
) -> list[dict]:
    """Resolve Rust cross-file references using rust-analyzer."""
    project_root = project_root.resolve()
    rs_files = [Path(f).resolve() for f in files if str(f).endswith(".rs")]
    if not rs_files:
        return []

    nodes_list = ast_extraction.get("nodes", [])
    node_index = _build_node_index(nodes_list, project_root)
    if not node_index["by_file"]:
        return []

    pos_index = _build_pos_index(nodes_list, project_root)

    cmd = ensure_server(RUST_ANALYZER)
    if cmd is None:
        return []

    try:
        rust_mod = importlib.import_module("tree_sitter_rust")
        from tree_sitter import Language, Parser
        rust_parser = Parser(Language(rust_mod.language()))
    except Exception as exc:
        _log.debug("tree-sitter-rust unavailable for rust_lsp: %s", exc)
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
                record_pid(RUST_ANALYZER.install_dir, client.pid)

            # Phase 1: parse all files and batch-open them before any queries.
            file_tasks: list[tuple[Path, str, str, list[tuple[int, int, str]]]] = []
            for rs_file in rs_files:
                try:
                    rel = _norm(str(rs_file.relative_to(project_root)))
                except ValueError:
                    continue

                src_id = node_index["by_file"].get(rel)
                if not src_id:
                    continue

                try:
                    source_bytes = rs_file.read_bytes()
                    tree = rust_parser.parse(source_bytes)
                except Exception:
                    continue

                positions = _collect_positions(tree.root_node)
                if not positions:
                    continue

                client.did_open(rs_file, "rust")
                file_tasks.append((rs_file, rel, src_id, positions))

            # Phase 2: query definitions with circuit breaker.
            for rs_file, rel, src_id, positions in file_tasks:
                if client.consecutive_timeouts >= _CIRCUIT_BREAKER_LIMIT:
                    _log.debug(
                        "rust_lsp: circuit breaker triggered (%d consecutive timeouts), "
                        "aborting remaining queries",
                        _CIRCUIT_BREAKER_LIMIT,
                    )
                    break

                for row, col, kind in positions:
                    if client.consecutive_timeouts >= _CIRCUIT_BREAKER_LIMIT:
                        break
                    for loc in client.definition(rs_file, row, col):
                        loc_norm = _norm(loc.path)
                        if not loc_norm.startswith(proj_str_norm):
                            continue
                        rel_tgt = loc_norm[len(proj_str_norm):].lstrip("/")
                        if rel_tgt == rel:
                            continue

                        if kind == "use":
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
                        annotate_edge(edge, LSP_RESOLVED, "rust_lsp")
                        edges.append(edge)

                client.did_close(rs_file)

    except Exception as exc:
        _log.debug("rust_lsp error: %s", type(exc).__name__)

    return edges
