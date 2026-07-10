"""Security verification layer - deterministic, evidence-based (no AI verdicts).

Two testable pieces here:
- build_security_plan(analysis): which security checks apply to this change, from its text/signals.
- check_security_headers(headers): a deterministic audit of HTTP response security headers.

Broader probes (authn/authz/injection/xss/csrf/ssrf/privilege-escalation) are executed via
Schemathesis fuzzing + targeted deterministic requests by the executor (later phase); this module
defines WHICH apply and provides the header audit as a first concrete deterministic probe. Findings
become part of the Definition-of-Done record.
"""
from __future__ import annotations

from dataclasses import dataclass

# check id -> tokens that make it applicable to a change
_SECURITY_CHECKS: dict[str, set[str]] = {
    "authentication": {"auth", "login", "token", "jwt", "oauth", "session", "credential", "password"},
    "authorization": {"authz", "permission", "role", "access control", "privilege", "acl", "rbac"},
    "sql_injection": {"sql", "query", "orm", "database", "prepared statement"},
    "xss": {"render", "html", "template", "innerhtml", "sanitize", "escape", "frontend"},
    "csrf": {"csrf", "form", "state-changing", "post", "cookie"},
    "ssrf": {"url", "fetch", "request", "webhook", "proxy", "redirect", "download"},
    "insecure_headers": {"header", "cors", "csp", "response", "endpoint", "api", "http"},
    "privilege_escalation": {"admin", "privilege", "role", "elevation", "sudo", "impersonat"},
}

# Response security headers that should be present, with a short reason.
REQUIRED_SECURITY_HEADERS: dict[str, str] = {
    "content-security-policy": "mitigates XSS / data injection",
    "x-content-type-options": "prevents MIME sniffing (should be 'nosniff')",
    "x-frame-options": "prevents clickjacking (or use CSP frame-ancestors)",
    "strict-transport-security": "enforces HTTPS (HSTS)",
    "referrer-policy": "limits referrer leakage",
}


@dataclass
class SecurityFinding:
    check: str
    passed: bool
    severity: str = "medium"   # low | medium | high | critical
    detail: str = ""


def _text(analysis: dict) -> str:
    return " ".join(str(analysis.get(k, "")) for k in
                    ("problem_summary", "detailed_description", "impact")).lower()


def build_security_plan(analysis: dict) -> list[str]:
    """Return the security check ids applicable to this change, based on its text signals.

    Empty list when the change shows no security surface. Deterministic - same input, same plan.
    """
    if not isinstance(analysis, dict) or not analysis:
        return []
    text = _text(analysis)
    plan = [cid for cid, toks in _SECURITY_CHECKS.items() if any(t in text for t in toks)]
    return sorted(plan)


def check_security_headers(headers: dict) -> list[SecurityFinding]:
    """Deterministic audit of HTTP response security headers. One finding per required header;
    passed=True when present (and, for x-content-type-options, correctly 'nosniff')."""
    lower = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
    findings: list[SecurityFinding] = []
    for name, reason in REQUIRED_SECURITY_HEADERS.items():
        present = name in lower
        passed = present
        detail = "" if present else f"missing: {reason}"
        if name == "x-content-type-options" and present and lower[name].strip().lower() != "nosniff":
            passed = False
            detail = "present but not 'nosniff'"
        sev = "high" if name == "content-security-policy" else "medium"
        findings.append(SecurityFinding(check=name, passed=passed, severity=sev, detail=detail))
    return findings
