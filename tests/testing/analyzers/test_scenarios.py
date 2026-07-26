from icx_engine.testing.analyzers.scenarios import build_scenario_guidance


def test_empty_inputs_return_empty():
    assert build_scenario_guidance(None, None) == ""
    assert build_scenario_guidance("", []) == ""


def test_nl_intent_included():
    g = build_scenario_guidance("test creating a user with a duplicate email", None)
    assert "duplicate email" in g
    assert "intent" in g.lower()


def test_acceptance_criteria_listed():
    g = build_scenario_guidance(None, ["must reject duplicate email", "must show inline error"])
    assert "must reject duplicate email" in g and "must show inline error" in g
    assert "acceptance" in g.lower()


def test_both_combined_ascii():
    g = build_scenario_guidance("delete then undo", ["undo restores the row"])
    assert "delete then undo" in g and "undo restores the row" in g
    assert all(ord(c) < 128 for c in g)


def test_ignores_blank_criteria():
    g = build_scenario_guidance(None, ["  ", "real one", ""])
    assert "real one" in g
    assert g.count("- ") == 1        # only the non-blank criterion is listed
