"""Build SkillEntry objects from agent-authored text and write/update them, hash-guarded.

One creation path: draft_skill_entry, called by the draft_skill MCP tool handler (mcp_server.py)
immediately after every verified save_memory call. The agent supplies the full authored content live,
with full context - no mechanical text concatenation, no deferred/statistical trigger. See design spec
2026-07-25-icx-skills-agent-authoring-redesign-design.md for why the prior emergent/statistical path
(draft_from_pattern, check_emergent_patterns) was removed."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from icx_engine.skills.schema import SkillEntry
from icx_engine.skills.storage import SkillStorage

if TYPE_CHECKING:
    from icx_engine.memory.schema import MemoryEntry


def _slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s or "unnamed-skill"


def _dedup_lines(values) -> list:
    seen: set = set()
    out: list = []
    for v in values:
        v = (v or "").strip()
        if not v or v in seen:
            continue
        seen.add(v)
        out.append(v)
    return out


def _scope_hint(project_count: int) -> str:
    """Deterministic from the number of distinct originating projects - 2+ distinct projects
    independently reinforcing the same skill is real evidence of portability; a single project's worth
    of evidence is not. 0 projects means the skill was authored with no project tie at all (manual
    general-purpose creation), which is also generic - only a count of exactly 1 is repo-specific."""
    return "generic" if project_count == 0 or project_count >= 2 else "repo-specific"


def draft_skill_entry(entry: "MemoryEntry", skill_name: str, description: str, when_to_use: str,
                      procedure: str, verification: str, pitfalls: str = "",
                      tags: list | None = None) -> SkillEntry:
    """Build a SkillEntry from agent-authored text. No fallback to raw memory-entry fields - the
    draft_skill MCP tool schema requires description/when_to_use/procedure/verification, so by the time
    this is called the agent has always supplied real, live-authored content."""
    now = datetime.now(timezone.utc).isoformat()
    root_cause = entry.root_cause_pattern
    merged_tags = _dedup_lines(
        [t.lower() for t in
         (list(tags) if tags else [])
         + ([root_cause] if root_cause and root_cause != "uncategorized" else [])
         + list(entry.tags or [])]
    )
    draft = SkillEntry(
        name=_slugify(skill_name),
        description=description,
        tags=merged_tags,
        origin_projects=[entry.project_key],
        origin_issue_keys=[entry.issue_key],
        scope_hint=_scope_hint(1),
        title=skill_name,
        when_to_use=when_to_use,
        procedure=procedure,
        pitfalls=pitfalls,
        verification=verification,
        created_at=now,
        updated_at=now,
    )
    draft.icx_hash = draft.compute_hash()
    return draft


def write_or_update(storage: SkillStorage, draft: SkillEntry) -> str:
    """Idempotent, hash-guarded write. Returns 'created', 'updated', or 'skipped_user_edited'."""
    existing = storage.read(draft.name)
    if existing is None:
        storage.write(draft)
        return "created"
    if existing.icx_hash != existing.compute_hash():
        return "skipped_user_edited"   # body no longer matches its own stored hash - a human edited it
    merged = SkillEntry(
        name=existing.name,
        description=draft.description or existing.description,
        tags=sorted(set(existing.tags) | set(draft.tags)),
        origin_projects=sorted(set(existing.origin_projects) | set(draft.origin_projects)),
        origin_issue_keys=_dedup_lines(existing.origin_issue_keys + draft.origin_issue_keys),
        scope_hint=_scope_hint(len(set(existing.origin_projects) | set(draft.origin_projects))),
        title=existing.title,
        when_to_use=draft.when_to_use or existing.when_to_use,
        procedure=draft.procedure or existing.procedure,
        pitfalls=draft.pitfalls or existing.pitfalls,
        verification=draft.verification or existing.verification,
        created_at=existing.created_at,
        updated_at=datetime.now(timezone.utc).isoformat(),
    )
    merged.icx_hash = merged.compute_hash()
    storage.write(merged)
    return "updated"
