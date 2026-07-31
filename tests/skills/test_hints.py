from __future__ import annotations

from icx_engine.skills.hints import attach_skill_hint
from icx_engine.skills.schema import SkillEntry
from icx_engine.skills.storage import SkillStorage


def _entry(name="a-skill"):
    return SkillEntry(name=name, description="does a thing", title="A Skill",
                       when_to_use="w", procedure="p", pitfalls="x", verification="v")


def test_attach_skill_hint_adds_index_when_skill_exists(tmp_path):
    storage = SkillStorage(root=tmp_path)
    storage.write(_entry())
    response = attach_skill_hint({"ok": True}, "a-skill", storage=storage)
    assert response["skills"]["index"] == [{"name": "a-skill", "description": "does a thing"}]


def test_attach_skill_hint_leaves_response_unchanged_when_skill_missing(tmp_path):
    storage = SkillStorage(root=tmp_path)
    response = attach_skill_hint({"ok": True}, "does-not-exist", storage=storage)
    assert "skills" not in response
    assert response == {"ok": True}


def test_attach_skill_hint_never_raises_on_storage_failure():
    class _BrokenStorage:
        def read(self, name):
            raise RuntimeError("boom")

    response = attach_skill_hint({"ok": True}, "any-skill", storage=_BrokenStorage())
    assert response == {"ok": True}


def test_attach_skill_hint_without_rank_prompt_keeps_old_single_lookup_behavior(tmp_path):
    storage = SkillStorage(root=tmp_path)
    storage.write(_entry())
    storage.write(_entry(name="other-skill"))
    response = attach_skill_hint({"ok": True}, "a-skill", storage=storage)
    assert response["skills"]["index"] == [{"name": "a-skill", "description": "does a thing"}]


def test_attach_skill_hint_appends_ranked_custom_skill_after_default(tmp_path):
    storage = SkillStorage(root=tmp_path)
    storage.write(_entry())
    custom = SkillEntry(name="custom-skill", description="handles jwt auth", tags=["jwt"],
                         title="Custom Skill", when_to_use="w", procedure="p", pitfalls="x", verification="v")
    storage.write(custom)
    response = attach_skill_hint(
        {"ok": True}, "a-skill", storage=storage, rank_prompt="jwt token issue", archetype="coding",
    )
    index = response["skills"]["index"]
    assert index[0] == {"name": "a-skill", "description": "does a thing"}
    names = [e["name"] for e in index]
    assert "custom-skill" in names
    assert names.count("a-skill") == 1


def test_attach_skill_hint_dedupes_ranked_skill_with_same_name_as_default(tmp_path):
    storage = SkillStorage(root=tmp_path)
    storage.write(_entry())
    response = attach_skill_hint(
        {"ok": True}, "a-skill", storage=storage, rank_prompt="a-skill does a thing", archetype="coding",
    )
    index = response["skills"]["index"]
    assert [e["name"] for e in index].count("a-skill") == 1


def test_attach_skill_hint_ranking_failure_still_leaves_default_attached(tmp_path, monkeypatch):
    storage = SkillStorage(root=tmp_path)
    storage.write(_entry())

    def _broken_rank_skills(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr("icx_engine.skills.hints.rank_skills", _broken_rank_skills)
    response = attach_skill_hint(
        {"ok": True}, "a-skill", storage=storage, rank_prompt="anything", archetype="coding",
    )
    assert response["skills"]["index"] == [{"name": "a-skill", "description": "does a thing"}]
