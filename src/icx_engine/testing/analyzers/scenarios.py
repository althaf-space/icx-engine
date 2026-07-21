"""Build the extra-scenario guidance appended to the author_flow_explore gate. Pure text - it names a
plain-English intent and/or a ticket's acceptance criteria so the CONNECTED agent authors steps that
exercise them. Empty when there is nothing to add (so the gate is unchanged by default). No LLM here -
this only composes the prompt the agent already answers."""
from __future__ import annotations


def _s(v) -> str:
    return str(v).strip() if v is not None else ""


def build_scenario_guidance(nl_intent, acceptance_criteria) -> str:
    intent = _s(nl_intent)
    crits = [c for c in ( _s(x) for x in (acceptance_criteria or []) ) if c]
    if not intent and not crits:
        return ""
    parts = ["\n\nADDITIONALLY author steps for these REQUESTED scenarios (append them too, same step "
             "schema, they run after the base):"]
    if intent:
        parts.append(f"NL intent: {intent}")
    if crits:
        parts.append("Acceptance criteria (author a scenario that exercises AND asserts each one):")
        for c in crits:
            parts.append(f"  - {c}")
    parts.append("For each, drive the exact UI actions the scenario needs and assert its expected "
                 "outcome (an error message, a saved value, a restored state). Skip any that the base "
                 "flow already covers.")
    return "\n".join(parts)
