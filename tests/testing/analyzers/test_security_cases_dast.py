"""Phase B DAST expansion: extra injection classes, object-id robustness, expanded headers."""
from __future__ import annotations

from icx_engine.testing.analyzers.security_cases import (
    api_security_requests, api_headers_check,
    INJECTION, SECURITY_HEADERS,
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
