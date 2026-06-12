"""Python LSP resolver backed by pyright.

Server lifecycle (venv creation, install, version-track, kill/reinstall on Python
version change) is managed by lsp_manager.py.

Supplements python_jedi.py with type-aware cross-file resolution:
  - Resolves imports through __init__.py re-exports
  - Type-narrowed call targets
  - More accurate cross-package import resolution than jedi

If Python venv/pip unavailable: silent no-op.
If pyright was installed under a different Python version: auto-reinstalls.
"""
from __future__ import annotations

import ast
import bisect
import logging
from collections import defaultdict
from pathlib import Path
from typing import Iterable

from icx_engine.graph.parser.confidence import LSP_RESOLVED, annotate_edge
from icx_engine.graph.parser.lsp_manager import PYRIGHT, ensure_server, record_pid

_log = logging.getLogger(__name__)

_MAX_POSITIONS_PER_FILE = 80
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
        if label.lower().endswith(".py") or label == Path(rel).name:
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


def _collect_positions(tree: ast.Module) -> list[tuple[int, int, str]]:
    """Collect (line_0indexed, col_0indexed, kind) from Python AST."""
    positions: list[tuple[int, int, str]] = []

    for node in ast.walk(tree):
        if len(positions) >= _MAX_POSITIONS_PER_FILE:
            break

        if isinstance(node, ast.ImportFrom) and node.names:
            for alias in node.names:
                if alias.name and alias.name != "*":
                    positions.append((node.lineno - 1, node.col_offset, "import"))
                    break

        elif isinstance(node, ast.Import):
            positions.append((node.lineno - 1, node.col_offset, "import"))

        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and hasattr(func, "end_col_offset"):
                col = max(0, func.end_col_offset - len(func.attr))
                positions.append((func.end_lineno - 1, col, "call"))
            elif isinstance(func, ast.Name):
                positions.append((func.lineno - 1, func.col_offset, "call"))

    return positions


def extract_pyright_edges(
    files: Iterable[Path],
    project_root: Path,
    ast_extraction: dict,
) -> list[dict]:
    """Resolve Python cross-file references using pyright LSP."""
    project_root = project_root.resolve()
    py_files = [Path(f).resolve() for f in files if str(f).endswith(".py")]
    if not py_files:
        return []

    nodes_list = ast_extraction.get("nodes", [])
    node_index = _build_node_index(nodes_list, project_root)
    if not node_index["by_file"]:
        return []

    pos_index = _build_pos_index(nodes_list, project_root)

    cmd = ensure_server(PYRIGHT)
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
                record_pid(PYRIGHT.install_dir, client.pid)

            # Phase 1: parse all files and batch-open them before any queries.
            # Opening all files first lets the server index the full workspace
            # once, avoiding N repeated re-analysis cycles that cause timeouts.
            file_tasks: list[tuple[Path, str, str, list[tuple[int, int, str]]]] = []
            for py_file in py_files:
                try:
                    rel = _norm(str(py_file.relative_to(project_root)))
                except ValueError:
                    continue

                src_id = node_index["by_file"].get(rel)
                if not src_id:
                    continue

                try:
                    source = py_file.read_text(encoding="utf-8", errors="replace")
                    tree = ast.parse(source, filename=str(py_file))
                except (OSError, SyntaxError):
                    continue

                positions = _collect_positions(tree)
                if not positions:
                    continue

                client.did_open(py_file, "python")
                file_tasks.append((py_file, rel, src_id, positions))

            # Phase 2: query definitions with circuit breaker.
            # After _CIRCUIT_BREAKER_LIMIT consecutive timeouts the server is
            # overloaded; stop querying rather than burning timeout budget on
            # every remaining file.
            for py_file, rel, src_id, positions in file_tasks:
                if client.consecutive_timeouts >= _CIRCUIT_BREAKER_LIMIT:
                    _log.debug(
                        "pyright_lsp: circuit breaker triggered (%d consecutive timeouts), "
                        "aborting remaining queries",
                        _CIRCUIT_BREAKER_LIMIT,
                    )
                    break

                for row, col, kind in positions:
                    if client.consecutive_timeouts >= _CIRCUIT_BREAKER_LIMIT:
                        break
                    for loc in client.definition(py_file, row, max(0, col)):
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
                        annotate_edge(edge, LSP_RESOLVED, "pyright_lsp")
                        edges.append(edge)

                client.did_close(py_file)

    except Exception as exc:
        _log.debug("pyright_lsp error: %s", type(exc).__name__)

    return edges
