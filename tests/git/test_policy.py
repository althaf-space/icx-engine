from __future__ import annotations

from icx_engine.git.policy import validate_branch_name, BranchPolicyResult


def test_validate_branch_name_valid_when_ticket_suffix_present():
    result = validate_branch_name("feature/datatable-sticky-align-ABC-123", require_ticket_suffix=True)
    assert result == BranchPolicyResult(
        valid=True, branch="feature/datatable-sticky-align-ABC-123", require_ticket_suffix=True,
    )


def test_validate_branch_name_valid_when_require_ticket_false_and_no_suffix():
    result = validate_branch_name("feature/datatable-sticky-align-fielditem-menu-props", require_ticket_suffix=False)
    assert result.valid is True
    assert result.reason is None
    assert result.missing_ticket is False


def test_validate_branch_name_invalid_when_require_ticket_true_and_no_suffix():
    branch = "feature/datatable-sticky-align-fielditem-menu-props"
    result = validate_branch_name(branch, require_ticket_suffix=True)
    assert result.valid is False
    assert result.missing_ticket is True
    assert result.expected_pattern == "feature/<name>-<JIRA_ID>"
    assert result.reason == (
        "Invalid branch name.\n\n"
        "Expected pattern:\n"
        "feature/<name>-<JIRA_ID>\n\n"
        "Received:\n"
        f"{branch}\n\n"
        "Missing JIRA/ticket identifier."
    )


def test_validate_branch_name_uses_custom_pattern_description():
    result = validate_branch_name("no-ticket-here", require_ticket_suffix=True, pattern_description="bugfix/<name>-<TICKET>")
    assert "Expected pattern:\nbugfix/<name>-<TICKET>" in result.reason
    assert result.expected_pattern == "bugfix/<name>-<TICKET>"


def test_validate_branch_name_valid_result_has_no_reason_or_pattern():
    result = validate_branch_name("feature/x-ABC-1", require_ticket_suffix=True)
    assert result.reason is None
    assert result.expected_pattern is None
    assert result.missing_ticket is False


def test_validate_branch_name_ticket_suffix_must_be_trailing():
    # ABC-1 appears mid-name, not as the trailing segment - naming.py's own
    # parse_ticket_key_from_branch anchors on the end of the string ($).
    result = validate_branch_name("feature/ABC-1-then-more-words", require_ticket_suffix=True)
    assert result.valid is False


def test_validate_branch_name_rejects_ticketless_0000_placeholder_when_ticket_required():
    # feature/<slug>-<PROJECT_CODE>-0000 (naming.py's ticketless_branch_name) parses as a
    # trailing ticket-shaped suffix but is never a real ticket - a repo that requires one
    # must still refuse it.
    result = validate_branch_name("feature/refactor-auth-module-ICX-0000", require_ticket_suffix=True)
    assert result.valid is False
    assert result.missing_ticket is True


def test_validate_branch_name_accepts_0000_placeholder_when_ticket_not_required():
    result = validate_branch_name("feature/refactor-auth-module-ICX-0000", require_ticket_suffix=False)
    assert result.valid is True
