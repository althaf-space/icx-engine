"""Branch-name derivation and parse-back. Convention (design spec Section 3.5):
feature/<slug>-<TICKET-KEY> - the ticket key is always the trailing segment,
so it is recoverable from the branch name alone. A ticketless branch uses a
human-supplied project_code with a literal 0000 in place of a real ticket
number (feature/<slug>-<PROJECT_CODE>-0000), so the same trailing-segment
convention - and parse_ticket_key_from_branch - covers both cases uniformly."""
from __future__ import annotations

import re

_TICKET_KEY_RE = re.compile(r'([A-Z][A-Z0-9]*-[0-9]+)$')
_NON_WORD_RE = re.compile(r"[^a-z0-9]+")
_PROJECT_CODE_RE = re.compile(r'^[A-Za-z][A-Za-z0-9]*$')


def slugify(text: str, max_words: int = 6) -> str:
    lowered = text.lower()
    words = [w for w in _NON_WORD_RE.sub(" ", lowered).split() if w]
    if not words:
        return "task"
    return "-".join(words[:max_words])


def validate_project_code(project_code: str) -> None:
    """Raises ValueError if project_code isn't a plain alphanumeric token (no spaces,
    slashes, or punctuation) - it becomes a literal branch-name segment, never derived
    or defaulted, so it must be safe to drop straight into a git ref."""
    if not project_code or not _PROJECT_CODE_RE.match(project_code):
        raise ValueError(
            f"project_code must be a non-empty alphanumeric token (e.g. 'ICX'), got: {project_code!r}"
        )


def derive_branch_name(ticket_key: str, summary: str) -> str:
    return f"feature/{slugify(summary)}-{ticket_key}"


def ticketless_branch_name(project_code: str, preferred_name: str) -> str:
    validate_project_code(project_code)
    return f"feature/{slugify(preferred_name)}-{project_code.upper()}-0000"


def parse_ticket_key_from_branch(branch: str) -> str | None:
    match = _TICKET_KEY_RE.search(branch)
    return match.group(1) if match else None
