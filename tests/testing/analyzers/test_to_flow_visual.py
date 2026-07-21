"""Screen baseline screenshot woven into census_to_flow (visual regression, SP5 Task 3)."""
from __future__ import annotations

from icx_engine.testing.analyzers.to_flow import census_to_flow


def test_flow_weaves_a_screen_screenshot():
    m = {"functionalities": [{"id": "F_RENDER", "functionality": "Render", "type": "Render",
         "modalDetails": {"triggerSelector": ".chart"}, "widgets": [{"kind": "chart", "selector": ".chart"}]}]}
    f = census_to_flow(m, "http://x/#/d", test_writes=True)
    shots = [s for s in f if s.get("action") == "screenshot"]
    assert shots, "expected at least one screenshot baseline step"
    assert any(s.get("value") == "screen" for s in shots)
    assert all(s.get("soft") for s in shots)          # soft: first run has no baseline
