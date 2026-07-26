"""Shared building blocks for the native security scanners: the Finding record, severity ordering, a
bounded source-file walk (OS-independent), and a Shannon-entropy helper for secret detection."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

# Severity rank for sorting (higher = worse).
SEVERITY_RANK = {"critical": 4, "high": 3, "medium": 2, "low": 1, "info": 0}

# Directories never worth scanning (vendored, build output, VCS, caches).
_SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "bower_components", "vendor", "venv", ".venv", "env",
    "__pycache__", ".mypy_cache", ".pytest_cache", ".tox", "dist", "build", "out", "target",
    ".next", ".nuxt", ".svelte-kit", "coverage", ".idea", ".vscode", ".gradle", ".icx",
    "site-packages", "migrations",
}
# Extensions worth reading as source/text.
_SOURCE_EXT = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".vue", ".svelte", ".java", ".kt", ".go",
    ".rb", ".php", ".cs", ".c", ".h", ".cpp", ".hpp", ".cc", ".scala", ".rs", ".swift", ".sql",
    ".sh", ".bash", ".ps1", ".yml", ".yaml", ".json", ".xml", ".tf", ".env", ".ini", ".cfg",
    ".properties", ".toml", ".gradle", ".config", ".html",
}
_MAX_BYTES = 1_500_000   # skip files larger than ~1.5 MB (generated bundles, data blobs)


@dataclass
class Finding:
    """One security finding. `scanner` is secrets|sast|sca|dast; `rule` is the rule id; `file` is
    repo-relative (or "" for non-file findings); `line` is 1-indexed (0 = n/a)."""
    scanner: str
    rule: str
    severity: str
    title: str
    file: str = ""
    line: int = 0
    detail: str = ""
    snippet: str = ""
    extra: dict = field(default_factory=dict)

    def key(self) -> tuple:
        return (self.scanner, self.rule, self.file, self.line)


def sort_findings(findings: list[Finding]) -> list[Finding]:
    """Most-severe first, then by file/line for a stable order."""
    return sorted(findings, key=lambda f: (-SEVERITY_RANK.get(f.severity, 0), f.file, f.line))


def dedupe(findings: list[Finding]) -> list[Finding]:
    seen: set = set()
    out: list[Finding] = []
    for f in findings:
        k = f.key()
        if k in seen:
            continue
        seen.add(k)
        out.append(f)
    return out


def iter_source_files(repo: Path, limit: int = 6000):
    """Yield readable source files under `repo`, skipping vendored/build dirs and oversized/binary files.
    OS-independent (pathlib). Bounded by `limit` files so a huge monorepo cannot hang a run."""
    repo = Path(repo)
    if not repo.exists():
        return
    count = 0
    for p in repo.rglob("*"):
        if count >= limit:
            return
        try:
            if not p.is_file():
                continue
            try:
                rel_parts = p.relative_to(repo).parts
            except ValueError:
                rel_parts = p.parts
            if any(part in _SKIP_DIRS for part in rel_parts):
                continue
            name = p.name.lower()
            ext = p.suffix.lower()
            # .env* files carry secrets even without a standard extension.
            if ext not in _SOURCE_EXT and not name.startswith(".env"):
                continue
            if p.stat().st_size > _MAX_BYTES:
                continue
        except OSError:
            continue
        count += 1
        yield p


def read_text(p: Path) -> str:
    try:
        return Path(p).read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def rel(repo: Path, p: Path) -> str:
    try:
        return str(Path(p).relative_to(repo)).replace("\\", "/")
    except ValueError:
        return str(p).replace("\\", "/")


def shannon_entropy(s: str) -> float:
    """Bits-per-char Shannon entropy - high values flag random-looking secret strings."""
    if not s:
        return 0.0
    freq: dict[str, int] = {}
    for ch in s:
        freq[ch] = freq.get(ch, 0) + 1
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in freq.values())
