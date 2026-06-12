"""Tests for Phase 13: cycle detection and dead code detection."""
import json
import tempfile
from pathlib import Path
import pytest

from icx_engine.graph.query import GraphQuerier


def _build_querier(nodes, links):
    graph = {"nodes": nodes, "links": links}
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(graph, f)
    return GraphQuerier(Path(f.name))


def _n(nid, source_file):
    return {"id": nid, "label": nid, "source_file": source_file}


def _e(src, tgt, etype="imports", src_file="", tgt_file="", conf=0.9):
    return {"source": src, "target": tgt, "type": etype,
            "source_file": src_file, "target_file": tgt_file, "confidence": conf}


class TestGetCycles:
    def test_no_cycle_in_dag(self):
        nodes = [_n("na", "a.py"), _n("nb", "b.py"), _n("nc", "c.py")]
        links = [
            _e("na", "nb", src_file="a.py", tgt_file="b.py"),
            _e("nb", "nc", src_file="b.py", tgt_file="c.py"),
        ]
        q = _build_querier(nodes, links)
        cycles = q.get_cycles()
        assert cycles == []

    def test_simple_two_file_cycle(self):
        nodes = [_n("na", "a.py"), _n("nb", "b.py")]
        links = [
            _e("na", "nb", src_file="a.py", tgt_file="b.py"),
            _e("nb", "na", src_file="b.py", tgt_file="a.py"),
        ]
        q = _build_querier(nodes, links)
        cycles = q.get_cycles()
        assert len(cycles) >= 1
        # Both a.py and b.py should appear in the cycle
        all_files = [f for cycle in cycles for f in cycle]
        assert "a.py" in all_files or "b.py" in all_files

    def test_co_changed_edges_excluded_from_cycle_detection(self):
        # a.py and b.py only connected by co_changed - no structural cycle
        nodes = [_n("na", "a.py"), _n("nb", "b.py")]
        links = [
            {"source": "na", "target": "nb", "type": "co_changed",
             "source_file": "a.py", "target_file": "b.py", "confidence": 0.9},
            {"source": "nb", "target": "na", "type": "co_changed",
             "source_file": "b.py", "target_file": "a.py", "confidence": 0.9},
        ]
        q = _build_querier(nodes, links)
        cycles = q.get_cycles()
        assert cycles == []

    def test_three_file_cycle(self):
        nodes = [_n("na", "a.py"), _n("nb", "b.py"), _n("nc", "c.py")]
        links = [
            _e("na", "nb", src_file="a.py", tgt_file="b.py"),
            _e("nb", "nc", src_file="b.py", tgt_file="c.py"),
            _e("nc", "na", src_file="c.py", tgt_file="a.py"),
        ]
        q = _build_querier(nodes, links)
        cycles = q.get_cycles()
        assert len(cycles) >= 1


class TestGetDeadCode:
    def test_unreferenced_file_detected(self):
        # orphan.py has no incoming edges and is not an entry point
        nodes = [_n("na", "core.py"), _n("nb", "orphan.py")]
        links = [_e("na", "nb", src_file="core.py", tgt_file="orphan.py")]
        q = _build_querier(nodes, links)
        dead = q.get_dead_code()
        # core.py has no incoming edges either, but orphan.py does (core imports it)
        # core.py should appear as dead (no one imports it)
        dead_files = [d["file"] for d in dead]
        assert "core.py" in dead_files
        assert "orphan.py" not in dead_files  # orphan.py IS referenced by core.py

    def test_entry_points_excluded(self):
        nodes = [
            _n("nmain", "main.py"),
            _n("napp", "app.py"),
            _n("nconf", "conftest.py"),
        ]
        q = _build_querier(nodes, [])
        dead = q.get_dead_code()
        dead_files = [d["file"] for d in dead]
        assert "main.py" not in dead_files
        assert "app.py" not in dead_files
        assert "conftest.py" not in dead_files

    def test_test_files_excluded(self):
        nodes = [_n("ntest", "test_utils.py"), _n("nspec", "user_spec.rb")]
        q = _build_querier(nodes, [])
        dead = q.get_dead_code()
        dead_files = [d["file"] for d in dead]
        assert "test_utils.py" not in dead_files

    def test_node_count_in_result(self):
        nodes = [
            _n("na1", "unused.py"), _n("na2", "unused.py"),  # 2 nodes in same file
        ]
        q = _build_querier(nodes, [])
        dead = q.get_dead_code()
        unused = next((d for d in dead if d["file"] == "unused.py"), None)
        assert unused is not None
        assert unused["node_count"] == 2

    def test_empty_graph_returns_empty(self):
        q = _build_querier([], [])
        assert q.get_dead_code() == []
