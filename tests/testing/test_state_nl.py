from icx_engine.testing.state import make_initial_state


def test_defaults_are_empty():
    s = make_initial_state(["a.jsx"])
    assert s["nl_intent"] is None
    assert s["acceptance_criteria"] == []


def test_carries_nl_intent_and_criteria():
    s = make_initial_state(["a.jsx"], nl_intent="test duplicate email error",
                           acceptance_criteria=["must reject duplicate email", "must show inline error"])
    assert s["nl_intent"] == "test duplicate email error"
    assert s["acceptance_criteria"] == ["must reject duplicate email", "must show inline error"]
