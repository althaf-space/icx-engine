from icx_engine.verification import build_dod_checklist


def test_bug_checklist_from_repro_and_behavior():
    analysis = {
        "issue_type": "Bug",
        "problem_summary": "login 500s",
        "reproduction_steps": ["POST /login with empty body"],
        "expected_behavior": "returns 400",
        "actual_behavior": "returns 500",
        "acceptance_criteria": [],
    }
    items = build_dod_checklist(analysis)
    assert any("reproduce" in i["check"].lower() for i in items)
    assert any(i["method"] == "reproduce" for i in items)
    assert all(i["passed"] is False and i["command"] == "" and i["output"] == "" for i in items)


def test_story_checklist_from_acceptance_criteria():
    analysis = {
        "issue_type": "Story",
        "problem_summary": "add export button",
        "acceptance_criteria": ["Export button visible", "Clicking downloads CSV"],
        "reproduction_steps": [],
    }
    items = build_dod_checklist(analysis)
    assert len(items) >= 2
    assert all(i["method"] == "acceptance" for i in items)


def test_empty_analysis_yields_run_and_observe_default():
    items = build_dod_checklist({"issue_type": "Task"})
    assert len(items) >= 1
    assert items[0]["method"] == "run-and-observe"


from icx_engine.verification import (
    compute_risk_tier, recommend_layers, validate_evidence, build_confidence_report,
    DEFAULT_TIER, DEFAULT_TIER_LAYERS,
)


def test_risk_tier_security_keywords_go_critical():
    a = {"problem_summary": "fix auth token bypass", "detailed_description": "", "impact": ""}
    assert compute_risk_tier(a) == "critical"


def test_risk_tier_default_when_no_signal():
    assert compute_risk_tier({}) == DEFAULT_TIER


def test_recommend_layers_matches_table():
    assert recommend_layers("high") == DEFAULT_TIER_LAYERS["high"]
    assert recommend_layers("nonsense") == DEFAULT_TIER_LAYERS[DEFAULT_TIER]  # default fallback


def test_validate_evidence_rejects_incomplete():
    items = [{"check": "c", "method": "unit", "passed": True, "command": "", "output": ""}]
    r = validate_evidence(items)
    assert r["accepted"] is False
    assert r["missing"]


def test_validate_evidence_accepts_complete():
    items = [{"check": "c", "method": "unit", "passed": True,
              "command": "pytest -k x", "output": "1 passed"}]
    assert validate_evidence(items)["accepted"] is True


def test_confidence_report_shape():
    items = [{"check": "c", "method": "unit", "passed": True,
              "command": "pytest", "output": "ok"}]
    rep = build_confidence_report(items, "medium", ["unit", "api"])
    assert 0.0 <= rep["confidence_score"] <= 1.0
    assert "dimensions" in rep and "remaining_risks" in rep
