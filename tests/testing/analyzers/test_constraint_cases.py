"""Constraint-driven field tests (submit-free, Constraint Validation API)."""
from __future__ import annotations

from icx_engine.testing.analyzers.constraint_cases import constraint_steps, has_constraints, _format_of


def _acts(field):
    return [s["action"] for s in constraint_steps(field)]


def test_maxlength_value_cap():
    steps = constraint_steps({"label": "Code", "domSelectors": ["#c"], "validations": {"maxLength": 8}})
    assert _acts({"label": "Code", "domSelectors": ["#c"], "validations": {"maxLength": 8}}) == ["fill", "assertjs"]
    fill = steps[0]
    assert len(fill["value"]) == 9                      # over maxLength
    assert "value.length <= 8" in steps[1]["target"]


def test_phone_msisdn_inferred_from_label():
    assert _format_of({"label": "MSISDN"}) == "phone"
    assert _format_of({"label": "Mobile Number"}) == "phone"
    assert _format_of({"label": "Contact number"}) == "phone"
    steps = constraint_steps({"label": "MSISDN", "domSelectors": ["#m"]})
    assert steps and steps[0]["value"] == "abcXYZ!!"    # non-phone value
    # native-guarded: passes when the input has no native constraint (can't verify != false-fail)
    assert "checkValidity()" in steps[1]["target"] and "native" in steps[1]["target"]


def test_email_format_check():
    steps = constraint_steps({"label": "Email", "domSelectors": ["#e"], "type": "email"})
    assert steps[0]["value"] == "not-an-email"
    assert "email format" in steps[1]["description"]


def test_numeric_range_checks():
    steps = constraint_steps({"label": "Age", "domSelectors": ["#a"],
                              "validations": {"min": 0, "max": 120}})
    descs = " ".join(s["description"] for s in steps)
    assert "max value 120" in descs and "min value 0" in descs
    # out-of-range fills
    vals = [s["value"] for s in steps if s["action"] == "fill"]
    assert "121" in vals and "-1" in vals


def test_explicit_pattern_check():
    steps = constraint_steps({"label": "Code", "domSelectors": ["#c"],
                              "validations": {"pattern": "^[A-Z]{3}$"}})
    assert any("its pattern" in s["description"] for s in steps)


def test_minlength_check():
    steps = constraint_steps({"label": "PIN", "domSelectors": ["#p"], "validations": {"minLength": 4}})
    assert steps[0]["value"] == "A"                     # too short
    assert "minLength 4" in steps[1]["description"]


def test_no_constraint_no_steps():
    assert constraint_steps({"label": "Notes", "domSelectors": ["#n"]}) == []
    assert not has_constraints({"label": "Notes", "domSelectors": ["#n"]})
    assert constraint_steps({"label": "x", "validations": {"maxLength": 5}}) == []   # no selector


def test_checkvalidity_guarded_against_missing_element_and_non_native():
    # the assertjs expression must not throw if the element is absent, and must NOT false-fail on a
    # plain text field with no native constraint (returns true when !native).
    steps = constraint_steps({"label": "Email", "domSelectors": ["#e"], "type": "email"})
    expr = steps[1]["target"]
    assert "if(!el)return true" in expr        # missing element -> pass
    assert "!native ||" in expr                # no native constraint -> pass (no false positive)
