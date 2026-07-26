"""Methodology generalized to any prompt (not just a Jira analysis dict)."""
from __future__ import annotations

from icx_engine import methodology as m


def test_classify_text_archetypes():
    assert m.classify_text("the login endpoint crashes with a 500") == "debugging"
    assert m.classify_text("design a caching layer for the API") == "design"
    assert m.classify_text("the dashboard query is very slow") == "performance"
    assert m.classify_text("is there a JWT injection risk here") == "security"
    assert m.classify_text("add a create-user form") == "coding"


def test_doubt_archetype_present():
    assert "doubt" in m._ARCHETYPES


def test_testing_archetype_present():
    assert "testing" in m._ARCHETYPES


def test_classify_text_e2e_testing_request_is_testing_not_performance():
    # REGRESSION: "slowmo" (a real Playwright term) used to prefix-collide with the bare "slow"
    # performance token ("slowmo".startswith("slow")), misclassifying every testing request that
    # mentions slow-motion replay as "performance".
    prompt = ("I need to do e2e testing for this screen, cover every case, and tell me which "
              "cases fail. Run it in slowmo so I can watch.")
    assert m.classify_text(prompt) == "testing"


def test_classify_text_slowmo_alone_does_not_trigger_performance():
    assert m.classify_text("run the test in slowmo") != "performance"


def test_classify_text_real_performance_complaint_still_classifies():
    assert m.classify_text("the dashboard is very slow to load") == "performance"
    assert m.classify_text("api response is slower than before") == "performance"


def test_classify_text_strips_url_path_words_before_matching():
    # a URL path segment ("/login") must not itself drive classification
    prompt = "run e2e tests on http://localhost:3000/app/login#/users and tell me which cases fail"
    assert m.classify_text(prompt) == "testing"


def test_build_checklist_for_general_task():
    c = m.build_checklist_for("add a create-user form", env={"has_repo": True})
    assert c["mandatory"] is True
    assert c["archetype"] == "coding"
    assert c["one_pager"] == m.ONE_PAGER
    assert "verification_battery" in c and "gate_sequence" in c


def test_build_checklist_for_classifies_when_archetype_omitted():
    c = m.build_checklist_for("the service crashes on startup")
    assert c["archetype"] == "debugging"


def test_jira_build_checklist_unchanged():
    c = m.build_checklist({"issue_type": "bug", "problem_summary": "crash on login"})
    assert c["archetype"] == "debugging"
    assert c["mandatory"] is True
