"""Merge of a discovered census (live selectors/structure) with a source census (JS-hidden constraints)."""
from __future__ import annotations

from icx_engine.testing.analyzers.census_merge import merge_census


def _disc():
    # discovery: real selectors + control kinds, but NO maxLength on synonym (JS-validated, DOM-invisible)
    return {"screenName": "Teams", "functionalitySummaryTable": [{"id": "F_CREATE", "type": "Create"}],
            "functionalities": [
                {"id": "F_CREATE", "functionality": "Create", "modalDetails": {"triggerSelector": "#c"},
                 "submitButton": {"selectors": ["input[value='SAVE']"]},
                 "fields": [
                     {"label": "Team Name EN", "domSelectors": ["[data-testid='team-name-EN']"], "type": "text"},
                     {"label": "Synonym EN", "domSelectors": ["[data-testid='team-synonym-EN']"], "type": "text"}]}]}


def _source():
    # source (agent read code): the synonym maxLength 3 the DOM never exposed, + a download it saw
    return {"functionalities": [
        {"id": "C", "functionality": "Create", "submitButton": {"selectors": ["#save"]},
         "fields": [
             {"label": "Team Name EN", "domSelectors": ["[data-testid='team-name-EN']"], "validations": {"maxLength": 20}},
             {"label": "Synonym EN", "domSelectors": ["[data-testid='team-synonym-EN']"], "validations": {"maxLength": 3}}]},
        {"id": "D", "functionality": "Export CSV", "modalDetails": {"triggerSelector": "#export"}}]}


def test_merge_layers_source_constraints_onto_discovery():
    m = merge_census(_disc(), _source())
    fields = m["functionalities"][0]["fields"]
    syn = next(f for f in fields if "synonym" in f["label"].lower())
    # discovery kept its live selector; source's JS-hidden maxLength was layered in
    assert syn["domSelectors"] == ["[data-testid='team-synonym-EN']"]
    assert syn["validations"]["maxLength"] == 3
    name = next(f for f in fields if f["label"] == "Team Name EN")
    assert name["validations"]["maxLength"] == 20


def test_merge_keeps_discovery_selectors_and_submit():
    m = merge_census(_disc(), _source())
    # discovery's live submit wins (not source's #save)
    assert m["functionalities"][0]["submitButton"]["selectors"] == ["input[value='SAVE']"]


def test_merge_appends_source_only_functionality():
    m = merge_census(_disc(), _source())
    kinds = [f["functionality"] for f in m["functionalities"]]
    assert any("Export" in k for k in kinds)          # the download discovery missed, from source


def test_merge_matches_by_label_when_selector_differs():
    disc = {"functionalities": [{"id": "F_CREATE", "functionality": "Create",
            "submitButton": {"selectors": ["#s"]},
            "fields": [{"label": "Mobile Number", "domSelectors": ["#mob"], "type": "text"}]}]}
    src = {"functionalities": [{"id": "C", "functionality": "Create",
           "fields": [{"label": "Mobile Number", "domSelectors": ["#different"], "type": "tel", "validations": {"maxLength": 10}}]}]}
    m = merge_census(disc, src)
    f = m["functionalities"][0]["fields"][0]
    assert f["domSelectors"] == ["#mob"]              # discovery selector kept
    assert f["type"] == "tel" and f["validations"]["maxLength"] == 10   # source semantics layered


def test_merge_appends_source_fields_discovery_missed():
    # discovery captured only Name; source has 2 react-selects discovery could not read -> merge adds them.
    disc = {"functionalities": [{"id": "F_CREATE", "functionality": "Create",
            "submitButton": {"selectors": ["#save"]},
            "fields": [{"label": "Name", "domSelectors": ["#name"], "type": "text"}]}]}
    src = {"functionalities": [{"id": "C", "functionality": "Create",
           "fields": [{"label": "Name", "domSelectors": ["#name"]},
                      {"label": "Segment", "domSelectors": ["#seg"], "interactionPattern": "react-select"},
                      {"label": "Attribute", "domSelectors": ["#attr"], "interactionPattern": "react-select"}]}]}
    m = merge_census(disc, src)
    fields = m["functionalities"][0]["fields"]
    labels = [f["label"] for f in fields]
    assert labels == ["Name", "Segment", "Attribute"]        # the 2 missing react-selects added
    assert all(f.get("interactionPattern") == "react-select" for f in fields if f["label"] in ("Segment", "Attribute"))


def test_merge_degrades_to_whichever_exists():
    assert merge_census(_disc(), None)["functionalities"]        # source missing -> discovery
    assert merge_census(None, _source())["functionalities"]      # discovery missing -> source
    assert merge_census({}, {}) == {}
