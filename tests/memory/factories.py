"""Production-realistic MemoryEntry factories.

BUG-1 (verify_resolution regressing a reinforced confidence) hid because the
memory tests only built entries from the schema default (memory_confidence=0.0)
and never from a *reinforced* or *verified* state. These factories seed those
real intermediate states so tests exercise the transitions that actually occur
in production.

The confidence math here mirrors MemoryManager.reinforce_usage /
verify_resolution; test_factories.py asserts it stays in sync by running the
real manager and comparing.

Persistence caveat: MemoryManager.save() RECOMPUTES memory_confidence for a
`resolution_confirmed` entry. To persist a factory entry's pre-set
confidence/count into the DB unchanged, call `save(entry, restore=True)`; a
plain `save()` will overwrite it (which is correct production behavior).

Import from any memory test:

    from memory.factories import make_entry, make_reinforced_entry, make_verified_entry
"""
from __future__ import annotations

import uuid
from typing import Any


def make_entry(issue_key: str = "PROJ-1", **over: Any):
    """A minimal, schema-valid MemoryEntry (all required fields populated).

    Any field can be overridden via keyword. Defaults mirror a fresh save:
    memory_confidence=0.0, usage_count=0, not verified, not negated.
    """
    from icx_engine.memory.schema import MemoryEntry

    data: dict = {
        "id": str(uuid.uuid4()),
        "issue_key": issue_key,
        "project_key": issue_key.split("-", 1)[0] if "-" in issue_key else "PROJ",
        "source_type": "jira",
        "issue_type": "Bug",
        "summary": f"Summary for {issue_key}",
        "problem_description": "Problem description.",
        "resolution_note": "Resolution note.",
        "files_changed": ["src/app.py"],
        "resolution_confirmed": False,
        "saved_at": "2026-01-01T00:00:00+00:00",
    }
    data.update(over)
    return MemoryEntry(**data)


def reinforced_confidence(usage_count: int) -> float:
    """memory_confidence that reinforce_usage sets for a given usage_count.

    Mirrors MemoryManager.reinforce_usage: >=10 citations -> 1.0, >=5 -> 0.75,
    otherwise unchanged from its prior value (0.0 for a fresh entry).
    """
    if usage_count >= 10:
        return 1.0
    if usage_count >= 5:
        return 0.75
    return 0.0


def make_reinforced_entry(issue_key: str = "PROJ-1", usage_count: int = 10, **over: Any):
    """An entry in a post-`reinforce_usage` state.

    Populates used_by_tickets / usage_count and the memory_confidence that
    reinforcement would have produced - the state BUG-1 needed to surface (a
    later verify must not lower a confidence already raised here).
    """
    citations = [f"CITE-{i}" for i in range(usage_count)]
    defaults: dict = {
        "used_by_tickets": citations,
        "usage_count": usage_count,
        "memory_confidence": reinforced_confidence(usage_count),
        "resolution_confirmed": True,
    }
    defaults.update(over)
    return make_entry(issue_key, **defaults)


def verified_confidence(confirmation_count: int) -> float:
    """memory_confidence that verify_resolution sets for a confirmation_count."""
    return min(1.0, confirmation_count * 0.25)


def make_verified_entry(issue_key: str = "PROJ-1", confirmation_count: int = 1, **over: Any):
    """An entry in a post-`verify_resolution` state."""
    defaults: dict = {
        "outcome_verified": True,
        "confirmation_count": confirmation_count,
        "memory_confidence": verified_confidence(confirmation_count),
        "resolution_confirmed": True,
    }
    defaults.update(over)
    return make_entry(issue_key, **defaults)
