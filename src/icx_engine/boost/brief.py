"""Deterministic assembly of the boosted brief + boosted prompt. No LLM: the strong restatement is
composed by template from the classified fields, and the connected agent does the semantic work guided
by it. ASCII-only; never raises."""
from __future__ import annotations

_DIRECTIVE = (
    "MANDATORY: this boosted brief is your working spec. Follow the methodology one_pager and the "
    "archetype discipline, treat boosted_prompt as the real task, resolve the clarifications, and pass "
    "the gates (lock_plan before coding, record_verification before done). Do not fall back to the raw "
    "prompt."
)


# Generic completeness dimensions per archetype - good-engineering concerns a thorough answer covers.
# These are REMINDERS of what to address (not answers), so directing the model at them raises coverage
# and quality without diluting the response into process-talk. Deterministic, no LLM.
_COMPLETENESS_DIMS = {
    "coding": ["input validation", "edge cases (empty / null / large / boundary)", "error handling",
               "resource cleanup", "a test or usage example"],
    "debugging": ["reproduce the issue first", "a root cause that explains ALL the symptoms",
                  "fix the class of bug, not just this instance", "how you verify the fix"],
    "security": ["input validation + sanitization", "authentication vs authorization (per-resource)",
                 "secrets handling (never plaintext / in logs)",
                 "injection / traversal / XSS defenses", "rate limiting + safe error messages"],
    "performance": ["measure before optimizing", "algorithmic complexity vs data size",
                    "indexing / caching where it helps", "pagination or streaming for large sets",
                    "avoid N+1 / unbounded memory"],
    "testing": ["happy path + every declared field/functionality", "negative + validation/constraint cases",
                "security probes (XSS/injection) on every free-text input", "accessibility + error-handling",
                "a pass is only real when the runner's own output says so"],
    "database": ["correct types (money as decimal/integer, not float)",
                 "constraints (not null, unique, checks)", "indexes for the access patterns",
                 "safe migrations (no long locks)"],
    "design": ["2-4 real alternatives with trade-offs", "failure modes + what happens at the limit",
               "consistency / invalidation concerns", "start with the simplest thing that works"],
    "planning": ["de-risk the uncertain parts first", "verifiable milestones",
                 "sequence by dependency", "slack for unknowns"],
    "research": ["check current / time-sensitive facts", "cite primary sources",
                 "note disagreement or uncertainty"],
    "writing": ["match the audience + purpose", "clear structure", "lead with the key point"],
    "doubt": ["answer the actual question directly", "a concrete example",
              "state any important caveat"],
}


def _context_lines(context: dict) -> list[str]:
    files = (context or {}).get("files") or []
    if not files:
        return []
    lines = ["", "Relevant files from the codebase (read/verify before editing):"]
    for f in files[:20]:
        lines.append(f"  - [{f.get('tier', '?')}] {f.get('path', '')}"
                     + (f" ({'; '.join(f.get('reasons') or [])})" if f.get("reasons") else ""))
    return lines


def compose_boosted_prompt(prompt: str, archetype: str, methodology: dict, context: dict) -> str:
    """Compose the boosted prompt: LEAD with the user's task (kept prominent), then a terse completeness
    checklist for the task type so the answer is thorough and concrete. Deterministic; no LLM. Leading
    with the task (not a process scaffold) keeps the model producing the concrete answer, not meta-talk."""
    prompt = (prompt or "").strip()
    dims = _COMPLETENESS_DIMS.get(archetype, _COMPLETENESS_DIMS["coding"])
    # Wording chosen by the A/B benchmark (variant "forgets" won: +34% requirement coverage on
    # underspecified prompts vs +20% for the plain checklist). The "a rushed answer forgets the hard
    # parts / do NOT skip" framing measurably raises coverage. See boost/variants.py + `icx boost benchmark`.
    parts = [
        prompt or "(empty request)",
        "",
        f"A rushed answer to this (task type: {archetype}) usually forgets the hard parts. Give a "
        "COMPLETE, concrete, production-grade answer - real code/steps, not a plan or a restatement - "
        "and make sure you do NOT skip any of these where they apply:",
    ]
    parts += [f"- {d}" for d in dims]
    parts += _context_lines(context)
    return "\n".join(parts)


def _link_lines(links: list) -> list[str]:
    if not links:
        return []
    out = ["", "Links (preserved - act on each before answering):"]
    for l in links:
        out.append(f"  - {l.get('url', '')} [{l.get('target', '')}]: {l.get('action', '')}")
    return out


def build_brief(prompt: str, archetype: str, methodology: dict, context: dict,
                activation, clarifications: list, links: list) -> dict:
    """Assemble the full boosted brief. Pure; never raises."""
    prompt = (prompt or "").strip()
    m = dict(methodology or {})
    gates = list(m.pop("gate_sequence", None) or [])   # surfaced as top-level `gates`; not duplicated in methodology
    boosted = compose_boosted_prompt(prompt, archetype, m, context or {})
    link_lines = _link_lines(list(links or []))
    if link_lines:
        boosted = boosted + "\n" + "\n".join(link_lines)
    return {
        "intent": f"{archetype} task: {prompt or '(empty request)'}",
        "archetype": archetype,
        "methodology": m,
        "context": context or {"activated_signals": [], "files": [], "skipped": ""},
        "links": list(links or []),
        "clarifications": list(clarifications or []),
        "gates": gates,
        "boosted_prompt": boosted,
        "boost_meta": {"deterministic": True, "llm_used": False},
        "mandatory_directive": _DIRECTIVE,
        "refine": {
            "tool": "icx_boost_refine",
            "instruction": (
                "For a CTO-grade spec (proven +18% requirement coverage), understand the request and "
                "draft a STRUCTURED version, then call icx_boost_refine with: objective (restate the ask "
                "professionally), requirements[], constraints[], deliverable, acceptance[] (definition of "
                f"done), dims[] (extra items a rushed answer forgets), archetype='{archetype}'. ICX adds "
                "the persona, context, gates and standards deterministically and returns the final "
                "expert prompt. Optional but recommended; skipping it leaves this one-pass brief as the floor."),
        },
    }
