"""Two-pass boost refinement -> a CTO-grade prompt. Pass 1 (icx_boost) asks the connected AGENT (free -
never the icx model) to understand the request and draft a STRUCTURED professional spec (objective,
requirements, constraints, deliverable, acceptance, dims). Pass 2 (icx_boost_refine) takes that draft and
DETERMINISTICALLY assembles the final expert-grade prompt: a best-in-class persona chosen per problem
(reusing personas.py, exactly like analyze), the agent's professional restatement, the codebase context,
the merged completeness requirements, constraints, deliverable, acceptance criteria (+ ICX gates), and the
methodology standard - so whoever typed the request (junior or senior, vague or precise) the LLM always
receives the same CTO-level spec. Pure; no LLM here (the understanding is the agent's own turn)."""
from __future__ import annotations

import re

from icx_engine.boost.brief import _COMPLETENESS_DIMS, _context_lines
from icx_engine.methodology import _GATE_SEQUENCE
from icx_engine.personas import persona_profile, select_persona

_MAX_DIMS = 12           # bound the merged requirements list so the prompt cannot bloat unboundedly
_MAX_ITEMS = 8           # per-section item cap (constraints, acceptance, etc.)
_MAX_LEN = 200           # per-line length cap
_BULLET_CHARS = "-*" + chr(0x2022)
_BULLET = re.compile(r"^\s*(?:[" + re.escape(_BULLET_CHARS) + r"]|\d+[.)])\s*")


def _clean(line: str) -> str:
    s = _BULLET.sub("", str(line or "")).strip()
    return s[:_MAX_LEN].rstrip()


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def _dedup_list(items, cap: int) -> list:
    out, seen = [], set()
    for raw in (items or []):
        d = _clean(raw)
        if not d or len(d) < 4:
            continue
        n = _norm(d)
        if n in seen:
            continue
        seen.add(n)
        out.append(d)
        if len(out) >= cap:
            break
    return out


def merge_dims(archetype: str, agent_dims) -> list:
    """Merge ICX's generic dims for the archetype with the agent's task-specific draft. Deterministic:
    ICX base dims first (guaranteed coverage), then novel agent dims deduped by normalized text, capped."""
    base = list(_COMPLETENESS_DIMS.get(archetype, _COMPLETENESS_DIMS["coding"]))
    seen = {_norm(d) for d in base}
    merged = list(base)
    for raw in (agent_dims or []):
        d = _clean(raw)
        if not d or len(d) < 4:
            continue
        n = _norm(d)
        if n in seen or any(n in s or s in n for s in seen):
            continue
        seen.add(n)
        merged.append(d)
        if len(merged) >= _MAX_DIMS:
            break
    return merged


def parse_agent_dims(text: str) -> list:
    """Extract a dimension list from the agent's free-text draft (bullets or newline items)."""
    return [_clean(l) for l in str(text or "").splitlines() if _clean(l) and len(_clean(l)) >= 4]


def _default_deliverable(archetype: str) -> str:
    return {
        "coding": "Working, production-grade code with a usage example or test.",
        "debugging": "The root cause, the concrete fix, and how it is verified.",
        "security": "The hardened implementation plus the specific threats it defends against.",
        "performance": "The measured bottleneck, the fix, and the before/after expectation.",
        "testing": "A real, run test (via ICX's testing session) with a pass/fail per case and any "
                   "confirmed app findings - not a plan for tests.",
        "database": "The schema/query change with constraints, indexes, and a safe migration.",
        "design": "A concrete design: chosen approach, the alternatives weighed, and the trade-offs.",
        "planning": "A sequenced, de-risked plan with verifiable milestones.",
        "research": "A synthesized answer with sources and any disagreement noted.",
        "writing": "The finished piece, structured for its audience.",
        "doubt": "A direct, correct answer with a concrete example.",
    }.get(archetype, "A complete, production-grade result.")


