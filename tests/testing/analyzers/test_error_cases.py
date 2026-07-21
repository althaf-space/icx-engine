"""Error-handling (network-fault) case generation."""
from __future__ import annotations

from icx_engine.testing.analyzers.error_cases import error_steps


def test_error_steps_route_trigger_assert_unroute():
    func = {"functionality": "Refresh", "apiIntegration": {"endpoint": "/api/teams"},
            "notifications": {"messageSelector": ".toast-error",
                              "messages": [{"type": "error", "text": "Failed to load"}]}}
    steps = error_steps(func, "#refresh", ".app", url="http://x")
    acts = [s["action"] for s in steps]
    # route -> goto(reload triggers faulted fetch) -> assert(error msg) -> unroute -> goto(reset) -> waitfor
    assert acts == ["route", "goto", "assert", "unroute", "goto", "waitfor"]
    assert steps[0]["value"] == "500" and steps[0]["target"] == "/api/teams"
    assert steps[2]["value"] == "Failed to load" and steps[2]["target"] == ".toast-error"
    assert steps[3]["action"] == "unroute" and steps[3]["target"] == "/api/teams"
    assert steps[-1]["target"] == ".app"          # list recovered on the anchor


def test_error_steps_no_declared_toast_falls_back_to_no_crash():
    func = {"functionality": "Refresh", "apiIntegration": {"endpoint": "/api/x"}}
    steps = error_steps(func, "#r", ".shell")   # no url -> no reset steps
    acts = [s["action"] for s in steps]
    # route -> waitfor(trigger) -> click -> waitfor(app still up) -> unroute
    assert acts == ["route", "waitfor", "click", "waitfor", "unroute"]
    assert steps[3]["target"] == ".shell"


def test_error_steps_empty_without_endpoint():
    assert error_steps({"functionality": "Refresh"}, "#r", ".a") == []
    assert error_steps({"apiIntegration": {"endpoint": "/x"}}, "", ".a") == []
