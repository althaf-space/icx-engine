from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from memory.factories import make_verified_entry  # noqa: E402


def test_draft_skill_entry_uses_agent_supplied_text_verbatim():
    from icx_engine.skills.writer import draft_skill_entry
    entry = make_verified_entry("PROJ-1")
    draft = draft_skill_entry(
        entry, skill_name="My New Skill",
        description="Fixes X when Y. Use when Z is observed.",
        when_to_use="When Z is observed in the logs.",
        procedure="Do X then Y.", verification="Ran the repro script.",
        pitfalls="Watch out for W.",
    )
    assert draft.name == "my-new-skill"
    assert draft.description == "Fixes X when Y. Use when Z is observed."
    assert draft.when_to_use == "When Z is observed in the logs."
    assert draft.procedure == "Do X then Y."
    assert draft.verification == "Ran the repro script."
    assert draft.pitfalls == "Watch out for W."
    assert draft.origin_projects == ["PROJ"]
    assert draft.origin_issue_keys == ["PROJ-1"]
    assert draft.icx_hash == draft.compute_hash()


def test_draft_skill_entry_never_backfills_empty_text_from_entry_fields():
    """The entire point of this redesign: agent-supplied text is used verbatim, never silently
    filled from the entry's own resolution_note/problem_description/summary/outcome_feedback_note
    when left blank - that fallback (draft_from_explicit's old behavior) is what made the mechanical
    path produce paraphrased-ticket-text skills instead of properly authored ones."""
    from icx_engine.skills.writer import draft_skill_entry
    entry = make_verified_entry(
        "PROJ-99", summary="a real summary", problem_description="a real problem description",
        resolution_note="a real resolution note", outcome_feedback_note="a real feedback note",
    )
    draft = draft_skill_entry(
        entry, skill_name="Empty Text Skill", description="", when_to_use="",
        procedure="", verification="",
    )
    assert draft.description == ""
    assert draft.when_to_use == ""
    assert draft.procedure == ""
    assert draft.verification == ""


def test_draft_skill_entry_pitfalls_defaults_to_empty_string():
    from icx_engine.skills.writer import draft_skill_entry
    entry = make_verified_entry("PROJ-2")
    draft = draft_skill_entry(
        entry, skill_name="No Pitfalls Skill", description="d",
        when_to_use="w", procedure="p", verification="v",
    )
    assert draft.pitfalls == ""


def test_draft_skill_entry_merges_agent_tags_with_entry_tags():
    from icx_engine.skills.writer import draft_skill_entry
    entry = make_verified_entry("PROJ-3", tags=["existing-tag"], root_cause_pattern="jwt-expiry")
    draft = draft_skill_entry(
        entry, skill_name="Tagged Skill", description="d", when_to_use="w",
        procedure="p", verification="v", tags=["agent-tag"],
    )
    assert set(draft.tags) == {"agent-tag", "existing-tag", "jwt-expiry"}


def test_draft_skill_entry_excludes_uncategorized_sentinel_from_tags():
    from icx_engine.skills.writer import draft_skill_entry
    entry = make_verified_entry("PROJ-4")   # root_cause_pattern defaults to "uncategorized"
    draft = draft_skill_entry(entry, skill_name="Skill", description="d", when_to_use="w",
                              procedure="p", verification="v")
    assert "uncategorized" not in draft.tags


def test_draft_skill_entry_lowercases_tags():
    from icx_engine.skills.writer import draft_skill_entry
    entry = make_verified_entry("PROJ-5", tags=[])
    draft = draft_skill_entry(entry, skill_name="Skill", description="d", when_to_use="w",
                              procedure="p", verification="v", tags=["MixedCase"])
    assert "mixedcase" in draft.tags
    assert "MixedCase" not in draft.tags


def test_write_or_update_creates_new_skill(tmp_path):
    from icx_engine.skills.storage import SkillStorage
    from icx_engine.skills.writer import draft_skill_entry, write_or_update
    storage = SkillStorage(root=tmp_path)
    entry = make_verified_entry("PROJ-1")
    draft = draft_skill_entry(entry, skill_name="Fresh Skill", description="d", when_to_use="w",
                              procedure="p", verification="v")
    status = write_or_update(storage, draft)
    assert status == "created"
    assert storage.read("fresh-skill") is not None


def test_write_or_update_merges_on_second_call(tmp_path):
    from icx_engine.skills.storage import SkillStorage
    from icx_engine.skills.writer import draft_skill_entry, write_or_update
    storage = SkillStorage(root=tmp_path)
    e1 = make_verified_entry("PROJ-1")
    write_or_update(storage, draft_skill_entry(e1, skill_name="Merge Skill", description="d1",
                                               when_to_use="w", procedure="p", verification="v"))
    e2 = make_verified_entry("OTHER-1", project_key="OTHER")
    status = write_or_update(storage, draft_skill_entry(e2, skill_name="Merge Skill", description="d2",
                                                         when_to_use="w2", procedure="p2", verification="v2"))
    assert status == "updated"
    merged = storage.read("merge-skill")
    assert set(merged.origin_projects) == {"PROJ", "OTHER"}
    assert set(merged.origin_issue_keys) == {"PROJ-1", "OTHER-1"}
    assert merged.procedure == "p2"   # fresh draft's text wins - this is the "refine" behavior


def test_write_or_update_keeps_generic_scope_hint_when_merging_zero_project_skills(tmp_path):
    """A manually-created (icx skills create), project-less skill is scope_hint='generic'. Merging
    it with another zero-project draft under the same name must not flip that back to
    'repo-specific' - 0 origins is the most general case, not evidence of narrowness."""
    from icx_engine.skills.schema import SkillEntry
    from icx_engine.skills.storage import SkillStorage
    from icx_engine.skills.writer import write_or_update
    storage = SkillStorage(root=tmp_path)
    first = SkillEntry(name="general-skill", description="d1", tags=[], origin_projects=[],
                        origin_issue_keys=[], scope_hint="generic", title="General Skill",
                        when_to_use="w", procedure="p1", verification="v")
    first.icx_hash = first.compute_hash()
    write_or_update(storage, first)
    second = SkillEntry(name="general-skill", description="d2", tags=[], origin_projects=[],
                         origin_issue_keys=[], scope_hint="generic", title="General Skill",
                         when_to_use="w2", procedure="p2", verification="v2")
    second.icx_hash = second.compute_hash()
    status = write_or_update(storage, second)
    assert status == "updated"
    merged = storage.read("general-skill")
    assert merged.scope_hint == "generic"


def test_write_or_update_never_overwrites_user_edited_file(tmp_path):
    from icx_engine.skills.storage import SkillStorage
    from icx_engine.skills.writer import draft_skill_entry, write_or_update
    storage = SkillStorage(root=tmp_path)
    e1 = make_verified_entry("PROJ-1")
    write_or_update(storage, draft_skill_entry(e1, skill_name="Edited Skill", description="d",
                                               when_to_use="w", procedure="p", verification="v"))
    path = tmp_path / "edited-skill" / "SKILL.md"
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace("## Procedure", "## Procedure\nHand-edited by a human.\n"),
                     encoding="utf-8")
    e2 = make_verified_entry("PROJ-2")
    status = write_or_update(storage, draft_skill_entry(e2, skill_name="Edited Skill", description="d2",
                                                         when_to_use="w2", procedure="p2", verification="v2"))
    assert status == "skipped_user_edited"
    assert "Hand-edited by a human." in path.read_text(encoding="utf-8")
