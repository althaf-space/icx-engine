from icx_engine.sonar import rules


def test_selection_rules_loaded_and_mandatory():
    text = rules.selection_rules()
    assert text
    assert "MANDATORY" in text
    # both selection dimensions must be covered
    assert "project" in text.lower()
    assert "branch" in text.lower()
    # the either/or paste option must be present
    assert "paste" in text.lower()


def test_ensure_seeded_writes_user_copy(tmp_path, monkeypatch):
    monkeypatch.setattr(rules.Path, "home", staticmethod(lambda: tmp_path))
    rules.ensure_seeded()
    seeded = tmp_path / ".icx" / "sonar_rules" / "selection.md"
    assert seeded.exists()
    assert "MANDATORY" in seeded.read_text(encoding="utf-8")
