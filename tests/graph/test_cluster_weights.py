"""Tests for confidence-weighted clustering enhancements."""
from __future__ import annotations
import networkx as nx
import pytest


def _make_graph(edges_with_conf: list[tuple[str, str, float, str]]) -> nx.Graph:
    G = nx.Graph()
    for src, tgt, conf, tag in edges_with_conf:
        G.add_edge(src, tgt, confidence_score=conf, confidence_source=tag, relation="calls")
    return G


def test_confidence_weight_lsp_higher_than_universal():
    from icx_engine.graph.parser.cluster import _confidence_weight
    lsp_w = _confidence_weight(0.95, "ts_lsp")
    univ_w = _confidence_weight(0.55, "universal_ast")
    assert lsp_w > univ_w


def test_confidence_weight_di_boost():
    from icx_engine.graph.parser.cluster import _confidence_weight
    spring_w = _confidence_weight(0.88, "spring")
    plain_w = _confidence_weight(0.88, "some_other_resolver")
    assert spring_w > plain_w


def test_apply_confidence_weights_sets_weight_attr():
    from icx_engine.graph.parser.cluster import _apply_confidence_weights
    G = _make_graph([
        ("a", "b", 0.95, "ts_lsp"),
        ("b", "c", 0.55, "universal_ast"),
    ])
    H = _apply_confidence_weights(G)
    assert H["a"]["b"]["weight"] > H["b"]["c"]["weight"]


def test_classify_god_nodes_di_tier():
    from icx_engine.graph.parser.cluster import classify_god_nodes
    G = nx.Graph()
    for i in range(15):
        G.add_edge(f"service_{i}", "repo",
                   confidence_score=0.90, confidence_source="spring",
                   relation="injects")
    for j in range(3):
        G.add_edge(f"util_{j}", "helper",
                   confidence_score=0.55, confidence_source="universal_ast",
                   relation="imports")
    result = classify_god_nodes(G)
    assert "structural" in result
    assert "di" in result
    assert "repo" in result["di"]


def test_cluster_uses_weights():
    from icx_engine.graph.parser.cluster import cluster
    G = _make_graph([
        ("a", "b", 0.95, "ts_lsp"),
        ("b", "c", 0.90, "spring"),
        ("c", "d", 0.55, "universal_ast"),
        ("d", "e", 0.55, "universal_ast"),
    ])
    communities = cluster(G)
    assert isinstance(communities, dict)
    all_nodes = set()
    for nodes in communities.values():
        all_nodes.update(nodes)
    assert all_nodes == {"a", "b", "c", "d", "e"}


def test_bounded_louvain_runs_inline_without_timeout_thread():
    """When networkx louvain supports max_level (bounded), _partition_safe runs
    inline - no background thread, no spurious-timeout fallback. Result must be a
    real partition (dict), not None."""
    from icx_engine.graph.parser.cluster import _partition_safe, _louvain_is_bounded
    import networkx as nx
    assert _louvain_is_bounded()  # networkx >= 3.x
    G = nx.Graph()
    G.add_edges_from([("a", "b"), ("b", "c"), ("x", "y"), ("y", "z")])
    result = _partition_safe(G)
    assert isinstance(result, dict) and result  # real partition, not a timeout None


def test_partition_safe_never_returns_none_on_normal_graph():
    """Regression: a healthy graph must never fall back to connected-components
    via a spurious timeout - _partition_safe returns a partition."""
    from icx_engine.graph.parser.cluster import _partition_safe
    import networkx as nx
    G = nx.gnm_random_graph(500, 1500, seed=1)
    G = nx.relabel_nodes(G, {n: f"n{n}" for n in G.nodes()})
    result = _partition_safe(G)
    assert isinstance(result, dict) and len(result) == G.number_of_nodes()
