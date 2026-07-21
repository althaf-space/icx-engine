"""Error-handling test cases: make the backend fail and assert the UI stays graceful.

Woven into the UI flow by `to_flow`. For a functionality that calls an API (census
`apiIntegration.endpoint`), ICX injects a network fault (HTTP 500) on that endpoint, triggers the
action, and asserts the app did NOT crash - its shell/anchor is still present, and (when the census
declares an error notification) that the error message is shown. Then it clears the fault so the rest
of the flow runs normally.

A passing error case = the screen degrades gracefully instead of white-screening on a backend error.
Pure; returns [] when there is no API endpoint to fault.
"""
from __future__ import annotations


def _s(v) -> str:
    return str(v).strip() if v is not None else ""


def endpoint_of(func: dict) -> str:
    """The API endpoint a functionality calls, from the census. "" when it declares none."""
    if not isinstance(func, dict):
        return ""
    api = func.get("apiIntegration")
    if isinstance(api, dict) and _s(api.get("endpoint")):
        return _s(api["endpoint"])
    # some census shapes carry it under responseHandling / a bare "endpoint"
    if _s(func.get("endpoint")):
        return _s(func["endpoint"])
    return ""


def _error_toast(func: dict) -> tuple[str, str]:
    nt = func.get("notifications") if isinstance(func.get("notifications"), dict) else {}
    for m in (nt.get("messages") or []):
        if isinstance(m, dict) and _s(m.get("type")) == "error" and _s(m.get("text")):
            sel = ""
            for c in (nt.get("messageSelector"), nt.get("selectors"), nt.get("containerSelector")):
                if isinstance(c, str) and c.strip():
                    sel = c.strip(); break
                if isinstance(c, (list, tuple)):
                    for x in c:
                        if isinstance(x, str) and x.strip():
                            sel = x.strip(); break
                if sel:
                    break
            return sel or "body", _s(m.get("text"))
    return "", ""


def error_steps(func: dict, trigger_selector: str, anchor: str, url: str = "") -> list[dict]:
    """Network-fault case for one functionality: route its endpoint to 500, trigger it, assert the app
    stays up (anchor visible) + error message if declared, then clear the fault. [] if no endpoint."""
    endpoint = endpoint_of(func)
    if not endpoint or not trigger_selector:
        return []
    steps: list[dict] = [
        {"action": "route", "target": endpoint, "value": "500",
         "description": f"ERROR-HANDLING: force 500 on {endpoint}"},
    ]
    # Trigger the faulted fetch. Prefer a page RELOAD (goto) - it re-fetches the list through the
    # faulted route deterministically, without depending on a refresh button that a loading overlay
    # may cover. Fall back to clicking the trigger when no url is available.
    if url:
        steps.append({"action": "goto", "target": url, "value": "",
                      "description": "ERROR-HANDLING: reload -> list re-fetches under backend failure"})
    else:
        steps.append({"action": "waitfor", "target": trigger_selector, "value": "",
                      "description": "ERROR-HANDLING: trigger ready"})
        steps.append({"action": "click", "target": trigger_selector, "value": "",
                      "description": "ERROR-HANDLING: trigger the action under backend failure"})
    esel, etext = _error_toast(func)
    if esel and etext:
        steps.append({"action": "assert", "target": esel, "value": etext,
                      "description": f"ERROR-HANDLING: error message shown ('{etext[:32]}')"})
    else:
        # no declared error toast -> the minimum bar is "did not crash": the app shell is still there.
        steps.append({"action": "waitfor", "target": anchor or "body", "value": "",
                      "description": "ERROR-HANDLING: app stays up (no white-screen) under failure"})
    steps.append({"action": "unroute", "target": endpoint, "value": "",
                  "description": "ERROR-HANDLING: clear the injected fault"})
    # RESTORE: the faulted request left the list empty/errored; unroute alone does not refetch. Reload
    # a clean page so later steps (view/edit that need real rows) are not starved by our own fault.
    if url:
        steps.append({"action": "goto", "target": url, "value": "",
                      "description": "ERROR-HANDLING: reload clean page after fault"})
        steps.append({"action": "waitfor", "target": anchor or "body", "value": "",
                      "description": "ERROR-HANDLING: list recovered"})
    return steps
