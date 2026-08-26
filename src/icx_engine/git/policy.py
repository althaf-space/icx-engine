"""Configurable branch-name policy validation. No organization-specific
values are hardcoded here - the only rule this module knows out of the box
is ICX's own existing branch-naming convention (naming.py):
feature/<slug>-<TICKET_KEY>, trailing ticket key parsed by the exact same
regex naming.py already uses to parse it back (parse_ticket_key_from_branch)
- one source of truth for what "has a ticket suffix" means, never a second,
separately-maintained pattern that could drift.

Whether a given repo actually REQUIRES that trailing ticket key is a
per-repo setting (git/settings.py's require_ticket_in_branch_name, default
False) - preserves the existing, tested, documented ticketless-branch
feature for every repo that never asked for stricter enforcement. A repo
that has been bitten by a remote pre-receive hook rejecting a ticketless
feature branch opts in explicitly via git_set_branch_policy; nothing here
guesses an org's real policy or auto-detects it from a failed push.
"""
from __future__ import annotations

from dataclasses import dataclass

from icx_engine.git.naming import parse_ticket_key_from_branch

EXPECTED_PATTERN_DESCRIPTION = "feature/<name>-<JIRA_ID>"


@dataclass
class BranchPolicyResult:
    valid: bool
    branch: str
    require_ticket_suffix: bool
    reason: str | None = None
    expected_pattern: str | None = None
    missing_ticket: bool = False


def validate_branch_name(
    branch: str, require_ticket_suffix: bool, pattern_description: str = EXPECTED_PATTERN_DESCRIPTION,
) -> BranchPolicyResult:
    """Pure validation - require_ticket_suffix is always passed in explicitly,
    never read from config here (see manager.check_branch_name_policy for the
    per-repo-setting-aware wrapper) - keeps this trivially testable and reusable
    by anything that has a candidate branch name and a policy decision already
    in hand, not just the git-workflow manager."""
    ticket = parse_ticket_key_from_branch(branch)
    # A trailing "-0000" is ICX's own ticketless placeholder (naming.py's
    # ticketless_branch_name), never a real ticket - real Jira ticket numbers start at 1,
    # so this can't collide with a genuine ticket key. Without this check, a repo that
    # opted into require_ticket_in_branch_name specifically to reject ticketless branches
    # would be silently satisfied by the placeholder, defeating the whole setting.
    has_real_ticket = ticket is not None and not ticket.endswith("-0000")
    if not require_ticket_suffix or has_real_ticket:
        return BranchPolicyResult(valid=True, branch=branch, require_ticket_suffix=require_ticket_suffix)
    return BranchPolicyResult(
        valid=False,
        branch=branch,
        require_ticket_suffix=require_ticket_suffix,
        reason=(
            f"Invalid branch name.\n\nExpected pattern:\n{pattern_description}\n\n"
            f"Received:\n{branch}\n\nMissing JIRA/ticket identifier."
        ),
        expected_pattern=pattern_description,
        missing_ticket=True,
    )
