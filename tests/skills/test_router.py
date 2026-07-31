from __future__ import annotations


def _write(storage, name, tags, description="d"):
    from icx_engine.skills.schema import SkillEntry
    e = SkillEntry(name=name, description=description, tags=tags, title=name,
                    when_to_use="x", procedure="x", pitfalls="x", verification="x")
    e.icx_hash = e.compute_hash()
    storage.write(e)


def test_rank_skills_empty_store_returns_empty_list(tmp_path):
    from icx_engine.skills.router import rank_skills
    from icx_engine.skills.storage import SkillStorage
    storage = SkillStorage(root=tmp_path)
    assert rank_skills("fix the auth bug", "debugging", storage=storage) == []


def test_rank_skills_matches_on_tag_overlap(tmp_path):
    from icx_engine.skills.router import rank_skills
    from icx_engine.skills.storage import SkillStorage
    storage = SkillStorage(root=tmp_path)
    _write(storage, "jwt-fix", tags=["jwt-expiry", "debugging"])
    _write(storage, "unrelated", tags=["css-layout"])
    results = rank_skills("the jwt-expiry token check is failing", "debugging", storage=storage)
    names = [r["name"] for r in results]
    assert "jwt-fix" in names
    assert "unrelated" not in names


def test_rank_skills_caps_at_five(tmp_path):
    from icx_engine.skills.router import rank_skills
    from icx_engine.skills.storage import SkillStorage
    storage = SkillStorage(root=tmp_path)
    for i in range(8):
        _write(storage, f"skill-{i}", tags=["debugging"])
    results = rank_skills("debug this", "debugging", storage=storage)
    assert len(results) <= 5


def test_rank_skills_returns_expected_shape(tmp_path):
    from icx_engine.skills.router import rank_skills
    from icx_engine.skills.storage import SkillStorage
    storage = SkillStorage(root=tmp_path)
    _write(storage, "debugging-skill", tags=["debugging"], description="A debugging skill.")
    results = rank_skills("debug this please", "debugging", storage=storage)
    assert results[0] == {"name": "debugging-skill", "description": "A debugging skill.",
                          "score": results[0]["score"], "scope_hint": "repo-specific"}
    assert results[0]["score"] >= 1


def test_rank_skills_matches_tag_despite_trailing_punctuation(tmp_path):
    from icx_engine.skills.router import rank_skills
    from icx_engine.skills.storage import SkillStorage
    storage = SkillStorage(root=tmp_path)
    _write(storage, "failing-fix", tags=["failing", "debugging"])
    results = rank_skills("the login check is failing.", "debugging", storage=storage)
    assert "failing-fix" in [r["name"] for r in results]


def test_rank_skills_matches_short_acronym_tag(tmp_path):
    from icx_engine.skills.router import rank_skills
    from icx_engine.skills.storage import SkillStorage
    storage = SkillStorage(root=tmp_path)
    _write(storage, "jwt-fix", tags=["jwt"])
    results = rank_skills("the jwt token check is failing", "debugging", storage=storage)
    assert "jwt-fix" in [r["name"] for r in results]


def test_rank_skills_for_tags_matches_on_exact_tag(tmp_path):
    from icx_engine.skills.router import rank_skills_for_tags
    from icx_engine.skills.storage import SkillStorage
    storage = SkillStorage(root=tmp_path)
    _write(storage, "jwt-skill", tags=["jwt-expiry", "auth"])
    _write(storage, "unrelated", tags=["css-layout"])
    results = rank_skills_for_tags(["jwt-expiry"], "uncategorized", storage=storage)
    names = [r["name"] for r in results]
    assert "jwt-skill" in names
    assert "unrelated" not in names


def test_rank_skills_for_tags_matches_on_root_cause_pattern(tmp_path):
    from icx_engine.skills.router import rank_skills_for_tags
    from icx_engine.skills.storage import SkillStorage
    storage = SkillStorage(root=tmp_path)
    _write(storage, "n-plus-one-skill", tags=["n-plus-one-query"])
    results = rank_skills_for_tags([], "n-plus-one-query", storage=storage)
    assert "n-plus-one-skill" in [r["name"] for r in results]


def test_rank_skills_for_tags_case_insensitive(tmp_path):
    from icx_engine.skills.router import rank_skills_for_tags
    from icx_engine.skills.storage import SkillStorage
    storage = SkillStorage(root=tmp_path)
    _write(storage, "case-skill", tags=["MixedCase"])
    results = rank_skills_for_tags(["mixedcase"], "uncategorized", storage=storage)
    assert "case-skill" in [r["name"] for r in results]


