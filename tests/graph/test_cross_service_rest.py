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


# -- Generic path-argument detection (any wrapper function, not just axios/fetch/etc) --
# Real-world finding: an enterprise codebase routed every HTTP call through its own
# wrapper (e.g. `sendRequest(data, "/getCampaignChannel")`), invisible to the old
# axios/fetch/requests/restTemplate-only patterns. Fix must be shape-based (any call
# passing a path-looking literal), never tied to one project's specific function names.

def test_generic_wrapper_call_detected_regardless_of_function_name(tmp_path):
    from icx_engine.graph.parser.resolvers.cross_service_rest import extract_http_calls

    f = tmp_path / "screen.jsx"
    f.write_text(
        'export function loadStuff() {\n'
        '  return sendRequest(requestData, "/getCampaignChannel");\n'
        '}\n'
        'export function loadOther() {\n'
        '  return myCompanysCustomHttpHelper(null, "/totallyDifferentWrapperName");\n'
        '}\n',
        encoding="utf-8",
    )
    calls = extract_http_calls([f], tmp_path)
    urls = [c["url_pattern"] for c in calls]
    assert "/getCampaignChannel" in urls
    assert "/totallyDifferentWrapperName" in urls


def test_generic_path_arg_excludes_static_assets(tmp_path):
    """A quoted path-shaped string that's actually an asset import must never be treated
    as an API call - false positives here would pollute cross-service links with noise.
    A bare `import x from "/path"` (no parens) never even matches the call-argument shape
    this pattern requires; require("/path.ext") does have call shape, so it's the real
    case that needs the extension-based exclusion."""
    from icx_engine.graph.parser.resolvers.cross_service_rest import extract_http_calls

    f = tmp_path / "screen.jsx"
    f.write_text(
        'const icon = require("/assets/icons/save.svg");\n'
        'const bg = require("/assets/images/background.png");\n',
        encoding="utf-8",
    )
    calls = extract_http_calls([f], tmp_path)
    urls = [c["url_pattern"] for c in calls]
    assert not any("save" in u or "background" in u for u in urls)


def test_generic_path_arg_does_not_duplicate_named_pattern_matches(tmp_path):
    """A call already matched by a named pattern (axios/fetch/etc) must not ALSO be
    double-counted by the generic fallback - both patterns can see the same call site."""
    from icx_engine.graph.parser.resolvers.cross_service_rest import extract_http_calls

    f = tmp_path / "api.js"
    f.write_text('fetch("/api/v1/orders");\n', encoding="utf-8")
    calls = extract_http_calls([f], tmp_path)
    matching = [c for c in calls if c["url_pattern"] == "/api/v1/orders"]
    assert len(matching) == 1


# -- Java route constants resolved by reference, not just inline literals --------------
# Real-world finding: Spring routes are frequently centralized in a constants class
# (e.g. `@GetMapping(MagikServicePath.GET_CAMPAIGN_CHANNEL)`) rather than inlined as
# string literals - the resolver must follow that reference to its real declaration
# anywhere in the project, not assume the literal sits right in the annotation.

def test_method_mapping_resolves_constant_reference_from_another_file(tmp_path):
    from icx_engine.graph.parser.resolvers.cross_service_rest import extract_rest_routes

    (tmp_path / "ServicePaths.java").write_text(
        "package com.example.util;\n"
        "public class ServicePaths {\n"
        '    public static final String GET_CAMPAIGN_CHANNEL = "/getCampaignChannel";\n'
        "}\n",
        encoding="utf-8",
    )
    controller = tmp_path / "CampaignController.java"
    controller.write_text(
        "package com.example.controller;\n"
        "import com.example.util.ServicePaths;\n"
        "public class CampaignController {\n"
        "    @GetMapping(ServicePaths.GET_CAMPAIGN_CHANNEL)\n"
        "    public void getCampaignChannel() {}\n"
        "}\n",
        encoding="utf-8",
    )
    java_files = [tmp_path / "ServicePaths.java", controller]
    routes = extract_rest_routes(java_files, tmp_path)
    url_patterns = [r["url_pattern"] for r in routes]
    assert "/getCampaignChannel" in url_patterns


