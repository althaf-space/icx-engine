"""Pure analytics over the run history: flakiness, pass-trend, slowest tests, heal-trend. No IO."""
from __future__ import annotations


def flakiness(history: dict) -> dict:
    """Per-test flaky score: 0.0 when a test always had the same status; else the fraction of runs
    whose status is the minority (how often it deviates from its own dominant outcome)."""
    out = {}
    for name, entries in (history or {}).items():
        statuses = [s for (s, _t) in entries]
        if len(statuses) < 2 or len(set(statuses)) < 2:
            out[name] = 0.0
            continue
        dominant = max(set(statuses), key=statuses.count)
        deviations = sum(1 for s in statuses if s != dominant)
        out[name] = deviations / len(statuses)
    return out


def suite_flakiness(history: dict) -> float:
    """Fraction of tests that are flaky (score > 0)."""
    fl = flakiness(history)
    if not fl:
        return 0.0
    return sum(1 for v in fl.values() if v > 0) / len(fl)


def pass_trend(runs) -> list:
    """(ts, pass_rate) oldest-first, for a time chart. pass_rate = passed / total (0 when total 0)."""
    pts = []
    for r in sorted(runs or [], key=lambda x: x.ts):
        rate = (r.passed / r.total) if r.total else 0.0
        pts.append((r.ts, rate))
    return pts


def slowest(history: dict, top: int = 10) -> list:
    """(test_name, mean_time) for the slowest tests by average time across their history."""
    means = []
    for name, entries in (history or {}).items():
        times = [t for (_s, t) in entries if isinstance(t, (int, float))]
        if times:
            means.append((name, sum(times) / len(times)))
    means.sort(key=lambda x: x[1], reverse=True)
    return means[:top]


def heal_trend(runs) -> list:
    """(ts, heals) oldest-first, for a heal-frequency chart."""
    return [(r.ts, int(r.heals)) for r in sorted(runs or [], key=lambda x: x.ts)]
