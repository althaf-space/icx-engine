"""Classify a raw request into an archetype, environment-aware. Deterministic; no LLM."""
from __future__ import annotations

from icx_engine.methodology import classify_text

# Archetypes that benefit from codebase context (graph/grep). The rest are knowledge/writing tasks.
CODE_ARCHETYPES = {"coding", "debugging", "performance", "database", "security"}


def classify(prompt: str, env: dict | None = None) -> str:
    """Return the archetype for a prompt. env is reserved for future shaping (currently the text
    classification is sufficient); kept in the signature so callers pass it consistently."""
    return classify_text(prompt or "")
