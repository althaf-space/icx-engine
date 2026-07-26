"""Census linter - enforces census quality independent of the producing agent."""
from __future__ import annotations

from icx_engine.testing.analyzers.census_lint import lint_ui_census


def _base():
    return {"functionalities": [
        {"id": "S", "functionality": "Search", "modalDetails": {"triggerSelector": "#q"}},
        {"id": "C", "functionality": "Create Team", "modalDetails": {"triggerSelector": "#c", "modalSelector": "#m"},
         "submitButton": {"selectors": ["#save"]},
         "fields": [{"label": "Name", "domSelectors": ["#name"], "validations": {"maxLength": 20}}]},
        {"id": "E", "functionality": "Edit Team", "modalDetails": {"triggerSelector": "[data-testid^='edit-']", "modalSelector": "#m"},
         "submitButton": {"selectors": ["#update"]},
         "fields": [{"label": "Name", "domSelectors": ["#name"], "validations": {"maxLength": 20}}]},
    ]}


def test_clean_census_passes():
    r = lint_ui_census(_base())
    assert r.ok and not r.hard


def test_create_edit_sharing_submit_is_hard_defect():
    # THE bug: edit reused create's submit selector.
    m = _base()
    m["functionalities"][2]["submitButton"] = {"selectors": ["#save"]}   # edit copies create's #save
    r = lint_ui_census(m)
    assert not r.ok
    assert any("share the SAME submit selector" in h for h in r.hard)


def test_create_edit_sharing_a_wizard_next_button_is_not_a_defect():
    # REGRESSION (live false positive): submitButtons[] holds PER-STEP wizard buttons (label/step/
    # selectors, per the analyzer prompt schema) - NOT alternate final-submit selectors. It is not
    # consumed by flow generation (the real terminal action always uses the singular submitButton) and its
    # per-step entries are commonly IDENTICAL step-navigation markup across create/edit (the same
    # NEXT button UI) - that is normal, not a copy-paste defect. The cross-mode duplicate check must
    # only compare the singular submitButton (the true terminal action), which is correctly distinct
    # here (#save vs #update).
    m = _base()
    same_next = [{"label": "Next", "step": 1, "selectors": [".modal .tab-pane.active input[value='NEXT']"]}]
    m["functionalities"][1]["submitButtons"] = same_next
    m["functionalities"][2]["submitButtons"] = same_next   # identical NEXT selector - fine
    r = lint_ui_census(m)
    assert r.ok and not r.hard


def test_create_with_fields_no_submit_is_hard():
    m = _base()
    del m["functionalities"][1]["submitButton"]
    r = lint_ui_census(m)
    assert not r.ok
    assert any("no submitButton" in h for h in r.hard)


def test_missing_trigger_is_hard():
    m = _base()
    m["functionalities"][2]["modalDetails"] = {"modalSelector": "#m"}   # edit lost its trigger
    r = lint_ui_census(m)
    assert not r.ok
    assert any("no triggerSelector" in h for h in r.hard)


def test_field_without_selector_is_hard():
    m = _base()
    m["functionalities"][1]["fields"].append({"label": "Ghost"})       # no domSelectors
    r = lint_ui_census(m)
    assert not r.ok
    assert any("no domSelectors/selector" in h for h in r.hard)


def test_duplicate_ids_is_hard():
    m = _base()
    m["functionalities"][2]["id"] = "C"                                # dup of create's id
    r = lint_ui_census(m)
    assert not r.ok
    assert any("duplicate functionality id" in h for h in r.hard)


def test_text_field_without_constraint_is_soft():
    m = _base()
    m["functionalities"][1]["fields"] = [{"label": "Notes", "domSelectors": ["#n"]}]   # no length/format
    r = lint_ui_census(m)
    assert r.ok                                                         # advisory, not blocking
    assert any("no length/format constraint" in s for s in r.soft)


def test_create_without_search_is_soft():
    m = {"functionalities": [
        {"id": "C", "functionality": "Create", "modalDetails": {"triggerSelector": "#c"},
         "submitButton": {"selectors": ["#save"]}, "fields": [{"label": "N", "domSelectors": ["#n"], "type": "email"}]}]}
    r = lint_ui_census(m)
    assert r.ok
    assert any("no search functionality" in s for s in r.soft)


def test_create_with_no_fields_or_steps_is_hard():
    m = {"functionalities": [
        {"id": "S", "functionality": "Search", "modalDetails": {"triggerSelector": "#q"}},
        {"id": "C", "functionality": "Create", "modalDetails": {"triggerSelector": "#c"},
         "submitButton": {"selectors": ["#save"]}}]}   # no fields, no steps
    r = lint_ui_census(m)
    assert not r.ok
    assert any("neither fields nor steps" in h for h in r.hard)


def test_form_with_both_fields_and_steps_is_hard():
    m = {"functionalities": [
        {"id": "C", "functionality": "Create", "modalDetails": {"triggerSelector": "#c"},
         "submitButton": {"selectors": ["#save"]},
         "fields": [{"label": "N", "domSelectors": ["#n"]}],
         "steps": [{"name": "s1", "fields": [{"label": "M", "domSelectors": ["#m"]}]}]}]}
    r = lint_ui_census(m)
    assert not r.ok
    assert any("BOTH fields and steps" in h for h in r.hard)


def test_wizard_step_missing_next_is_hard():
    m = {"functionalities": [
        {"id": "C", "functionality": "Create", "modalDetails": {"triggerSelector": "#c"},
         "submitButton": {"selectors": ["#save"]},
         "steps": [
             {"name": "s1", "fields": [{"label": "A", "domSelectors": ["#a"]}]},   # not last, no next
             {"name": "s2", "fields": [{"label": "B", "domSelectors": ["#b"]}]}]}]}
    r = lint_ui_census(m)
    assert not r.ok
    assert any("no nextButton" in h for h in r.hard)


def test_wizard_step_field_missing_selector_is_hard():
    m = {"functionalities": [
        {"id": "C", "functionality": "Create", "modalDetails": {"triggerSelector": "#c"},
         "submitButton": {"selectors": ["#save"]},
         "steps": [{"name": "s1", "fields": [{"label": "Ghost"}]}]}]}   # step field no selector
    r = lint_ui_census(m)
    assert not r.ok
    assert any("wizard step 1 field" in h for h in r.hard)


def test_valid_wizard_passes():
    m = {"functionalities": [
        {"id": "S", "functionality": "Search", "modalDetails": {"triggerSelector": "#q"}},
        {"id": "C", "functionality": "Create", "modalDetails": {"triggerSelector": "#c"},
         "submitButton": {"selectors": ["#save"]},
         "steps": [
             {"name": "s1", "nextButton": {"selectors": ["#next"]}, "fields": [{"label": "A", "domSelectors": ["#a"], "type": "email"}]},
             {"name": "s2", "fields": [{"label": "B", "domSelectors": ["#b"], "type": "email"}]}]}]}
    r = lint_ui_census(m)
    assert r.ok


def test_download_needs_trigger():
    m = {"functionalities": [{"id": "D", "functionality": "Export CSV"}]}   # download, no trigger
    r = lint_ui_census(m)
    assert not r.ok
    assert any("no triggerSelector" in h for h in r.hard)


def test_empty_census_is_hard():
    assert not lint_ui_census({}).ok
    assert not lint_ui_census({"functionalities": []}).ok
    assert not lint_ui_census(None).ok
