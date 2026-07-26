"""Deterministic rubric grader: an output satisfies a rubric item if it contains (case-insensitive) ANY
of the item's any_of substrings. Score = sum of satisfied item weights. Pure; never raises."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class GradeResult:
    score: float = 0.0
    max_score: float = 0.0
    hits: list = field(default_factory=list)
    misses: list = field(default_factory=list)

    @property
    def fraction(self) -> float:
        return round(self.score / self.max_score, 4) if self.max_score else 1.0


def grade(output: str, rubric: list) -> GradeResult:
    text = (output or "").lower()
    res = GradeResult()
    for item in rubric or []:
        w = getattr(item, "weight", 1)
        res.max_score += w
        anyof = getattr(item, "any_of", []) or []
        if any(str(s).lower() in text for s in anyof):
            res.score += w
            res.hits.append(getattr(item, "check", ""))
        else:
            res.misses.append(getattr(item, "check", ""))
    return res
