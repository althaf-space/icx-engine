"""Tests for Phase 2: multi-source edge fusion (fuse_and_dedup)."""
import pytest
from icx_engine.graph.parser.dedup import fuse_and_dedup


def _edge(src_file, tgt_file, etype, confidence, resolver="test"):
    return {
        "source": f"node_{src_file}",
        "target": f"node_{tgt_file}",
        "source_file": src_file,
        "target_file": tgt_file,
        "type": etype,
        "confidence": confidence,
        "resolver": resolver,
    }


class TestFuseAndDedup:
    def test_pyright_and_scip_same_pair_fused(self):
        edges = [
            _edge("a.py", "b.py", "pyright_import", 0.78, "pyright"),
            _edge("a.py", "b.py", "scip_reference", 0.95, "scip"),
        ]
        result = fuse_and_dedup(edges)
        assert len(result) == 1
        e = result[0]
        assert e["confidence"] == round(min(0.98, 0.78 + 0.95), 4)
        assert e["resolver"] == "fused"
        assert set(e["resolver_sources"]) == {"pyright", "scip"}

    def test_three_resolvers_capped_at_0_98(self):
        edges = [
            _edge("a.py", "b.py", "pyright_import", 0.80, "pyright"),
            _edge("a.py", "b.py", "scip_reference", 0.90, "scip"),
            _edge("a.py", "b.py", "jedi_import", 0.70, "jedi"),
        ]
        result = fuse_and_dedup(edges)
        assert len(result) == 1
        assert result[0]["confidence"] == 0.98

    def test_kafka_publish_and_subscribe_not_fused(self):
        edges = [
            _edge("a.py", "b.py", "kafka_publish", 0.80, "event"),
            _edge("a.py", "b.py", "kafka_subscribe", 0.80, "event"),
        ]
        result = fuse_and_dedup(edges)
        assert len(result) == 2

    def test_co_changed_and_import_not_fused(self):
        edges = [
            _edge("a.py", "b.py", "pyright_import", 0.78, "pyright"),
            _edge("a.py", "b.py", "co_changed", 0.65, "cochange"),
        ]
        result = fuse_and_dedup(edges)
        assert len(result) == 2

    def test_unknown_edge_type_own_family(self):
        edges = [
            _edge("a.py", "b.py", "custom_edge_xyz", 0.60, "custom1"),
            _edge("a.py", "b.py", "custom_edge_xyz", 0.70, "custom2"),
        ]
        result = fuse_and_dedup(edges)
        # Unknown type is its own family, not fusable - keep highest confidence
        assert len(result) == 1
        assert result[0]["confidence"] == 0.70

    def test_resolver_sources_all_names_present(self):
        edges = [
            _edge("x.py", "y.py", "go_import", 0.88, "go"),
            _edge("x.py", "y.py", "scip_reference", 0.92, "scip"),
        ]
        result = fuse_and_dedup(edges)
        assert len(result) == 1
        assert "go" in result[0]["resolver_sources"]
        assert "scip" in result[0]["resolver_sources"]

    def test_exact_duplicate_same_type_deduped(self):
        e = _edge("a.py", "b.py", "pyright_import", 0.78, "pyright")
        result = fuse_and_dedup([e, dict(e)])
        assert len(result) == 1

    def test_empty_list_returns_empty(self):
        assert fuse_and_dedup([]) == []

    def test_non_dict_edges_skipped(self):
        result = fuse_and_dedup([None, "bad", {"source_file": "a.py", "target_file": "b.py", "type": "go_import", "confidence": 0.5}])
        assert len(result) == 1

    def test_string_enum_confidence_does_not_crash_fusion(self):
        # java_symbols validation / LLM edges set `confidence` to a STRING enum
        # while keeping the numeric `confidence_score`. A fusable group mixing
        # such an edge with a numeric-only edge must not raise TypeError
        # (previously: str vs float comparison aborted the whole graph build).
        edges = [
            {"source": "na", "target": "nb", "source_file": "A.java",
             "target_file": "B.java", "type": "java_symbol_import",
             "confidence": "EXTRACTED", "confidence_score": 1.0, "resolver": "java_symbols"},
            {"source": "na", "target": "nb", "source_file": "A.java",
             "target_file": "B.java", "type": "java_symbol_call",
             "confidence_score": 0.9, "resolver": "java_lsp"},
        ]
        result = fuse_and_dedup(edges)
        assert len(result) == 1
        # total = 1.0 (string enum -> falls back to confidence_score) + 0.9 -> capped 0.98
        assert result[0]["confidence"] == 0.98
        assert set(result[0]["resolver_sources"]) == {"java_symbols", "java_lsp"}

    def test_non_fusable_string_confidence_does_not_crash(self):
        # Non-fusable family (own-family) with a string-enum confidence edge
        # must pick highest by numeric score without crashing.
        edges = [
            {"source": "na", "target": "nb", "source_file": "a.py", "target_file": "b.py",
             "type": "kafka_publish", "confidence": "AMBIGUOUS", "confidence_score": 0.3},
            {"source": "na", "target": "nb", "source_file": "a.py", "target_file": "b.py",
             "type": "kafka_publish", "confidence_score": 0.8},
        ]
        result = fuse_and_dedup(edges)
        assert len(result) == 1
        assert result[0]["confidence_score"] == 0.8

    def test_deduplicate_entities_still_importable(self):
        from icx_engine.graph.parser.dedup import deduplicate_entities
        assert callable(deduplicate_entities)
