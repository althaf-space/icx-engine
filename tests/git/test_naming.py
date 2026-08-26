from __future__ import annotations
import pytest
from icx_engine.git.naming import (
    slugify, derive_branch_name, ticketless_branch_name, parse_ticket_key_from_branch,
    validate_project_code,
)


def test_slugify_lowercases_and_hyphenates():
    assert slugify("Fix Login Timeout") == "fix-login-timeout"


def test_slugify_strips_punctuation():
    assert slugify("Fix login timeout! (again)") == "fix-login-timeout-again"


def test_slugify_caps_word_count():
    assert slugify("one two three four five six seven eight", max_words=6) == "one-two-three-four-five-six"


def test_slugify_empty_input_returns_task():
    assert slugify("") == "task"
    assert slugify("!!!") == "task"


def test_derive_branch_name_matches_convention():
    assert derive_branch_name("ABC-123", "Fix login timeout") == "feature/fix-login-timeout-ABC-123"


def test_ticketless_branch_name_uses_project_code_with_0000_suffix():
    assert ticketless_branch_name("ICX", "Refactor auth module") == "feature/refactor-auth-module-ICX-0000"


def test_ticketless_branch_name_uppercases_project_code():
    assert ticketless_branch_name("icx", "Refactor auth module") == "feature/refactor-auth-module-ICX-0000"


def test_ticketless_branch_name_rejects_invalid_project_code():
    with pytest.raises(ValueError):
        ticketless_branch_name("", "Refactor auth module")
    with pytest.raises(ValueError):
        ticketless_branch_name("not a code", "Refactor auth module")
    with pytest.raises(ValueError):
        ticketless_branch_name("ICX/1", "Refactor auth module")


def test_validate_project_code_accepts_alphanumeric():
    validate_project_code("ICX")
    validate_project_code("Proj2")


def test_parse_ticket_key_from_branch_extracts_trailing_key():
    assert parse_ticket_key_from_branch("feature/fix-login-timeout-ABC-123") == "ABC-123"


def test_parse_ticket_key_from_branch_extracts_ticketless_placeholder():
    assert parse_ticket_key_from_branch("feature/refactor-auth-module-ICX-0000") == "ICX-0000"


def test_parse_ticket_key_from_branch_none_for_prefixless_branch():
    assert parse_ticket_key_from_branch("refactor-auth-module") is None


def test_parse_ticket_key_from_branch_none_for_non_feature_branch():
    assert parse_ticket_key_from_branch("main") is None
