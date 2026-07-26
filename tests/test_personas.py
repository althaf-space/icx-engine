from icx_engine.personas import (
    strip_urls, keyword_persona, select_persona, persona_profile, PERSONA_SLUGS, PERSONA_PROFILE,
)


def test_persona_slugs_and_profile_keys_match():
    assert set(PERSONA_PROFILE.keys()) == PERSONA_SLUGS


def test_strip_urls_removes_url_but_keeps_surrounding_text():
    out = strip_urls("test this http://localhost:3000/app/login#/users please")
    assert "http" not in out and "login" not in out
    assert "test this" in out and "please" in out


def test_strip_urls_noop_when_no_url():
    assert strip_urls("plain text with no link") == "plain text with no link"


def test_url_login_path_does_not_trigger_security_persona():
    # REGRESSION: "login" appearing only in a pasted URL path used to fire the security persona
    # via a bare "login" keyword, even when the actual request was pure QA/e2e testing.
    text = "e2e test every case at http://localhost:3000/Magik_3.0_UI/login#/users"
    assert keyword_persona(text) == "principal-qa-automation-architect"


def test_real_login_flow_request_still_reasonable_without_url():
    # a genuine security ask (exploit-shaped language) still routes to security
    assert keyword_persona("check for JWT injection vulnerabilities in the auth flow") == \
        "principal-security-architect"


def test_qa_automation_persona_for_e2e_request():
    assert keyword_persona("run e2e tests and tell me which cases fail, use slowmo") == \
        "principal-qa-automation-architect"


def test_api_test_persona_for_contract_request():
    assert keyword_persona("write an api test for this endpoint and check its status code") == \
        "principal-api-test-architect"


def test_unit_test_persona_for_unit_request():
    assert keyword_persona("add unit tests with mocks for this function") == \
        "principal-unit-test-architect"


def test_select_persona_falls_back_to_testing_archetype_default():
    # no keyword hit at all -> archetype default
    assert select_persona("please help me", "testing") == "principal-qa-automation-architect"


def test_persona_profile_returns_title_and_focus_for_new_personas():
    title, focus = persona_profile("principal-qa-automation-architect")
    assert "QA automation" in title and focus
    title, focus = persona_profile("principal-api-test-architect")
    assert "API test" in title and focus
    title, focus = persona_profile("principal-unit-test-architect")
    assert "unit test" in title and focus
