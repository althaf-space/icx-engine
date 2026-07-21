"""Candidate boost-prompt variants for offline A/B benchmarking. The production boost lives in
`brief.compose_boosted_prompt` (the current champion); these are alternatives measured against it so we
ship the one that GENUINELY scores best - never a guessed choice. All deterministic, no LLM."""
from __future__ import annotations

from icx_engine.boost.brief import _COMPLETENESS_DIMS, compose_boosted_prompt


def _dims(archetype: str) -> list:
    return _COMPLETENESS_DIMS.get(archetype, _COMPLETENESS_DIMS["coding"])


def v_champion(prompt: str, archetype: str) -> str:
    """The current production wording (lead with task + completeness checklist)."""
    return compose_boosted_prompt(prompt, archetype, {}, {"files": [], "skipped": "benchmark"})


def v_checklist_plain(prompt: str, archetype: str) -> str:
    """Leaner: task + bare checklist, no framing sentence."""
    dims = _dims(archetype)
    return (prompt.strip() + "\n\nCover each of these where it applies:\n"
            + "\n".join(f"- {d}" for d in dims))


def v_forgets(prompt: str, archetype: str) -> str:
    """Task + checklist + an explicit 'what a rushed answer forgets' nudge."""
    dims = _dims(archetype)
    return (prompt.strip()
            + "\n\nA rushed answer to this usually forgets the hard parts. Give a COMPLETE, concrete, "
              "production-grade answer and make sure you do NOT skip any of these:\n"
            + "\n".join(f"- {d}" for d in dims))


def v_selfcheck(prompt: str, archetype: str) -> str:
    """Task + checklist + a self-check instruction at the end."""
    dims = _dims(archetype)
    return (prompt.strip()
            + "\n\nGive a COMPLETE, concrete answer. Address each where it applies:\n"
            + "\n".join(f"- {d}" for d in dims)
            + "\n\nBefore finishing, re-read your answer and add anything from the list you missed.")


# name -> callable(prompt, archetype) -> boosted prompt. The A/B harness runs all of these.
VARIANTS = {
    "champion": v_champion,
    "checklist_plain": v_checklist_plain,
    "forgets": v_forgets,
    "selfcheck": v_selfcheck,
}