def test_rank_skills_for_tags_empty_input_returns_empty_list(tmp_path):
    from icx_engine.skills.router import rank_skills_for_tags
    from icx_engine.skills.storage import SkillStorage
    storage = SkillStorage(root=tmp_path)
    _write(storage, "some-skill", tags=["something"])
    assert rank_skills_for_tags([], "uncategorized", storage=storage) == []


def test_rank_skills_for_tags_empty_store_returns_empty_list(tmp_path):
    from icx_engine.skills.router import rank_skills_for_tags
    from icx_engine.skills.storage import SkillStorage
    storage = SkillStorage(root=tmp_path)
    assert rank_skills_for_tags(["anything"], "uncategorized", storage=storage) == []


def _write_full(storage, name, tags, description, when_to_use="x"):
    from icx_engine.skills.schema import SkillEntry
    e = SkillEntry(name=name, description=description, tags=tags, title=name,
                    when_to_use=when_to_use, procedure="x", pitfalls="x", verification="x")
    e.icx_hash = e.compute_hash()
    storage.write(e)


def test_rank_skills_text_fallback_surfaces_hand_created_skill_with_no_tags(tmp_path):
    from icx_engine.skills.router import rank_skills
    from icx_engine.skills.storage import SkillStorage
    storage = SkillStorage(root=tmp_path)
    _write_full(storage, "text-fallback-skill", tags=[],
                description="Handles the payment webhook retries with exponential backoff.")
    results = rank_skills("the payment webhook retries are failing", "debugging", storage=storage)
    assert "text-fallback-skill" in [r["name"] for r in results]


def test_rank_skills_tag_match_outranks_text_fallback_match(tmp_path):
    from icx_engine.skills.router import rank_skills
    from icx_engine.skills.storage import SkillStorage
    storage = SkillStorage(root=tmp_path)
    _write_full(storage, "tagged-skill", tags=["retries"], description="Handles retries.")
    _write_full(storage, "text-fallback-skill", tags=["unrelated"],
                description="Mentions retries in passing only.")
    results = rank_skills("fix the retries logic", "debugging", storage=storage)
    names = [r["name"] for r in results]
    assert "tagged-skill" in names and "text-fallback-skill" in names
    assert names.index("tagged-skill") < names.index("text-fallback-skill")


def test_rank_skills_zero_overlap_skill_never_appears(tmp_path):
    from icx_engine.skills.router import rank_skills
    from icx_engine.skills.storage import SkillStorage
    storage = SkillStorage(root=tmp_path)
    _write_full(storage, "zero-overlap-skill", tags=["totally-unrelated"],
                description="Nothing to do with any of this.", when_to_use="nothing at all")
    results = rank_skills("fix the retries logic", "debugging", storage=storage)
    assert "zero-overlap-skill" not in [r["name"] for r in results]


def test_rank_skills_for_tags_text_fallback_surfaces_hand_created_skill_with_no_tags(tmp_path):
    from icx_engine.skills.router import rank_skills_for_tags
    from icx_engine.skills.storage import SkillStorage
    storage = SkillStorage(root=tmp_path)
    _write_full(storage, "text-fallback-skill", tags=[],
                description="Handles the payment webhook retries with backoff.")
    results = rank_skills_for_tags(["webhook", "retries"], "uncategorized", storage=storage)
    assert "text-fallback-skill" in [r["name"] for r in results]


def test_rank_skills_for_tags_tag_match_outranks_text_fallback_match(tmp_path):
    from icx_engine.skills.router import rank_skills_for_tags
    from icx_engine.skills.storage import SkillStorage
    storage = SkillStorage(root=tmp_path)
    _write_full(storage, "tagged-skill", tags=["retries"], description="Handles retries.")
    _write_full(storage, "text-fallback-skill", tags=["unrelated"],
                description="Mentions retries in passing only.")
    results = rank_skills_for_tags(["retries"], "uncategorized", storage=storage)
    names = [r["name"] for r in results]
    assert "tagged-skill" in names and "text-fallback-skill" in names
    assert names.index("tagged-skill") < names.index("text-fallback-skill")


def test_rank_skills_for_tags_zero_overlap_skill_never_appears(tmp_path):
    from icx_engine.skills.router import rank_skills_for_tags
    from icx_engine.skills.storage import SkillStorage
    storage = SkillStorage(root=tmp_path)
    _write_full(storage, "zero-overlap-skill", tags=["nope"],
                description="Nothing relevant.", when_to_use="nothing at all")
    results = rank_skills_for_tags(["retries"], "uncategorized", storage=storage)
    assert "zero-overlap-skill" not in [r["name"] for r in results]
