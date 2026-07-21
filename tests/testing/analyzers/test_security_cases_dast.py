"""Phase B DAST expansion: extra injection classes, object-id robustness, expanded headers, URL DOM-XSS."""
from __future__ import annotations

from icx_engine.testing.analyzers.security_cases import (
    api_security_requests, api_headers_check, ui_url_xss_steps,
    INJECTION, SECURITY_HEADERS, XSS_MARKER, XSS_SAFE_EXPR,
)


def test_new_injection_classes_present():
    for cls in ("ldap", "xpath", "crlf"):
        assert cls in INJECTION
    files = api_security_requests({"method": "GET", "path": "/x", "auth": {"required": False}})
    names = " ".join(n for n, _ in files)
    for cls in ("ldap", "xpath", "crlf"):
        assert f"inj_{cls}" in names


def test_object_id_probe_only_for_id_token_endpoints():
    with_id = api_security_requests({"method": "GET", "path": "/users/{id}", "auth": {"required": True}})
    assert any("objid" in n for n, _ in with_id)
    objid = next(c for n, c in with_id if "objid" in n)
    assert "999999999" in objid and "status < 500" in objid
    assert "{id}" not in objid
    no_id = api_security_requests({"method": "GET", "path": "/users", "auth": {"required": False}})
    assert not any("objid" in n for n, _ in no_id)


def test_expanded_security_headers_audited():
    _n, content = api_headers_check("/x")
    for h in ("Strict-Transport-Security", "Referrer-Policy"):
        assert h in SECURITY_HEADERS
        assert f'header "{h}" exists' in content


def test_ui_url_xss_probe_steps():
    steps = ui_url_xss_steps("http://x/#/users", "table")
    actions = [s["action"] for s in steps]
    assert actions == ["goto", "waitfor", "assertjs"]
    assert XSS_MARKER in steps[0]["target"]         # canary encoded into the URL
    assert steps[2]["target"] == XSS_SAFE_EXPR       # asserts it did not execute
    assert ui_url_xss_steps("", "table") == []


def test_ui_url_xss_uses_ampersand_when_query_present():
    steps = ui_url_xss_steps("http://x/page?a=1", "body")
    assert "?a=1&q=" in steps[0]["target"]
