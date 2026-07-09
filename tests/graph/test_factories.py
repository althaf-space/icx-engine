"""Self-validation for the production-realistic graph factories.

If these pass, `factories.graph_edge` emits the exact edge shape `build.py`
produces, so any test built on the factory exercises real data - closing the
fixture-drift gap that hid BUG-5/6/7.
"""
from __future__ import annotations

import pytest

from graph.factories import (
    confidence_enum,
    graph_edge,
    graph_node,
    build_querier,
    build_graph,
)


class TestConfidenceEnumMatchesBuild:
    @pytest.mark.parametrize("score,expected", [
        (1.0, "EXTRACTED"),
        (0.8, "EXTRACTED"),
        (0.79, "INFERRED"),
        (0.4, "INFERRED"),
        (0.39, "AMBIGUOUS"),
        (0.0, "AMBIGUOUS"),
    ])
    def test_enum_bands(self, score, expected):
        assert confidence_enum(score) == expected

    def test_factory_enum_matches_build_normalization(self):
        # Authoritative check: run the score through build.py's OWN normalization
        # and assert the factory agrees. If build.py changes its thresholds, this
        # fails - forcing the factory (and every test on it) back into sync.
        from icx_engine.graph.parser.build import build_from_json

        for score in (0.95, 0.8, 0.6, 0.4, 0.2):
            nodes = [
                graph_node("a", "a.py"),
                graph_node("b", "b.py"),
            ]
            # Edge with only confidence_score set -> build.py derives `confidence`.
            edge = {
                "source": "a", "target": "b", "relation": "calls", "type": "calls",
                "source_file": "a.py", "target_file": "b.py",
                "confidence_score": score,
            }
            G = build_from_json({"nodes": nodes, "edges": [edge]}, directed=True)
            built = next(iter(G.edges(data=True)))[2]
            assert built["confidence"] == confidence_enum(score), (
                f"factory enum diverged from build.py at score={score}"
            )


class TestFactoryEdgeShape:
    def test_edge_has_both_confidence_keys_correct_types(self):
        e = graph_edge("a", "b", score=0.95, source_file="a.py", target_file="b.py")
        assert isinstance(e["confidence_score"], float)
        assert e["confidence"] == "EXTRACTED"          # string enum, not float
        assert isinstance(e["confidence"], str)

    def test_low_score_edge_is_ambiguous_enum(self):
        e = graph_edge("a", "b", score=0.2)
        assert e["confidence"] == "AMBIGUOUS"
        assert e["confidence_score"] == 0.2

    def test_node_has_required_fields(self):
        from icx_engine.graph.parser.validate import REQUIRED_NODE_FIELDS
        n = graph_node("a", "src/a.py")
        assert REQUIRED_NODE_FIELDS <= set(n)


class TestRoundTripThroughRealBuild:
    def test_factory_graph_builds_and_queries_without_error(self):
        from icx_engine.graph.parser.build import build_from_json
        from icx_engine.graph.parser.validate import validate_extraction

        nodes = [
            graph_node("svc", "src/service.py", community=0, importance=0.9),
            graph_node("repo", "src/repo.py", community=0),
            graph_node("api", "src/api.py", community=1),
        ]
        edges = [
            graph_edge("api", "svc", score=0.95, source_file="src/api.py", target_file="src/service.py"),
            graph_edge("svc", "repo", score=0.55, source_file="src/service.py", target_file="src/repo.py"),
        ]
        extraction = {"nodes": nodes, "edges": edges}
        # Real validator must accept the factory's shape (schema-conformant).
        real_errors = [
            e for e in validate_extraction(extraction)
            if "does not match any node id" not in e
        ]
        assert real_errors == []
        # Real builder must accept it.
        G = build_from_json(extraction, directed=True)
        assert G.number_of_nodes() == 3


class TestRegressionScenariosOnRealShape:
    def test_blast_radius_survives_string_enum_confidence(self, tmp_path):
        # BUG-6 lived here: string-enum `confidence` crashed the numeric compare.
        nodes = [graph_node("core", "core.py"), graph_node("user", "user.py")]
        edges = [graph_edge("user", "core", score=0.95,
                            source_file="user.py", target_file="core.py")]
        q = build_querier(tmp_path, nodes, edges)
        result = q.get_blast_radius(["core.py"], min_confidence=0.5)
        assert "user.py" in (
            result["direct_dependents"] + result["transitive_dependents"]
        )

    def test_fuse_and_dedup_survives_string_enum_confidence(self):
        # BUG-7 lived here: a fusable group with a string-enum edge aborted build.
        from icx_engine.graph.parser.dedup import fuse_and_dedup

        edges = [
            graph_edge("a", "b", score=1.0, etype="java_symbol_import",
                       source_file="A.java", target_file="B.java", resolver="java_symbols"),
            graph_edge("a", "b", score=0.9, etype="java_symbol_call",
                       source_file="A.java", target_file="B.java", resolver="java_symbols_call"),
        ]
        result = fuse_and_dedup(edges)  # must not raise
        assert len(result) == 1
        assert result[0]["confidence"] == 0.98  # fused float, capped
