"""Census validation + reconciliation gate."""
from __future__ import annotations

import json

from icx_engine.testing.analyzers.schema import validate_census


def _good_ui():
    return {
        "elementCensus": {"counts": {"eventHandlers": 3, "inputSurfaces": 2}},
        "functionalities": [{"id": "FUNC_001", "functionality": "Create"}],
        "coverageReport": {"reconciliation": {
            "eventHandlers": {"total": 3, "mapped": 3, "unmapped": 0},
            "inputSurfaces": {"total": 2, "mapped": 2, "unmapped": 0},
        }},
    }


def test_valid_ui_census_reconciles():
    r = validate_census("ui", _good_ui())
    assert r.ok is True and not r.errors
    assert r.coverage_score == 1.0
    assert r.totals["eventHandlers"] == 3


def test_valid_ui_census_accepts_json_string():
    r = validate_census("ui", json.dumps(_good_ui()))
    assert r.ok is True


def test_reconciliation_mismatch_fails():
    obj = _good_ui()
    obj["coverageReport"]["reconciliation"]["eventHandlers"] = {"total": 3, "mapped": 2, "unmapped": 0}
    r = validate_census("ui", obj)
    assert r.ok is False
    assert any("eventHandlers" in e and "!= total" in e for e in r.errors)


def test_partial_coverage_score():
    obj = _good_ui()
    obj["coverageReport"]["reconciliation"]["eventHandlers"] = {"total": 4, "mapped": 3, "unmapped": 1}
    r = validate_census("ui", obj)
    assert r.ok is True                       # 3+1==4 reconciles
    # 3 mapped of 4 + 2 of 2 = 5/6
    assert 0.8 < r.coverage_score < 0.85


def test_missing_reconciliation_flagged():
    obj = _good_ui()
    del obj["coverageReport"]["reconciliation"]
    r = validate_census("ui", obj)
    assert r.ok is False
    assert any("reconciliation is missing" in e for e in r.errors)


def test_missing_top_level_key_flagged():
    obj = _good_ui()
    del obj["functionalities"]
    r = validate_census("ui", obj)
    assert r.ok is False
    assert any("functionalities" in e for e in r.errors)


def test_backend_family_requires_census_not_functionalities():
    obj = {
        "elementCensus": {"counts": {"apiCallSites": 2}},
        "coverageReport": {"reconciliation": {
            "apiCallSites": {"total": 2, "mapped": 2, "unmapped": 0}}},
    }
    r = validate_census("backend", obj)
    assert r.ok is True


def test_junk_is_not_ok():
    assert validate_census("ui", "not json").ok is False
    assert validate_census("ui", None).ok is False


def test_empty_census_scores_full_coverage():
    # A valid census with genuinely zero elements (nothing to miss) must score 1.0, not 0.0 - else it
    # wrongly drags down DoD confidence for a correct result.
    obj = {
        "elementCensus": {"counts": {"routes": 0}},
        "coverageReport": {"reconciliation": {"routes": {"total": 0, "mapped": 0, "unmapped": 0}}},
    }
    r = validate_census("backend", obj)
    assert r.ok is True and r.coverage_score == 1.0


def test_new_families_validate():
    obj = {"elementCensus": {"counts": {}}, "coverageReport": {"reconciliation": {
        "x": {"total": 1, "mapped": 1, "unmapped": 0}}}}
    assert validate_census("grpc", obj).ok is True
    assert validate_census("iac", obj).ok is True
