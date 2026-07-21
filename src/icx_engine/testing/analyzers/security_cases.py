"""Built-in security test cases, woven into whatever method the user selected (Playwright UI / API).

NOT a separate scanner - these are deterministic, offline cases generated INTO the same flow/spec:
  - UI  (Playwright): inject an XSS canary into inputs, submit, assert it did NOT execute (and, where
                      reflected, that it is escaped). The canary sets a neutral window flag only if the
                      app renders it unescaped - so a passing case = the field is XSS-safe.
  - API (hurl):       send SQLi / injection payloads in params + bodies and assert the server did not
                      500 (crash/leak); for auth-gated endpoints, an unauthenticated call must 401/403.

Payloads are the well-known OWASP / nuclei set. Always-on: every UI create/edit and every API endpoint
gets its security cases so "what security tests to create" is a rule, not a per-run choice.
"""
from __future__ import annotations

# The XSS canary sets a window flag ONLY if the browser executes it (i.e. the app rendered it
# unescaped). The UI harness `assertjs` step then asserts the flag is absent -> field is safe.
XSS_MARKER = "__xss_probe"
XSS_PAYLOAD = f'"><img src=x onerror="window.{XSS_MARKER}=1">'
XSS_SAFE_EXPR = f"window.{XSS_MARKER} === undefined"

# Injection payloads per class - each is sent through the selected method and the server must not 500
# (a 500 means it reached the engine unhandled) and must not leak an engine error in the body.
INJECTION = {
    "sqli":     "' OR '1'='1",
    "nosql":    '{"$ne": null}',
    "command":  "; id",
    "template":  "${7*7}",
    "path":     "../../../../etc/passwd",
    "ldap":     "*)(uid=*))(|(uid=*",
    "xpath":    "' or '1'='1",
    "crlf":     "test%0d%0aSet-Cookie:injected=1",
}
SQLI_PAYLOADS = [INJECTION["sqli"], "1; DROP TABLE users--", "' UNION SELECT NULL--"]

# Response substrings that mean the server leaked an internal error (info disclosure / unhandled).
LEAK_MARKERS = ["SQLException", "SQLSTATE", "ORA-", "syntax error", "Traceback (most recent",
                "at java.", "at org.springframework", "Warning: mysql", "MongoError",
                "stack trace", "Exception in thread"]

# Fields an attacker adds hoping the server binds them (mass assignment / privilege escalation).
MASS_ASSIGN_FIELDS = {"isAdmin": True, "is_admin": True, "role": "admin", "is_staff": True,
                      "id": 999999, "verified": True}

# Response security headers every app should send (audited via hurl `header ... exists`).
SECURITY_HEADERS = ["X-Content-Type-Options", "X-Frame-Options", "Content-Security-Policy",
                    "Strict-Transport-Security", "Referrer-Policy"]

# Out-of-band object ids used to probe object-level robustness (IDOR-adjacent): a hostile/non-existent
# id must not crash the server (500) or leak an engine error - it should 401/403/404.
OOB_OBJECT_IDS = ["999999999", "0", "-1"]


# UI XSS is woven directly in to_flow._emit_form: the create/update write submit fills every text
# field with XSS_PAYLOAD (a valid non-empty string) and then asserts XSS_SAFE_EXPR, so the mutation
# and the XSS check are ONE submit (no reopen, no loader race). The constants below are the contract.


def ui_input_security_steps(selector: str, anchor: str, name: str = "input") -> list[dict]:
    """Security probe for ANY free-text input that reaches a backend (search boxes, filters, query
    fields - not just create/edit form fields). Types an XSS canary and asserts it does not execute
    (reflected XSS), then types a SQLi payload and asserts the app stays up (no white-screen / crash
    from a hostile query reaching the backend). Returns [] when there is no selector to test."""
    if not selector:
        return []
    a = anchor or "body"
    return [
        {"action": "fill", "target": selector, "value": XSS_PAYLOAD,
         "description": f"SECURITY(XSS): reflected payload in {name}"},
        {"action": "waitfor", "target": a, "value": "", "description": f"{name}: reacts to payload"},
        {"action": "assertjs", "target": XSS_SAFE_EXPR, "value": "",
         "description": f"SECURITY(XSS): reflected input in {name} did NOT execute"},
        {"action": "fill", "target": selector, "value": SQLI_PAYLOADS[0],
         "description": f"SECURITY(SQLi): hostile query in {name}"},
        {"action": "waitfor", "target": a, "value": "",
         "description": f"SECURITY(SQLi): {name} stays up under a hostile query (no crash)"},
    ]


