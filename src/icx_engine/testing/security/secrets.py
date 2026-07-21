"""Native secrets scanner - deterministic regex ruleset over the repo's own source. Catches the
high-frequency real leaks (cloud keys, private keys, tokens, hardcoded credentials) plus a
high-entropy assignment heuristic. No external tool, no network."""
from __future__ import annotations

import re
from pathlib import Path

from icx_engine.testing.security.scan_base import (
    Finding,
    iter_source_files,
    read_text,
    rel,
    shannon_entropy,
)

# rule id -> (severity, human title, compiled pattern). Patterns match a token, not a whole line.
_PATTERNS: list[tuple[str, str, str, re.Pattern]] = [
    ("aws-access-key", "critical", "AWS access key id",
     re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA)[0-9A-Z]{16}\b")),
    ("private-key", "critical", "Private key block",
     re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----")),
    ("gcp-api-key", "high", "Google API key",
     re.compile(r"\bAIza[0-9A-Za-z\-_]{35}\b")),
    ("github-token", "critical", "GitHub token",
     re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[0-9A-Za-z]{36}\b")),
    ("slack-token", "high", "Slack token",
     re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{10,}\b")),
    ("stripe-key", "critical", "Stripe secret key",
     re.compile(r"\b(?:sk|rk)_live_[0-9A-Za-z]{20,}\b")),
    ("jwt", "medium", "JSON Web Token",
     re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
    ("slack-webhook", "high", "Slack webhook URL",
     re.compile(r"https://hooks\.slack\.com/services/[A-Za-z0-9/_+-]{20,}")),
    ("basic-auth-url", "high", "Credentials in URL",
     re.compile(r"[a-z][a-z0-9+.\-]*://[^/\s:@]+:[^/\s:@]+@")),
]

# A hardcoded assignment: key = "value". Flag when the name looks secret AND the value is non-trivial.
_ASSIGN = re.compile(
    r"""(?ix)
    \b(password|passwd|pwd|secret|api[_-]?key|apikey|access[_-]?token|auth[_-]?token|
       client[_-]?secret|private[_-]?key|db[_-]?pass|encryption[_-]?key)\b
    \s*[:=]\s*
    ['"]([^'"\n]{6,})['"]
    """)
# Values that are obviously not real secrets (placeholders, env indirection).
_PLACEHOLDER = re.compile(
    r"(?i)^(?:x{3,}|\*{3,}|\.{3,}|changeme|placeholder|example|your[_-]?|<[^>]+>|\$\{?[a-z0-9_]+\}?|"
    r"process\.env|os\.environ|none|null|true|false|test|dummy|sample|redacted|\d+)$")


def _looks_placeholder(v: str) -> bool:
    return bool(_PLACEHOLDER.match(v.strip()))


def scan_secrets(repo: Path, file_limit: int = 6000) -> list[Finding]:
    repo = Path(repo)
    findings: list[Finding] = []
    for p in iter_source_files(repo, limit=file_limit):
        text = read_text(p)
        if not text:
            continue
        relp = rel(repo, p)
        lines = text.splitlines()
        for i, line in enumerate(lines, 1):
            if len(line) > 4000:
                continue
            for rule, sev, title, pat in _PATTERNS:
                if pat.search(line):
                    findings.append(Finding(
                        scanner="secrets", rule=rule, severity=sev, title=title,
                        file=relp, line=i, detail=f"{title} detected in source.",
                        snippet=_mask(line)))
            m = _ASSIGN.search(line)
            if m:
                val = m.group(2)
                if not _looks_placeholder(val) and shannon_entropy(val) >= 3.0:
                    findings.append(Finding(
                        scanner="secrets", rule="hardcoded-credential", severity="high",
                        title="Hardcoded credential", file=relp, line=i,
                        detail=f"'{m.group(1)}' assigned a literal value in source.",
                        snippet=_mask(line.replace(val, val[:2] + "..."))))
    return findings


def _mask(line: str) -> str:
    """Redact long tokens so the snippet in the report never re-leaks the secret."""
    s = line.strip()[:200]
    return re.sub(r"([A-Za-z0-9+/_\-]{6})[A-Za-z0-9+/_\-]{6,}", r"\1...", s)
