"""Tests for incremental graph build (Phase 1): file_cache.py functions."""
import json
import hashlib
from pathlib import Path
import pytest

from icx_engine.graph.parser.file_cache import (
    load_hashes,
    save_hashes,
    hash_file,
    compute_changed_files,
)
from icx_engine.graph.builder import _merge_incremental, _rel_path


class TestLoadSaveHashes:
    def test_load_empty_when_missing(self, tmp_path):
        result = load_hashes(tmp_path / "nonexistent.json")
        assert result == {}

    def test_load_empty_when_corrupted(self, tmp_path):
        p = tmp_path / "hashes.json"
        p.write_text("not valid json")
        result = load_hashes(p)
        assert result == {}

    def test_round_trip(self, tmp_path):
        p = tmp_path / "hashes.json"
        data = {"src/a.py": "abc123", "src/b.py": "def456"}
        save_hashes(p, data)
        loaded = load_hashes(p)
        assert loaded == data


class TestHashFile:
    def test_known_hash(self, tmp_path):
        p = tmp_path / "test.py"
        content = b"hello world"
        p.write_bytes(content)
        expected = hashlib.sha256(content).hexdigest()
        assert hash_file(p) == expected

    def test_missing_file_returns_empty_string(self, tmp_path):
        result = hash_file(tmp_path / "nonexistent.py")
        assert result == ""


class TestComputeChangedFiles:
    def test_full_build_no_stored_hashes(self, tmp_path):
        f1 = tmp_path / "a.py"
        f1.write_text("content a")
        f2 = tmp_path / "b.py"
        f2.write_text("content b")
        changed, deleted, new_hashes = compute_changed_files(
            str(tmp_path), ["a.py", "b.py"], {}
        )
        assert set(changed) == {"a.py", "b.py"}
        assert deleted == []
        assert set(new_hashes.keys()) == {"a.py", "b.py"}

    def test_no_changes_returns_empty_changed(self, tmp_path):
        f1 = tmp_path / "a.py"
        f1.write_text("content a")
        _, _, first_hashes = compute_changed_files(str(tmp_path), ["a.py"], {})
        # Second pass with same content
        changed, deleted, new_hashes = compute_changed_files(
            str(tmp_path), ["a.py"], first_hashes
        )
        assert changed == []
        assert deleted == []

    def test_modified_file_detected(self, tmp_path):
        f1 = tmp_path / "a.py"
        f1.write_text("original")
        _, _, first_hashes = compute_changed_files(str(tmp_path), ["a.py"], {})
        f1.write_text("modified content")
        changed, deleted, new_hashes = compute_changed_files(
            str(tmp_path), ["a.py"], first_hashes
        )
        assert "a.py" in changed

    def test_deleted_file_detected(self, tmp_path):
        f1 = tmp_path / "a.py"
        f1.write_text("content")
        _, _, first_hashes = compute_changed_files(str(tmp_path), ["a.py"], {})
        # Now "a.py" is no longer in the file list (deleted)
        changed, deleted, new_hashes = compute_changed_files(
            str(tmp_path), [], first_hashes
        )
        assert "a.py" in deleted


class TestMergeIncremental:
    def test_surviving_nodes_preserved(self):
        existing = {
            "nodes": [
                {"id": "n1", "file": "a.py", "source_file": "a.py"},
                {"id": "n2", "file": "b.py", "source_file": "b.py"},
            ],
            "links": [],
        }
        new_extraction = {"nodes": [{"id": "n3", "source_file": "a.py"}], "edges": []}
        merged = _merge_incremental(existing, new_extraction, ["a.py"], [])
        node_ids = {n["id"] for n in merged["nodes"]}
        assert "n2" in node_ids  # b.py survived
        assert "n3" in node_ids  # new a.py node added
        assert "n1" not in node_ids  # old a.py node removed

    def test_fix_confidence_delta_preserved_on_surviving_edges(self):
        existing = {
            "nodes": [{"id": "n1", "source_file": "b.py"}],
            "links": [
                {
                    "source": "n1", "target": "n2",
                    "source_file": "b.py", "target_file": "c.py",
                    "fix_confidence_delta": 0.15, "resolution_weight": 0.3,
                }
            ],
        }
        new_extraction = {"nodes": [], "edges": []}
        merged = _merge_incremental(existing, new_extraction, ["a.py"], [])
        assert len(merged["links"]) == 1
        assert merged["links"][0]["fix_confidence_delta"] == 0.15

    def test_deleted_file_nodes_removed(self):
        existing = {
            "nodes": [
                {"id": "n1", "source_file": "deleted.py"},
                {"id": "n2", "source_file": "kept.py"},
            ],
            "links": [],
        }
        merged = _merge_incremental(existing, {"nodes": [], "edges": []}, [], ["deleted.py"])
        node_ids = {n["id"] for n in merged["nodes"]}
        assert "n1" not in node_ids
        assert "n2" in node_ids

    def test_regenerated_id_keeps_new_edges_but_prunes_truly_removed(self):
        # Re-parsing a changed file regenerates the SAME deterministic node id.
        # Its freshly-extracted edges must survive (they were wrongly dropped
        # when the re-created id was treated as "removed"), while an edge to a
        # genuinely deleted node is still pruned as dangling.
        existing = {
            "nodes": [
                {"id": "re", "source_file": "a.py"},     # changed -> re-extracted
                {"id": "gone", "source_file": "gone.py"},  # deleted -> not re-extracted
                {"id": "keep", "source_file": "b.py"},
            ],
            # Untagged dangling edge to the deleted node (not caught by file filter).
            "links": [{"source": "keep", "target": "gone"}],
        }
        new_extraction = {
            "nodes": [{"id": "re", "source_file": "a.py"}],  # same id, re-created
            "links": [
                {"source": "re", "target": "keep",
                 "source_file": "a.py", "target_file": "b.py"},
            ],
        }
        merged = _merge_incremental(existing, new_extraction, ["a.py"], ["gone.py"])
        links = merged["links"]
        assert any(e["source"] == "re" and e["target"] == "keep" for e in links)  # kept
        assert not any(e.get("source") == "gone" or e.get("target") == "gone" for e in links)  # pruned

    def test_absolute_edge_source_file_purged_with_root_posix(self):
        existing = {
            "nodes": [{"id": "n1", "source_file": "src/a.py"}],
            "links": [
                {
                    "source": "n1", "target": "n2",
                    "source_file": "/proj/src/a.py", "target_file": "src/b.py",
                }
            ],
        }
        merged = _merge_incremental(
            existing, {"nodes": [], "edges": []}, ["src/a.py"], [],
            root_posix="/proj",
        )
        assert merged["links"] == []

    def test_windows_style_node_source_file_purged(self):
        existing = {
            "nodes": [{"id": "n1", "file": "src\\a.py", "source_file": "src\\a.py"}],
            "links": [],
        }
        merged = _merge_incremental(
            existing, {"nodes": [], "edges": []}, ["src/a.py"], [],
        )
        assert merged["nodes"] == []


class TestRelPath:
    def test_no_root_posix_only_normalizes_separators(self):
        assert _rel_path("src\\a.py", "") == "src/a.py"

    def test_strips_absolute_root_prefix(self):
        assert _rel_path("/proj/src/a.py", "/proj") == "src/a.py"

    def test_empty_path_returned_as_is(self):
        assert _rel_path("", "/proj") == ""
