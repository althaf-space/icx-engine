"""Tests for cross_service_rest resolver."""
from __future__ import annotations
from pathlib import Path
import pytest


_FIXTURE = Path(__file__).parent / "eval" / "fixtures" / "cross_service_sample"


def test_extract_http_calls_from_js():
    from icx_engine.graph.parser.resolvers.cross_service_rest import extract_http_calls

    if not _FIXTURE.exists():
        pytest.skip("fixture not found")

    ui_root = _FIXTURE / "ui"
    calls = extract_http_calls(list(ui_root.rglob("*.js")), ui_root)
    urls = [c["url_pattern"] for c in calls]
    assert "/api/v1/orders" in urls
    assert "/api/v1/users" in urls


def test_extract_rest_routes_from_java():
    from icx_engine.graph.parser.resolvers.cross_service_rest import extract_rest_routes

    if not _FIXTURE.exists():
        pytest.skip("fixture not found")

    svc_root = _FIXTURE / "svc"
    java_files = list(svc_root.rglob("*.java"))
    routes = extract_rest_routes(java_files, svc_root)
    url_patterns = [r["url_pattern"] for r in routes]
    assert "/api/v1/orders" in url_patterns
    assert "/api/v1/users" in url_patterns


def test_normalize_url_strips_path_params():
    from icx_engine.graph.parser.resolvers.cross_service_rest import normalize_url
    assert normalize_url("/api/v1/orders/{id}") == "/api/v1/orders/*"
    assert normalize_url("/api/v1/users/:id/posts") == "/api/v1/users/*/posts"


def test_normalize_url_strips_trailing_slash():
    from icx_engine.graph.parser.resolvers.cross_service_rest import normalize_url
    assert normalize_url("/api/v1/orders/") == "/api/v1/orders"


def test_normalize_url_strips_query_string():
    from icx_engine.graph.parser.resolvers.cross_service_rest import normalize_url
    assert normalize_url("/api/v1/orders?page=1") == "/api/v1/orders"


def test_match_calls_to_routes():
    from icx_engine.graph.parser.resolvers.cross_service_rest import (
        extract_http_calls, extract_rest_routes, match_calls_to_routes,
    )

    if not _FIXTURE.exists():
        pytest.skip("fixture not found")

    ui_root = _FIXTURE / "ui"
    svc_root = _FIXTURE / "svc"

    calls = extract_http_calls(list(ui_root.rglob("*.js")), ui_root)
    routes = extract_rest_routes(list(svc_root.rglob("*.java")), svc_root)
    matches = match_calls_to_routes(calls, routes, caller_project_id="ui_proj", callee_project_id="svc_proj")

    assert len(matches) >= 2, f"expected >=2 matches, got {matches}"
    for m in matches:
        assert "source_file" in m
        assert "target_file" in m
        assert "url_pattern" in m
        assert 0.0 <= m["confidence"] <= 1.0


def test_run_cross_service_linking_writes_file(tmp_path):
    from icx_engine.graph.parser.resolvers.cross_service_rest import run_cross_service_linking
    import json

    if not _FIXTURE.exists():
        pytest.skip("fixture not found")

    graphs_dir = tmp_path / "graphs"
    svc_project_id = "svc001"
    svc_graph_dir = graphs_dir / svc_project_id
    svc_graph_dir.mkdir(parents=True)

    svc_graph = {
        "nodes": [{"id": "order_ctrl", "label": "OrderController.java",
                   "source_file": "src/main/java/com/example/OrderController.java",
                   "file_type": "code"}],
        "links": [],
    }
    (svc_graph_dir / "graph.json").write_text(json.dumps(svc_graph), encoding="utf-8")

    meta = {"project_root": str(_FIXTURE / "svc"), "name": "svc", "project_id": svc_project_id}
    (svc_graph_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")

    registry = [{"name": "svc", "path": str(_FIXTURE / "svc"), "project_id": svc_project_id}]
    (graphs_dir / "registry.json").write_text(json.dumps(registry), encoding="utf-8")

    ui_project_id = "ui001"
    ui_graph_dir = graphs_dir / ui_project_id
    ui_graph_dir.mkdir(parents=True)

    ui_files = list((_FIXTURE / "ui").rglob("*.js"))
    ui_extraction = {
        "nodes": [{"id": "api_js", "label": "api.js",
                   "source_file": str(_FIXTURE / "ui" / "src" / "api.js"),
                   "file_type": "code"}],
        "edges": [],
    }

    run_cross_service_linking(
        ui_files,
        _FIXTURE / "ui",
        ui_extraction,
        ui_graph_dir,
        graphs_root=graphs_dir,
    )

    cross_links_file = ui_graph_dir / "cross_links.json"
    assert cross_links_file.exists(), "cross_links.json not written"
    data = json.loads(cross_links_file.read_text(encoding="utf-8"))
    assert "links" in data
    assert "generated_at" in data
