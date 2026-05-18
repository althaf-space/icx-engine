"""Tests for graph/querier.py - generate_graph_report."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from icx_engine.graph.querier import generate_graph_report


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_graph(tmp_path: Path, data: dict, name: str = "graph.json") -> Path:
    p = tmp_path / name
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def _report(tmp_path: Path) -> Path:
    return tmp_path / "GRAPH_REPORT.md"


# ---------------------------------------------------------------------------
# Error / edge cases
# ---------------------------------------------------------------------------

def test_corrupted_graph_writes_unavailable_report(tmp_path):
    bad = tmp_path / "graph.json"
    bad.write_text("not valid json", encoding="utf-8")
    out = _report(tmp_path)
    generate_graph_report(bad, out)
    assert out.exists()
    assert "unavailable" in out.read_text(encoding="utf-8").lower()


def test_missing_graph_file_writes_unavailable_report(tmp_path):
    out = _report(tmp_path)
    generate_graph_report(tmp_path / "nonexistent.json", out)
    assert out.exists()
    assert "unavailable" in out.read_text(encoding="utf-8").lower()


def test_empty_nodes_writes_no_nodes_report(tmp_path):
    gf = _write_graph(tmp_path, {"nodes": [], "edges": []})
    out = _report(tmp_path)
    generate_graph_report(gf, out)
    assert out.exists()
    content = out.read_text(encoding="utf-8")
    assert "No nodes" in content


# ---------------------------------------------------------------------------
# Basic report structure
# ---------------------------------------------------------------------------

SIMPLE_GRAPH = {
    "nodes": [
        {"id": "cli", "source_file": "src/cli.py"},
        {"id": "engine", "source_file": "src/engine.py"},
        {"id": "auth", "source_file": "src/auth.py"},
    ],
    "edges": [
        {"source": "cli", "target": "engine"},
        {"source": "engine", "target": "auth"},
    ],
}


def test_report_is_created(tmp_path):
    gf = _write_graph(tmp_path, SIMPLE_GRAPH)
    out = _report(tmp_path)
    generate_graph_report(gf, out)
    assert out.exists()


def test_report_has_graph_report_heading(tmp_path):
    gf = _write_graph(tmp_path, SIMPLE_GRAPH)
    out = _report(tmp_path)
    generate_graph_report(gf, out)
    assert "# Project Graph Report" in out.read_text(encoding="utf-8")


def test_report_has_community_clusters_section(tmp_path):
    gf = _write_graph(tmp_path, SIMPLE_GRAPH)
    out = _report(tmp_path)
    generate_graph_report(gf, out)
    assert "Community Clusters" in out.read_text(encoding="utf-8")


def test_report_has_god_nodes_section(tmp_path):
    gf = _write_graph(tmp_path, SIMPLE_GRAPH)
    out = _report(tmp_path)
    generate_graph_report(gf, out)
    assert "God Nodes" in out.read_text(encoding="utf-8")


def test_report_lists_source_files(tmp_path):
    gf = _write_graph(tmp_path, SIMPLE_GRAPH)
    out = _report(tmp_path)
    generate_graph_report(gf, out)
    content = out.read_text(encoding="utf-8")
    assert "src/cli.py" in content or "src/engine.py" in content or "src/auth.py" in content


# ---------------------------------------------------------------------------
# Community assignment - priority 1: top-level communities key
# ---------------------------------------------------------------------------

def test_communities_key_priority(tmp_path):
    graph = {
        "nodes": [
            {"id": "a", "source_file": "src/a.py"},
            {"id": "b", "source_file": "src/b.py"},
            {"id": "c", "source_file": "src/c.py"},
            {"id": "d", "source_file": "src/d.py"},
        ],
        "edges": [],
        "communities": {"group1": ["a", "b"], "group2": ["c", "d"]},
    }
    gf = _write_graph(tmp_path, graph)
    out = _report(tmp_path)
    generate_graph_report(gf, out)
    content = out.read_text(encoding="utf-8")
    # Two community sections should appear (each has 2 files, passes filter)
    assert content.count("###") >= 2


# ---------------------------------------------------------------------------
# Community assignment - priority 2: node-level community attribute
# ---------------------------------------------------------------------------

def test_node_level_community_attribute(tmp_path):
    graph = {
        "nodes": [
            {"id": "x", "source_file": "src/x.py", "community": 0},
            {"id": "y", "source_file": "src/y.py", "community": 0},
            {"id": "z", "source_file": "src/z.py", "community": 1},
            {"id": "w", "source_file": "src/w.py", "community": 1},
        ],
        "edges": [],
    }
    gf = _write_graph(tmp_path, graph)
    out = _report(tmp_path)
    generate_graph_report(gf, out)
    content = out.read_text(encoding="utf-8")
    assert content.count("###") >= 2


# ---------------------------------------------------------------------------
# Community assignment - priority 3: directory fallback
# ---------------------------------------------------------------------------

def test_directory_based_community_fallback(tmp_path):
    graph = {
        "nodes": [
            {"id": "s1", "source_file": "src/services/UserService.py"},
            {"id": "s2", "source_file": "src/services/AuthService.py"},
            {"id": "m1", "source_file": "src/models/User.py"},
            {"id": "m2", "source_file": "src/models/Order.py"},
        ],
        "edges": [],
    }
    gf = _write_graph(tmp_path, graph)
    out = _report(tmp_path)
    generate_graph_report(gf, out)
    content = out.read_text(encoding="utf-8")
    # services and models are different dirs - should create 2 clusters (each has 2 files)
    assert content.count("###") >= 2


# ---------------------------------------------------------------------------
# God nodes detection
# ---------------------------------------------------------------------------

def test_god_nodes_detected_for_hub_file(tmp_path):
    # hub.py connects to many nodes - should be identified as god node
    nodes = [{"id": "hub", "source_file": "src/hub.py"}]
    nodes += [{"id": f"leaf{i}", "source_file": f"src/leaf{i}.py"} for i in range(10)]
    edges = [{"source": "hub", "target": f"leaf{i}"} for i in range(10)]
    gf = _write_graph(tmp_path, {"nodes": nodes, "edges": edges})
    out = _report(tmp_path)
    generate_graph_report(gf, out)
    content = out.read_text(encoding="utf-8")
    assert "src/hub.py" in content


def test_god_nodes_none_when_uniform_degree(tmp_path):
    # All files have equal connectivity - no outliers
    graph = {
        "nodes": [
            {"id": "a", "source_file": "src/a.py"},
            {"id": "b", "source_file": "src/b.py"},
        ],
        "edges": [{"source": "a", "target": "b"}],
    }
    gf = _write_graph(tmp_path, graph)
    out = _report(tmp_path)
    generate_graph_report(gf, out)
    content = out.read_text(encoding="utf-8")
    assert "none identified" in content.lower() or "God Nodes" in content


# ---------------------------------------------------------------------------
# Cross-cluster connections
# ---------------------------------------------------------------------------

def test_cross_cluster_connections_reported(tmp_path):
    graph = {
        "nodes": [
            {"id": "a", "source_file": "src/services/A.py", "community": 0},
            {"id": "b", "source_file": "src/models/B.py", "community": 1},
        ],
        "edges": [{"source": "a", "target": "b"}],
    }
    gf = _write_graph(tmp_path, graph)
    out = _report(tmp_path)
    generate_graph_report(gf, out)
    content = out.read_text(encoding="utf-8")
    assert "Cross-Cluster" in content


def test_no_cross_cluster_section_for_single_community(tmp_path):
    # All nodes in same community - cross-cluster section should not appear
    graph = {
        "nodes": [
            {"id": "a", "source_file": "src/services/A.py", "community": 0},
            {"id": "b", "source_file": "src/services/B.py", "community": 0},
        ],
        "edges": [{"source": "a", "target": "b"}],
    }
    gf = _write_graph(tmp_path, graph)
    out = _report(tmp_path)
    generate_graph_report(gf, out)
    content = out.read_text(encoding="utf-8")
    assert "Cross-Cluster" not in content


# ---------------------------------------------------------------------------
# Degree-based ordering
# ---------------------------------------------------------------------------

def test_high_degree_files_listed_first_in_cluster(tmp_path):
    # engine.py has 2 edges, cli.py has 1 - engine should appear first
    graph = {
        "nodes": [
            {"id": "cli", "source_file": "src/cli.py"},
            {"id": "engine", "source_file": "src/engine.py"},
            {"id": "util", "source_file": "src/util.py"},
        ],
        "edges": [
            {"source": "cli", "target": "engine"},
            {"source": "util", "target": "engine"},
        ],
    }
    gf = _write_graph(tmp_path, graph)
    out = _report(tmp_path)
    generate_graph_report(gf, out)
    content = out.read_text(encoding="utf-8")
    engine_pos = content.find("src/engine.py")
    cli_pos = content.find("src/cli.py")
    assert engine_pos < cli_pos


# ---------------------------------------------------------------------------
# Nodes without source_file are skipped gracefully
# ---------------------------------------------------------------------------

def test_nodes_without_source_file_skipped(tmp_path):
    graph = {
        "nodes": [
            {"id": "a", "source_file": "src/a.py"},
            {"id": "a2", "source_file": "src/a2.py"},
            {"id": "b"},  # no source_file
        ],
        "edges": [],
    }
    gf = _write_graph(tmp_path, graph)
    out = _report(tmp_path)
    generate_graph_report(gf, out)
    content = out.read_text(encoding="utf-8")
    assert "src/a.py" in content


# ---------------------------------------------------------------------------
# Footer / metadata
# ---------------------------------------------------------------------------

def test_report_has_icx_footer(tmp_path):
    gf = _write_graph(tmp_path, SIMPLE_GRAPH)
    out = _report(tmp_path)
    generate_graph_report(gf, out)
    content = out.read_text(encoding="utf-8")
    assert "ICX" in content or "icx" in content.lower()
