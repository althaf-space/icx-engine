"""Edge confidence tiers used by icx-graph extractors and resolvers."""
from __future__ import annotations

from typing import Final

AST_DIRECT: Final[float] = 1.00
LSP_RESOLVED: Final[float] = 0.95
FRAMEWORK_RESOLVED: Final[float] = 0.95
LLM_CONSENSUS: Final[float] = 0.85
AST_INFERRED: Final[float] = 0.80
UNIVERSAL_AST: Final[float] = 0.55
LLM_SINGLE_PASS: Final[float] = 0.50
LLM_EMBEDDING_REJECTED: Final[float] = 0.10

DEFAULT_MIN_CONFIDENCE: Final[float] = 0.80


def clamp(score: float) -> float:
    if score < 0.0:
        return 0.0
    if score > 1.0:
        return 1.0
    return score


def annotate_edge(edge: dict, score: float, source: str) -> dict:
    edge["confidence_score"] = clamp(score)
    edge["confidence_source"] = source
    return edge
