"""Kotlin LSP resolver.

Uses kotlin-language-server (fwcd/kotlin-language-server) to resolve
cross-file imports and calls with compiler-grade accuracy. Server lifecycle
(download/version-track/kill/reinstall) is managed by lsp_manager.py.

No tree-sitter-kotlin dependency exists, so import/call positions are
collected via regex (reusing kotlin_symbols.py's import-line pattern)
rather than AST traversal.

Supplements kotlin_symbols.py with:
  - Cross-file import resolution verified by the real Kotlin compiler
  - Cross-file call edges resolved through the type system that
    kotlin_symbols.py's regex-based approach cannot follow

If Java is not installed: silent no-op (kotlin-language-server requires a JDK).
If kotlin-language-server was installed for a different Java version: auto-reinstalls.
"""
from __future__ import annotations

import bisect
import logging
import re
from collections import defaultdict
from pathlib import Path
from typing import Iterable

from icx_engine.graph.parser.confidence import LSP_RESOLVED, annotate_edge
from icx_engine.graph.parser.lsp_manager import KOTLIN_LS, ensure_server, record_pid
from icx_engine.graph.parser.resolvers.kotlin_symbols import _KT_IMPORT

_log = logging.getLogger(__name__)

_MAX_POSITIONS_PER_FILE = 100
_CIRCUIT_BREAKER_LIMIT = 5

_CALL_RE = re.compile(r"(?<![\w.])([A-Za-z_][A-Za-z0-9_]*)\s*\(")


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
        if label.lower().endswith((".kt", ".kts")) or label == Path(rel).name:
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


def _collect_positions(source: str) -> list[tuple[int, int, str]]:
    """Collect (line_0indexed, col_0indexed, kind) from Kotlin source via regex.

    kind "import": import statement  kind "call": function/method call identifier.
    """
    positions: list[tuple[int, int, str]] = []
    lines = source.splitlines()

    for i, line in enumerate(lines):
        m = _KT_IMPORT.match(line)
        if m:
            col = line.index(m.group(1))
            positions.append((i, col, "import"))
            if len(positions) >= _MAX_POSITIONS_PER_FILE:
                return positions

    for i, line in enumerate(lines):
        for m in _CALL_RE.finditer(line):
            if len(positions) >= _MAX_POSITIONS_PER_FILE:
                return positions
            positions.append((i, m.start(1), "call"))

    return positions


def extract_kotlin_lsp_edges(
    files: Iterable[Path],
    project_root: Path,
    ast_extraction: dict,
) -> list[dict]:
    """Resolve Kotlin cross-file references using kotlin-language-server."""
    project_root = project_root.resolve()
    kotlin_files = [Path(f).resolve() for f in files if str(f).endswith((".kt", ".kts"))]
    if not kotlin_files:
        return []

    nodes_list = ast_extraction.get("nodes", [])
    node_index = _build_node_index(nodes_list, project_root)
    if not node_index["by_file"]:
        return []

    pos_index = _build_pos_index(nodes_list, project_root)

    cmd = ensure_server(KOTLIN_LS)
    if cmd is None:
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
                record_pid(KOTLIN_LS.install_dir, client.pid)

            # Phase 1: parse all files and batch-open them before any queries.
            file_tasks: list[tuple[Path, str, str, list[tuple[int, int, str]]]] = []
            for kt_file in kotlin_files:
                try:
                    rel = _norm(str(kt_file.relative_to(project_root)))
                except ValueError:
                    continue

                src_id = node_index["by_file"].get(rel)
                if not src_id:
                    continue

                try:
                    source = kt_file.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue

                positions = _collect_positions(source)
                if not positions:
                    continue

                client.did_open(kt_file, "kotlin")
                file_tasks.append((kt_file, rel, src_id, positions))

            # Phase 2: query definitions with circuit breaker.
            for kt_file, rel, src_id, positions in file_tasks:
                if client.consecutive_timeouts >= _CIRCUIT_BREAKER_LIMIT:
                    _log.debug(
                        "kotlin_lsp: circuit breaker triggered (%d consecutive timeouts), "
                        "aborting remaining queries",
                        _CIRCUIT_BREAKER_LIMIT,
                    )
                    break

                for row, col, kind in positions:
                    if client.consecutive_timeouts >= _CIRCUIT_BREAKER_LIMIT:
                        break
                    for loc in client.definition(kt_file, row, col):
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
                        annotate_edge(edge, LSP_RESOLVED, "kotlin_lsp")
                        edges.append(edge)

                client.did_close(kt_file)

    except Exception as exc:
        _log.debug("kotlin_lsp error: %s", type(exc).__name__)

    return edges
