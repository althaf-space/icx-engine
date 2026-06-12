"""Tests for Phase 9: CO_CHANGED edges from git history."""
import json
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock
import pytest

from icx_engine.graph.parser.resolvers.cochange_resolver import resolve_cochange


def _node(node_id, rel_path):
    return {"id": node_id, "source_file": rel_path, "file": rel_path, "name": node_id}


def _make_git_log(*file_groups):
    """Build fake git log output with file_groups as separate commits."""
    lines = []
    for group in file_groups:
        lines.append("__COMMIT__")
        lines.extend(group)
    return "\n".join(lines)


class TestResolveCochange:
    def _run(self, tmp_path, commits, files_and_nodes):
        """Helper: mock git and run resolver."""
        files = [tmp_path / f for f in files_and_nodes]
        nodes = [_node(f"n_{f}", f) for f in files_and_nodes]
        log_output = _make_git_log(*commits)

        with patch("subprocess.run") as mock_run:
            # First call: git rev-parse (returns 0)
            rev_mock = MagicMock()
            rev_mock.returncode = 0
            # Second call: git log
            log_mock = MagicMock()
            log_mock.returncode = 0
            log_mock.stdout = log_output
            mock_run.side_effect = [rev_mock, log_mock]
            return resolve_cochange(files, tmp_path, {"nodes": nodes})

    def test_high_cooccurrence_creates_edges(self, tmp_path):
        commits = [["a.py", "b.py"]] * 5  # 5/5 commits together
        edges = self._run(tmp_path, commits, ["a.py", "b.py"])
        types = [e["type"] for e in edges]
        assert "co_changed" in types

    def test_strength_1_gives_confidence_0_90(self, tmp_path):
        commits = [["a.py", "b.py"]] * 5
        edges = self._run(tmp_path, commits, ["a.py", "b.py"])
        co = [e for e in edges if e["type"] == "co_changed"]
        assert all(e["confidence"] == 0.90 for e in co)

    def test_low_cooccurrence_below_threshold_no_edge(self, tmp_path):
        # Only 1/10 commits together -> strength = 0.1, below 0.30 threshold
        commits = [["a.py", "b.py"]] + [["a.py"]] * 9
        edges = self._run(tmp_path, commits, ["a.py", "b.py"])
        assert not any(e["type"] == "co_changed" for e in edges)

    def test_below_min_cooccurrences_no_edge(self, tmp_path):
        # Only 2 commits together, min_cooccurrences=3
        commits = [["a.py", "b.py"]] * 2 + [["a.py"]] * 8
        edges = self._run(tmp_path, commits, ["a.py", "b.py"])
        # Strength may pass but count < 3 -> no edge
        assert not any(e["type"] == "co_changed" for e in edges)

    def test_bidirectional_edges_created(self, tmp_path):
        commits = [["a.py", "b.py"]] * 5
        edges = self._run(tmp_path, commits, ["a.py", "b.py"])
        co = [e for e in edges if e["type"] == "co_changed"]
        # Should have edges in both directions
        assert len(co) == 2

    def test_git_not_available_returns_empty(self, tmp_path):
        files = [tmp_path / "a.py"]
        nodes = [_node("n_a", "a.py")]
        with patch("subprocess.run", side_effect=FileNotFoundError()):
            result = resolve_cochange(files, tmp_path, {"nodes": nodes})
        assert result == []

    def test_non_git_directory_returns_empty(self, tmp_path):
        files = [tmp_path / "a.py"]
        nodes = [_node("n_a", "a.py")]
        with patch("subprocess.run") as mock_run:
            rev_mock = MagicMock()
            rev_mock.returncode = 128  # not a git repo
            mock_run.return_value = rev_mock
            result = resolve_cochange(files, tmp_path, {"nodes": nodes})
        assert result == []

    def test_edges_have_relation_field(self, tmp_path):
        commits = [["a.py", "b.py"]] * 5
        edges = self._run(tmp_path, commits, ["a.py", "b.py"])
        assert edges
        assert all("relation" in e for e in edges)
        assert all(e["relation"] == "co_changed" for e in edges)


class TestGetCochangePartners:
    def test_get_cochange_partners_sorted(self):
        """Test get_cochange_partners returns sorted by strength."""
        from icx_engine.graph.query import GraphQuerier

        graph = {
            "nodes": [
                {"id": "n1", "source_file": "a.py", "label": "a"},
                {"id": "n2", "source_file": "b.py", "label": "b"},
                {"id": "n3", "source_file": "c.py", "label": "c"},
            ],
            "links": [
                {"source": "n1", "target": "n2", "type": "co_changed",
                 "source_file": "a.py", "target_file": "b.py",
                 "co_change_strength": 0.8, "co_occurrences": 4, "confidence": 0.90},
                {"source": "n1", "target": "n3", "type": "co_changed",
                 "source_file": "a.py", "target_file": "c.py",
                 "co_change_strength": 0.4, "co_occurrences": 2, "confidence": 0.70},
            ],
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(graph, f)
            fpath = f.name

        q = GraphQuerier(Path(fpath))
        partners = q.get_cochange_partners("a.py")
        assert len(partners) == 2
        assert partners[0]["strength"] >= partners[1]["strength"]
        assert partners[0]["file"] == "b.py"
