"""SP7 dataflow weave: a soft DB verify after a create, and a soft graceful-under-slow-network check."""
from __future__ import annotations

from icx_engine.testing.analyzers.to_flow import census_to_flow


def _crud():
    return {"functionalitySummaryTable": [{"id": "S", "type": "Search"}, {"id": "C", "type": "Create"}],
            "functionalities": [
                {"id": "S", "functionality": "Search", "modalDetails": {"triggerSelector": "#search"}},
                {"id": "C", "functionality": "Create",
                 "modalDetails": {"triggerSelector": "#c", "modalSelector": "#m"},
                 "submitButton": {"selectors": ["#save"]}, "fields": [{"label": "Name", "domSelectors": ["#n"]}]}]}


def test_flow_weaves_dbverify_after_create():
    f = census_to_flow(_crud(), "http://x", test_writes=True)
    db = [s for s in f if s.get("action") == "dbverify"]
    # NOT soft: an unset ICX_SQL_VERIFY_CMD skips inside the action, but a configured-yet-absent record
    # must fail loud (a real data-integrity bug), not be swallowed.
    assert db and not any(s.get("soft") for s in db)
    assert any("DATAFLOW" in s.get("description", "") for s in db)


def test_flow_weaves_net_graceful():
    f = census_to_flow(_crud(), "http://x", test_writes=True)
    net = [s for s in f if s.get("action") == "netprofile"]
    assert any(s.get("value") == "slow" for s in net) and any(s.get("value") == "reset" for s in net)
    assert any("graceful under slow network" in s.get("description", "").lower() for s in f)