def ui_url_xss_steps(url: str, anchor: str) -> list[dict]:
    """Reflected DOM-XSS probe via the URL: load the screen with an XSS canary in a query param, then
    assert the app did not execute it (a screen that reflects location into the DOM unescaped would).
    Returns [] when there is no URL/anchor to test."""
    if not url:
        return []
    a = anchor or "body"
    # URL-encoded <img src=x onerror=window.__xss_probe=1>
    enc = f"%3Cimg%20src%3Dx%20onerror%3Dwindow.{XSS_MARKER}%3D1%3E"
    sep = "&" if "?" in url else "?"
    hostile = f"{url}{sep}q={enc}"
    return [
        {"action": "goto", "target": hostile, "value": "",
         "description": "SECURITY(DOM-XSS): load the screen with a hostile URL parameter"},
        {"action": "waitfor", "target": a, "value": "",
         "description": "SECURITY(DOM-XSS): screen settles"},
        {"action": "assertjs", "target": XSS_SAFE_EXPR, "value": "",
         "description": "SECURITY(DOM-XSS): reflected URL parameter did NOT execute"},
    ]


# -- API (hurl) ---------------------------------------------------------------

def _hurl_lines(method: str, path: str, base_var: str, body: dict | None) -> list[str]:
    lines = [f"{method} {{{{{base_var}}}}}{path}"]
    if body is not None and method in ("POST", "PUT", "PATCH"):
        import json as _json
        lines += ["Content-Type: application/json", "```json", _json.dumps(body, indent=2), "```"]
    return lines


def _leak_asserts() -> list[str]:
    return [f'body not contains "{m}"' for m in LEAK_MARKERS]


def _body_fields(endpoint: dict, method: str) -> list[str]:
    body = endpoint.get("requestBody") or {}
    fields = body.get("fields") if isinstance(body, dict) else None
    if isinstance(fields, list) and method in ("POST", "PUT", "PATCH"):
        return [f["name"] for f in fields if isinstance(f, dict) and f.get("name")]
    return []


def api_security_requests(endpoint: dict, base_var: str = "base") -> list[tuple[str, str]]:
    """Full per-endpoint security suite as hurl files, woven into the same API run:
      - injection (SQLi / NoSQL / command / template / path): server must not 500 and must not leak an
        engine error in the body,
      - mass assignment: extra privilege fields in the body must not 500 (ideally ignored),
      - broken auth: an auth-gated endpoint called without credentials must be 401/403.
    Returns [(filename, content)]. Deterministic - no external scanner, no credentials needed."""
    import re
    method = str(endpoint.get("method", "GET")).strip().upper() or "GET"
    path = str(endpoint.get("path", "/"))
    if not path.startswith("/"):
        path = "/" + path
    p = re.sub(r"\{[^}]+\}", "1", path)   # substitute path tokens
    names = _body_fields(endpoint, method)
    out: list[tuple[str, str]] = []

    # 1) injection classes - each payload in the body (POST/PUT) or the q param (GET/DELETE).
    for cls, payload in INJECTION.items():
        pbody = {n: payload for n in names} if names else None
        q = "" if pbody else f"?q={payload}"
        lines = _hurl_lines(method, p + q, base_var, pbody)
        lines += ["[Asserts]", "status < 500"] + _leak_asserts()
        out.append((f"sec_inj_{cls}_{method.lower()}.hurl", "\n".join(lines) + "\n"))

    # 2) mass assignment - inject privilege fields alongside the real ones; server must handle (< 500).
    if names:
        pbody = {n: "test123" for n in names}
        pbody.update(MASS_ASSIGN_FIELDS)
        lines = _hurl_lines(method, p, base_var, pbody)
        lines += ["[Asserts]", "status < 500"]
        out.append((f"sec_massassign_{method.lower()}.hurl", "\n".join(lines) + "\n"))

    # 3) broken auth - auth-gated endpoint without credentials must be rejected (401/403).
    auth = endpoint.get("auth") or {}
    if isinstance(auth, dict) and auth.get("required"):
        lines = _hurl_lines(method, p, base_var, None)
        lines += ["[Asserts]", "status >= 400", "status < 404"]
        out.append((f"sec_auth_{method.lower()}.hurl", "\n".join(lines) + "\n"))

    # 4) object-level robustness (IDOR-adjacent) - only for endpoints with a path id token. A hostile /
    # out-of-band object id must not crash (500) or leak an engine error; it should 401/403/404.
    if re.search(r"\{[^}]+\}", path):
        oob = re.sub(r"\{[^}]+\}", OOB_OBJECT_IDS[0], path)
        lines = _hurl_lines(method, oob, base_var, None)
        lines += ["[Asserts]", "status < 500"] + _leak_asserts()
        out.append((f"sec_objid_{method.lower()}.hurl", "\n".join(lines) + "\n"))
    return out


def api_headers_check(first_path: str = "/", base_var: str = "base") -> tuple[str, str]:
    """One response-security-header audit for the API (headers are app-wide). Asserts the standard
    hardening headers are present - a missing one is a real, reportable finding."""
    path = first_path if first_path.startswith("/") else "/" + first_path
    import re
    path = re.sub(r"\{[^}]+\}", "1", path)
    lines = [f"GET {{{{{base_var}}}}}{path}", "[Asserts]"]
    lines += [f'header "{h}" exists' for h in SECURITY_HEADERS]
    return ("sec_headers.hurl", "\n".join(lines) + "\n")
