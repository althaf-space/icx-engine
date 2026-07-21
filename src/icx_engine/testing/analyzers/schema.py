"""Validate an analyzer's census JSON and run the reconciliation (nothing-missed) gate.

The analyzer prompts all emit `elementCensus.counts` + `coverageReport.reconciliation`, where each
category's `total == mapped + unmapped`. That arithmetic is what makes "nothing missed" verifiable:
if totals do not reconcile, the census was cut short or an element was dropped silently. This module
does NOT judge test quality - only structural presence and count consistency.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

# Top-level keys each family's output must carry. Kept minimal - the reconciliation gate is the real
# check; these just catch a wrong-shaped or wrong-family payload early.
_REQUIRED_TOP: dict[str, tuple[str, ...]] = {
    "ui":      ("elementCensus", "functionalities", "coverageReport"),
    "backend": ("elementCensus", "coverageReport"),
    "cpp":     ("elementCensus", "coverageReport"),
    "sql":     ("elementCensus", "coverageReport"),
    "grpc":    ("elementCensus", "coverageReport"),     # endpoint-shaped, not HTTP
    "iac":     ("elementCensus", "coverageReport"),     # Terraform testableUnits schema
}


@dataclass
class CensusReport:
    ok: bool
    family: str
    errors: list[str] = field(default_factory=list)
    reconciliation: dict[str, dict[str, int]] = field(default_factory=dict)  # category -> {total,mapped,unmapped}
    coverage_score: float = 0.0        # mapped / total across all categories (1.0 = fully reconciled)
    totals: dict[str, int] = field(default_factory=dict)


def _as_obj(spec: object) -> dict | None:
    if isinstance(spec, dict):
        return spec
    if isinstance(spec, str):
        try:
            v = json.loads(spec)
            return v if isinstance(v, dict) else None
        except ValueError:
            return None
    return None


def validate_census(family: str, spec: object) -> CensusReport:
    """Structural + reconciliation check for a census payload. Never raises."""
    fam = family if family in _REQUIRED_TOP else "ui"
    obj = _as_obj(spec)
    if obj is None:
        return CensusReport(ok=False, family=fam, errors=["census is not valid JSON object"])

    errors: list[str] = []
    for key in _REQUIRED_TOP[fam]:
        if key not in obj or obj.get(key) in (None, "", [], {}):
            errors.append(f"missing/empty top-level key: {key}")

    recon_out: dict[str, dict[str, int]] = {}
    total_all = mapped_all = 0
    recon = (((obj.get("coverageReport") or {}) if isinstance(obj.get("coverageReport"), dict) else {})
             .get("reconciliation") or {})
    if not isinstance(recon, dict) or not recon:
        errors.append("coverageReport.reconciliation is missing - cannot verify completeness")
    else:
        for category, row in recon.items():
            if not isinstance(row, dict):
                errors.append(f"reconciliation.{category} is not an object")
                continue
            total = _int(row.get("total"))
            mapped = _int(row.get("mapped"))
            unmapped = _int(row.get("unmapped"))
            recon_out[category] = {"total": total, "mapped": mapped, "unmapped": unmapped}
            if mapped + unmapped != total:
                errors.append(
                    f"reconciliation.{category}: mapped({mapped}) + unmapped({unmapped}) "
                    f"!= total({total}) - an element was dropped or the census was cut short")
            total_all += total
            mapped_all += mapped

    # Nothing to miss == fully covered. A valid census with zero elements in every category (e.g. a
    # pure utility module) must score 1.0, not 0.0 - else it wrongly drags down the DoD confidence.
    coverage = (mapped_all / total_all) if total_all > 0 else 1.0
    counts = obj.get("elementCensus", {})
    counts = counts.get("counts", {}) if isinstance(counts, dict) else {}
    totals = {k: _int(v) for k, v in counts.items()} if isinstance(counts, dict) else {}

    return CensusReport(
        ok=not errors,
        family=fam,
        errors=errors,
        reconciliation=recon_out,
        coverage_score=round(coverage, 4),
        totals=totals,
    )


def _int(v: object) -> int:
    try:
        return int(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0
