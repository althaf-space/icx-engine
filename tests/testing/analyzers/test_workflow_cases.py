"""State/workflow cases: duplicate-create rejection + delete-then-verify."""
from __future__ import annotations

from icx_engine.testing.analyzers.workflow_cases import dup_create_steps, delete_verify_steps


def _create_func():
    return {
        "functionality": "Create Team",
        "modalDetails": {"triggerSelector": "#create", "modalSelector": "#modal"},
        "submitButton": {"selectors": ["#save"]},
        "fields": [
            {"label": "Name", "domSelectors": ["#name"]},
            {"label": "Tenant", "domSelectors": ["#tenant"], "interactionPattern": "select"},  # skipped
            {"label": "Logo", "domSelectors": ["#logo"]},
        ],
    }


def test_dup_create_from_validation_matrix():
    model = {"validationMatrix": [{"validationType": "duplicate_check", "errorMessage": "Team already exists"}]}
    steps = dup_create_steps(_create_func(), "#create", "#modal", "#save", "Test 111", model, url="http://x")
    acts = [s["action"] for s in steps]
    assert "click" in acts and steps[-1]["action"] == "assert"
    assert steps[-1]["value"] == "Team already exists"
    # only TEXT fields re-filled (select skipped)
    fills = [s["target"] for s in steps if s["action"] == "fill"]
    assert "#name" in fills and "#logo" in fills and "#tenant" not in fills
    assert all(s["value"] == "Test 111" for s in steps if s["action"] == "fill")


def test_dup_create_from_notification_message():
    func = _create_func()
    func["notifications"] = {"messageSelector": ".toast", "messages": [{"text": "Already exists!", "type": "error"}]}
    steps = dup_create_steps(func, "#create", "#modal", "#save", "T", {}, url="")
    assert steps and steps[-1]["value"] == "Already exists!" and steps[-1]["target"] == ".toast"


def test_dup_create_empty_without_dup_message():
    # no duplicate rule anywhere -> no dup case
    assert dup_create_steps(_create_func(), "#create", "#modal", "#save", "T", {}) == []


def test_delete_verify_search_delete_verify_gone():
    from icx_engine.testing.analyzers.to_flow import _row_scoped
    delf = {"functionality": "Delete", "modalDetails": {"triggerSelector": "[data-testid^='team-del-']"},
            "submitButton": {"selectors": ["#confirmDelete"]}}
    steps = delete_verify_steps(delf, "#search", "Test 999", url="http://x", row_scope=_row_scoped)
    assert steps[0]["action"] == "type" and steps[0]["value"] == "Test 999"      # search for our record
    # the delete trigger is ROW-SCOPED to our tag and SOFT (skip, never touch existing data)
    del_clicks = [s for s in steps if s["action"] == "click" and "delete OUR row" in s.get("description", "")]
    assert del_clicks and 'tr:has-text("Test 999")' in del_clicks[0]["target"] and del_clicks[0].get("soft")
    # confirm is soft too
    assert any(s["action"] == "click" and "confirm" in s.get("description", "") and s.get("soft") for s in steps)
    assert steps[-1]["action"] == "assertgone" and steps[-1]["value"] == "Test 999"


def test_delete_only_acts_on_our_tagged_row():
    # DATA SAFETY: delete must be row-scoped to our unique tag - it can never match a stranger's row.
    from icx_engine.testing.analyzers.to_flow import _row_scoped
    delf = {"functionality": "Delete", "modalDetails": {"triggerSelector": "img[title='Delete']"},
            "submitButton": {"selectors": [".yesButton"]}}
    steps = delete_verify_steps(delf, "#s", "Test 42", url="", row_scope=_row_scoped)
    trig = next(s["target"] for s in steps if "delete OUR row" in s.get("description", ""))
    assert trig == 'tr:has-text("Test 42") img[title=\'Delete\']'   # scoped to our tag's row only


def test_delete_verify_empty_without_trigger_or_search():
    assert delete_verify_steps({"functionality": "Delete"}, "#search", "T") == []
    delf = {"modalDetails": {"triggerSelector": "#del"}}
    assert delete_verify_steps(delf, "", "T") == []
