"""Tests for the community-detection wall-clock safety net.

Guarantees the graph build can never hang in louvain on a weakly-separable
dense component: _partition_safe caps its runtime and cluster() degrades to a
connected-components fallback. Every graph that partitions in time is unaffected.
"""
from __future__ import annotations
import networkx as nx
import pytest

import icx_engine.graph.parser.cluster as cl


def _two_triangles() -> nx.Graph:
    """Two disjoint 3-cliques - clear two-community structure, each component
    below _MIN_SPLIT_SIZE so no secondary split pass runs."""
    G = nx.Graph()
    for a, b in [("a", "b"), ("b", "c"), ("a", "c")]:
        G.add_edge(a, b, confidence_score=0.9, confidence_source="ts_lsp")
    for a, b in [("x", "y"), ("y", "z"), ("x", "z")]:
        G.add_edge(a, b, confidence_score=0.9, confidence_source="ts_lsp")
    return G


def _spin_forever(G, resolution=1.0):
    """Stand-in for a louvain partition that never converges. Pure-Python loop so
    PyThreadState_SetAsyncExc can interrupt it between bytecodes."""
    while True:
        pass


# -- timeout selection --------------------------------------------------------

def test_bounded_timeout_default():
    assert cl._bounded_timeout() == cl._PARTITION_TIMEOUT_BOUNDED_DEFAULT


def test_bounded_timeout_env_override(monkeypatch):
    monkeypatch.setenv("ICX_LOUVAIN_TIMEOUT", "12.5")
    assert cl._bounded_timeout() == 12.5


@pytest.mark.parametrize("bad", ["0", "-5", "abc", ""])
def test_bounded_timeout_invalid_falls_back_to_default(monkeypatch, bad):
    monkeypatch.setenv("ICX_LOUVAIN_TIMEOUT", bad)
    assert cl._bounded_timeout() == cl._PARTITION_TIMEOUT_BOUNDED_DEFAULT


# -- happy path: no regression ------------------------------------------------

def test_partition_safe_returns_real_result_when_fast():
    """A graph that partitions quickly returns the true partition, never None."""
    G = _two_triangles()
    part = cl._partition_safe(G)
    assert part is not None
    assert set(part.keys()) == set(G.nodes())
    # the two triangles land in different communities
    assert part["a"] == part["b"] == part["c"]
    assert part["x"] == part["y"] == part["z"]
    assert part["a"] != part["x"]


def test_cluster_normal_graph_unaffected():
    G = _two_triangles()
    communities = cl.cluster(G, exclude_hubs_percentile=99)
    assert isinstance(communities, dict)
    all_nodes = {n for nodes in communities.values() for n in nodes}
    assert all_nodes == set(G.nodes())


# -- safety net: grind is capped ----------------------------------------------

def test_partition_safe_times_out_and_returns_none(monkeypatch):
    """When the partition never converges, _partition_safe returns None within
    the cap rather than blocking forever."""
    monkeypatch.setenv("ICX_LOUVAIN_TIMEOUT", "1")
    monkeypatch.setattr(cl, "_partition", _spin_forever)
    result = cl._partition_safe(_two_triangles())
    assert result is None


def test_coarse_partition_separates_components():
    """The coarse single-level pass still produces real communities."""
    part = cl._coarse_partition(_two_triangles())
    assert part["a"] == part["b"] == part["c"]
    assert part["x"] == part["y"] == part["z"]
    assert part["a"] != part["x"]


def test_cluster_uses_coarse_fallback_on_timeout(monkeypatch, caplog):
    """When the full partition caps out, cluster() degrades to the fast coarse
    louvain - complete assignment, meaningful communities, never hangs."""
    import logging
    monkeypatch.setenv("ICX_LOUVAIN_TIMEOUT", "1")
    monkeypatch.setattr(cl, "_partition", _spin_forever)  # full partition never converges
    G = _two_triangles()
    with caplog.at_level(logging.WARNING):
        communities = cl.cluster(G, exclude_hubs_percentile=99)
    all_nodes = {n for nodes in communities.values() for n in nodes}
    assert all_nodes == set(G.nodes())
    triangle_a = next(c for c in communities.values() if "a" in c)
    triangle_x = next(c for c in communities.values() if "x" in c)
    assert set(triangle_a) == {"a", "b", "c"}
    assert set(triangle_x) == {"x", "y", "z"}
    assert any("label-propagation" in r.message for r in caplog.records)


def test_cluster_last_resort_connected_components(monkeypatch, caplog):
    """If even the coarse pass overruns, cluster() falls back to connected-
    components - still complete, still never hangs."""
    import logging
    monkeypatch.setenv("ICX_LOUVAIN_TIMEOUT", "1")
    monkeypatch.setattr(cl, "_COARSE_TIMEOUT", 1)
    monkeypatch.setattr(cl, "_partition", _spin_forever)
    monkeypatch.setattr(cl, "_coarse_partition", _spin_forever)
    G = _two_triangles()
    with caplog.at_level(logging.WARNING):
        communities = cl.cluster(G, exclude_hubs_percentile=99)
    all_nodes = {n for nodes in communities.values() for n in nodes}
    assert all_nodes == set(G.nodes())
    triangle_a = next(c for c in communities.values() if "a" in c)
    triangle_x = next(c for c in communities.values() if "x" in c)
    assert set(triangle_a) == {"a", "b", "c"}
    assert set(triangle_x) == {"x", "y", "z"}
    assert any("connected-components" in r.message for r in caplog.records)
