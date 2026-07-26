"""Classify a raw request into an archetype, environment-aware. Deterministic; no LLM."""
from __future__ import annotations

import re

from icx_engine.methodology import classify_text

# Archetypes that benefit from codebase context (graph/grep). The rest are knowledge/writing tasks.
CODE_ARCHETYPES = {"coding", "debugging", "performance", "database", "security", "testing"}

# Conversational / acknowledgement / continuation words. A message made up ONLY of these (short, no task
# or question) is trivial - boosting it is pure waste, so icx_boost returns a cheap skip. CONSERVATIVE:
# anything with a real task verb or a question about a subject is NOT trivial and still boosts.
_CONVO_WORDS = {
    "thanks", "thank", "thankyou", "ty", "ok", "okay", "k", "kk", "cool", "nice", "great", "perfect",
    "awesome", "good", "fine", "yes", "yep", "yeah", "yup", "y", "no", "nope", "sure", "correct",
    "right", "exactly", "agreed", "agree", "done", "got", "it", "understood", "continue", "proceed",
    "go", "ahead", "keep", "going", "next", "please", "do", "pls", "plz", "looks", "sounds", "lgtm",
    "makes", "sense", "and", "then", "now", "yea", "ya", "hmm", "hm", "alright",
}
_TRIVIAL_PHRASES = {
    "continue", "proceed", "go ahead", "go on", "keep going", "please continue", "continue please",
    "do it", "please do", "just do it", "looks good", "lgtm", "sounds good", "makes sense", "got it",
    "thanks", "thank you", "ok", "okay", "yes", "no", "sure", "perfect", "great", "nice", "cool",
    "yes please", "no thanks", "carry on", "next", "go for it", "please proceed",
}
_TASK_HINT = re.compile(
    r"\b(fix|add|build|create|write|make|implement|refactor|debug|test|check|review|analyz|design|"
    r"optimi|update|change|remove|delete|explain|why|how|what|which|when|where|who|can you|could you|"
    r"should|is|are|does|error|bug|fail|slow|secure)\b", re.IGNORECASE)


def is_trivial(prompt: str) -> bool:
    """True for a purely conversational / acknowledgement / continuation message that does NOT warrant a
    boost (thanks / ok / yes / continue / do it). Conservative: any task verb or real question -> False
    (it still boosts). Deterministic."""
    t = (prompt or "").strip().lower().rstrip(".!?, ")
    if not t:
        return True
    if t in _TRIVIAL_PHRASES:
        return True
    words = [w.strip(".,;:!?()[]{}\"'") for w in t.split()]
    words = [w for w in words if w]
    if len(words) > 5:
        return False                       # a longer message likely carries real content -> boost
    # short message: trivial only if EVERY word is conversational AND there is no task/question hint
    if any(w not in _CONVO_WORDS for w in words):
        return False
    return not bool(_TASK_HINT.search(t))


def classify(prompt: str, env: dict | None = None) -> str:
    """Return the archetype for a prompt. env is reserved for future shaping (currently the text
    classification is sufficient); kept in the signature so callers pass it consistently."""
    return classify_text(prompt or "")
