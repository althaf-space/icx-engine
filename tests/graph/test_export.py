"""Tests for export.py: compact JSON format and skip_safety_check behaviour."""
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import networkx as nx
import pytest


def _make_graph(n_nodes: int = 5) -> tuple[nx.Graph, dict]:
    G = nx.DiGraph()
    for i in range(n_nodes):
        G.add_node(f"n{i}", label=f"node{i}", source_file=f"file{i}.py")
    for i in range(n_nodes - 1):
        G.add_edge(f"n{i}", f"n{i+1}", relation="imports", weight=1.0)
    communities = {0: [f"n{i}" for i in range(n_nodes)]}
    return G, communities


class TestToJsonCompactFormat:
    def test_output_is_valid_json(self, tmp_path):
        from icx_engine.graph.parser.export import to_json
        G, communities = _make_graph()
        out = str(tmp_path / "graph.json")
        result = to_json(G, communities, out)
        assert result is True
        data = json.loads(Path(out).read_text(encoding="utf-8"))
        assert "nodes" in data

    def test_output_has_no_indent(self, tmp_path):
        from icx_engine.graph.parser.export import to_json
        G, communities = _make_graph()
        out = str(tmp_path / "graph.json")
        to_json(G, communities, out)
        raw = Path(out).read_text(encoding="utf-8")
        # Compact JSON has no leading whitespace on non-first lines
        lines = raw.splitlines()
        assert len(lines) == 1, (
            f"Expected compact single-line JSON, got {len(lines)} lines. "
            "indent=2 was not removed."
        )

    def test_output_smaller_than_indented_equivalent(self, tmp_path):
        from icx_engine.graph.parser.export import to_json
        G, communities = _make_graph(20)
        out = str(tmp_path / "graph.json")
        to_json(G, communities, out)
        compact_size = Path(out).stat().st_size

        # Compare against what indent=2 would produce
        data = json.loads(Path(out).read_text(encoding="utf-8"))
        indented_size = len(json.dumps(data, indent=2).encode("utf-8"))

        assert compact_size < indented_size, "Compact format should be smaller than indent=2"

    def test_node_and_edge_counts_correct(self, tmp_path):
        from icx_engine.graph.parser.export import to_json
        G, communities = _make_graph(10)
        out = str(tmp_path / "graph.json")
        to_json(G, communities, out)
        data = json.loads(Path(out).read_text(encoding="utf-8"))
        assert len(data["nodes"]) == 10
        assert len(data["links"]) == 9


class TestToJsonSafetyCheck:
    def test_skip_safety_check_does_not_read_existing_file(self, tmp_path):
        """With skip_safety_check=True the existing graph.json is never read."""
        from icx_engine.graph.parser.export import to_json
        G, communities = _make_graph(5)
        out = tmp_path / "graph.json"
        # Write a fake existing graph with more nodes to trigger the safety check
        out.write_text(json.dumps({"nodes": [{"id": f"old{i}"} for i in range(100)], "links": []}))

        read_calls = []
        original_read = Path.read_text

        def tracking_read(self, **kwargs):
            if self == out:
                read_calls.append(str(self))
            return original_read(self, **kwargs)

        with patch.object(Path, "read_text", tracking_read):
            result = to_json(G, communities, str(out), skip_safety_check=True)

        assert result is True
        assert str(out) not in read_calls, "existing graph.json should not be read with skip_safety_check=True"

    def test_safety_check_active_by_default_blocks_shrink(self, tmp_path):
        """Without skip_safety_check, a smaller new graph is rejected."""
        from icx_engine.graph.parser.export import to_json
        G, communities = _make_graph(3)
        out = tmp_path / "graph.json"
        # Existing graph has 100 nodes - new graph has 3
        out.write_text(json.dumps({"nodes": [{"id": f"n{i}"} for i in range(100)], "links": []}))
        result = to_json(G, communities, str(out))
        assert result is False, "Safety check should block overwrite with smaller graph"