def test_class_mapping_resolves_constant_reference(tmp_path):
    from icx_engine.graph.parser.resolvers.cross_service_rest import extract_rest_routes

    (tmp_path / "ServicePaths.java").write_text(
        "package com.example.util;\n"
        "public class ServicePaths {\n"
        '    public static final String BASE = "/api/v2";\n'
        "}\n",
        encoding="utf-8",
    )
    controller = tmp_path / "OrdersController.java"
    controller.write_text(
        "package com.example.controller;\n"
        "@RequestMapping(ServicePaths.BASE)\n"
        "public class OrdersController {\n"
        '    @GetMapping("/orders")\n'
        "    public void list() {}\n"
        "}\n",
        encoding="utf-8",
    )
    java_files = [tmp_path / "ServicePaths.java", controller]
    routes = extract_rest_routes(java_files, tmp_path)
    url_patterns = [r["url_pattern"] for r in routes]
    assert "/api/v2/orders" in url_patterns


def test_unresolvable_constant_reference_is_skipped_not_fabricated(tmp_path):
    """If the referenced constant can't be found anywhere in the project (e.g. it's
    declared in a library ICX doesn't have source for), the route must be silently
    skipped - never guess or fabricate a path."""
    from icx_engine.graph.parser.resolvers.cross_service_rest import extract_rest_routes

    controller = tmp_path / "MysteryController.java"
    controller.write_text(
        "package com.example.controller;\n"
        "public class MysteryController {\n"
        "    @GetMapping(SomeExternalLib.UNKNOWN_PATH)\n"
        "    public void mystery() {}\n"
        "}\n",
        encoding="utf-8",
    )
    routes = extract_rest_routes([controller], tmp_path)
    assert routes == []


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


# -- Same-project matching: real graph edges, not just cross_links.json ----------------
# A frontend calling its own backend in the same monorepo - previously produced nothing
# at all, since the resolver explicitly skipped matching against the current project.

def test_same_project_matching_returns_real_graph_edges(tmp_path):
    from icx_engine.graph.parser.resolvers.cross_service_rest import run_cross_service_linking

    if not _FIXTURE.exists():
        pytest.skip("fixture not found")

    files = list((_FIXTURE / "ui").rglob("*.js")) + list((_FIXTURE / "svc").rglob("*.java"))
    api_js = _FIXTURE / "ui" / "src" / "api.js"
    order_ctrl = _FIXTURE / "svc" / "src" / "main" / "java" / "com" / "example" / "OrderController.java"
    user_ctrl = _FIXTURE / "svc" / "src" / "main" / "java" / "com" / "example" / "UserController.java"

    extraction = {
        "nodes": [
            {"id": "api_js_node", "label": "api.js", "source_file": str(api_js).replace("\\", "/")},
            {"id": "order_ctrl_node", "label": "OrderController", "source_file": str(order_ctrl).replace("\\", "/")},
            {"id": "user_ctrl_node", "label": "UserController", "source_file": str(user_ctrl).replace("\\", "/")},
        ],
        "edges": [],
    }

    # graphs_root points at an empty tmp dir (no registry.json) - never touches the real
    # ~/.icx/graphs, and out_dir is a tmp dir too - never writes into the repo fixture.
    edges = run_cross_service_linking(
        files, _FIXTURE, extraction, out_dir=tmp_path / "out", graphs_root=tmp_path / "graphs",
    )

    assert edges, "expected same-project calls_api edges, got none"
    for e in edges:
        assert e["relation"] == "calls_api"
        assert e["source"] == "api_js_node"
        assert e["target"] in ("order_ctrl_node", "user_ctrl_node")
        assert e["confidence_score"] == 0.85
        assert e["confidence_source"] == "cross_service_rest"


def test_same_project_matching_skips_source_equals_target_file():
    """A call and the route it matches living in the SAME file must not produce a
    self-loop edge - that's noise (e.g. a route registration file also containing a
    stray matching call string), not a real cross-layer dependency."""
    from icx_engine.graph.parser.resolvers.cross_service_rest import run_cross_service_linking

    project_root = Path(__file__).parent  # any real dir; only used to resolve relative paths
    src = (
        'fetch("/api/v1/self");\n'
        '@app.get("/api/v1/self")\n'
        'def handler(): pass\n'
    )
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        f = td_path / "combined.py"
        f.write_text(src, encoding="utf-8")
        extraction = {
            "nodes": [{"id": "combined_node", "label": "combined.py", "source_file": "combined.py"}],
            "edges": [],
        }
        edges = run_cross_service_linking(
            [f], td_path, extraction, out_dir=td_path, graphs_root=td_path / "empty_graphs",
        )
        assert edges == []


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


