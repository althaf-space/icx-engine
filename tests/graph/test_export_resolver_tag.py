"""Tests for tier-based resolver_tag fallback in export.py."""
from __future__ import annotations


def _apply_tag(link: dict) -> dict:
    """Apply the resolver_tag logic from export.py to a link dict (extracted for testing)."""
    from icx_engine.graph.parser.export import _resolve_resolver_tag
    link = dict(link)
    link["resolver_tag"] = _resolve_resolver_tag(link)
    return link


def test_confidence_source_takes_precedence():
    link = _apply_tag({"relation": "calls", "confidence_source": "java_symbols", "confidence_score": 0.95})
    assert link["resolver_tag"] == "java_symbols"


def test_contains_edge_gets_ast_structural():
    link = _apply_tag({"relation": "contains", "confidence_score": 1.0})
    assert link["resolver_tag"] == "ast_structural"


def test_boundary_0_95_gets_ast_direct():
    link = _apply_tag({"relation": "calls", "confidence_score": 0.95})
    assert link["resolver_tag"] == "ast_direct"


def test_boundary_0_8_gets_ast_inferred():
    link = _apply_tag({"relation": "calls", "confidence_score": 0.8})
    assert link["resolver_tag"] == "ast_inferred"


def test_high_conf_no_source_gets_ast_direct():
    link = _apply_tag({"relation": "calls", "confidence_score": 0.99})
    assert link["resolver_tag"] == "ast_direct"


def test_mid_conf_gets_ast_inferred():
    link = _apply_tag({"relation": "uses", "confidence_score": 0.82})
    assert link["resolver_tag"] == "ast_inferred"


def test_low_conf_gets_ast_low():
    link = _apply_tag({"relation": "uses", "confidence_score": 0.55})
    assert link["resolver_tag"] == "ast_low"


def test_missing_score_defaults_to_ast_direct():
    link = _apply_tag({"relation": "imports"})
    assert link["resolver_tag"] == "ast_direct"


def test_existing_resolver_tag_not_overwritten():
    from icx_engine.graph.parser.export import _resolve_resolver_tag
    assert _resolve_resolver_tag({"relation": "contains", "edge_type": "contains"}) == "ast_structural"
