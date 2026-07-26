"""Built-in security cases (SQLi/auth woven into API)."""
from __future__ import annotations

from icx_engine.testing.analyzers.security_cases import (
    api_security_requests, api_headers_check,
    INJECTION, LEAK_MARKERS, SECURITY_HEADERS,
)


def test_api_sqli_probe_must_not_500():
    ep = {"method": "GET", "path": "/users", "auth": {"required": False}}
    files = api_security_requests(ep)
    names = [n for n, _ in files]
    assert any("sqli" in n for n in names)
    sqli = next(c for n, c in files if "sqli" in n)
    assert "OR '1'='1" in sqli
    assert "[Asserts]" in sqli and "status < 500" in sqli


def test_api_sqli_in_body_for_post():
    ep = {"method": "POST", "path": "/users", "requestBody": {"fields": [{"name": "email"}, {"name": "name"}]}}
    sqli = next(c for n, c in api_security_requests(ep) if "sqli" in n)
    assert '"email":' in sqli and '"name":' in sqli and "OR '1'='1" in sqli


def test_api_auth_case_only_when_auth_required():
    with_auth = api_security_requests({"method": "GET", "path": "/admin", "auth": {"required": True}})
    assert any("auth" in n for n, _ in with_auth)
    auth = next(c for n, c in with_auth if "auth" in n)
    assert "status >= 400" in auth and "status < 404" in auth   # 401/403
    no_auth = api_security_requests({"method": "GET", "path": "/public", "auth": {"required": False}})
    assert not any("auth" in n for n, _ in no_auth)


def test_api_path_tokens_substituted_in_security():
    ep = {"method": "GET", "path": "/users/{id}/roles/{rid}", "auth": {"required": True}}
    for _n, c in api_security_requests(ep):
        assert "{id}" not in c and "{rid}" not in c


def test_api_all_injection_classes_generated():
    files = api_security_requests({"method": "GET", "path": "/x", "auth": {"required": False}})
    names = " ".join(n for n, _ in files)
    for cls in INJECTION:                       # sqli, nosql, command, template, path
        assert cls in names
    # each injection asserts no-500 AND no engine-error leak in the body
    inj = next(c for n, c in files if "inj_sqli" in n)
    assert "status < 500" in inj
    assert any(f'not contains "{m}"' in inj for m in LEAK_MARKERS)


def test_api_mass_assignment_case():
    ep = {"method": "POST", "path": "/users", "requestBody": {"fields": [{"name": "email"}]}}
    files = api_security_requests(ep)
    massa = next(c for n, c in files if "massassign" in n)
    assert '"isAdmin":' in massa and '"role": "admin"' in massa
    assert "status < 500" in massa


def test_api_mass_assignment_skipped_without_body():
    files = api_security_requests({"method": "GET", "path": "/users"})
    assert not any("massassign" in n for n, _ in files)


def test_api_headers_audit():
    name, content = api_headers_check("/users/{id}")
    assert "sec_headers" in name
    for h in SECURITY_HEADERS:
        assert f'header "{h}" exists' in content
    assert "{id}" not in content
