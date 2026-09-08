"""UI-visible text extraction.

Extracts literal, human-visible text from UI source files (JSX/TSX text nodes,
title/aria-label/placeholder/alt attribute values) so graph_find_context can match a
ticket description against what a user actually SAW on screen, not just code
identifiers/filenames. A ticket rarely quotes a class name, but often echoes a page
title or button label almost verbatim ("Loyalty Points-Earned V/S Redemption is wrong")
- this closes that gap with zero LLM calls and zero embeddings, purely deterministic
text extraction feeding the existing word-boundary/phrase matching in query.py.

Deliberately does NOT attempt disambiguation here - a common UI word (e.g. "Loyalty")
legitimately appears across many real, distinct screens. Surfacing that as several
ranked candidates (query.py's job) is correct behavior for a genuinely ambiguous
description, not a bug to hide behind a forced single guess.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

_UI_TEXT_EXTENSIONS = frozenset({".jsx", ".tsx", ".js", ".ts", ".vue", ".html"})

# Text directly between JSX/HTML tags, e.g. `<h1>Loyalty Status</h1>` -> "Loyalty Status".
# Must start with a letter (excludes bare `{expr}` interpolations, numbers, punctuation-only
# content) and stays on one line (avoids accidentally spanning unrelated code blocks).
_TAG_TEXT_RE = re.compile(r">([A-Za-z][A-Za-z0-9 ,.'\-&/():]{1,99})<")

# Standard accessibility/display attributes whose value is, by definition, text meant to
# be seen or read by a user - not a codebase-specific convention.
_TEXT_ATTR_RE = re.compile(
    r'\b(?:title|aria-label|placeholder|alt)\s*=\s*["\']([A-Za-z][^"\']{1,99})["\']',
)

# Excludes obvious non-text content that can slip through the tag-text pattern: pure
# JS-expression-looking fragments, CSS-class-looking tokens (no spaces, all lowercase
# with hyphens only), and single short words that are more likely markup noise than
# real UI copy.
_LOOKS_LIKE_CODE_RE = re.compile(r"^[a-z0-9_\-]+$")


def _is_real_text(candidate: str) -> bool:
    candidate = candidate.strip()
    if len(candidate) < 3:
        return False
    if _LOOKS_LIKE_CODE_RE.match(candidate) and " " not in candidate:
        return False
    return True


def extract_ui_text(files: Iterable[Path], project_root: Path) -> dict[str, str]:
    """{project-relative-posix-path: pipe-joined visible text} for every UI file with
    extractable text. Never raises - a read/regex failure on one file just yields no
    text for that file, never fatal to the caller."""
    project_root = Path(project_root).resolve()
    result: dict[str, str] = {}

    for f in files:
        f = Path(f).resolve()
        if f.suffix.lower() not in _UI_TEXT_EXTENSIONS:
            continue
        try:
            rel = f.relative_to(project_root).as_posix()
            code = f.read_text(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            continue

        seen: set[str] = set()
        texts: list[str] = []
        for pattern in (_TAG_TEXT_RE, _TEXT_ATTR_RE):
            for m in pattern.finditer(code):
                candidate = m.group(1).strip()
                if not _is_real_text(candidate) or candidate in seen:
                    continue
                seen.add(candidate)
                texts.append(candidate)

        if texts:
            result[rel] = " | ".join(texts)

    return result
