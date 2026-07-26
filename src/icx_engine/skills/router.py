"""Rank stored skills against the current boost request. Deterministic tag/keyword overlap - no ML, no
LLM, same style as the rest of the boost/ package."""
from __future__ import annotations

from icx_engine.skills.storage import SkillStorage

_MAX_SKILLS_IN_INDEX = 5
_MIN_SKILL_SCORE = 1


_PUNCT = ".,;:!?()[]{}\"'"


def _keywords(prompt: str, archetype: str) -> set:
    # Strip trailing/leading punctuation per-word (same convention as boost/classify.py and
    # methodology.py) so ordinary prose ("...is failing.") still matches a bare tag ("failing").
    # Length floor is 2, not the stricter cutoff a naive stopword filter would use, so common
    # short-but-meaningful tool tags (jwt, sql, api, aws, orm, git) stay matchable.
    words = {w.strip(_PUNCT).lower() for w in (prompt or "").split()}
    words = {w for w in words if len(w) >= 2}
    # Accepted trade-off: archetype is always a candidate keyword, so a skill tagged only with the
    # bare archetype name (e.g. "debugging") scores >= 1 on every request of that archetype,
    # regardless of prompt content - a coarse-precision cost of this deterministic overlap ranker,
    # not a correctness bug. Bounded by _MAX_SKILLS_IN_INDEX; not worth a weighting scheme here.
    words.add(archetype.lower())
    return words


def rank_skills(prompt: str, archetype: str, storage: SkillStorage | None = None) -> list:
    storage = storage or SkillStorage()
    skills = storage.list_all()
    if not skills:
        return []
    kw = _keywords(prompt, archetype)
    scored = []
    for s in skills:
        tags = {t.lower() for t in s.tags}
        score = len(kw & tags)
        if score >= _MIN_SKILL_SCORE:
            scored.append((score, s))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [
        {"name": s.name, "description": s.description, "score": score, "scope_hint": s.scope_hint}
        for score, s in scored[:_MAX_SKILLS_IN_INDEX]
    ]


def rank_skills_for_tags(tags: list, root_cause_pattern: str, storage: SkillStorage | None = None) -> list:
    """Like rank_skills but matches directly against structured tags/root_cause_pattern - no prompt-text
    keyword extraction, no punctuation-stripping, no length floor - used by save_memory to surface
    'existing skills near this fix' immediately after a save, where exact structured data is already
    available (unlike /icx-boost, which only has free prompt text)."""
    kw = {t.lower() for t in (tags or [])}
    if root_cause_pattern and root_cause_pattern != "uncategorized":
        kw.add(root_cause_pattern.lower())
    if not kw:
        return []   # cheap check first - avoid the disk scan below on the common no-signal save
    storage = storage or SkillStorage()
    skills = storage.list_all()
    if not skills:
        return []
    scored = []
    for s in skills:
        stags = {t.lower() for t in s.tags}
        score = len(kw & stags)
        if score >= _MIN_SKILL_SCORE:
            scored.append((score, s))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [
        {"name": s.name, "description": s.description, "score": score, "scope_hint": s.scope_hint}
        for score, s in scored[:_MAX_SKILLS_IN_INDEX]
    ]
