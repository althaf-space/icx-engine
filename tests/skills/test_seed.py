from __future__ import annotations

import json

from icx_engine.skills.defaults import DEFAULT_SKILLS
from icx_engine.skills.storage import SkillStorage


def test_seed_writes_all_defaults_into_empty_store(tmp_path):
    from icx_engine.skills.seed import seed_default_skills
    storage = SkillStorage(root=tmp_path)
    result = seed_default_skills(storage)
    assert set(result["seeded"]) == {d["name"] for d in DEFAULT_SKILLS}
    assert result["updated"] == []
    assert result["skipped_customized"] == []
    names = {s.name for s in storage.list_all()}
    assert names == {d["name"] for d in DEFAULT_SKILLS}
    for s in storage.list_all():
        assert s.scope_hint == "generic"


def test_seed_is_idempotent(tmp_path):
    from icx_engine.skills.seed import seed_default_skills
    storage = SkillStorage(root=tmp_path)
    seed_default_skills(storage)
    before = {s.name: s.icx_hash for s in storage.list_all()}
    second = seed_default_skills(storage)
    assert second["seeded"] == []
    assert second["updated"] == []
    after = {s.name: s.icx_hash for s in storage.list_all()}
    assert before == after


def test_seed_never_overwrites_user_customized_skill(tmp_path, monkeypatch):
    from icx_engine.skills import seed as seed_mod
    storage = SkillStorage(root=tmp_path)
    seed_mod.seed_default_skills(storage)

    edited = storage.read("systematic-debugging")
    edited.procedure = "MY OWN CUSTOM PROCEDURE - do not touch."
    storage.write(edited)

    changed_defaults = [dict(d) for d in DEFAULT_SKILLS]
    for d in changed_defaults:
        if d["name"] == "systematic-debugging":
            d["procedure"] = "a completely different shipped procedure"
    monkeypatch.setattr(seed_mod, "DEFAULT_SKILLS", changed_defaults)

    result = seed_mod.seed_default_skills(storage)
    assert "systematic-debugging" in result["skipped_customized"]
    assert storage.read("systematic-debugging").procedure == "MY OWN CUSTOM PROCEDURE - do not touch."


def test_seed_updates_unedited_skill_when_defaults_change(tmp_path, monkeypatch):
    from icx_engine.skills import seed as seed_mod
    storage = SkillStorage(root=tmp_path)
    seed_mod.seed_default_skills(storage)

    changed_defaults = [dict(d) for d in DEFAULT_SKILLS]
    for d in changed_defaults:
        if d["name"] == "systematic-debugging":
            d["procedure"] = "a new, improved shipped procedure"
    monkeypatch.setattr(seed_mod, "DEFAULT_SKILLS", changed_defaults)

    result = seed_mod.seed_default_skills(storage)
    assert "systematic-debugging" in result["updated"]
    assert storage.read("systematic-debugging").procedure == "a new, improved shipped procedure"


def test_seed_handles_corrupted_state_file_safely(tmp_path):
    from icx_engine.skills.seed import seed_default_skills, _state_path
    storage = SkillStorage(root=tmp_path)
    seed_default_skills(storage)
    original = storage.read("systematic-debugging")

    _state_path(storage).write_text("not valid json {{{", encoding="utf-8")

    result = seed_default_skills(storage)
    assert "systematic-debugging" in result["skipped_customized"]
    assert storage.read("systematic-debugging").procedure == original.procedure


def test_seed_leaves_foreign_skill_with_same_name_untouched(tmp_path):
    from icx_engine.skills.schema import SkillEntry
    from icx_engine.skills.seed import seed_default_skills
    storage = SkillStorage(root=tmp_path)

    foreign = SkillEntry(
        name="systematic-debugging", description="unrelated user skill", title="Not ours",
        when_to_use="w", procedure="the user's own unrelated content", pitfalls="x", verification="v",
    )
    foreign.icx_hash = foreign.compute_hash()
    storage.write(foreign)

    result = seed_default_skills(storage)
    assert "systematic-debugging" in result["skipped_customized"]
    assert storage.read("systematic-debugging").procedure == "the user's own unrelated content"


def test_seed_returns_expected_summary_shape(tmp_path):
    from icx_engine.skills.seed import seed_default_skills
    storage = SkillStorage(root=tmp_path)
    result = seed_default_skills(storage)
    assert set(result.keys()) == {"seeded", "updated", "skipped_customized"}
    assert all(isinstance(v, list) for v in result.values())


def test_seed_state_file_persists_shipped_hashes(tmp_path):
    from icx_engine.skills.seed import seed_default_skills, _state_path
    storage = SkillStorage(root=tmp_path)
    seed_default_skills(storage)
    state = json.loads(_state_path(storage).read_text(encoding="utf-8"))
    assert set(state.keys()) == {d["name"] for d in DEFAULT_SKILLS}
