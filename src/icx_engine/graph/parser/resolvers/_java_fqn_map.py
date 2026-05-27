"""Regex-based Java FQN -> relative-file map.

Complements _java_parse_cache: captures types from files that exceed
the parse timeout or cannot be parsed by javalang. O(n) regex scan
per file, never blocks, never fails.
"""
from __future__ import annotations

import re
from pathlib import Path

_PKG_RE = re.compile(r'^\s*package\s+([\w.]+)\s*;', re.MULTILINE)

# Java type names start with uppercase by convention; this filters noise.
_TYPE_RE = re.compile(
    r'\b(?:class|interface|enum|record)\s+([A-Z]\w*)\b',
    re.MULTILINE,
)


def build_fqn_map(java_files: list[Path], project_root: Path) -> dict[str, str]:
    """Return {fqn: rel_posix_path} for all Java types found via regex."""
    project_root = project_root.resolve()
    out: dict[str, str] = {}
    for jf in java_files:
        try:
            rel = jf.relative_to(project_root).as_posix()
        except ValueError:
            continue
        try:
            source = jf.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        pkg_m = _PKG_RE.search(source)
        package = pkg_m.group(1) if pkg_m else ""
        for m in _TYPE_RE.finditer(source):
            name = m.group(1)
            fqn = f"{package}.{name}" if package else name
            out.setdefault(fqn, rel)
    return out
