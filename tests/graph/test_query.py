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


@pytest.fixture
def false_substring_graph(tmp_path: Path) -> Path:
    """A node whose label contains "and" as a raw substring (inside "ReferAndWin...") but
    never as a whole word - reproduces the exact false-match shape reported against real
    Java code (e.g. "and" inside "ApprovalDaoImpl" is not this literal case, but the same
    class of bug: a short query term matching inside an unrelated identifier)."""
    graph = {
        "nodes": [
            # importance > 0 with zero degree/edges so a false "and" substring match would
            # still produce a nonzero score under the old bug (base=0 path still returns
            # kw_score * 0.1 * importance) - without this the test would pass vacuously
            # regardless of whether the bug is present, since a zero-degree/zero-importance
            # node scores 0 either way.
            {"id": "promo_ctrl", "label": "ReferAndWinRestController",
             "source_file": "src/promo/referandwin.py", "community": 0, "role_tag": "",
             "importance": 0.5},
            {"id": "unrelated", "label": "PaymentGateway",
             "source_file": "src/payments/gateway.py", "community": 1, "role_tag": "",
             "importance": 0.5},
        ],
        "links": [],
        "communities": {"0": ["promo_ctrl"], "1": ["unrelated"]},
    }
    p = tmp_path / "graph.json"
    p.write_text(json.dumps(graph), encoding="utf-8")
    return p


def test_find_context_ignores_short_word_false_substring_match(false_substring_graph: Path):
    """"and" must not false-match inside "ReferAndWinRestController" merely because it is
    a raw substring there - regression guard for the word-boundary fix in
    _score_node/find_context. Query terms are "review and summary": "and" is a real
    substring of the promo controller's label but never a whole word in either node, so
    under the old `t in text` substring bug this node would score/match on "and"; after
    the fix it must not."""
    from icx_engine.graph.query import GraphQuerier
    q = GraphQuerier(false_substring_graph)
    results = q.find_context("review and summary")
    # None of "review"/"and"/"summary" is a whole-word match anywhere in this fixture, so
    # with the word-boundary fix the promo controller must score 0 and be excluded entirely
    # (under the old substring bug it would false-match "and" and appear here).
    assert "promo_ctrl" not in [r.node_id for r in results]
    for r in results:
        assert "'and'" not in r.reason


def test_find_context_matches_terms_inside_snake_case_and_constant_case_identifiers(tmp_path: Path):
    """Critical regression guard: Python's `\\b` treats "_" as a word character, so a naive
    `\\bterm\\b` fix would fail to match "user"/"master" inside "MFS_USER_MASTER" or a
    snake_case source path - breaking exactly the kind of query this scorer must support
    (e.g. "ApprovalDaoImpl reads and writes MFS_USER_MASTER via UserMaster entity"). The
    boundary must treat "_" as a separator, not as part of the word."""
    from icx_engine.graph.query import GraphQuerier
    graph = {
        "nodes": [
            {"id": "user_master_dao", "label": "MFS_USER_MASTER",
             "source_file": "src/dao/approval_dao_impl.java", "community": 0, "role_tag": "",
             "importance": 0.5},
        ],
        "links": [],
        "communities": {"0": ["user_master_dao"]},
    }
    p = tmp_path / "graph.json"
    p.write_text(json.dumps(graph), encoding="utf-8")
    q = GraphQuerier(p)
    # find_context only splits the task on whitespace (never on "_" or camelCase), so
    # "user" and "master" must be given as separate whitespace-delimited query words to
    # exercise matching them individually inside the underscore-joined "mfs_user_master".
    results = q.find_context("user master reads")
    assert len(results) == 1
    assert "user_master_dao" == results[0].node_id
    assert "'user'" in results[0].reason
    assert "'master'" in results[0].reason


def test_find_context_still_matches_whole_word_terms(tiny_graph: Path):
    """Regression guard: the word-boundary fix must not over-tighten matching - a real
    whole-word term that legitimately appears inside an identifier (e.g. "db" inside the
    node id/label "db_session") must still match."""
    from icx_engine.graph.query import GraphQuerier
    q = GraphQuerier(tiny_graph)
    results = q.find_context("db session")
    matched_files = [r.file for r in results]
    assert any("session" in f for f in matched_files)


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
