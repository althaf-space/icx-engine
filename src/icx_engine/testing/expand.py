from __future__ import annotations

import os
import re
from pathlib import Path, PurePosixPath

_SOURCE_EXT = {".js", ".jsx", ".ts", ".tsx", ".vue", ".svelte", ".java", ".kt",
               ".go", ".cs", ".rb", ".php", ".py"}
_SKIP_DIRS = {"node_modules", ".git", "dist", "build", "out", "__pycache__",
              ".venv", "venv", "target", ".next", ".idea", "coverage"}


def _norm(p: str) -> str:
    return p.replace("\\", "/")


def _basename_no_ext(path: str) -> str:
    return PurePosixPath(_norm(path)).stem


def expand_via_grep(file_paths: list[str], project_root: Path, max_files: int = 2000) -> list[str]:
    seeds_norm = {_norm(p) for p in file_paths}
    stems = {_basename_no_ext(p) for p in file_paths if _basename_no_ext(p)}
    if not stems:
        return []
    import_re = re.compile(
        r'(?:import|from|require)\b[^\n]*[\'"][^\'"\n]*?(' + "|".join(re.escape(s) for s in stems) + r')[\'"./]',
    )
    found: set[str] = set()
    scanned = 0
    for dirpath, dirnames, filenames in os.walk(project_root):
        if scanned >= max_files:
            break
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for filename in filenames:
            if scanned >= max_files:
                break
            path = Path(dirpath) / filename
            if path.suffix.lower() not in _SOURCE_EXT:
                continue
            p_norm = _norm(str(path))
            if p_norm in seeds_norm:
                continue
            scanned += 1
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if import_re.search(text):
                found.add(str(path))
    return sorted(found)


def union_rank(seeds: list[str], graph_files: list[str], grep_files: list[str]) -> list[tuple[str, str]]:
    seed_set = {_norm(p) for p in seeds}
    graph_set = {_norm(p) for p in graph_files}
    grep_set = {_norm(p) for p in grep_files}

    ordered: list[tuple[str, str]] = []
    used: set[str] = set()

    def _add(paths: list[str], decide):
        for p in paths:
            n = _norm(p)
            if n in used:
                continue
            used.add(n)
            ordered.append((p, decide(n)))

    _add(seeds, lambda n: "seed")
    both = sorted((graph_set & grep_set) - seed_set)
    _add(both, lambda n: "both")
    _add(sorted(graph_set - grep_set - seed_set), lambda n: "graph")
    _add(sorted(grep_set - graph_set - seed_set), lambda n: "grep")
    return ordered
