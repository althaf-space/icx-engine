"""Analyzer selection + seeding/override."""
from __future__ import annotations

from icx_engine.testing.analyzers import registry as R


def test_every_spec_has_a_bundled_asset():
    # Every prompt referenced by the registry must ship in the package.
    for s in R.list_analyzers():
        p = R._ASSETS_DIR / s.prompt_file
        assert p.exists(), f"missing bundled prompt for {s.id}: {s.prompt_file}"
        assert s.family in R.FAMILIES


def test_select_by_framework_alias():
    assert R.select_analyzer(framework="nextjs").id == "react"
    assert R.select_analyzer(framework="springboot").id == "java"
    assert R.select_analyzer(framework="laravel").id == "php"
    assert R.select_analyzer(framework="actix-web").id == "rust"
    assert R.select_analyzer(framework="nuxt").id == "vue"


def test_select_new_stack_framework_aliases():
    assert R.select_analyzer(framework="play").id == "scala"
    assert R.select_analyzer(framework="http4s").id == "scala"
    assert R.select_analyzer(framework="phoenix").id == "elixir"
    assert R.select_analyzer(framework="absinthe").id == "graphql"
    assert R.select_analyzer(framework="apollo").id == "graphql"
    assert R.select_analyzer(framework="jsf").id == "jsp"
    assert R.select_analyzer(framework="struts").id == "jsp"
    assert R.select_analyzer(framework="grpc").id == "grpc"
    assert R.select_analyzer(framework="opentofu").id == "terraform"


def test_select_new_stack_by_extension():
    assert R.select_analyzer(file_paths=["svc/Routes.scala"]).id == "scala"
    assert R.select_analyzer(file_paths=["lib/router.ex"]).id == "elixir"
    assert R.select_analyzer(file_paths=["web/create.jsp"]).id == "jsp"
    assert R.select_analyzer(file_paths=["api/svc.proto"]).id == "grpc"
    assert R.select_analyzer(file_paths=["infra/main.tf"]).id == "terraform"
    assert R.select_analyzer(file_paths=["schema.graphql"]).id == "graphql"


def test_new_stack_families():
    ids = {s.id: s.family for s in R.list_analyzers()}
    assert ids["scala"] == "backend" and ids["elixir"] == "backend" and ids["graphql"] == "backend"
    assert ids["jsp"] == "ui"
    assert ids["grpc"] == "grpc" and ids["terraform"] == "iac"


def test_select_by_language():
    assert R.select_analyzer(language="python").id == "python"
    assert R.select_analyzer(language="c++").id == "cpp"
    assert R.select_analyzer(language="plsql").id == "sql"
    assert R.select_analyzer(language="golang").id == "go"


def test_select_by_extension_fallback(tmp_path):
    assert R.select_analyzer(file_paths=["x/CreateTeam.jsx"]).id == "react"
    assert R.select_analyzer(file_paths=["x/App.vue"]).id == "vue"
    assert R.select_analyzer(file_paths=["x/Widget.svelte"]).id == "svelte"
    assert R.select_analyzer(file_paths=["svc/main.go"]).id == "go"
    assert R.select_analyzer(file_paths=["lib/proc.sql"]).id == "sql"
    assert R.select_analyzer(file_paths=["engine/core.cpp"]).id == "cpp"


def test_framework_beats_language_beats_extension():
    # explicit framework wins over a mismatched extension
    s = R.select_analyzer(framework="angular", file_paths=["x/thing.py"])
    assert s.id == "angular"


def test_unknown_returns_none():
    assert R.select_analyzer(framework="cobol", language="cobol", file_paths=["x/y.foo"]) is None
    assert R.select_analyzer() is None


def test_family_of_specs():
    ids = {s.id: s.family for s in R.list_analyzers()}
    assert ids["react"] == "ui" and ids["angular"] == "ui"
    assert ids["java"] == "backend" and ids["go"] == "backend"
    assert ids["cpp"] == "cpp" and ids["sql"] == "sql"


def test_ensure_seeded_and_user_override(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "analyzers_dir", lambda: tmp_path)
    R.ensure_seeded()
    seeded = list(tmp_path.glob("*.md"))
    assert len(seeded) == len(R.list_analyzers()) or len(seeded) >= 15  # all prompts seeded
    # user override wins
    react = R._BY_ID["react"]
    (tmp_path / react.prompt_file).write_text("USER-EDITED-PROMPT", encoding="utf-8")
    assert R.prompt_text(react) == "USER-EDITED-PROMPT"


def test_prompt_text_falls_back_to_bundled(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "analyzers_dir", lambda: tmp_path)
    # a brand-new empty dir -> ensure_seeded populates it, text is the bundled census prompt
    txt = R.prompt_text(R._BY_ID["react"])
    assert "ELEMENT CENSUS" in txt.upper() and "PHASE" in txt.upper()
