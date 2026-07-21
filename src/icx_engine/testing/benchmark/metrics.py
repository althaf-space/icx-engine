"""Pure metric functions for the benchmark. Operate on saved data (census dict, JUnit TestReport,
ground-truth dict) so they are fully unit-testable without a live app."""
from __future__ import annotations

import re as _re
from dataclasses import dataclass, field


def _norm(s) -> str:
    return _re.sub(r"[^a-z0-9]", "", str(s or "").lower())


@dataclass
class CoverageScore:
    recall: float
    precision: float
    true_total: int
    discovered_total: int
    matched: int


@dataclass
class RunMetrics:
    app: str
    url: str
    coverage: CoverageScore
    misfire_rate: float
    flakiness: float
    speed_seconds: float
    authoring_actions: int
    total_tests: int
    real_findings: int
    cross_browser: dict = field(default_factory=dict)   # target label -> pass ratio; {} when single-target
    self_heal: dict = field(default_factory=dict)   # {"injected", "recovered", "rate"}; {} when not run
    visual: dict = field(default_factory=dict)   # {"checked","baselines","regressions"}; {} when none
    a11y: dict = field(default_factory=dict)   # {"violations","critical","serious"}; {} when no a11y case
    dataflow: dict = field(default_factory=dict)   # {"db_checked","db_confirmed","net_checked"}; {} when none


def _discovered_labels(census: dict) -> set[str]:
    """Every functionality name + field label the census discovered, normalized for matching."""
    out: set[str] = set()
    for fn in (census.get("functionalities") or []):
        if not isinstance(fn, dict):
            continue
        if _norm(fn.get("functionality")):
            out.add(_norm(fn.get("functionality")))
        for fld in (fn.get("fields") or []):
            if isinstance(fld, dict) and _norm(fld.get("label") or fld.get("fieldName")):
                out.add(_norm(fld.get("label") or fld.get("fieldName")))
        for st in (fn.get("steps") or []):
            for fld in (st.get("fields") or []) if isinstance(st, dict) else []:
                if isinstance(fld, dict) and _norm(fld.get("label") or fld.get("fieldName")):
                    out.add(_norm(fld.get("label") or fld.get("fieldName")))
    return out


def coverage(census: dict, ground_truth: dict) -> CoverageScore:
    """Recall = how much of the ground truth the census found; precision = how much of what it found is
    real (present in the ground truth). Matching is on normalized labels."""
    truth = {_norm(e.get("label")) for e in (ground_truth.get("elements") or [])
             if isinstance(e, dict) and _norm(e.get("label"))}
    disc = _discovered_labels(census)
    matched = len(truth & disc)
    recall = matched / len(truth) if truth else 0.0
    precision = matched / len(disc) if disc else 0.0
    return CoverageScore(recall=recall, precision=precision, true_total=len(truth),
                         discovered_total=len(disc), matched=matched)


# failing testcases that are GENUINE findings, not tool misfires.
_REAL_CATEGORIES = ("accessibility", "a11y", "security", "sqli", "xss")


def _is_real_finding(text: str, ground_truth: dict) -> bool:
    # Accessibility/security categories need no ground-truth match: those checkers are
    # deterministic true-positive assertions (a real WCAG violation, or an XSS canary that
    # actually executed), never a tool guess - counting them as real findings (not misfires)
    # is what keeps the misfire rate honest.
    low = text.lower()
    if any(c in low for c in _REAL_CATEGORIES):
        return True
    for kb in (ground_truth.get("known_bugs") or []):
        if isinstance(kb, dict) and _norm(kb.get("match")) and _norm(kb.get("match")) in _norm(text):
            return True
    return False


def misfire_rate(report, ground_truth: dict) -> tuple[float, int]:
    """Fraction of assertions that failed for a NON-real reason (a tool misfire), plus the count of
    genuine findings among the failures. A near-zero misfire rate is the accuracy goal."""
    cases = list(getattr(report, "cases", []) or [])
    total = len(cases)
    if total == 0:
        return 0.0, 0
    real = 0
    misfires = 0
    for c in cases:
        if getattr(c, "status", "") != "failed":
            continue
        name = getattr(c, "name", "")
        message = getattr(c, "message", "") or ""
        combined_text = name + " " + message
        if _is_real_finding(combined_text, ground_truth):
            real += 1
        else:
            misfires += 1
    return misfires / total, real


def flakiness(reports: list) -> float:
    """Fraction of tests whose pass/fail status varied across the repeat runs. 0.0 with < 2 runs."""
    if not reports or len(reports) < 2:
        return 0.0
    status_by_test: dict[str, set[str]] = {}
    for rep in reports:
        for c in (getattr(rep, "cases", []) or []):
            name = getattr(c, "name", "")
            st = getattr(c, "status", "")
            status_by_test.setdefault(name, set()).add(st)
    if not status_by_test:
        return 0.0
    flaky = sum(1 for sts in status_by_test.values() if len(sts) > 1)
    return flaky / len(status_by_test)


