"""Embedding-based cross-check for LLM-emitted edges.

For each LLM edge whose source and target both have a known source file
in the AST node set, embed a snippet of the source symbol and the target
symbol and compute cosine similarity. Edges below the similarity floor
are rejected (downgraded confidence + flagged). The check runs only on
edges produced by the LLM tier; AST/LSP/framework edges are untouched.
"""
from __future__ import annotations

import logging
import math
from pathlib import Path
from typing import Iterable

from icx_engine.graph.parser.confidence import (
    LLM_EMBEDDING_REJECTED,
    annotate_edge,
)

_log = logging.getLogger(__name__)

DEFAULT_MIN_SIMILARITY = 0.30
SNIPPET_LINE_RADIUS = 25
SNIPPET_MAX_CHARS = 1600

_LLM_EDGE_SOURCES = frozenset({"llm_consensus", "llm_single_pass"})


def filter_llm_edges_by_embedding(
    extraction: dict,
    project_root: Path,
    *,
    min_similarity: float = DEFAULT_MIN_SIMILARITY,
    drop_rejected: bool = False,
) -> dict:
    """Mutate `extraction["edges"]` so LLM-tier edges below `min_similarity`
    are either dropped (`drop_rejected=True`) or kept with downgraded
    confidence and a `rejected_by_embedding=True` flag.

    Returns the same extraction dict (caller can chain). Embedding is
    skipped silently when the memory model is unavailable; in that case
    LLM edges pass through unchanged.
    """
    edges = extraction.get("edges", [])
    if not edges:
        return extraction

    candidate_indices = [
        i for i, e in enumerate(edges)
        if e.get("confidence_source") in _LLM_EDGE_SOURCES
    ]
    if not candidate_indices:
        return extraction

    embedder = _try_load_embedder()
    if embedder is None:
        _log.debug("embedding model unavailable; skipping LLM cross-check")
        return extraction

    node_by_id = {n.get("id"): n for n in extraction.get("nodes", []) if n.get("id")}

    snippet_cache: dict[str, str] = {}
    vector_cache: dict[str, list[float]] = {}

    keep: list[int] = []
    rejected = 0

    for idx in candidate_indices:
        edge = edges[idx]
        src_id = edge.get("source")
        tgt_id = edge.get("target")
        src_node = node_by_id.get(src_id)
        tgt_node = node_by_id.get(tgt_id)
        if src_node is None or tgt_node is None:
            keep.append(idx)
            continue

        src_text = _snippet_for_node(src_node, project_root, snippet_cache)
        tgt_text = _snippet_for_node(tgt_node, project_root, snippet_cache)
        if not src_text or not tgt_text:
            keep.append(idx)
            continue

        try:
            src_vec = vector_cache.get(src_id) or embedder.embed(src_text)
            tgt_vec = vector_cache.get(tgt_id) or embedder.embed(tgt_text)
        except Exception as exc:
            _log.debug("embedding call failed (%s); leaving edge as-is",
                       type(exc).__name__)
            keep.append(idx)
            continue
        vector_cache[src_id] = src_vec
        vector_cache[tgt_id] = tgt_vec

        sim = _cosine(src_vec, tgt_vec)
        if sim < min_similarity:
            rejected += 1
            if not drop_rejected:
                annotate_edge(edge, LLM_EMBEDDING_REJECTED, "llm_embedding_filter")
                edge["rejected_by_embedding"] = True
                edge["embedding_similarity"] = round(sim, 4)
                keep.append(idx)
            continue
        edge["embedding_similarity"] = round(sim, 4)
        keep.append(idx)

    if drop_rejected and rejected > 0:
        kept_set = set(keep)
        non_candidate = [i for i in range(len(edges)) if i not in set(candidate_indices)]
        new_edges = [edges[i] for i in sorted(set(non_candidate) | kept_set)]
        extraction["edges"] = new_edges

    extraction["embedding_filter_stats"] = {
        "checked": len(candidate_indices),
        "rejected": rejected,
        "min_similarity": min_similarity,
        "dropped": drop_rejected,
    }
    return extraction


def _try_load_embedder():
    try:
        from icx_engine.memory.embeddings import EmbeddingsManager
    except ImportError:
        return None
    try:
        mgr = EmbeddingsManager()
        mgr.check_ready()
        return mgr
    except Exception as exc:
        _log.debug("EmbeddingsManager init failed (%s)", type(exc).__name__)
        return None


def _snippet_for_node(
    node: dict, project_root: Path, cache: dict[str, str],
) -> str:
    """Read a small code window centered on the node's source_location line.
    Falls back to the node label when the file is unreadable."""
    nid = node.get("id") or ""
    if nid in cache:
        return cache[nid]

    src_file = node.get("source_file") or ""
    if not src_file:
        text = (node.get("label") or "").strip()
        cache[nid] = text
        return text

    path = Path(src_file)
    if not path.is_absolute():
        path = project_root / path

    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = (node.get("label") or "").strip()
        cache[nid] = text
        return text

    line_no = _parse_line_no(node.get("source_location"))
    snippet = _extract_window(content, line_no, SNIPPET_LINE_RADIUS)
    snippet = snippet[:SNIPPET_MAX_CHARS]
    if not snippet.strip():
        snippet = (node.get("label") or "").strip()
    cache[nid] = snippet
    return snippet


def _parse_line_no(location: str | None) -> int | None:
    if not location:
        return None
    s = str(location).strip()
    if s.startswith("L"):
        s = s[1:]
    try:
        return int(s)
    except ValueError:
        return None


def _extract_window(content: str, line_no: int | None, radius: int) -> str:
    lines = content.splitlines()
    if not lines:
        return ""
    if line_no is None or line_no <= 0:
        return "\n".join(lines[: min(len(lines), 2 * radius)])
    center = max(1, min(line_no, len(lines)))
    start = max(0, center - 1 - radius)
    end = min(len(lines), center + radius)
    return "\n".join(lines[start:end])


def _cosine(a: Iterable[float], b: Iterable[float]) -> float:
    av = list(a)
    bv = list(b)
    if not av or not bv:
        return 0.0
    dot = sum(x * y for x, y in zip(av, bv))
    norm_a = math.sqrt(sum(x * x for x in av))
    norm_b = math.sqrt(sum(y * y for y in bv))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)
