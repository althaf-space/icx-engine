"""Tests for the mandatory ICX agent methodology (pure module + MCP wiring)."""
from __future__ import annotations

from icx_engine.methodology import build_checklist, full_text, ONE_PAGER, METHODOLOGY_VERSION


def test_build_checklist_shape_and_mandatory():
    c = build_checklist({"issue_type": "Bug", "problem_summary": "crash on empty cart"})
    assert c["mandatory"] is True
    assert c["archetype"] == "debugging"
    assert c["intake_checklist"] and c["verification_battery"] and c["gate_sequence"]
    assert "lock_plan" in " ".join(c["gate_sequence"])
    assert c["one_pager"] == ONE_PAGER
    assert isinstance(c["archetype_discipline"], str) and c["archetype_pitfalls"]
    assert c["failure_modes_to_avoid"] and c["hallucination_high_risk_zones"]
    assert any("symptom" in m for m in c["failure_modes_to_avoid"])


def test_build_checklist_classifies_archetypes():
    assert build_checklist({"problem_summary": "design a scalable service"})["archetype"] == "design"
    assert build_checklist({"problem_summary": "query is slow, high latency"})["archetype"] == "performance"
    assert build_checklist({"problem_summary": "add jwt auth token check"})["archetype"] == "security"
    assert build_checklist({"problem_summary": "add a new export button"})["archetype"] == "coding"


def test_build_checklist_guarded_on_junk():
    assert build_checklist(None)["mandatory"] is True
    assert build_checklist("not a dict")["archetype"] == "coding"


def test_full_text_ascii_and_complete():
    t = full_text()
    assert all(ord(ch) < 128 for ch in t)                 # ASCII-only
    for token in ("INTAKE", "VERIFY", "Archetypes", "invariants", "Confidence",
                  "Failure modes", "high-risk"):
        assert token in t


def test_one_pager_ascii():
    assert all(ord(ch) < 128 for ch in ONE_PAGER)


# -- MCP wiring --------------------------------------------------------------------

async def test_get_methodology_tool_returns_framework():
    from icx_engine.mcp_server import _call_tool
    import json
    r = await _call_tool("get_methodology", {})
    p = json.loads(r[0].text)
    assert p["version"] == METHODOLOGY_VERSION
    assert "INTAKE" in p["methodology"]


async def test_get_methodology_registered_before_memory_and_testing():
    from icx_engine.mcp_server import _list_tools
    from icx_engine.models.config import AppConfig
    from unittest.mock import patch
    with patch("icx_engine.mcp_server.ConfigManager") as mock_cm:
        mock_cm.load.return_value = AppConfig()
        names = [t.name for t in await _list_tools()]
    assert "get_methodology" in names
    assert names.index("get_methodology") < names.index("start_testing_session")


def test_apply_methodology_injects_one_pager_and_checklist():
    from icx_engine.mcp_server import _apply_methodology
    instr, info = _apply_methodology({"issue_type": "Bug", "problem_summary": "x"}, "BASE-INSTRUCTION")
    assert "BASE-INSTRUCTION" in instr
    assert "MANDATORY METHODOLOGY" in instr and "INTAKE" in instr
    assert info is not None and info["mandatory"] is True and info["archetype"] == "debugging"