# -- Standard (not custom-team) framework conventions previously missed ----------------
# Found by checking real usage: FastAPI's own officially-recommended multi-router
# structure, and two ordinary Spring Boot idioms - none of these are one team's quirk.

def test_fastapi_router_prefix_is_resolved(tmp_path):
    """APIRouter(prefix=...) is FastAPI's documented way to structure any app with more
    than one file of routes - the prefix must not be silently dropped."""
    from icx_engine.graph.parser.resolvers.cross_service_rest import extract_rest_routes

    f = tmp_path / "orders.py"
    f.write_text(
        'from fastapi import APIRouter\n'
        'router = APIRouter(prefix="/orders")\n\n'
        '@router.get("/")\n'
        'def list_orders():\n'
        '    pass\n\n'
        '@router.get("/{order_id}")\n'
        'def get_order(order_id: int):\n'
        '    pass\n',
        encoding="utf-8",
    )
    routes = extract_rest_routes([f], tmp_path)
    urls = [r["url_pattern"] for r in routes]
    assert "/orders" in urls
    assert "/orders/*" in urls


def test_fastapi_router_prefix_not_tied_to_literal_variable_name(tmp_path):
    """The router variable can be named anything - detection must not hardcode
    literally "router" or "app"."""
    from icx_engine.graph.parser.resolvers.cross_service_rest import extract_rest_routes

    f = tmp_path / "orders.py"
    f.write_text(
        'from fastapi import APIRouter\n'
        'orders_api = APIRouter(prefix="/orders")\n\n'
        '@orders_api.get("/summary")\n'
        'def summary():\n'
        '    pass\n',
        encoding="utf-8",
    )
    routes = extract_rest_routes([f], tmp_path)
    urls = [r["url_pattern"] for r in routes]
    assert "/orders/summary" in urls


def test_fastapi_plain_app_without_router_prefix_still_works(tmp_path):
    """The single-file app.get(...) style (no APIRouter/prefix at all) must keep
    working unchanged."""
    from icx_engine.graph.parser.resolvers.cross_service_rest import extract_rest_routes

    f = tmp_path / "main.py"
    f.write_text(
        'from fastapi import FastAPI\n'
        'app = FastAPI()\n\n'
        '@app.get("/health")\n'
        'def health():\n'
        '    pass\n',
        encoding="utf-8",
    )
    routes = extract_rest_routes([f], tmp_path)
    urls = [r["url_pattern"] for r in routes]
    assert "/health" in urls


def test_spring_path_attribute_alias_resolved(tmp_path):
    """`path = "..."` is a standard alias for `value = "..."` in real Spring
    annotations, not a custom convention - must resolve the same way."""
    from icx_engine.graph.parser.resolvers.cross_service_rest import extract_rest_routes

    f = tmp_path / "OrderController.java"
    f.write_text(
        "package com.example;\n"
        '@RequestMapping("/api/orders")\n'
        "public class OrderController {\n"
        '    @PostMapping(path = "/create")\n'
        "    public void create() {}\n"
        "}\n",
        encoding="utf-8",
    )
    routes = extract_rest_routes([f], tmp_path)
    urls = [r["url_pattern"] for r in routes]
    assert "/api/orders/create" in urls


def test_spring_class_level_mapping_not_double_counted_as_method_route(tmp_path):
    """Regression guard for a pre-existing bug (confirmed present before this fix too):
    the class-level @RequestMapping must not also be picked up by the method-mapping
    scan as if it were a separate method route, producing a garbled
    class_prefix+class_prefix entry."""
    from icx_engine.graph.parser.resolvers.cross_service_rest import extract_rest_routes

    f = tmp_path / "OrderController.java"
    f.write_text(
        "package com.example;\n"
        '@RequestMapping("/api/orders")\n'
        "public class OrderController {\n"
        "    @GetMapping\n"
        "    public void list() {}\n"
        "}\n",
        encoding="utf-8",
    )
    routes = extract_rest_routes([f], tmp_path)
    urls = [r["url_pattern"] for r in routes]
    assert urls == ["/api/orders"]
