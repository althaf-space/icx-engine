"""Run each corpus prompt through the model twice - raw and ICX-boosted - grade REQUIREMENT COVERAGE with
the rubric, and aggregate the lift overall, per difficulty class, and per archetype. generate + boost are
injected so the harness is pure and testable; a generator failure scores 0 for that run (never crashes)."""
from __future__ import annotations

from dataclasses import dataclass, field

from icx_engine.boost.benchmark.corpus import load_corpus
from icx_engine.boost.benchmark.grader import grade


@dataclass
class BenchReport:
    rows: list = field(default_factory=list)
    raw_avg: float = 0.0
    boosted_avg: float = 0.0
    lift_pct: float = 0.0          # relative; 0 and undefined when raw_avg is 0 (see abs_gain_pts)
    abs_gain_pts: float = 0.0      # absolute gain in percentage points (boosted - raw) * 100
    by_difficulty: dict = field(default_factory=dict)
    by_archetype: dict = field(default_factory=dict)


def _safe_gen(generate, prompt: str) -> str:
    try:
        out = generate(prompt)
        return out if isinstance(out, str) else ""
    except Exception:
        return ""


def _avg_fraction(generate, grade_fn, prompt: str, rubric, repeats: int) -> tuple:
    """Average coverage fraction over `repeats` single-shot runs (reduces model variance). Returns
    (avg_fraction, avg_covered_count) rounded. repeats<=1 is a single run."""
    n = max(1, int(repeats))
    fracs = []
    covered = []
    for _ in range(n):
        g = grade_fn(_safe_gen(generate, prompt), rubric)
        fracs.append(g.fraction)
        covered.append(len(g.hits))
    return round(sum(fracs) / n, 4), round(sum(covered) / n, 2)


def _group_stats(rows: list, key: str) -> dict:
    agg: dict = {}
    for r in rows:
        g = agg.setdefault(r[key], {"raw": 0.0, "boosted": 0.0, "n": 0})
        g["raw"] += r["raw_frac"]
        g["boosted"] += r["boosted_frac"]
        g["n"] += 1
    out = {}
    for k, v in agg.items():
        raw = v["raw"] / v["n"]
        boosted = v["boosted"] / v["n"]
        out[k] = {
            "raw": round(raw, 4),
            "boosted": round(boosted, 4),
            "abs_gain_pts": round((boosted - raw) * 100, 1),
            "lift_pct": round((boosted - raw) / raw * 100, 1) if raw else 0.0,
            "n": v["n"],
        }
    return out


def run_benchmark(generate, boost, corpus=None, repeats: int = 1) -> BenchReport:
    """Run each prompt raw vs boosted, grade requirement coverage, aggregate. `repeats` averages each
    single-shot run to reduce model variance (repeats=1 is a single run)."""
    corpus = corpus if corpus is not None else load_corpus()
    rep = BenchReport()
    raw_total = boosted_total = 0.0
    for p in corpus:
        raw, raw_cov = _avg_fraction(generate, grade, p.prompt, p.rubric, repeats)
        boosted, boost_cov = _avg_fraction(generate, grade, boost(p.prompt), p.rubric, repeats)
        req_total = len(p.rubric)
        rep.rows.append({
            "id": p.id, "archetype": p.archetype,
            "difficulty": getattr(p, "difficulty", "hard"),
            "raw_frac": raw, "boosted_frac": boosted, "delta": round(boosted - raw, 4),
            "req_total": req_total,
            "raw_covered": raw_cov, "boosted_covered": boost_cov,
        })
        raw_total += raw
        boosted_total += boosted
    n = len(corpus) or 1
    rep.raw_avg = round(raw_total / n, 4)
    rep.boosted_avg = round(boosted_total / n, 4)
    rep.lift_pct = round((rep.boosted_avg - rep.raw_avg) / rep.raw_avg * 100, 1) if rep.raw_avg else 0.0
    rep.abs_gain_pts = round((rep.boosted_avg - rep.raw_avg) * 100, 1)
    rep.by_difficulty = _group_stats(rep.rows, "difficulty")
    rep.by_archetype = _group_stats(rep.rows, "archetype")
    return rep
