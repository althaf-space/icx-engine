"""Definition-of-Done verification: checklist, risk tiering, evidence validation, confidence.

Pure module - no I/O, no MCP/engine imports - so it is fully unit-testable and reusable by CLI,
MCP, and tests. All knobs have best-practice defaults; callers never need to configure to get the
recommended path.
"""
from __future__ import annotations

_BUG_TYPES = {"bug", "defect", "incident", "error"}
_STORY_TYPES = {"story", "task", "epic"}

# Default risk-tier -> recommended verification layers. Recommendation only; the user chooses.
DEFAULT_TIER_LAYERS: dict[str, list[str]] = {
    "low": ["unit"],
    "medium": ["unit", "api"],
    "high": ["unit", "api", "ui", "regression"],
    "critical": ["unit", "mutation", "api", "ui", "regression", "performance", "security"],
}
DEFAULT_TIER = "medium"

# Default performance-regression thresholds (percent increase that fails a ticket). Overridable.
DEFAULT_PERF_THRESHOLDS: dict[str, float] = {
    "latency_pct": 20.0,
    "memory_pct": 25.0,
    "cpu_pct": 30.0,
    "sql_query_count_pct": 0.0,   # any increase in query count is flagged
    "response_time_pct": 20.0,
    "payload_size_pct": 25.0,
}


def _dod_item(check: str, method: str) -> dict:
    return {"check": check, "method": method, "passed": False, "command": "", "output": ""}


def build_dod_checklist(analysis: dict) -> list[dict]:
    """Build an explicit Definition-of-Done checklist from an IssueContext-shaped dict.

    Bug: reproduce -> confirm failure -> fix -> confirm resolved (from reproduction_steps +
    expected/actual). Story/Task/Epic: one check per acceptance_criteria. Falls back to a single
    run-and-observe item so there is always at least one check.
    """
    itype = str(analysis.get("issue_type", "")).lower()
    items: list[dict] = []

    if itype in _BUG_TYPES:
        for step in analysis.get("reproduction_steps") or []:
            items.append(_dod_item(f"Reproduce then confirm resolved: {step}", "reproduce"))
        exp = analysis.get("expected_behavior")
        act = analysis.get("actual_behavior")
        if exp or act:
            items.append(_dod_item(
                f"Behavior now matches expected ('{exp or ''}') not actual ('{act or ''}')",
                "reproduce",
            ))
    else:
        for ac in analysis.get("acceptance_criteria") or []:
            items.append(_dod_item(f"Acceptance criterion satisfied: {ac}", "acceptance"))

    if not items:
        summary = analysis.get("problem_summary") or "the reported change"
        items.append(_dod_item(f"Run the affected path and observe: {summary}", "run-and-observe"))
    return items


_SECURITY_TOKENS = {"auth", "token", "jwt", "oauth", "password", "secret", "vulnerab",
                    "injection", "xss", "csrf", "ssrf", "privilege", "encrypt"}
_DB_TOKENS = {"schema", "migration", "query", "sql", "index", "table", "database"}
_API_TOKENS = {"api", "endpoint", "public api", "contract", "route"}
_UI_TOKENS = {"ui", "button", "screen", "layout", "form", "page", "render"}


def _text(analysis: dict) -> str:
    return " ".join(str(analysis.get(k, "")) for k in
                    ("problem_summary", "detailed_description", "impact")).lower()


def compute_risk_tier(analysis: dict, graphs: list[dict] | None = None) -> str:
    """Best-practice default risk tier from available signals. Recommendation only.

    Security-sensitive -> critical. DB/public-API/multi-signal -> high. Single interface signal
    -> medium. Nothing detectable -> DEFAULT_TIER (medium) so the user still gets a sane default.
    """
    if not isinstance(analysis, dict) or not analysis:
        return DEFAULT_TIER
    text = _text(analysis)
    if any(t in text for t in _SECURITY_TOKENS):
        return "critical"
    signals = 0
    if any(t in text for t in _DB_TOKENS):
        signals += 1
    if any(t in text for t in _API_TOKENS):
        signals += 1
    if any(t in text for t in _UI_TOKENS):
        signals += 1
    if str(analysis.get("issue_type", "")).lower() == "epic":
        signals += 1
    if signals >= 2:
        return "high"
    if signals == 1:
        return "medium"
    return DEFAULT_TIER


def recommend_layers(tier: str) -> list[str]:
    return list(DEFAULT_TIER_LAYERS.get(tier, DEFAULT_TIER_LAYERS[DEFAULT_TIER]))


def validate_evidence(items: list[dict]) -> dict:
    """Accept only when every item has a non-empty command AND output AND passed is true."""
    missing: list[str] = []
    if not items:
        return {"accepted": False, "missing": ["no verification items provided"]}
    for i, it in enumerate(items):
        label = str(it.get("check") or f"item {i}")
        if not str(it.get("command", "")).strip():
            missing.append(f"{label}: missing command")
        if not str(it.get("output", "")).strip():
            missing.append(f"{label}: missing output")
        if not bool(it.get("passed", False)):
            missing.append(f"{label}: not passed")
    return {"accepted": not missing, "missing": missing}


def build_confidence_report(items: list[dict], tier: str, layers_run: list[str]) -> dict:
    """Confidence = fraction of DoD items with complete, passing evidence. Plus dimensions and
    remaining risks (recommended layers not yet run)."""
    total = len(items) or 1
    complete = sum(
        1 for it in items
        if str(it.get("command", "")).strip() and str(it.get("output", "")).strip()
        and bool(it.get("passed", False))
    )
    score = round(complete / total, 2)
    recommended = recommend_layers(tier)
    remaining = [l for l in recommended if l not in (layers_run or [])]
    return {
        "confidence_score": score,
        "risk_tier": tier,
        "dimensions": {
            "dod_items_total": len(items),
            "dod_items_passed": complete,
            "layers_run": list(layers_run or []),
            "layers_recommended": recommended,
        },
        "remaining_risks": remaining,
    }
