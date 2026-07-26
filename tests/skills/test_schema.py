from __future__ import annotations


def _sample_entry(**over):
    from icx_engine.skills.schema import SkillEntry
    defaults = dict(
        name="sqlalchemy-n-plus-one",
        description="N+1 query pattern in SQLAlchemy - use joinedload.",
        tags=["database", "performance"],
        origin_projects=["PROJ"],
        origin_issue_keys=["PROJ-101"],
        scope_hint="repo-specific",
        title="SQLAlchemy N+1 Query Pattern",
        when_to_use="A list endpoint is slow and profiling shows repeated single-row queries.",
        procedure="Add .options(joinedload(Model.relation)) to the query.",
        pitfalls="Don't joinedload a one-to-many relation used for counting - it duplicates rows.",
        verification="Confirmed via query count dropping from N+1 to 1 in the debug toolbar.",
    )
    defaults.update(over)
    e = SkillEntry(**defaults)
    e.icx_hash = e.compute_hash()
    return e


def test_to_markdown_round_trips_through_from_markdown():
    from icx_engine.skills.schema import SkillEntry
    entry = _sample_entry()
    text = entry.to_markdown()
    parsed = SkillEntry.from_markdown(text)
    assert parsed.name == entry.name
    assert parsed.description == entry.description
    assert parsed.tags == entry.tags
    assert parsed.origin_projects == entry.origin_projects
    assert parsed.origin_issue_keys == entry.origin_issue_keys
    assert parsed.scope_hint == entry.scope_hint
    assert parsed.title == entry.title
    assert parsed.when_to_use == entry.when_to_use
    assert parsed.procedure == entry.procedure
    assert parsed.pitfalls == entry.pitfalls
    assert parsed.verification == entry.verification
    assert parsed.icx_hash == entry.icx_hash


def test_frontmatter_is_valid_json():
    import json
    from icx_engine.skills.schema import SkillEntry
    entry = _sample_entry()
    text = entry.to_markdown()
    parts = text.split("---")
    meta = json.loads(parts[1])
    assert meta["name"] == "sqlalchemy-n-plus-one"


def test_compute_hash_ignores_metadata_only_changes():
    """Frontmatter fields like origin_projects/timestamps must NOT affect the hash - only body text -
    or every append-only metadata update would look like a user edit and block future auto-updates."""
    entry = _sample_entry()
    h1 = entry.compute_hash()
    entry.origin_projects.append("OTHER")
    entry.created_at = "2020-01-01T00:00:00+00:00"
    h2 = entry.compute_hash()
    assert h1 == h2


def test_compute_hash_changes_when_body_changes():
    entry = _sample_entry()
    h1 = entry.compute_hash()
    entry.procedure = "Something completely different."
    h2 = entry.compute_hash()
    assert h1 != h2


def test_from_markdown_raises_on_missing_delimiters():
    import pytest
    from icx_engine.skills.schema import SkillEntry
    with pytest.raises(ValueError):
        SkillEntry.from_markdown("no frontmatter here at all")


def test_round_trip_survives_triple_dash_in_a_field():
    from icx_engine.skills.schema import SkillEntry
    e = SkillEntry(name="x", description="some text --- with triple dash", tags=[],
                   title="x", when_to_use="x", procedure="x", pitfalls="x", verification="x")
    e.icx_hash = e.compute_hash()
    parsed = SkillEntry.from_markdown(e.to_markdown())
    assert parsed.description == "some text --- with triple dash"


def test_round_trip_preserves_body_text_containing_a_markdown_heading_line():
    from icx_engine.skills.schema import SkillEntry
    e = SkillEntry(name="x", description="d", tags=[], title="x", when_to_use="x",
                   procedure="Line one\n## Not a real heading\nLine two",
                   pitfalls="x", verification="x")
    e.icx_hash = e.compute_hash()
    parsed = SkillEntry.from_markdown(e.to_markdown())
    assert parsed.procedure == "Line one\n## Not a real heading\nLine two"


def test_round_trip_survives_earlier_section_heading_appearing_in_a_later_section():
    from icx_engine.skills.schema import SkillEntry
    e = SkillEntry(name="x", description="d", tags=[], title="x",
                   when_to_use="real when-to-use text",
                   procedure="x", pitfalls="x",
                   verification="some verification text\n## When to Use\nmore verification text")
    e.icx_hash = e.compute_hash()
    parsed = SkillEntry.from_markdown(e.to_markdown())
    assert parsed.when_to_use == "real when-to-use text"
    assert parsed.verification == "some verification text\n## When to Use\nmore verification text"


def test_from_markdown_raises_valueerror_on_non_dict_frontmatter():
    import pytest
    from icx_engine.skills.schema import SkillEntry
    with pytest.raises(ValueError):
        SkillEntry.from_markdown("---\n[1, 2, 3]\n---\n\nbody")
