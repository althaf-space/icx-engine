"""Native SAST-lite - deterministic static rules over the repo's source. Python files get real AST
analysis (stdlib `ast`, precise); other languages get a regex sink ruleset (heuristic, labelled).
Catches the high-frequency dangerous patterns; it is NOT a full taint-flow engine (honest ceiling)."""
from __future__ import annotations

import ast
import re
from pathlib import Path

from icx_engine.testing.security.scan_base import Finding, iter_source_files, read_text, rel

# ---- Python AST rules -------------------------------------------------------

_PY_DANGEROUS_CALLS = {
    "eval": ("py-eval", "critical", "Use of eval()"),
    "exec": ("py-exec", "critical", "Use of exec()"),
}
_PY_WEAK_HASH = {"md5", "sha1"}


class _PyVisitor(ast.NodeVisitor):
    def __init__(self, relp: str):
        self.relp = relp
        self.findings: list[Finding] = []

    def _add(self, rule, sev, title, node, detail):
        self.findings.append(Finding(scanner="sast", rule=rule, severity=sev, title=title,
                                     file=self.relp, line=getattr(node, "lineno", 0), detail=detail))

    def visit_Call(self, node: ast.Call):
        fname = _call_name(node.func)
        if fname in _PY_DANGEROUS_CALLS:
            rule, sev, title = _PY_DANGEROUS_CALLS[fname]
            self._add(rule, sev, title, node, f"{fname}() executes arbitrary code.")
        # subprocess(..., shell=True)
        if fname and ("subprocess" in fname or fname in ("run", "call", "Popen", "check_output")):
            for kw in node.keywords:
                if kw.arg == "shell" and _is_true(kw.value):
                    self._add("py-shell-true", "high", "subprocess shell=True", node,
                              "shell=True with a non-literal command allows command injection.")
        # requests(..., verify=False)
        for kw in node.keywords:
            if kw.arg == "verify" and _is_false(kw.value):
                self._add("py-tls-verify-off", "high", "TLS verification disabled", node,
                          "verify=False disables certificate validation (MITM risk).")
        # hashlib.md5 / sha1 used for security
        if fname in _PY_WEAK_HASH or (fname and fname.split(".")[-1] in _PY_WEAK_HASH):
            self._add("py-weak-hash", "medium", "Weak hash algorithm", node,
                      f"{fname} is unsuitable for passwords/signatures.")
        # yaml.load without SafeLoader
        if fname in ("yaml.load", "load") and "yaml" in fname:
            if not any(kw.arg == "Loader" for kw in node.keywords):
                self._add("py-yaml-load", "high", "Unsafe yaml.load", node,
                          "yaml.load without SafeLoader can execute arbitrary objects.")
        # pickle.loads
        if fname in ("pickle.loads", "loads") and "pickle" in fname:
            self._add("py-pickle", "high", "Unsafe deserialization", node,
                      "pickle.loads on untrusted data executes arbitrary code.")
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign):
        # DEBUG = True at module scope
        for t in node.targets:
            if isinstance(t, ast.Name) and t.id == "DEBUG" and _is_true(node.value):
                self._add("py-debug-true", "medium", "DEBUG enabled", node,
                          "DEBUG=True in production leaks stack traces and settings.")
        self.generic_visit(node)


def _call_name(func) -> str:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        parts = []
        cur = func
        while isinstance(cur, ast.Attribute):
            parts.append(cur.attr)
            cur = cur.value
        if isinstance(cur, ast.Name):
            parts.append(cur.id)
        return ".".join(reversed(parts))
    return ""


def _is_true(n) -> bool:
    return isinstance(n, ast.Constant) and n.value is True


def _is_false(n) -> bool:
    return isinstance(n, ast.Constant) and n.value is False


def _scan_python(relp: str, text: str) -> list[Finding]:
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return []
    v = _PyVisitor(relp)
    v.visit(tree)
    return v.findings


# ---- Cross-language regex sink rules ----------------------------------------

# rule -> (severity, title, pattern, applicable extensions or None=all)
_REGEX_RULES: list[tuple[str, str, str, re.Pattern, set | None]] = [
    ("js-innerhtml", "high", "innerHTML sink (DOM XSS)",
     re.compile(r"\.innerHTML\s*="), {".js", ".jsx", ".ts", ".tsx", ".vue", ".svelte", ".html"}),
    ("js-dangerously-set", "high", "dangerouslySetInnerHTML (React XSS)",
     re.compile(r"dangerouslySetInnerHTML"), {".js", ".jsx", ".ts", ".tsx"}),
    ("js-document-write", "medium", "document.write sink",
     re.compile(r"document\.write\s*\("), {".js", ".jsx", ".ts", ".tsx", ".html", ".vue"}),
    ("js-eval", "critical", "Use of eval()",
     re.compile(r"\beval\s*\("), {".js", ".jsx", ".ts", ".tsx", ".vue", ".svelte"}),
    ("cors-wildcard", "high", "Wildcard CORS origin",
     re.compile(r"""Access-Control-Allow-Origin['"]?\s*[:,]\s*['"]\*['"]"""), None),
    ("cors-wildcard-cfg", "high", "Wildcard CORS origin",
     re.compile(r"""(?i)(?:origin|allowedOrigins|cors)\s*[:=].{0,40}['"]\*['"]"""), None),
    ("sql-concat", "high", "SQL built by string concatenation",
     re.compile(r"""(?i)(?:SELECT|INSERT|UPDATE|DELETE)\b[^;'"]{0,80}['"]\s*(?:\+|\.|%|\|\|)\s*[A-Za-z_]"""),
     None),
    ("php-weak", "high", "Dangerous PHP call",
     re.compile(r"\b(?:eval|system|exec|passthru|shell_exec|popen)\s*\("), {".php"}),
    ("java-runtime-exec", "high", "Runtime.exec (command injection risk)",
     re.compile(r"Runtime\.getRuntime\(\)\.exec\s*\("), {".java"}),
    ("hardcoded-http", "low", "Cleartext HTTP endpoint",
     re.compile(r"""(?i)['"]http://(?!localhost|127\.0\.0\.1|0\.0\.0\.0)[a-z0-9.-]+"""), None),
]


def _scan_regex(relp: str, text: str, ext: str) -> list[Finding]:
    findings: list[Finding] = []
    lines = text.splitlines()
    for i, line in enumerate(lines, 1):
        if len(line) > 4000:
            continue
        for rule, sev, title, pat, exts in _REGEX_RULES:
            if exts is not None and ext not in exts:
                continue
            if pat.search(line):
                findings.append(Finding(scanner="sast", rule=rule, severity=sev, title=title,
                                        file=relp, line=i, detail=title + ".",
                                        snippet=line.strip()[:200]))
    return findings


def scan_sast(repo: Path, file_limit: int = 6000) -> list[Finding]:
    repo = Path(repo)
    findings: list[Finding] = []
    for p in iter_source_files(repo, limit=file_limit):
        text = read_text(p)
        if not text:
            continue
        relp = rel(repo, p)
        ext = p.suffix.lower()
        if ext == ".py":
            findings.extend(_scan_python(relp, text))
        findings.extend(_scan_regex(relp, text, ext))
    return findings
