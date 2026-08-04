"""Context-completeness engine: fuse graph + grep + semantic + memory signals, rank, and detect
files a plan missed - so the agent never plans/codes without the relevant context.

Pure module. It does NO I/O itself and calls NO LLM. Each retrieval signal is injected as a callable
returning ``[(path, reason)]``; the MCP layer supplies the real graph/grep/semantic/memory providers.
This keeps the engine fully unit-testable with fakes and reusable by both the analyze flow and the
testing gates.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

# Signal weights (deliberate; agreement + prior-fix dominate). Module constants, best-practice.
_W_AGREEMENT = 3.0     # per additional signal that hits the file
_W_CENTRALITY = 2.0    # graph importance in [0,1]
_W_RECENCY = 1.0       # recently changed in [0,1]
_W_PRIOR_FIX = 4.0     # a prior resolution touched it

# A retrieval signal: () -> list[(path, reason)]. Returning [] (no graph, no memory) is fine.
Signal = Callable[[], "list[tuple[str, str]]"]


def _norm(p: str) -> str:
    return (p or "").replace("\\", "/")


@dataclass
class Candidate:
    path: str
    signals: set[str] = field(default_factory=set)
    reasons: list[str] = field(default_factory=list)


@dataclass
class ScoredFile:
    path: str
    score: float
    tier: str                      # "high" | "medium" | "low"
    signals: list[str]
    reasons: list[str]

    def to_dict(self) -> dict:
        return {"path": self.path, "score": round(self.score, 3), "tier": self.tier,
                "signals": sorted(self.signals), "reasons": self.reasons}


# Signals whose presence ALONE marks a file high-tier (blocking): a direct structural graph
# tie only. "memory" (a past ticket's resolution touched this file) used to be bundled in
# here too, alone promoting a file to blocking - real-world use showed this produces false
# blocks: a file a wholly unrelated past ticket happened to touch (e.g. a JPA entity, a
# config file) got flagged as a blocking miss on an unrelated one-line UI fix, forcing a
# justification for every such file regardless of actual relevance. memory overlap is still
# scored prominently (_W_PRIOR_FIX) and still promotes to "high" when combined with another
# signal (real multi-signal agreement, via len(non_seed) >= 2) - it just no longer blocks on
# its own. (grep = weak/noisy, semantic/memory-alone = medium/advisory. These match the
# signal names fan_out emits.)
_STRUCTURAL_SIGNALS = {"graph"}


def fan_out(
    seeds: list[str],
    *,
    graph: Signal | None = None,
    grep: Signal | None = None,
    semantic: Signal | None = None,
    memory: Signal | None = None,
    extra: dict[str, Signal] | None = None,
) -> list[Candidate]:
    """Gather candidates from every provided signal. Seeds are always included (they are the change).
    Each signal is guarded - one raising or returning junk never breaks the others."""
    by_path: dict[str, Candidate] = {}

    def _add(path: str, signal: str, reason: str) -> None:
        n = _norm(path)
        if not n:
            return
        c = by_path.get(n)
        if c is None:
            c = Candidate(path=n)
            by_path[n] = c
        c.signals.add(signal)
        if reason and reason not in c.reasons:
            c.reasons.append(reason)

    for s in seeds or []:
        _add(s, "seed", "changed/seed file")

    named: dict[str, Signal | None] = {
        "graph": graph, "grep": grep, "semantic": semantic, "memory": memory,
    }
    if extra:
        named.update(extra)
    for name, sig in named.items():
        if sig is None:
            continue
        try:
            results = sig() or []
        except Exception:
            results = []
        for item in results:
            try:
                path, reason = item[0], (item[1] if len(item) > 1 else "")
            except (TypeError, IndexError):
                continue
            _add(str(path), name, str(reason))
    return list(by_path.values())


def _tier(cand: Candidate, is_prior_fix: bool) -> str:
    """`is_prior_fix` (path is in fuse_rank's `prior_fix` set, or carries a "memory" signal -
    see fan_out) is advisory-only, never a blocking-tier promotion by itself - see
    _STRUCTURAL_SIGNALS' comment for why. It still promotes to "high" when combined with
    another real signal (len(non_seed) >= 2)."""
    if "seed" in cand.signals:
        return "seed"
    non_seed = cand.signals - {"seed"}
    if len(non_seed) >= 2 or (cand.signals & _STRUCTURAL_SIGNALS):
        return "high"
    if "semantic" in non_seed or is_prior_fix:
        return "medium"
    return "low"


def fuse_rank(
    candidates: list[Candidate],
    *,
    centrality: dict[str, float] | None = None,
    recency: dict[str, float] | None = None,
    prior_fix: set[str] | None = None,
) -> list[ScoredFile]:
    """Merge + score + rank. Union recall (nothing dropped), ranked precision. centrality/recency are
    optional per-path signals in [0,1]; prior_fix is the set of files past resolutions touched."""
    centrality = centrality or {}
    recency = recency or {}
    prior_fix = {_norm(p) for p in (prior_fix or set())}

    scored: list[ScoredFile] = []
    for c in candidates:
        non_seed = c.signals - {"seed"}
        n = _norm(c.path)
        pf = 1.0 if (n in prior_fix or "memory" in c.signals) else 0.0
        score = (
            _W_AGREEMENT * len(non_seed)
            + _W_CENTRALITY * float(centrality.get(n, 0.0))
            + _W_RECENCY * float(recency.get(n, 0.0))
            + _W_PRIOR_FIX * pf
        )
        scored.append(ScoredFile(
            path=c.path, score=score, tier=_tier(c, pf > 0.0),
            signals=sorted(c.signals), reasons=list(c.reasons),
        ))
    # Rank: seeds first, then by score desc, then path for stability.
    _order = {"seed": 0, "high": 1, "medium": 2, "low": 3}
    scored.sort(key=lambda s: (_order.get(s.tier, 9), -s.score, s.path))
    return scored


def coverage(chosen: list[str], scored: list[ScoredFile], justified: set[str] | None = None) -> float:
    """Fraction of HIGH-tier candidates the plan covers (chosen or justified). 1.0 when none exist."""
    justified = {_norm(p) for p in (justified or set())}
    chosen_set = {_norm(p) for p in (chosen or [])} | justified
    high = [s for s in scored if s.tier == "high"]
    if not high:
        return 1.0
    covered = sum(1 for s in high if _norm(s.path) in chosen_set)
    return round(covered / len(high), 3)


def miss_check(
    chosen: list[str],
    scored: list[ScoredFile],
    justifications: dict[str, str] | None = None,
) -> dict:
    """Block on unresolved HIGH-tier misses; medium/low are advisory. justify-or-include: a high-tier
    file is covered if chosen OR justified. Returns {ok, coverage, missed, accepted}. Never raises."""
    justifications = {_norm(k): v for k, v in (justifications or {}).items()}
    chosen_set = {_norm(p) for p in (chosen or [])}
    justified = set(justifications)

    missed: list[dict] = []
    for s in scored:
        if s.tier in ("seed",):
            continue
        n = _norm(s.path)
        if n in chosen_set or n in justified:
            continue
        missed.append({
            "path": s.path, "tier": s.tier, "signals": s.signals, "reasons": s.reasons,
            "blocking": s.tier == "high",
        })
    blocking = [m for m in missed if m["blocking"]]
    cov = coverage(chosen, scored, justified)
    return {
        "ok": not blocking,
        "coverage": cov,
        "missed": missed,
        "blocking_missed": blocking,
        "accepted": sorted(chosen_set | justified),
    }
