"""One-time confirmation tokens enforcing the MCP path's confirmation rule
(design spec Section 9.2): a risky tool call first returns
`pending_confirmation` plus a token; the actual action only executes when
that exact token is passed back on a follow-up call. This forces an explicit
two-step round-trip - a single call can never execute the destructive
action - but it does not, and structurally cannot, prove a human actually
saw and approved the pending action; MCP gives ICX no channel to a human
independent of the agent. Do not describe this as "unbypassable" - it
guards against a single-call or ignored-two-step mistake, not against a
fully complicit or malfunctioning agent.

In-memory only - scoped to one MCP server process's lifetime, which is
exactly the lifetime a token needs to survive (one editor session). Bounded
to `_MAX_PENDING` entries (oldest evicted first, dicts preserve insertion
order): an abandoned pending_confirmation (agent mints a token, never
follows up) still leaves its entry - including the full arguments payload -
resident, but no longer unbounded, so a buggy or looping agent minting
tokens without ever confirming cannot grow this past a fixed ceiling.
The CLI path does not use this module at all; it confirms via plain
`typer.confirm()` since a human is directly at the terminal, not an agent
that could ignore instructions.

Package-neutral: shared by every MCP subsystem that gates a destructive
action behind a confirmation round-trip (git-workflow, Jira write-back,
etc.) - not specific to any one of them."""
from __future__ import annotations

import uuid

_MAX_PENDING = 500

_PENDING: dict[str, tuple[str, dict]] = {}


def issue_token(action: str, payload: dict) -> str:
    if len(_PENDING) >= _MAX_PENDING:
        oldest = next(iter(_PENDING))
        del _PENDING[oldest]
    token = uuid.uuid4().hex
    _PENDING[token] = (action, payload)
    return token


def verify_token(token: str, action: str) -> dict | None:
    """Single-use: a valid token is consumed (popped) on success. Returns None
    (and leaves the token untouched) for an unknown token or an action
    mismatch, so a caller can retry with the correct action."""
    entry = _PENDING.get(token)
    if entry is None:
        return None
    stored_action, payload = entry
    if stored_action != action:
        return None
    del _PENDING[token]
    return payload
