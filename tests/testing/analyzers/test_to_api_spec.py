"""Backend census -> OpenAPI + hurl conversion."""
from __future__ import annotations

import json

from icx_engine.testing.analyzers.to_api_spec import (
    census_to_openapi, census_to_hurl, materialize_api_spec,
)


def _model():
    return {
        "endpoints": [
            {
                "id": "EP_001", "method": "POST", "path": "/users/{orgId}/create",
                "pathParams": [{"name": "orgId", "type": "int"}],
                "queryParams": [{"name": "notify", "type": "bool", "required": False}],
                "headers": [{"name": "X-Trace", "required": True}],
                "requestBody": {"fields": [
                    {"name": "email", "type": "str", "required": True, "happyExample": "a@b.com"},
                    {"name": "age", "type": "int", "required": False},
                ]},
                "successResponses": [{"status": 201}],
                "errorCatalog": [{"status": 422, "errorCode": "VALIDATION"}],
                "auth": {"required": True, "onFailure": {"status": 401}},
            },
            {"id": "EP_002", "method": "GET", "path": "/users", "successResponses": [{"status": 200}]},
        ]
    }


def test_openapi_paths_methods_params():
    doc = census_to_openapi(_model(), base_url="http://svc")
    assert doc["openapi"].startswith("3.")
    assert doc["servers"][0]["url"] == "http://svc"
    post = doc["paths"]["/users/{orgId}/create"]["post"]
    kinds = {(p["name"], p["in"]) for p in post["parameters"]}
    assert ("orgId", "path") in kinds and ("notify", "query") in kinds and ("X-Trace", "header") in kinds
    # request body schema + required
    schema = post["requestBody"]["content"]["application/json"]["schema"]
    assert schema["properties"]["email"]["type"] == "string"
    assert schema["properties"]["age"]["type"] == "integer"
    assert schema["required"] == ["email"]
    # responses include success, error, and auth-failure
    assert set(post["responses"]) >= {"201", "422", "401"}
    assert doc["paths"]["/users"]["get"]["responses"]["200"]


def test_openapi_defaults_on_sparse_endpoint():
    doc = census_to_openapi({"endpoints": [{"method": "GET", "path": "ping"}]})
    op = doc["paths"]["/ping"]["get"]
    assert op["responses"] == {"200": {"description": "success"}}   # normalized path + default 200


def test_hurl_per_endpoint_with_body_and_status():
    files = census_to_hurl(_model())
    assert len(files) == 2
    names = [n for n, _ in files]
    post = next(c for n, c in files if "post" in n)
    assert "POST {{base}}/users/1/create" in post          # path param substituted
    assert "Content-Type: application/json" in post and '"email": "a@b.com"' in post
    assert "HTTP 201" in post
    get = next(c for n, c in files if "get" in n)
    assert "GET {{base}}/users" in get and "HTTP 200" in get


def test_materialize_writes_spec_and_hurl(tmp_path):
    res = materialize_api_spec(_model(), tmp_path, base_url="http://svc")
    assert res["endpoints"] == 2
    oa = tmp_path / ".icx-apispec" / "openapi.json"
    assert oa.exists()
    doc = json.loads(oa.read_text(encoding="utf-8"))
    assert "/users" in doc["paths"]
    hurls = [p.name for p in (tmp_path / ".icx-apispec").glob("*.hurl")]
    # 2 functional (one per endpoint) + security cases woven in
    assert sum(1 for n in hurls if "sec_" not in n) == 2
    assert any("sec_" in n for n in hurls)


def test_path_param_synthesized_when_missing_from_census():
    # census under-reports a path param: {orgId} in the path but empty pathParams. Both the OpenAPI
    # (else invalid -> schemathesis may reject the whole run) and the hurl (else literal {orgId} 404s)
    # must synthesize it.
    model = {"endpoints": [{"id": "E", "method": "GET", "path": "/orgs/{orgId}/users/{uid}",
                            "pathParams": [], "successResponses": [{"status": 200}]}]}
    doc = census_to_openapi(model)
    op = doc["paths"]["/orgs/{orgId}/users/{uid}"]["get"]
    path_params = {p["name"] for p in op["parameters"] if p["in"] == "path"}
    assert path_params == {"orgId", "uid"}          # both synthesized, both required
    assert all(p["required"] for p in op["parameters"] if p["in"] == "path")
    hurl = census_to_hurl(model)[0][1]
    assert "{orgId}" not in hurl and "{uid}" not in hurl    # every token substituted
    assert "GET {{base}}/orgs/1/users/1" in hurl


def test_security_hurl_woven_into_materialize(tmp_path):
    # every endpoint gets the full security suite (injection classes + mass-assign + auth) + one
    # app-wide headers audit, always-on.
    res = materialize_api_spec(_model(), tmp_path, base_url="http://svc")
    assert res.get("security_cases", 0) >= 6
    d = tmp_path / ".icx-apispec"
    names = [p.name for p in d.glob("*.hurl")]
    assert any("inj_sqli" in n for n in names)
    assert any("inj_nosql" in n for n in names)
    assert any("massassign" in n for n in names)
    assert any("auth" in n for n in names)          # EP_001 has auth.required
    assert any("sec_headers" in n for n in names)
    sqli = next(p for p in d.glob("*inj_sqli*.hurl"))
    assert "status < 500" in sqli.read_text(encoding="utf-8")


def test_materialize_empty_on_no_endpoints(tmp_path):
    assert materialize_api_spec({"endpoints": []}, tmp_path) == {}
    assert materialize_api_spec({}, tmp_path) == {}


def test_generated_schema_is_found_by_api_runner(tmp_path):
    # the schemathesis adapter must discover the generated openapi.json
    materialize_api_spec(_model(), tmp_path, base_url="http://svc")
    from icx_engine.testing.runners.api import _find_schema
    found = _find_schema(tmp_path)
    assert found and found.endswith("openapi.json") and ".icx-apispec" in found
