from icx_engine.testing.nodes import node_author_flow


async def test_explore_gate_includes_scenario_guidance(monkeypatch, tmp_path):
    from icx_engine.testing.runners import ui as _ui
    monkeypatch.setattr(_ui, "_ui_cache_dir", lambda: tmp_path)

    captured = {}

    def _interrupt(payload):
        if payload.get("gate") == "author_flow_explore":
            captured["message"] = payload.get("message", "")
            return {"steps": []}
        return {}
    monkeypatch.setattr("icx_engine.testing.nodes.interrupt", _interrupt)
    # skip the live verify/heal probe
    async def _none(*a, **k):
        return None
    import icx_engine.testing.local_executor as _le
    monkeypatch.setattr(_le, "run_ui_discovery", _none)
    monkeypatch.setattr(_le, "run_ui_verify", _none)

    model = {"functionalities": [{"functionality": "Create", "modalDetails": {"triggerSelector": "#c"},
             "submitButton": {"selectors": ["#s"]}, "fields": [{"label": "Email", "domSelectors": ["#e"]}]}]}
    await node_author_flow({"test_type": "agent", "url": "http://x/#/u", "file_paths": ["a.jsx"],
                            "project": "P", "screen_model": model,
                            "nl_intent": "test duplicate email error",
                            "acceptance_criteria": ["must reject duplicate email"]})
    assert "duplicate email" in captured.get("message", "")
    assert "must reject duplicate email" in captured["message"]


async def test_explore_gate_unchanged_without_inputs(monkeypatch, tmp_path):
    from icx_engine.testing.runners import ui as _ui
    monkeypatch.setattr(_ui, "_ui_cache_dir", lambda: tmp_path)
    captured = {}
    def _interrupt(payload):
        if payload.get("gate") == "author_flow_explore":
            captured["message"] = payload.get("message", "")
        return {"steps": []}
    monkeypatch.setattr("icx_engine.testing.nodes.interrupt", _interrupt)
    async def _none(*a, **k):
        return None
    import icx_engine.testing.local_executor as _le
    monkeypatch.setattr(_le, "run_ui_discovery", _none)
    monkeypatch.setattr(_le, "run_ui_verify", _none)
    model = {"functionalities": [{"functionality": "Create", "modalDetails": {"triggerSelector": "#c"},
             "submitButton": {"selectors": ["#s"]}, "fields": [{"label": "Email", "domSelectors": ["#e"]}]}]}
    await node_author_flow({"test_type": "agent", "url": "http://x/#/u", "file_paths": ["a.jsx"],
                            "project": "P2", "screen_model": model})
    # no NL/criteria -> no "REQUESTED scenarios" block
    assert "REQUESTED scenarios" not in captured.get("message", "")
