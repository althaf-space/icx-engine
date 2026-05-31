"""Tests for GraphQuerier. Uses a synthetic graph.json fixture."""
from __future__ import annotations
import json
from pathlib import Path
import pytest


@pytest.fixture
def tiny_graph(tmp_path: Path) -> Path:
    """Write a minimal graph.json with 4 nodes + 3 edges for testing."""
    graph = {
        "nodes": [
            {"id": "auth_service", "label": "AuthService", "source_file": "src/auth/service.py",
             "community": 0, "role_tag": "[service]"},
            {"id": "user_repo",    "label": "UserRepository", "source_file": "src/auth/repo.py",
             "community": 0, "role_tag": "[dao]"},
            {"id": "api_routes",   "label": "api_routes", "source_file": "src/api/routes.py",
             "community": 1, "role_tag": "[route]"},
            {"id": "db_session",   "label": "db_session", "source_file": "src/db/session.py",
             "community": 1, "role_tag": ""},
        ],
        "links": [
            {"source": "api_routes",   "target": "auth_service", "relation": "calls",
             "confidence_score": 0.95, "confidence_source": "ts_lsp", "resolver_tag": "ts_lsp"},
            {"source": "auth_service", "target": "user_repo",    "relation": "injects",
             "confidence_score": 0.90, "confidence_source": "spring", "resolver_tag": "spring"},
            {"source": "user_repo",    "target": "db_session",   "relation": "calls",
             "confidence_score": 0.55, "confidence_source": "universal_ast", "resolver_tag": "universal_ast"},
        ],
        "communities": {"0": ["auth_service", "user_repo"], "1": ["api_routes", "db_session"]},
    }
    p = tmp_path / "graph.json"
    p.write_text(json.dumps(graph), encoding="utf-8")
    return p


def test_querier_loads(tiny_graph: Path):
    from icx_engine.graph.query import GraphQuerier
    q = GraphQuerier(tiny_graph)
    assert q.node_count == 4
    assert q.edge_count == 3


def test_find_context_returns_results(tiny_graph: Path):
    from icx_engine.graph.query import GraphQuerier
    q = GraphQuerier(tiny_graph)
    results = q.find_context("auth service")
    assert len(results) >= 1
    top_files = [r.file for r in results]
    assert any("auth" in f for f in top_files)


def test_get_call_chain(tiny_graph: Path):
    from icx_engine.graph.query import GraphQuerier
    q = GraphQuerier(tiny_graph)
    chain = q.get_call_chain("auth_service", depth=2, min_confidence=0.5)
    downstream_ids = [n.node_id for n in chain.downstream]
    assert "user_repo" in downstream_ids
    upstream_ids = [n.node_id for n in chain.upstream]
    assert "api_routes" in upstream_ids


def test_get_call_chain_filters_confidence(tiny_graph: Path):
    from icx_engine.graph.query import GraphQuerier
    q = GraphQuerier(tiny_graph)
    chain = q.get_call_chain("user_repo", depth=2, min_confidence=0.85)
    downstream_ids = [n.node_id for n in chain.downstream]
    assert "db_session" not in downstream_ids


def test_get_impact(tiny_graph: Path):
    from icx_engine.graph.query import GraphQuerier
    q = GraphQuerier(tiny_graph)
    impact = q.get_impact("user_repo", min_confidence=0.5)
    assert "auth_service" in impact.direct
    assert impact.total >= 1


def test_get_subsystem(tiny_graph: Path):
    from icx_engine.graph.query import GraphQuerier
    q = GraphQuerier(tiny_graph)
    result = q.get_subsystem("src/auth/service.py")
    assert "src/auth/service.py" in result.files
    assert "src/auth/repo.py" in result.files
