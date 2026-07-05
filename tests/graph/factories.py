"""Production-realistic graph fixture factories.

Ad-hoc `_n`/`_e` helpers scattered across the graph tests each emitted a
different edge shape - and none matched what `build.py` actually writes to
`graph.json`. That drift let real bugs pass green suites:

  - BUG-5: incremental merge tested only with distinct node ids
  - BUG-6: `blast_radius` fixtures set `confidence` as a float; production writes
           a STRING enum -> TypeError on real graphs
  - BUG-7: `fuse_and_dedup` fixtures set `confidence` as a float; a real
           java_symbols-validated edge carries a string enum -> build abort

These factories emit the real shape so future tests can't drift the same way:
every edge carries BOTH `confidence` (the string enum, derived from the score
exactly as `build.py` does) AND `confidence_score` (the float). Nodes carry the
required node fields.

Import from any graph test:

    from factories import graph_node, graph_edge, build_querier
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# Mirrors build.py's normalization (the single source of truth). If build.py's
# thresholds change, this test contract must change with it - test_factories.py
# asserts they stay in sync.
_EXTRACTED_MIN = 0.8
_INFERRED_MIN = 0.4


def confidence_enum(score: float) -> str:
    """Map a numeric confidence_score to the string enum build.py writes."""
    if score >= _EXTRACTED_MIN:
        return "EXTRACTED"
    if score >= _INFERRED_MIN:
        return "INFERRED"
    return "AMBIGUOUS"


def graph_node(
    nid: str,
    source_file: str,
    *,
    label: str | None = None,
    file_type: str = "code",
    community: int | None = None,
    importance: float | None = None,
    role_tag: str | None = None,
    **extra: Any,
) -> dict:
    """A node dict shaped like a real graph.json node.

    Includes the fields validate.REQUIRED_NODE_FIELDS expects
    (id, label, file_type, source_file). Optional attributes are added only when
    provided so a node can be minimal or fully-featured.
    """
    node: dict = {
        "id": nid,
        "label": label if label is not None else nid,
        "file_type": file_type,
        "source_file": source_file,
    }
    if community is not None:
        node["community"] = community
    if importance is not None:
        node["importance"] = importance
    if role_tag is not None:
        node["role_tag"] = role_tag
    node.update(extra)
    return node


def graph_edge(
    source: str,
    target: str,
    *,
    score: float = 0.9,
    relation: str = "calls",
    etype: str | None = None,
    source_file: str = "",
    target_file: str = "",
    resolver: str = "test_resolver",
    **extra: Any,
) -> dict:
    """An edge dict shaped like a real graph.json edge.

    Sets BOTH `confidence` (string enum, derived from `score` via the same
    contract build.py applies) and `confidence_score` (the float). This is the
    exact dual-key shape that BUG-6/BUG-7 crashed on when tests used a float
    `confidence`. `etype` defaults to `relation` when unset.
    """
    edge: dict = {
        "source": source,
        "target": target,
        "relation": relation,
        "type": etype if etype is not None else relation,
        "confidence_score": score,
        "confidence": confidence_enum(score),
        "confidence_source": resolver,
        "resolver_tag": resolver,
        "resolver": resolver,
        "source_file": source_file,
        "target_file": target_file,
    }
    edge.update(extra)
    return edge


def build_graph(
    nodes: list[dict],
    edges: list[dict],
    communities: dict[str, list[str]] | None = None,
) -> dict:
    """Assemble a graph.json dict (nodes + links + communities)."""
    graph: dict = {"nodes": nodes, "links": edges}
    if communities is not None:
        graph["communities"] = communities
    return graph


def write_graph_json(
    tmp_path: Path,
    nodes: list[dict],
    edges: list[dict],
    communities: dict[str, list[str]] | None = None,
    name: str = "graph.json",
) -> Path:
    """Write a production-shaped graph.json to tmp_path and return its path."""
    graph = build_graph(nodes, edges, communities)
    p = tmp_path / name
    p.write_text(json.dumps(graph), encoding="utf-8")
    return p


def build_querier(
    tmp_path: Path,
    nodes: list[dict],
    edges: list[dict],
    communities: dict[str, list[str]] | None = None,
):
    """Write a production-shaped graph and return a GraphQuerier over it."""
    from icx_engine.graph.query import GraphQuerier

    return GraphQuerier(write_graph_json(tmp_path, nodes, edges, communities))
