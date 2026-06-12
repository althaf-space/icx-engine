"""Tests for Phase 11: PageRank + betweenness centrality."""
import pytest
from icx_engine.graph.parser.centrality import compute_centrality


def _n(nid):
    return {"id": nid, "label": nid}


def _e(src, tgt):
    return {"source": src, "target": tgt}


class TestComputeCentrality:
    def test_empty_returns_empty(self):
        assert compute_centrality([], []) == {}

    def test_nodes_without_edges_get_nonzero_pagerank(self):
        nodes = [_n("a"), _n("b"), _n("c")]
        scores = compute_centrality(nodes, [])
        # Dangling nodes distribute PR evenly, so PR should be non-zero and equal
        assert all(scores[n["id"]]["pagerank"] > 0 for n in nodes)

    def test_star_hub_has_highest_pagerank(self):
        # Hub -> 5 leaves: hub receives most inbound links
        hub = _n("hub")
        leaves = [_n(f"leaf{i}") for i in range(5)]
        nodes = [hub] + leaves
        edges = [_e(f"leaf{i}", "hub") for i in range(5)]  # All leaves point to hub
        scores = compute_centrality(nodes, edges)
        hub_pr = scores["hub"]["pagerank"]
        assert all(hub_pr >= scores[f"leaf{i}"]["pagerank"] for i in range(5))

    def test_linear_chain_middle_has_higher_betweenness(self):
        # A -> B -> C -> D: B and C are on all shortest paths, A and D are not
        nodes = [_n("A"), _n("B"), _n("C"), _n("D")]
        edges = [_e("A", "B"), _e("B", "C"), _e("C", "D")]
        scores = compute_centrality(nodes, edges)
        b_btw = scores["B"]["betweenness"]
        c_btw = scores["C"]["betweenness"]
        a_btw = scores["A"]["betweenness"]
        d_btw = scores["D"]["betweenness"]
        assert b_btw >= a_btw
        assert c_btw >= d_btw

    def test_importance_formula_correct(self):
        nodes = [_n("a"), _n("b")]
        edges = [_e("a", "b")]
        scores = compute_centrality(nodes, edges)
        for nid, s in scores.items():
            expected = round(0.50 * s["pagerank"] + 0.30 * s["degree_centrality"] + 0.20 * s["betweenness"], 6)
            assert abs(s["importance"] - expected) < 1e-5

    def test_single_node_no_edges(self):
        nodes = [_n("solo")]
        scores = compute_centrality(nodes, [])
        assert "solo" in scores
        assert scores["solo"]["importance"] >= 0

    def test_all_scores_between_0_and_1(self):
        nodes = [_n(f"n{i}") for i in range(10)]
        edges = [_e(f"n{i}", f"n{i+1}") for i in range(9)] + [_e("n9", "n0")]
        scores = compute_centrality(nodes, edges)
        for nid, s in scores.items():
            for key in ["pagerank", "degree_centrality", "betweenness", "importance"]:
                assert 0.0 <= s[key] <= 1.0, f"{nid}.{key} = {s[key]} out of range"

    def test_large_dangling_graph_completes_quickly(self):
        """Regression: O(D*N) dangling PageRank would take 50+ seconds for 1000 nodes."""
        import time
        nodes = [_n(f"n{i}") for i in range(1000)]
        # ~800 dangling nodes (no outgoing edges)
        edges = [_e(f"n{i}", f"n{i+1}") for i in range(200)]
        start = time.monotonic()
        scores = compute_centrality(nodes, edges)
        elapsed = time.monotonic() - start
        assert len(scores) == 1000
        assert elapsed < 5.0, f"PageRank took {elapsed:.1f}s - dangling O(N) fix may have regressed"

    def test_find_context_prefers_high_importance(self):
        """Integration: GraphQuerier.find_context boosts high-importance nodes."""
        import json
        import tempfile
        from pathlib import Path
        from icx_engine.graph.query import GraphQuerier

        graph = {
            "nodes": [
                {"id": "high_imp", "label": "auth", "source_file": "auth.py",
                 "importance": 0.95, "pagerank": 0.95},
                {"id": "low_imp", "label": "auth", "source_file": "utils.py",
                 "importance": 0.05, "pagerank": 0.05},
            ],
            "links": [],
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(graph, f)
            fpath = f.name

        q = GraphQuerier(Path(fpath))
        results = q.find_context("auth")
        files = [r.file for r in results]
        # High importance file should rank first
        assert files.index("auth.py") < files.index("utils.py")