def cross_browser_pass(reports_by_target: dict) -> dict:
    """Per-target pass ratio (passed / total cases). Empty dict for no targets. A target whose report is
    None (engine unavailable) is omitted, so an uninstalled engine does not read as a failure."""
    out = {}
    for label, rep in (reports_by_target or {}).items():
        if rep is None:
            continue
        cases = list(getattr(rep, "cases", []) or [])
        if not cases:
            continue
        passed = sum(1 for c in cases if getattr(c, "status", "") == "passed")
        out[label] = passed / len(cases)
    return out


def self_heal_rate(injected: int, recovered: int) -> dict:
    """Recovery ratio for the self-heal probe: recovered / injected (0.0 when nothing injected)."""
    injected = int(injected or 0)
    recovered = int(recovered or 0)
    rate = (recovered / injected) if injected else 0.0
    return {"injected": injected, "recovered": recovered, "rate": rate}


def visual_summary(report) -> dict:
    """Count visual-regression cases (name starts with 'VISUAL:'): total checked, baselines captured,
    and regressions. A regression is a strict failure (status=="failed") OR a soft-flagged woven step
    (status=="skipped" carrying a "VISUAL DIFF (review)" name/message) - the woven step never hard-fails
    the run, but a real pixel diff must still count against the visual-regression metric. {} when there
    are no visual cases."""
    cases = [c for c in (getattr(report, "cases", []) or []) if str(getattr(c, "name", "")).startswith("VISUAL:")]
    if not cases:
        return {}
    baselines = sum(1 for c in cases if "baseline captured" in str(c.name))
    regressions = sum(1 for c in cases
                      if getattr(c, "status", "") == "failed"
                      or "VISUAL DIFF (review)" in str(c.name) + (getattr(c, "message", "") or ""))
    return {"checked": len(cases), "baselines": baselines, "regressions": regressions}


def a11y_summary(report) -> dict:
    """Parse the a11y case (name contains 'ACCESSIBILITY' or 'a11y') for axe violation counts by impact.
    A passing a11y case -> zeros; no a11y case -> {}. Handles both axe format (a11y violations (axe wcag2.1aa) N [...])
    and builtin format (a11y violations (N):...)."""
    cases = [c for c in (getattr(report, "cases", []) or [])
             if "accessibility" in str(getattr(c, "name", "")).lower() or "a11y" in str(getattr(c, "name", "")).lower()]
    if not cases:
        return {}
    c = cases[0]
    if getattr(c, "status", "") != "failed":
        return {"violations": 0, "critical": 0, "serious": 0}
    msg = str(getattr(c, "message", "") or "")
    def _n(key):
        m = _re.search(rf"{key}:(\d+)", msg)
        return int(m.group(1)) if m else 0
    total = 0
    mt = _re.search(r"\)\s*(\d+)\s*\[", msg)
    if mt:
        total = int(mt.group(1))
    else:
        mb = _re.search(r"a11y violations \((\d+)\)", msg)
        if mb:
            total = int(mb.group(1))
    return {"violations": total, "critical": _n("critical"), "serious": _n("serious")}


def dataflow_summary(report) -> dict:
    """Count the DATAFLOW: cases: DB verifies (checked / confirmed) and network-graceful checks. {} when
    none. A DB verify is 'confirmed' when its case passed and the name carries 'confirmed'; a network
    check is any DATAFLOW case whose name mentions 'network'."""
    cases = [c for c in (getattr(report, "cases", []) or [])
             if str(getattr(c, "name", "")).startswith("DATAFLOW:")]
    if not cases:
        return {}
    db = [c for c in cases if "db verify" in str(getattr(c, "name", "")).lower()]
    db_confirmed = sum(1 for c in db if getattr(c, "status", "") == "passed"
                       and "confirmed" in str(getattr(c, "name", "")).lower())
    net = [c for c in cases if "network" in str(getattr(c, "name", "")).lower()]
    return {"db_checked": len(db), "db_confirmed": db_confirmed, "net_checked": len(net)}


def build_run_metrics(app: str, url: str, census: dict, reports: list, seconds: float,
                      authoring_actions: int, ground_truth: dict,
                      cross_browser: dict | None = None, self_heal: dict | None = None,
                      visual: dict | None = None, a11y: dict | None = None,
                      dataflow: dict | None = None) -> RunMetrics:
    """Assemble the full metric set for one app from its census, its repeat-run reports, and timing."""
    first = reports[0] if reports else None
    rate, real = misfire_rate(first, ground_truth) if first is not None else (0.0, 0)
    total = len(getattr(first, "cases", []) or []) if first is not None else 0
    return RunMetrics(
        app=app, url=url,
        coverage=coverage(census, ground_truth),
        misfire_rate=rate,
        flakiness=flakiness(reports),
        speed_seconds=float(seconds),
        authoring_actions=int(authoring_actions),
        total_tests=total,
        real_findings=real,
        cross_browser=cross_browser or {},
        self_heal=self_heal or {},
        visual=visual or {},
        a11y=a11y or {},
        dataflow=dataflow or {},
    )
