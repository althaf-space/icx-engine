"""Performance regression verification - deterministic before/after comparison.

A ticket can FAIL verification even when all functional tests pass, if a metric regressed beyond its
threshold. Pure + testable; thresholds default to verification.DEFAULT_PERF_THRESHOLDS, overridable.
"""
from __future__ import annotations

from dataclasses import dataclass

from icx_engine.verification import DEFAULT_PERF_THRESHOLDS

# metric key in before/after -> threshold key in DEFAULT_PERF_THRESHOLDS
_METRIC_THRESHOLD = {
    "latency": "latency_pct",
    "memory": "memory_pct",
    "cpu": "cpu_pct",
    "sql_query_count": "sql_query_count_pct",
    "response_time": "response_time_pct",
    "payload_size": "payload_size_pct",
}


@dataclass
class PerfFinding:
    metric: str
    before: float
    after: float
    pct_change: float
    threshold_pct: float
    passed: bool


def compare_performance(before: dict, after: dict, thresholds: dict | None = None) -> list[PerfFinding]:
    """Compare before/after metrics; a metric fails when its percent increase exceeds its threshold.

    Only metrics present in BOTH before and after are compared. sql_query_count has a 0% default
    threshold - any increase in query count is flagged (N+1 guard). Decreases always pass.
    """
    th = {**DEFAULT_PERF_THRESHOLDS, **(thresholds or {})}
    findings: list[PerfFinding] = []
    for metric, thkey in _METRIC_THRESHOLD.items():
        if metric not in before or metric not in after:
            continue
        b = float(before[metric])
        a = float(after[metric])
        if b == 0:
            pct = 0.0 if a == 0 else 100.0
        else:
            pct = round((a - b) / b * 100.0, 2)
        limit = float(th.get(thkey, 0.0))
        passed = pct <= limit
        findings.append(PerfFinding(metric=metric, before=b, after=a, pct_change=pct,
                                    threshold_pct=limit, passed=passed))
    return findings
