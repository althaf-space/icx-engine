"""Boost archetype classification (env-aware wrapper over methodology.classify_text)."""
from __future__ import annotations

from icx_engine.boost.classify import classify, CODE_ARCHETYPES


def test_code_task_is_code_archetype():
    assert classify("add a create-user endpoint", {"has_repo": True}) in CODE_ARCHETYPES


def test_plain_question_is_doubt():
    assert classify("what is a monad?", {"has_repo": False}) == "doubt"


def test_debugging_detected():
    assert classify("the app crashes on login", {"has_repo": True}) == "debugging"


def test_empty_prompt_defaults_safely():
    assert classify("", {}) in CODE_ARCHETYPES | {"doubt"}


def test_code_archetypes_membership():
    assert "coding" in CODE_ARCHETYPES and "debugging" in CODE_ARCHETYPES
    assert "doubt" not in CODE_ARCHETYPES


def test_is_trivial_conversational_skips():
    from icx_engine.boost.classify import is_trivial
    for p in ["thanks", "ok", "yes", "no", "sure", "continue", "proceed", "go ahead", "do it",
              "looks good", "please continue", "got it", "perfect", "great", "yes please"]:
        assert is_trivial(p), f"{p!r} should be trivial"


def test_is_trivial_real_requests_boost():
    from icx_engine.boost.classify import is_trivial
    for p in ["fix the auth bug", "add a login endpoint", "what is a monad", "test this screen",
              "can you check the database schema", "why is it slow", "continue the migration script"]:
        assert not is_trivial(p), f"{p!r} should NOT be trivial (it has real content)"


def test_is_trivial_empty_is_trivial():
    from icx_engine.boost.classify import is_trivial
    assert is_trivial("") and is_trivial("   ")
