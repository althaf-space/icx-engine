"""ICX Skills - distilled, human-readable procedural knowledge learned from verified fixes.

A skill is injected expertise, not a shortcut: it never causes the ICX methodology, diagnosis, or
verification gate to be skipped. See docs/superpowers/specs/2026-07-24-icx-self-improving-skills-design.md
for the full design."""
from __future__ import annotations

from icx_engine.skills.schema import SkillEntry
from icx_engine.skills.storage import SkillStorage

__all__ = ["SkillEntry", "SkillStorage"]
