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
