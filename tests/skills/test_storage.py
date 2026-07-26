from __future__ import annotations


def _entry(name="test-skill", **over):
    from icx_engine.skills.schema import SkillEntry
    defaults = dict(
        name=name, description="A test skill.", tags=["testing"],
        origin_projects=["PROJ"], origin_issue_keys=["PROJ-1"],
        title="Test Skill", when_to_use="when testing", procedure="do the thing",
        pitfalls="none", verification="it worked",
    )
    defaults.update(over)
    e = SkillEntry(**defaults)
    e.icx_hash = e.compute_hash()
    return e


def test_write_then_read_round_trips(tmp_path):
    from icx_engine.skills.storage import SkillStorage
    storage = SkillStorage(root=tmp_path)
    entry = _entry()
    storage.write(entry)
    back = storage.read("test-skill")
    assert back is not None
    assert back.name == "test-skill"
    assert back.procedure == "do the thing"


def test_write_creates_one_directory_per_skill(tmp_path):
    from icx_engine.skills.storage import SkillStorage
    storage = SkillStorage(root=tmp_path)
    storage.write(_entry())
    assert (tmp_path / "test-skill" / "SKILL.md").exists()


def test_read_returns_none_for_missing(tmp_path):
    from icx_engine.skills.storage import SkillStorage
    storage = SkillStorage(root=tmp_path)
    assert storage.read("does-not-exist") is None


def test_write_creates_dir_owner_only_perms_posix(tmp_path):
    import sys, stat
    from icx_engine.skills.storage import SkillStorage
    storage = SkillStorage(root=tmp_path)
    storage.write(_entry())
    d = tmp_path / "test-skill"
    if sys.platform != "win32":
        assert stat.S_IMODE(d.stat().st_mode) == 0o700


def test_list_all_returns_every_written_skill(tmp_path):
    from icx_engine.skills.storage import SkillStorage
    storage = SkillStorage(root=tmp_path)
    storage.write(_entry(name="skill-a"))
    storage.write(_entry(name="skill-b"))
    names = {s.name for s in storage.list_all()}
    assert names == {"skill-a", "skill-b"}


def test_list_all_empty_store_returns_empty_list(tmp_path):
    from icx_engine.skills.storage import SkillStorage
    storage = SkillStorage(root=tmp_path / "does-not-exist-yet")
    assert storage.list_all() == []


def test_list_all_skips_corrupt_file(tmp_path):
    from icx_engine.skills.storage import SkillStorage
    storage = SkillStorage(root=tmp_path)
    storage.write(_entry(name="good-skill"))
    corrupt_dir = tmp_path / "bad-skill"
    corrupt_dir.mkdir()
    (corrupt_dir / "SKILL.md").write_text("not a valid skill file", encoding="utf-8")
    names = {s.name for s in storage.list_all()}
    assert names == {"good-skill"}


def test_write_is_atomic_no_leftover_tmp_files(tmp_path):
    from icx_engine.skills.storage import SkillStorage
    storage = SkillStorage(root=tmp_path)
    storage.write(_entry())
    leftovers = list((tmp_path / "test-skill").glob("*.tmp"))
    assert leftovers == []


def test_list_all_skips_file_with_non_dict_frontmatter(tmp_path):
    from icx_engine.skills.storage import SkillStorage
    storage = SkillStorage(root=tmp_path)
    bad_dir = tmp_path / "bad-skill-2"
    bad_dir.mkdir()
    (bad_dir / "SKILL.md").write_text("---\n[1, 2, 3]\n---\n\nbody", encoding="utf-8")
    assert storage.list_all() == []


def test_read_rejects_path_traversal_name(tmp_path):
    from icx_engine.skills.storage import SkillStorage
    storage = SkillStorage(root=tmp_path)
    assert storage.read("../../etc/passwd") is None
    assert storage.read("some/nested/path") is None


def test_write_rejects_unsafe_name(tmp_path):
    import pytest
    from icx_engine.skills.schema import SkillEntry
    from icx_engine.skills.storage import SkillStorage
    storage = SkillStorage(root=tmp_path)
    bad_entry = SkillEntry(name="../escape", description="d", tags=[], title="x",
                           when_to_use="x", procedure="x", pitfalls="x", verification="x")
    with pytest.raises(ValueError):
        storage.write(bad_entry)
