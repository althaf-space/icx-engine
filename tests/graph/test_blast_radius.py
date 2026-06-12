"""Tests for Phase 12: blast radius MCP tool."""
import json
import tempfile
from pathlib import Path
import pytest

from icx_engine.graph.query import GraphQuerier


def _build_querier(nodes, links):
    graph = {"nodes": nodes, "links": links}
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(graph, f)
        tmp_path = f.name
    return GraphQuerier(Path(tmp_path))


def _n(nid, source_file, importance=0.0):
    return {"id": nid, "label": nid, "source_file": source_file, "importance": importance}


def _e(src, tgt, confidence=0.9, src_file="", tgt_file=""):
    return {"source": src, "target": tgt, "confidence": confidence,
            "source_file": src_file, "target_file": tgt_file}


class TestGetBlastRadius:
    def test_direct_dependents_detected(self):
        nodes = [
            _n("n_core", "core.py"),
            _n("n_user", "user.py"),
            _n("n_api", "api.py"),
        ]
        # user.py and api.py import core.py
        links = [
            _e("n_user", "n_core", src_file="user.py", tgt_file="core.py"),
            _e("n_api", "n_core", src_file="api.py", tgt_file="core.py"),
        ]
        q = _build_querier(nodes, links)
        result = q.get_blast_radius(["core.py"])
        assert "user.py" in result["direct_dependents"] or "user.py" in result["transitive_dependents"]
        assert result["total_affected"] >= 2

    def test_changed_files_not_in_dependents(self):
        nodes = [_n("n_a", "a.py"), _n("n_b", "b.py")]
        links = [_e("n_a", "n_b", src_file="a.py", tgt_file="b.py")]
        q = _build_querier(nodes, links)
        result = q.get_blast_radius(["a.py"])
        assert "a.py" not in result["direct_dependents"]
        assert "a.py" not in result["transitive_dependents"]

    def test_risk_score_between_0_and_1(self):
        nodes = [_n("n_a", "a.py", importance=0.9), _n("n_b", "b.py", importance=0.1)]
        links = [_e("n_b", "n_a", src_file="b.py", tgt_file="a.py")]
        q = _build_querier(nodes, links)
        result = q.get_blast_radius(["a.py"])
        assert 0.0 <= result["risk_score"] <= 1.0

    def test_empty_changed_files(self):
        nodes = [_n("n_a", "a.py")]
        q = _build_querier(nodes, [])
        result = q.get_blast_radius([])
        assert result["total_affected"] == 0
        assert result["direct_dependents"] == []
        assert result["transitive_dependents"] == []

    def test_low_confidence_edges_not_followed(self):
        nodes = [_n("n_a", "a.py"), _n("n_b", "b.py")]
        links = [_e("n_b", "n_a", confidence=0.1, src_file="b.py", tgt_file="a.py")]
        q = _build_querier(nodes, links)
        result = q.get_blast_radius(["a.py"], min_confidence=0.5)
        # Low confidence edge should not be followed
        assert "b.py" not in result["direct_dependents"]
        assert "b.py" not in result["transitive_dependents"]

    def test_missing_changes_includes_cochange_partners(self):
        nodes = [_n("n_a", "a.py"), _n("n_b", "b.py"), _n("n_c", "c.py")]
        links = [
            _e("n_b", "n_a", src_file="b.py", tgt_file="a.py"),
            # co_changed edge between a.py and c.py
            {"source": "n_a", "target": "n_c", "type": "co_changed",
             "source_file": "a.py", "target_file": "c.py",
             "co_change_strength": 0.8, "co_occurrences": 4, "confidence": 0.90},
        ]
        q = _build_querier(nodes, links)
        result = q.get_blast_radius(["a.py"])
        # c.py co-changes with a.py but is not in changed_files -> should be missing
        assert "c.py" in result["missing_changes"]
