"""Two-pass boost refinement -> CTO-grade prompt (deterministic assembly from the agent's structured spec)."""
from __future__ import annotations

from icx_engine.boost.refine import (
    merge_dims, compose_cto_prompt, compose_refined_prompt, parse_agent_dims)


def test_base_dims_always_present():
    merged = merge_dims("security", [])
    assert any("authoriz" in d.lower() or "authentication" in d.lower() for d in merged)


def test_agent_novel_dim_appended():
    merged = merge_dims("coding", ["CSRF token rotation on every request"])
    assert any("csrf token rotation" in d.lower() for d in merged)


def test_agent_duplicate_dim_deduped():
    base = merge_dims("coding", [])
    merged = merge_dims("coding", ["input validation"])
    assert len([d for d in merged if "validation" in d.lower()]) == \
        len([d for d in base if "validation" in d.lower()])


def test_merge_is_bounded():
    merged = merge_dims("coding", [f"extra dimension number {i}" for i in range(50)])
    assert len(merged) <= 12


def test_parse_agent_dims_handles_bullets_and_numbers():
    text = "1. rate limit the endpoint\n- validate the payload\n* sanitize file names\nplain line"
    dims = parse_agent_dims(text)
    assert "rate limit the endpoint" in dims and "validate the payload" in dims
    assert "sanitize file names" in dims and "plain line" in dims


# -- CTO-grade prompt ---------------------------------------------------------

def test_cto_prompt_has_all_sections():
    spec = {"objective": "Build a secure user login", "requirements": ["hash passwords"],
            "constraints": ["Python 3.12, FastAPI"], "deliverable": "Working endpoint + tests",
            "acceptance": ["all auth paths covered by tests"], "dims": ["account lockout"]}
    p = compose_cto_prompt("add login", "security", spec, {"files": []})
    for section in ("# ROLE", "# OBJECTIVE", "# REQUIREMENTS", "# CONSTRAINTS",
                    "# DELIVERABLE", "# ACCEPTANCE CRITERIA", "# APPROACH", "# STANDARDS"):
        assert section in p
    # senior rubric (matches analyze): blast radius, rollback, and the confidence gate
    low = p.lower()
    assert "blast radius" in low and "rollback" in low and "clarifying question" in low
    assert "two approaches" in low or "at least two" in low
    assert 'Original request (verbatim, for reference): "add login"' in p


def test_cto_persona_is_per_problem():
    # a security request -> security-architect persona; a UI request -> UI/UX persona (never hardcoded)
    sec = compose_cto_prompt("add jwt login auth", "security", {"objective": "secure auth"}, {})
    ui = compose_cto_prompt("fix the button css layout", "coding",
                            {"objective": "fix the modal layout and styling"}, {})
    assert "security architect" in sec.lower()
    assert "ui/ux" in ui.lower() or "frontend" in ui.lower()


def test_cto_fills_gaps_when_agent_gives_little():
    # only an objective -> ICX still fills persona, requirements (base dims), deliverable, acceptance
    p = compose_cto_prompt("add a cache", "performance", {"objective": "add a caching layer"}, {})
    assert "# ROLE" in p and "# DELIVERABLE" in p
    assert "# REQUIREMENTS" in p
    assert "invalidat" in p.lower() or "ttl" in p.lower()   # base performance dims present


def test_cto_merges_agent_requirements_with_base():
    p = compose_cto_prompt("build api", "coding",
                           {"objective": "build a users API", "requirements": ["OpenAPI docs"]}, {})
    assert "openapi docs" in p.lower()
    assert "validation" in p.lower()   # base coding dim


def test_cto_deterministic():
    spec = {"objective": "x", "dims": ["y dimension"]}
    assert compose_cto_prompt("x", "coding", spec, {}) == compose_cto_prompt("x", "coding", spec, {})


def test_backcompat_refined_prompt_still_works():
    p = compose_refined_prompt("add a login endpoint", "security", ["account lockout"])
    assert p.startswith("add a login endpoint")
    assert "do NOT skip" in p and "account lockout" in p.lower()
