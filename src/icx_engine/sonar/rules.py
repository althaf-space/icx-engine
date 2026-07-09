"""Mandatory, user-editable selection rules for Sonar discovery.

Mirrors the testing rulebook (`testing/rules.py`): the source of truth lives in
`~/.icx/sonar_rules/`, seeded once from bundled defaults and never overwritten.
ICX loads `selection.md` and injects its text into every discovery response
(`sonar_projects`, `sonar_branches`), so the agent always confronts the current
mandatory protocol - no dependency on the agent remembering it, no drift across
sessions.
"""
from __future__ import annotations

import sys
from pathlib import Path

_DEFAULTS_DIR = Path(__file__).parent / "rules_defaults"
_SELECTION = "selection"


def rules_dir() -> Path:
    return Path.home() / ".icx" / "sonar_rules"


def ensure_seeded() -> None:
    """Copy any missing bundled default into ~/.icx/sonar_rules/. Never overwrites
    a file the user has already edited or created."""
    d = rules_dir()
    d.mkdir(parents=True, exist_ok=True,
            **({"mode": 0o700} if sys.platform != "win32" else {}))
    if not _DEFAULTS_DIR.exists():
        return
    for src in _DEFAULTS_DIR.glob("*.md"):
        dst = d / src.name
        if not dst.exists():
            try:
                dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
            except OSError:
                pass


def _read_one(name: str) -> str:
    """Read a rule file - user copy first, bundled default as fallback."""
    f = rules_dir() / f"{name}.md"
    if f.exists():
        try:
            return f.read_text(encoding="utf-8")
        except OSError:
            pass
    df = _DEFAULTS_DIR / f"{name}.md"
    if df.exists():
        try:
            return df.read_text(encoding="utf-8")
        except OSError:
            pass
    return ""


def selection_rules() -> str:
    """The mandatory selection protocol text injected into discovery responses."""
    ensure_seeded()
    return _read_one(_SELECTION).strip()
