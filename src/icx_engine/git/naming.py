"""Branch-name derivation and parse-back. Convention (design spec Section 3.5):
feature/<slug>-<TICKET-KEY> - the ticket key is always the trailing segment,
so it is recoverable from the branch name alone."""
from __future__ import annotations

import re

_TICKET_KEY_RE = re.compile(r'([A-Z][A-Z0-9]*-[0-9]+)$')
_NON_WORD_RE = re.compile(r"[^a-z0-9]+")


def slugify(text: str, max_words: int = 6) -> str:
    lowered = text.lower()
    words = [w for w in _NON_WORD_RE.sub(" ", lowered).split() if w]
    if not words:
        return "task"
    return "-".join(words[:max_words])


def derive_branch_name(ticket_key: str, summary: str) -> str:
    return f"feature/{slugify(summary)}-{ticket_key}"


def ticketless_branch_name(preferred_name: str) -> str:
    return f"feature/{slugify(preferred_name)}"


def parse_ticket_key_from_branch(branch: str) -> str | None:
    match = _TICKET_KEY_RE.search(branch)
    return match.group(1) if match else None