def compose_cto_prompt(prompt: str, archetype: str, spec: dict | None = None,
                       context: dict | None = None) -> str:
    """Assemble the final CTO-grade prompt from the agent's structured spec + ICX scaffolding.
    spec keys (all optional - ICX fills any gap): objective, requirements[], constraints[],
    deliverable, acceptance[], dims[]. Deterministic; never raises on partial input."""
    prompt = (prompt or "").strip()
    spec = spec if isinstance(spec, dict) else {}
    context = context or {}

    # Persona: chosen per problem from the request text + archetype (best-in-class, never hardcoded).
    persona = select_persona(prompt + " " + str(spec.get("objective", "")), archetype)
    title, focus = persona_profile(persona)

    objective = _clean(spec.get("objective", "")) or prompt or "(no objective supplied)"
    requirements = merge_dims(archetype, list(spec.get("requirements", []) or []) + list(spec.get("dims", []) or []))
    constraints = _dedup_list(spec.get("constraints", []), _MAX_ITEMS)
    acceptance = _dedup_list(spec.get("acceptance", []), _MAX_ITEMS)
    deliverable = _clean(spec.get("deliverable", "")) or _default_deliverable(archetype)

    lines = [
        "# ROLE",
        f"You are a {title}. Hold every decision to that bar - junior-level guessing is not acceptable. "
        f"As a {title}, {focus}.",
        "",
        "# OBJECTIVE",
        objective,
        "",
    ]
    ctx_lines = _context_lines(context)
    if ctx_lines:
        lines.append("# CONTEXT")
        lines.extend(l for l in ctx_lines if l.strip())
        lines.append("")

    lines.append("# REQUIREMENTS (address each where it applies; do not skip the ones a rushed answer forgets)")
    lines.extend(f"- {r}" for r in requirements)
    lines.append("")

    if constraints:
        lines.append("# CONSTRAINTS")
        lines.extend(f"- {c}" for c in constraints)
        lines.append("")

    lines.append("# DELIVERABLE")
    lines.append(deliverable)
    lines.append("")

    lines.append("# ACCEPTANCE CRITERIA (definition of done)")
    lines.extend(f"- {a}" for a in acceptance)
    lines.append("- Verify correctness: run or mentally-execute the critical paths and edge cases.")
    lines.append("")

    # Senior approach rubric - matches the analyze layer's senior planning rubric so the boost prompt
    # holds the same expert bar (root-cause-with-evidence, 2+ approaches, blast radius, risks + rollback,
    # and a confidence gate to ask before guessing on an ambiguous ask).
    lines.append("# APPROACH (senior standard - satisfy before implementing)")
    lines.append("- For a bug: establish the root cause with concrete evidence; for a feature: pin the "
                 "exact requirement and the interface/data contracts - before writing code.")
    lines.append("- Consider at least two approaches and state why the chosen one wins.")
    lines.append("- State the blast radius and the affected callers of the change.")
    lines.append("- Name the risks, failure modes, and the rollback.")
    lines.append("- If any requirement is genuinely ambiguous and a wrong guess is expensive, ask one "
                 "targeted clarifying question first - do not guess.")
    lines.append("")

    lines.append("# STANDARDS")
    lines.append("Work to the ICX methodology: read the real artifacts before theorizing; fix the root "
                 "cause not the symptom; be complete, correct, secure, and consistent; state calibrated "
                 "confidence and any assumption. Be concrete - real code/steps, not a plan-shaped answer.")
    lines.append("")
    lines.append(f'Original request (verbatim, for reference): "{prompt}"')
    return "\n".join(lines)


# -- Back-compat: the older dims-only refined prompt (kept so callers that pass only dims still work) --

def compose_refined_prompt(prompt: str, archetype: str, agent_dims, context: dict | None = None) -> str:
    """Dims-only refinement (task + merged completeness checklist). Retained for backward compatibility;
    compose_cto_prompt is the richer path when the agent supplies a structured spec."""
    prompt = (prompt or "").strip()
    dims = merge_dims(archetype, agent_dims)
    parts = [
        prompt or "(empty request)",
        "",
        f"A rushed answer to this (task type: {archetype}) usually forgets the hard parts. Give a "
        "COMPLETE, concrete, production-grade answer - real code/steps, not a plan or a restatement - "
        "and make sure you do NOT skip any of these where they apply:",
    ]
    parts += [f"- {d}" for d in dims]
    parts += _context_lines(context or {})
    return "\n".join(parts)
