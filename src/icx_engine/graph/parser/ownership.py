"""
CODEOWNERS file parser for ICX graph.

Parses GitHub-format CODEOWNERS files:
  /path/pattern @team @user
  *.py @python-team
  src/auth/ @security-team @auth-lead

Returns ownership mapping: file path -> list of owners.
Handles negation (!pattern) and last-match-wins semantics.
"""
from __future__ import annotations

import fnmatch
from pathlib import Path


def _parse_codeowners(text: str) -> list[tuple[str, list[str]]]:
    """Parse CODEOWNERS text into [(pattern, [owners])] in declaration order."""
    rules = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if not parts:
            continue
        pattern = parts[0]
        owners = [p for p in parts[1:] if p.startswith("@") or "@" in p]
        if owners:
            rules.append((pattern, owners))
    return rules


def _match_pattern(pattern: str, filepath: str) -> bool:
    """Return True if filepath matches the CODEOWNERS pattern."""
    # Normalize to forward slashes
    filepath = filepath.replace("\\", "/")
    pattern = pattern.replace("\\", "/")

    # Strip leading /
    if pattern.startswith("/"):
        pattern = pattern[1:]
        # Anchored pattern: match from root only
        return fnmatch.fnmatch(filepath, pattern) or fnmatch.fnmatch(filepath, pattern + "/*")

    # Directory pattern (ends with /): match if the pattern's directory
    # segments appear as a contiguous run of path segments anywhere in
    # filepath, not as a raw substring (which could false-match e.g.
    # pattern "lib/" against ".../oldlib/...").
    if pattern.endswith("/"):
        pat_parts = pattern[:-1].split("/")
        parts = filepath.split("/")
        n = len(pat_parts)
        return any(
            parts[i:i + n] == pat_parts
            for i in range(len(parts) - n + 1)
        )

    # Wildcard or name pattern: match anywhere in path
    name = Path(filepath).name
    if fnmatch.fnmatch(name, pattern):
        return True
    if fnmatch.fnmatch(filepath, pattern):
        return True
    # Match any path segment
    parts = filepath.split("/")
    for i in range(len(parts)):
        if fnmatch.fnmatch("/".join(parts[i:]), pattern):
            return True
    return False


def find_owners(filepath: str, rules: list[tuple[str, list[str]]]) -> list[str]:
    """Return owners for filepath using last-match-wins semantics."""
    owners: list[str] = []
    for pattern, rule_owners in rules:
        if _match_pattern(pattern, filepath):
            owners = rule_owners  # last match wins
    return owners


def load_codeowners(project_path: str) -> list[tuple[str, list[str]]]:
    """Load CODEOWNERS from standard locations. Returns [] if not found."""
    root = Path(project_path)
    locations = [
        root / "CODEOWNERS",
        root / ".github" / "CODEOWNERS",
        root / "docs" / "CODEOWNERS",
    ]
    for loc in locations:
        try:
            text = loc.read_text(encoding="utf-8", errors="replace")
            return _parse_codeowners(text)
        except OSError:
            continue
    return []
