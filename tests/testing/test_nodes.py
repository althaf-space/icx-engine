from __future__ import annotations
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from icx_engine.testing.state import make_initial_state, TestingState
from icx_engine.testing.nodes import (
    node_expand_files,
    node_mode_select,
    node_manual_result,
    route_after_check_issues,
    route_after_mode_select,
    _expand_files_via_graph,
    _load_querier,
)


def _base_state(**overrides) -> TestingState:
    s = make_initial_state(
        file_paths=["src/auth/login.py", "src/auth/token.py"],
        context="Fix login bug",
        max_iterations=3,
    )
    s.update(overrides)
    return s


async def test_node_expand_files_no_graph_returns_seeds():
    state = _base_state()
    with patch("icx_engine.testing.nodes._load_querier", return_value=None):
        from langgraph.types import interrupt as _interrupt
        # interrupt pauses graph - we test the pre-interrupt expansion logic
        # by calling the expand helper directly
        from icx_engine.testing.nodes import _expand_files_via_graph
        result = _expand_files_via_graph(["src/auth/login.py"], querier=None)
        assert result == ["src/auth/login.py"]


async def test_expand_files_via_graph_deduplicates():
    from icx_engine.testing.nodes import _expand_files_via_graph
    mock_querier = MagicMock()
    mock_querier._file_nodes = {"src/auth/login.py": ["node-1"]}
    mock_querier.get_blast_radius.return_value = {
        "direct_dependents": ["src/auth/login.py", "src/views/login_view.py"],
        "transitive_dependents": ["src/routes/auth.py"],
    }
    mock_querier.get_call_chain.return_value = MagicMock(
        upstream=[MagicMock(file="src/app.py")],
        downstream=[MagicMock(file="src/auth/token.py")],
    )
    mock_querier.get_subsystem.return_value = MagicMock(
        top_files=["src/auth/login.py", "src/auth/register.py"]
    )
    result = _expand_files_via_graph(["src/auth/login.py"], querier=mock_querier)
    assert len(result) == len(set(result))  # no duplicates
    assert "src/auth/login.py" in result
    assert "src/views/login_view.py" in result




















def test_route_after_check_issues_zero_issues_returns_ui_check():
    state = _base_state(issues=[], iteration=1, max_iterations=3)
    assert route_after_check_issues(state) == "ui_check"


def test_route_after_check_issues_below_max_returns_loop():
    state = _base_state(issues=[{"name": "bug1"}], iteration=1, max_iterations=3)
    assert route_after_check_issues(state) == "loop"


def test_route_after_check_issues_at_max_returns_limit_gate():
    state = _base_state(issues=[{"name": "bug1"}], iteration=3, max_iterations=3)
    assert route_after_check_issues(state) == "limit_gate"




# -- _load_querier path fix tests -------------------------------------------

def test_load_querier_finds_deep_java_file():
    from pathlib import Path
    mock_info = MagicMock()
    mock_info.project_id = "abc123"
    mock_graph_path = MagicMock()
    mock_graph_path.exists.return_value = True
    mock_querier = MagicMock()

    with patch("icx_engine.graph.storage.lookup_for_file", return_value=mock_info) as mock_lookup, \
         patch("icx_engine.graph.storage.graph_path", return_value=mock_graph_path), \
         patch("icx_engine.graph.query.GraphQuerier", return_value=mock_querier):
        result = _load_querier([
            "F:/clients/vil/cms-re-wo-svc/src/main/java/com/sixdee/dao/impl/WorkOrderDaoImpl.java"
        ])
    assert result is mock_querier
    mock_lookup.assert_called_once()


def test_load_querier_returns_none_when_no_registered_project():
    with patch("icx_engine.graph.storage.lookup_for_file", return_value=None):
        result = _load_querier(["F:/clients/project/src/Foo.java"])
    assert result is None


# -- _expand_files_via_graph tool coverage tests ----------------------------

def test_expand_files_uses_blast_radius():
    mock_querier = MagicMock()
    mock_querier.get_blast_radius.return_value = {
        "direct_dependents": ["src/ServiceA.java"],
        "transitive_dependents": ["src/ServiceB.java"],
    }
    mock_querier.get_subsystem.return_value = MagicMock(top_files=[])
    mock_querier.get_cochange_partners.return_value = []
    result = _expand_files_via_graph(["src/DaoImpl.java"], mock_querier)
    assert "src/ServiceA.java" in result
    assert "src/ServiceB.java" in result
    mock_querier.get_blast_radius.assert_called_once_with(["src/DaoImpl.java"])


def test_expand_files_uses_subsystem_top_files():
    mock_querier = MagicMock()
    mock_querier.get_blast_radius.return_value = {"direct_dependents": [], "transitive_dependents": []}
    mock_querier.get_subsystem.return_value = MagicMock(top_files=["src/ClusterPeer.java"])
    mock_querier.get_cochange_partners.return_value = []
    result = _expand_files_via_graph(["src/DaoImpl.java"], mock_querier)
    assert "src/ClusterPeer.java" in result


def test_expand_files_uses_cochange_partners_above_threshold():
    mock_querier = MagicMock()
    mock_querier.get_blast_radius.return_value = {"direct_dependents": [], "transitive_dependents": []}
    mock_querier.get_subsystem.return_value = MagicMock(top_files=[])
    mock_querier.get_cochange_partners.return_value = [
        {"file": "src/FrequentPartner.java", "strength": 0.8},
        {"file": "src/WeakPartner.java", "strength": 0.2},
    ]
    result = _expand_files_via_graph(["src/DaoImpl.java"], mock_querier)
    assert "src/FrequentPartner.java" in result
    assert "src/WeakPartner.java" not in result


def test_expand_files_uses_find_context_when_context_given():
    mock_querier = MagicMock()
    mock_querier.get_blast_radius.return_value = {"direct_dependents": [], "transitive_dependents": []}
    mock_querier.get_subsystem.return_value = MagicMock(top_files=[])
    mock_querier.get_cochange_partners.return_value = []
    mock_querier.find_context.return_value = [
        MagicMock(file="src/Semantic.java"),
        MagicMock(file="src/Semantic2.java"),
    ]
    result = _expand_files_via_graph(["src/DaoImpl.java"], mock_querier, context="work order dao fix")
    assert "src/Semantic.java" in result
    mock_querier.find_context.assert_called_once_with("work order dao fix")


def test_expand_files_no_find_context_without_context():
    mock_querier = MagicMock()
    mock_querier.get_blast_radius.return_value = {"direct_dependents": [], "transitive_dependents": []}
    mock_querier.get_subsystem.return_value = MagicMock(top_files=[])
    mock_querier.get_cochange_partners.return_value = []
    _expand_files_via_graph(["src/DaoImpl.java"], mock_querier, context=None)
    mock_querier.find_context.assert_not_called()


def test_expand_files_deduplicates():
    mock_querier = MagicMock()
    mock_querier.get_blast_radius.return_value = {
        "direct_dependents": ["src/DaoImpl.java"],  # same as seed
        "transitive_dependents": [],
    }
    mock_querier.get_subsystem.return_value = MagicMock(top_files=["src/DaoImpl.java"])
    mock_querier.get_cochange_partners.return_value = []
    result = _expand_files_via_graph(["src/DaoImpl.java"], mock_querier)
    assert result.count("src/DaoImpl.java") == 1


# -- new node tests ---------------------------------------------------------

def test_route_after_mode_select_automated():
    state = _base_state(test_mode="automated")
    assert route_after_mode_select(state) == "pick_type"


def test_route_after_mode_select_manual():
    state = _base_state(test_mode="manual")
    assert route_after_mode_select(state) == "expand_files"


def test_route_after_mode_select_default_automated_when_none():
    state = _base_state(test_mode=None)
    assert route_after_mode_select(state) == "pick_type"


async def test_node_mode_select_automated():
    state = _base_state()
    with patch("icx_engine.testing.nodes.interrupt", return_value={"choice": "automated"}):
        result = await node_mode_select(state)
    assert result["test_mode"] == "automated"


async def test_node_mode_select_manual():
    state = _base_state()
    with patch("icx_engine.testing.nodes.interrupt", return_value={"choice": "manual"}):
        result = await node_mode_select(state)
    assert result["test_mode"] == "manual"


async def test_node_mode_select_invalid_defaults_to_automated():
    state = _base_state()
    with patch("icx_engine.testing.nodes.interrupt", return_value={"choice": "unknown"}):
        result = await node_mode_select(state)
    assert result["test_mode"] == "automated"


async def test_node_mode_select_accepts_number_1():
    state = _base_state()
    with patch("icx_engine.testing.nodes.interrupt", return_value={"choice": "1"}):
        result = await node_mode_select(state)
    assert result["test_mode"] == "automated"


async def test_node_mode_select_accepts_number_2():
    state = _base_state()
    with patch("icx_engine.testing.nodes.interrupt", return_value={"choice": "2"}):
        result = await node_mode_select(state)
    assert result["test_mode"] == "manual"


async def test_node_mode_select_skips_interrupt_when_test_mode_preset():
    state = _base_state(test_mode="automated")
    with patch("icx_engine.testing.nodes.interrupt") as mock_interrupt:
        result = await node_mode_select(state)
    mock_interrupt.assert_not_called()
    assert result == {}


async def test_node_mode_select_skips_interrupt_when_manual_preset():
    state = _base_state(test_mode="manual")
    with patch("icx_engine.testing.nodes.interrupt") as mock_interrupt:
        result = await node_mode_select(state)
    mock_interrupt.assert_not_called()
    assert result == {}


async def test_node_manual_result_stores_fields():
    state = _base_state()
    response = {"passed": False, "issues": ["Button not visible"], "notes": "Checked on Chrome"}
    with patch("icx_engine.testing.nodes.interrupt", return_value=response):
        result = await node_manual_result(state)
    assert result["manual_result"]["passed"] is False
    assert result["manual_result"]["issues"] == ["Button not visible"]
    assert result["manual_result"]["notes"] == "Checked on Chrome"
    assert result["status"] == "test_complete"


async def test_node_manual_result_passed_true():
    state = _base_state()
    with patch("icx_engine.testing.nodes.interrupt", return_value={"passed": True}):
        result = await node_manual_result(state)
    assert result["manual_result"]["passed"] is True
    assert result["manual_result"]["issues"] == []


# ---------------------------------------------------------------------------
# route_after_poll / route_after_error_gate - status-driven routing.
# ---------------------------------------------------------------------------











# ---------------------------------------------------------------------------
# node_config_gate (Gate 3) - URL-required validation
# clamping, and headless parsing.
# ---------------------------------------------------------------------------

async def test_node_config_gate_agent_without_url_raises():
    from icx_engine.testing.nodes import node_config_gate
    state = _base_state(url=None)
    state["test_type"] = "agent"
    with patch("icx_engine.testing.nodes.interrupt", return_value={}):
        with pytest.raises(ValueError, match="requires a URL"):
            await node_config_gate(state)


async def test_node_config_gate_recommends_layers_and_defaults_to_recommendation():
    from icx_engine.testing.nodes import node_config_gate
    state = _base_state()
    state["test_type"] = "unit"
    # user accepts recommendation (no explicit layers) -> selected == recommended
    with patch("icx_engine.testing.nodes.interrupt", return_value={}) as m:
        result = await node_config_gate(state)
    payload = m.call_args[0][0]
    assert payload["gate"] == 3
    assert payload["recommended_layers"]  # risk-based recommendation present
    assert result["selected_layers"] == payload["recommended_layers"]
    assert result["risk_tier"] in {"low", "medium", "high", "critical"}


async def test_node_config_gate_user_selects_subset_of_layers():
    from icx_engine.testing.nodes import node_config_gate
    state = _base_state()
    state["test_type"] = "unit"
    with patch("icx_engine.testing.nodes.interrupt", return_value={"layers": ["unit"]}):
        result = await node_config_gate(state)
    assert result["selected_layers"] == ["unit"]


async def test_node_config_gate_confirms_url():
    from icx_engine.testing.nodes import node_config_gate
    state = _base_state(url=None)
    state["test_type"] = "api"
    with patch("icx_engine.testing.nodes.interrupt", return_value={"url": "http://svc/api"}):
        result = await node_config_gate(state)
    assert result["url"] == "http://svc/api"


async def test_node_config_gate_slowmo_headless_is_zero():
    # agent, default (no visible) -> headless -> slowmo 0
    from icx_engine.testing.nodes import node_config_gate
    state = _base_state(url="http://x/#/home")
    state["test_type"] = "agent"
    with patch("icx_engine.testing.nodes.interrupt", return_value={"url": "http://x/#/home"}):
        result = await node_config_gate(state)
    assert result["headless"] is True
    assert result["slowmo"] == 0


async def test_node_config_gate_slowmo_visible_defaults_to_1000():
    # agent + visible, no explicit slowmo -> headed -> default 1000
    from icx_engine.testing.nodes import node_config_gate
    state = _base_state(url="http://x/#/home")
    state["test_type"] = "agent"
    with patch("icx_engine.testing.nodes.interrupt", return_value={"visible": True}):
        result = await node_config_gate(state)
    assert result["headless"] is False
    assert result["slowmo"] == 1000


async def test_node_config_gate_slowmo_user_override():
    # agent + visible + explicit slowmo -> that value
    from icx_engine.testing.nodes import node_config_gate
    state = _base_state(url="http://x/#/home")
    state["test_type"] = "agent"
    with patch("icx_engine.testing.nodes.interrupt", return_value={"visible": True, "slowmo": 2500}):
        result = await node_config_gate(state)
    assert result["headless"] is False and result["slowmo"] == 2500


async def test_node_config_gate_no_slowmo_for_non_browser():
    # unit/api never get a slowmo key
    from icx_engine.testing.nodes import node_config_gate
    state = _base_state()
    state["test_type"] = "unit"
    with patch("icx_engine.testing.nodes.interrupt", return_value={}):
        result = await node_config_gate(state)
    assert "slowmo" not in result


async def test_node_config_gate_reasks_missing_url_instead_of_raising():
    # agent with no URL: the gate must RE-ASK (fresh interrupt) and accept the URL, NOT raise (a
    # raise escapes the unguarded graph.ainvoke and permanently strands the session).
    from icx_engine.testing.nodes import node_config_gate
    state = _base_state(url=None)
    state["test_type"] = "agent"
    # 1st interrupt (main gate) has no url; re-ask supplies it.
    with patch("icx_engine.testing.nodes.interrupt",
               side_effect=[{"visible": False}, {"url": "http://x/#/home"}]) as m:
        result = await node_config_gate(state)
    assert result["url"] == "http://x/#/home"
    assert m.call_count == 2  # main + one re-ask


async def test_node_config_gate_raises_only_after_bounded_reasks():
    # if the URL is never supplied, the gate errors after the bounded re-asks (never loops forever).
    import pytest as _pytest
    from icx_engine.testing.nodes import node_config_gate, _URL_GATE_MAX_REASK
    state = _base_state(url=None)
    state["test_type"] = "agent"
    with patch("icx_engine.testing.nodes.interrupt", return_value={}) as m:
        with _pytest.raises(ValueError):
            await node_config_gate(state)
    assert m.call_count == 1 + _URL_GATE_MAX_REASK  # main + bounded re-asks


async def test_node_local_run_execution_error_reported_as_issue(monkeypatch):
    # An exception in run_local_verification must surface as a FAILURE issue, not an empty-issues
    # "all tests passed" (which would make memory_save record a false green).
    import icx_engine.testing.local_executor as _le
    from icx_engine.testing.nodes import node_local_run

    async def _boom(*a, **k):
        raise RuntimeError("runner blew up")
    monkeypatch.setattr(_le, "run_local_verification", _boom)
    out = await node_local_run({"engine": "local", "test_type": "unit",
                                "file_paths": ["a.py"], "url": None})
    assert out["status"] == "error"
    assert out["issues"] and out["issues"][0]["severity"] == "high"


import pytest
from icx_engine.testing.state import make_initial_state
from icx_engine.testing import nodes


@pytest.mark.asyncio
async def test_pick_type_sets_test_type(monkeypatch):
    captured = {}

    def fake_interrupt(payload):
        captured["payload"] = payload
        return {"test_type": "1"}   # numbered -> agent (order: agent, api, unit)

    monkeypatch.setattr(nodes, "interrupt", fake_interrupt)
    out = await nodes.node_pick_type(make_initial_state(file_paths=["a.tsx"], test_mode="automated"))
    assert out["test_type"] == "agent"
    assert captured["payload"]["gate"] == "pick_type"


@pytest.mark.asyncio
async def test_pick_type_supports_unit(monkeypatch):
    monkeypatch.setattr(nodes, "interrupt", lambda p: {"test_type": "3"})
    out = await nodes.node_pick_type(make_initial_state(file_paths=["a.py"], test_mode="automated"))
    assert out["test_type"] == "unit"


@pytest.mark.asyncio
async def test_config_gate_anchors_layer_on_picked_type(monkeypatch):
    # gate 3 must default to the type picked at pick_type (agent), NOT re-recommend a contradictory set.
    s = make_initial_state(file_paths=["a.jsx"], test_mode="automated")
    s["test_type"] = "agent"
    s["url"] = "http://x/login"
    monkeypatch.setattr(nodes, "interrupt", lambda p: {})   # user accepts
    out = await nodes.node_config_gate(s)
    assert out["selected_layers"] == ["agent"]


@pytest.mark.asyncio
async def test_author_flow_gate_auth_aware_login_instruction(monkeypatch):
    # With a restored session (capture/inline/reuse), ICX logs the app in automatically - the agent
    # must be told NOT to author login steps. Public apps (no session) get told to author them.
    for mode in ("capture", "inline", "reuse"):
        cap = {}
        monkeypatch.setattr(nodes, "interrupt", lambda p, _c=cap: _c.setdefault("p", p) or {"report_path": "r.xml"})
        s = make_initial_state(file_paths=["login.tsx"], test_mode="automated")
        s["test_type"] = "agent"; s["url"] = "http://x/#/home"; s["auth_mode"] = mode
        await nodes.node_author_flow(s)
        msg = cap["p"]["message"].lower()
        assert "do not author" in msg and "login" in msg
        assert cap["p"]["auth_mode"] == mode
        assert cap["p"]["gate"] == "author_flow"

    # public -> DO author login steps
    cap = {}
    monkeypatch.setattr(nodes, "interrupt", lambda p, _c=cap: _c.setdefault("p", p) or {"report_path": "r.xml"})
    s = make_initial_state(file_paths=["login.tsx"], test_mode="automated")
    s["test_type"] = "agent"; s["url"] = "http://x/#/home"; s["auth_mode"] = "public"
    await nodes.node_author_flow(s)
    msg = cap["p"]["message"].lower()
    assert "author real login steps" in msg


def test_route_after_auth_unit_with_census_goes_to_unit_author():
    s = make_initial_state(file_paths=["a.py"], test_mode="automated")
    s["test_type"] = "unit"; s["screen_model"] = {"elementCensus": {}}
    assert nodes.route_after_auth(s) == "unit_author"
    # plain unit (no census) -> straight to run
    s2 = make_initial_state(file_paths=["a.py"], test_mode="automated")
    s2["test_type"] = "unit"
    assert nodes.route_after_auth(s2) == "local_run"
    # agent always authors
    s3 = make_initial_state(file_paths=["a.jsx"], test_mode="automated")
    s3["test_type"] = "agent"
    assert nodes.route_after_auth(s3) == "author_flow"


@pytest.mark.asyncio
async def test_node_unit_author_instructs_writing_tests(monkeypatch):
    cap = {}
    monkeypatch.setattr(nodes, "interrupt", lambda p: cap.setdefault("p", p) or {})
    s = make_initial_state(file_paths=["src/lib.cpp"], test_mode="automated")
    s["test_type"] = "unit"; s["analyzer_family"] = "cpp"
    s["screen_model"] = {"elementCensus": {"counts": {}}, "coverageReport": {}}
    out = await nodes.node_unit_author(s)
    msg = cap["p"]["message"].lower()
    assert cap["p"]["gate"] == "unit_author"
    assert "write comprehensive unit tests" in msg and "googletest" in msg  # cpp hint present
    assert "read_receipts" in out


@pytest.mark.asyncio
async def test_node_unit_author_noop_without_census(monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr(nodes, "interrupt", lambda p: called.__setitem__("n", called["n"] + 1))
    s = make_initial_state(file_paths=["a.py"], test_mode="automated")
    s["test_type"] = "unit"
    out = await nodes.node_unit_author(s)
    assert out == {} and called["n"] == 0


def _valid_react_census():
    return {
        "elementCensus": {"counts": {"eventHandlers": 2, "inputSurfaces": 1}},
        "functionalities": [
            {"id": "FUNC_000", "functionality": "Search", "modalDetails": {"triggerSelector": "#q"}},
            {"id": "FUNC_001", "functionality": "Create",
             "modalDetails": {"triggerSelector": "#create", "modalSelector": "#m"},
             "submitButton": {"selectors": ["#save"]},
             "fields": [{"label": "Name", "domSelectors": ["#name"], "validations": {"maxLength": 20}}]}],
        "coverageReport": {"reconciliation": {
            "eventHandlers": {"total": 2, "mapped": 2, "unmapped": 0},
            "inputSurfaces": {"total": 1, "mapped": 1, "unmapped": 0}}},
    }


@pytest.mark.asyncio
async def test_analyze_screen_runs_census_for_known_framework(monkeypatch):
    # .jsx -> react analyzer -> agent returns a reconciled census -> stored as screen_model.
    captured = {}

    def fake_interrupt(payload):
        captured["payload"] = payload
        return {"screen_model": _valid_react_census()}
    monkeypatch.setattr(nodes, "interrupt", fake_interrupt)
    out = await nodes.node_analyze_screen(make_initial_state(
        file_paths=["src/CreateTeam.jsx"], test_mode="automated"))
    assert out["analyzer_id"] == "react" and out["analyzer_family"] == "ui"
    assert isinstance(out["screen_model"], dict) and out["census_coverage"] == 1.0
    assert captured["payload"]["gate"] == "analyze_screen"
    assert "analyzer_prompt" in captured["payload"] and captured["payload"]["analyzer_prompt"]


@pytest.mark.asyncio
async def test_analyze_screen_reasks_on_lint_defect(monkeypatch):
    # a census where edit REUSES create's submit selector (the copy bug) must be RE-ASKED, not accepted -
    # this is how census quality is enforced regardless of which agent produced it.
    bad = {
        "elementCensus": {"counts": {"eventHandlers": 2, "inputSurfaces": 1}},
        "functionalities": [
            {"id": "C", "functionality": "Create", "modalDetails": {"triggerSelector": "#c", "modalSelector": "#m"},
             "submitButton": {"selectors": ["#save"]}, "fields": [{"label": "N", "domSelectors": ["#n"], "type": "email"}]},
            {"id": "E", "functionality": "Edit", "modalDetails": {"triggerSelector": "#e", "modalSelector": "#m"},
             "submitButton": {"selectors": ["#save"]}, "fields": [{"label": "N", "domSelectors": ["#n"], "type": "email"}]}],
        "coverageReport": {"reconciliation": {
            "eventHandlers": {"total": 2, "mapped": 2, "unmapped": 0},
            "inputSurfaces": {"total": 1, "mapped": 1, "unmapped": 0}}},
    }
    calls = {"n": 0, "notes": []}

    def fake_interrupt(payload):
        calls["n"] += 1
        if "FIX:" in payload.get("message", ""):
            calls["notes"].append(payload["message"])
        return {"screen_model": bad}
    monkeypatch.setattr(nodes, "interrupt", fake_interrupt)
    await nodes.node_analyze_screen(make_initial_state(file_paths=["a.jsx"], test_mode="automated"))
    assert calls["n"] > 1                                   # it re-asked (did not accept the bad census)
    assert any("share the SAME submit selector" in n for n in calls["notes"])   # with the exact defect


@pytest.mark.asyncio
async def test_analyze_screen_records_soft_warnings(monkeypatch):
    # a text field with no length constraint is a SOFT warning - recorded, not blocking.
    census = {
        "elementCensus": {"counts": {"eventHandlers": 1, "inputSurfaces": 1}},
        "functionalities": [
            {"id": "S", "functionality": "Search", "modalDetails": {"triggerSelector": "#q"}},
            {"id": "C", "functionality": "Create", "modalDetails": {"triggerSelector": "#c", "modalSelector": "#m"},
             "submitButton": {"selectors": ["#save"]}, "fields": [{"label": "Notes", "domSelectors": ["#n"]}]}],
        "coverageReport": {"reconciliation": {
            "eventHandlers": {"total": 1, "mapped": 1, "unmapped": 0},
            "inputSurfaces": {"total": 1, "mapped": 1, "unmapped": 0}}},
    }
    monkeypatch.setattr(nodes, "interrupt", lambda p: {"screen_model": census})
    out = await nodes.node_analyze_screen(make_initial_state(file_paths=["a.jsx"], test_mode="automated"))
    assert out["screen_model"] is not None                 # accepted (soft, not blocking)
    assert any("no length/format constraint" in w for w in out["census_warnings"])


@pytest.mark.asyncio
async def test_analyze_screen_degrades_for_unknown_framework(monkeypatch):
    # unknown extension -> no analyzer -> no interrupt, empty update (free authoring downstream).
    called = {"n": 0}
    monkeypatch.setattr(nodes, "interrupt", lambda p: called.__setitem__("n", called["n"] + 1))
    out = await nodes.node_analyze_screen(make_initial_state(
        file_paths=["src/thing.cobol"], test_mode="automated"))
    assert out == {}
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_analyze_screen_reasks_on_bad_reconciliation(monkeypatch):
    # census whose counts do not add up -> re-ask (bounded), keep best-effort model.
    from icx_engine.testing.nodes import _CENSUS_MAX_REASK
    bad = _valid_react_census()
    bad["coverageReport"]["reconciliation"]["eventHandlers"] = {"total": 5, "mapped": 2, "unmapped": 0}
    calls = {"n": 0}

    def fake_interrupt(payload):
        calls["n"] += 1
        return {"screen_model": bad}
    monkeypatch.setattr(nodes, "interrupt", fake_interrupt)
    out = await nodes.node_analyze_screen(make_initial_state(
        file_paths=["a.jsx"], test_mode="automated"))
    assert calls["n"] == 1 + _CENSUS_MAX_REASK      # initial + bounded re-asks
    assert out["analyzer_id"] == "react"            # still records the attempt


def test_route_after_mode_select_automated_to_pick_type():
    s = make_initial_state(file_paths=["a.tsx"], test_mode="automated")
    assert nodes.route_after_mode_select(s) == "pick_type"


def test_route_after_mode_select_manual_to_expand():
    s = make_initial_state(file_paths=["a.tsx"], test_mode="manual")
    assert nodes.route_after_mode_select(s) == "expand_files"


def test_route_after_expand_branches_on_mode():
    # updated: automated path now routes to compat_scan (agent-driven detection) not compat_check
    auto = make_initial_state(file_paths=["a.tsx"], test_mode="automated")
    man = make_initial_state(file_paths=["a.tsx"], test_mode="manual")
    assert nodes.route_after_expand(auto) == "compat_scan"
    assert nodes.route_after_expand(man) == "manual_wait"


@pytest.mark.asyncio
async def test_expand_filters_to_relevant_layer(monkeypatch, tmp_path):
    fe = tmp_path / "Page.tsx"
    fe.write_text("export default function Page(){return <div data-testid='x'/>;}", encoding="utf-8")
    be = tmp_path / "UserController.java"
    be.write_text("@RestController class C { @PostMapping void m(){} }", encoding="utf-8")

    monkeypatch.setattr(nodes, "_load_querier", lambda paths: None)        # no graph
    monkeypatch.setattr(nodes, "expand_via_grep", lambda fps, root: [str(be)])

    seen = {}
    def fake_interrupt(payload):
        if payload["gate"] == "expand_scan":
            return {}   # no related_files -> fallback to ICX grep (already monkeypatched)
        seen["payload"] = payload
        return {"confirmed_files": payload["selected_files"]}
    monkeypatch.setattr(nodes, "interrupt", fake_interrupt)

    s = make_initial_state(file_paths=[str(fe)], test_mode="automated")
    s["test_type"] = "agent"
    out = await nodes.node_expand_files(s)
    sel = {p.replace("\\", "/") for p in seen["payload"]["selected_files"]}
    assert any(p.endswith("Page.tsx") for p in sel)
    off = {p.replace("\\", "/") for p in seen["payload"]["excluded_off_type"]}
    assert any(p.endswith("UserController.java") for p in off)


@pytest.mark.asyncio
async def test_compat_check_clean_proceeds(monkeypatch, tmp_path):
    f = tmp_path / "Page.tsx"
    f.write_text("export default function P(){return <div data-testid='x'/>;}\n", encoding="utf-8")
    called = {"interrupt": False}
    def fake_interrupt(p):
        called["interrupt"] = True
        return {}
    monkeypatch.setattr(nodes, "interrupt", fake_interrupt)
    s = make_initial_state(file_paths=[str(f)], test_mode="automated")
    s["test_type"] = "agent"
    out = await nodes.node_compat_check(s)
    assert called["interrupt"] is False           # no issues -> no gate
    assert out["status"] == "compat_ok"
    assert nodes.route_after_compat({**s, **out}) == "config_gate"


@pytest.mark.asyncio
async def test_compat_check_issue_drop(monkeypatch, tmp_path):
    f = tmp_path / "Plain.tsx"
    f.write_text("export default function P(){return <div/>;}\n", encoding="utf-8")  # no selector
    def fake_interrupt(p):
        assert p["gate"] == "compat_check"
        return {"decision": "reject", "resolution": {str(f): "drop"}}
    monkeypatch.setattr(nodes, "interrupt", fake_interrupt)
    s = make_initial_state(file_paths=[str(f)], test_mode="automated")
    s["test_type"] = "agent"
    # node_compat_check now consumes compat_findings set by node_compat_scan upstream
    s["compat_findings"] = [{"path": str(f), "compatible": False,
                              "reasons": ["no selector"], "required_changes": ["add data-testid"]}]
    out = await nodes.node_compat_check(s)
    assert str(f) not in out["file_paths"]          # dropped
    assert out["status"] in ("compat_ok", "compat_empty")


@pytest.mark.asyncio
async def test_compat_check_approve_rechecks(monkeypatch, tmp_path):
    f = tmp_path / "Plain.tsx"
    f.write_text("export default function P(){return <div/>;}\n", encoding="utf-8")
    def fake_interrupt(p):
        return {"decision": "approve"}              # agent will edit + re-scan
    monkeypatch.setattr(nodes, "interrupt", fake_interrupt)
    s = make_initial_state(file_paths=[str(f)], test_mode="automated")
    s["test_type"] = "agent"
    # node_compat_check now consumes compat_findings set by node_compat_scan upstream
    s["compat_findings"] = [{"path": str(f), "compatible": False,
                              "reasons": ["no selector"], "required_changes": ["add data-testid"]}]
    out = await nodes.node_compat_check(s)
    assert out["status"] == "compat_recheck"
    # approve routes back to compat_scan (agent re-reads files after fix) not compat_check
    assert nodes.route_after_compat({**s, **out}) == "compat_scan"


@pytest.mark.asyncio
async def test_compat_check_max_iterations_forces_resolution(monkeypatch, tmp_path):
    f = tmp_path / "Plain.tsx"
    f.write_text("export default function P(){return <div/>;}\n", encoding="utf-8")
    def fake_interrupt(p):
        assert p.get("forced") is True
        return {"resolution": {str(f): "manual"}}
    monkeypatch.setattr(nodes, "interrupt", fake_interrupt)
    s = make_initial_state(file_paths=[str(f)], test_mode="automated")
    s["test_type"] = "agent"
    s["compat_iteration"] = 3
    s["max_compat_iterations"] = 3
    s["compat_findings"] = [{"path": str(f), "compatible": False, "reasons": ["x"], "required_changes": ["y"]}]
    out = await nodes.node_compat_check(s)
    assert out["status"] in ("compat_ok", "compat_empty")
    # forced branch ran: status is not "compat_recheck" (which is what approve() returns)
    assert nodes.route_after_compat({**s, **out}) != "compat_scan"


@pytest.mark.asyncio


@pytest.mark.asyncio
async def test_config_gate_does_not_ask_test_type(monkeypatch):
    captured = {}
    def fake_interrupt(p):
        captured["payload"] = p
        return {}
    monkeypatch.setattr(nodes, "interrupt", fake_interrupt)
    s = make_initial_state(file_paths=["a.tsx"], test_mode="automated")
    s["test_type"] = "agent"
    s["url"] = "http://host-x/app"
    await nodes.node_config_gate(s)
    assert "test_type" not in captured["payload"].get("options", {})


from icx_engine.testing import auth as _auth


@pytest.mark.asyncio
async def test_auth_gate_skips_api(monkeypatch):
    s = make_initial_state(file_paths=["x.java"], test_mode="automated")
    s["test_type"] = "api"
    called = {"i": False}
    monkeypatch.setattr(nodes, "interrupt", lambda p: called.__setitem__("i", True) or {})
    out = await nodes.node_auth_gate(s)
    assert called["i"] is False
    assert out == {}


@pytest.mark.asyncio
async def test_auth_gate_public_sets_recover_false(monkeypatch):
    s = make_initial_state(file_paths=["a.tsx"], test_mode="automated")
    s["test_type"] = "agent"; s["url"] = "http://host-x/app"
    monkeypatch.setattr(nodes, "interrupt", lambda p: {"auth_mode": "public"})
    out = await nodes.node_auth_gate(s)
    assert out["auth_mode"] == "public"
    assert out["auto_auth_recover"] is False
    assert out["host"] == "host-x"


@pytest.mark.asyncio
async def test_auth_gate_capture_saves_session(monkeypatch, tmp_path):
    store = tmp_path / "auth.json"
    monkeypatch.setattr(_auth, "store_path", lambda: store)
    s = make_initial_state(file_paths=["a.tsx"], test_mode="automated")
    s["test_type"] = "agent"; s["url"] = "http://host-x/app"; s["project"] = "proj-a"
    monkeypatch.setattr(nodes, "interrupt", lambda p: {"auth_mode": "capture", "session_id": "cap-1"})
    out = await nodes.node_auth_gate(s)
    assert out["auth_mode"] == "capture"
    assert _auth.load_session("proj-a", "host-x", store=store).session_id == "cap-1"
    assert "session_id" not in out and "_auth_session_id" not in out


@pytest.mark.asyncio
async def test_auth_gate_reuse_with_missing_storage_state_falls_back_to_public(monkeypatch, tmp_path):
    # A stored session RECORD can exist (TTL not expired) while its storage_state file was deleted,
    # never written, or points nowhere. reuse must not silently proceed as if authenticated.
    store = tmp_path / "auth.json"
    monkeypatch.setattr(_auth, "store_path", lambda: store)
    _auth.save_session("proj-a", "host-x", "sid-1", store=store, storage_state="")
    s = make_initial_state(file_paths=["a.tsx"], test_mode="automated")
    s["test_type"] = "agent"; s["url"] = "http://host-x/app"; s["project"] = "proj-a"
    monkeypatch.setattr(nodes, "interrupt", lambda p: {"auth_mode": "reuse"})
    out = await nodes.node_auth_gate(s)
    assert out["auth_mode"] == "public"
    assert out["auto_auth_recover"] is False


@pytest.mark.asyncio
async def test_auth_gate_reuse_with_nonexistent_storage_state_file_falls_back_to_public(monkeypatch, tmp_path):
    store = tmp_path / "auth.json"
    monkeypatch.setattr(_auth, "store_path", lambda: store)
    _auth.save_session("proj-a", "host-x", "sid-1", store=store,
                       storage_state=str(tmp_path / "does-not-exist.json"))
    s = make_initial_state(file_paths=["a.tsx"], test_mode="automated")
    s["test_type"] = "agent"; s["url"] = "http://host-x/app"; s["project"] = "proj-a"
    monkeypatch.setattr(nodes, "interrupt", lambda p: {"auth_mode": "reuse"})
    out = await nodes.node_auth_gate(s)
    assert out["auth_mode"] == "public"


@pytest.mark.asyncio
async def test_auth_gate_reuse_with_corrupt_storage_state_falls_back_to_public(monkeypatch, tmp_path):
    store = tmp_path / "auth.json"
    statefile = tmp_path / "state.json"
    statefile.write_text("{not valid json", encoding="utf-8")
    monkeypatch.setattr(_auth, "store_path", lambda: store)
    _auth.save_session("proj-a", "host-x", "sid-1", store=store, storage_state=str(statefile))
    s = make_initial_state(file_paths=["a.tsx"], test_mode="automated")
    s["test_type"] = "agent"; s["url"] = "http://host-x/app"; s["project"] = "proj-a"
    monkeypatch.setattr(nodes, "interrupt", lambda p: {"auth_mode": "reuse"})
    out = await nodes.node_auth_gate(s)
    assert out["auth_mode"] == "public"


@pytest.mark.asyncio
async def test_auth_gate_reuse_with_valid_storage_state_stays_reuse(monkeypatch, tmp_path):
    # Regression: a genuinely usable session must NOT be downgraded.
    store = tmp_path / "auth.json"
    statefile = tmp_path / "state.json"
    statefile.write_text(json.dumps({"cookies": [{"name": "sid", "value": "x"}], "origins": []}),
                         encoding="utf-8")
    monkeypatch.setattr(_auth, "store_path", lambda: store)
    _auth.save_session("proj-a", "host-x", "sid-1", store=store, storage_state=str(statefile))
    s = make_initial_state(file_paths=["a.tsx"], test_mode="automated")
    s["test_type"] = "agent"; s["url"] = "http://host-x/app"; s["project"] = "proj-a"
    monkeypatch.setattr(nodes, "interrupt", lambda p: {"auth_mode": "reuse"})
    out = await nodes.node_auth_gate(s)
    assert out["auth_mode"] == "reuse"


@pytest.mark.asyncio




@pytest.mark.asyncio


@pytest.mark.asyncio


@pytest.mark.asyncio


@pytest.mark.asyncio


@pytest.mark.asyncio
async def test_review_passes_full_report_and_requires_approval(monkeypatch):
    seen = {}
    def fake_interrupt(p):
        if p["gate"] == 4:
            seen["report"] = p.get("full_report")
            return {}
        return {"approve_iteration": True, "fixes_applied": ["fix1"]}
    monkeypatch.setattr(nodes, "interrupt", fake_interrupt)
    s = make_initial_state(file_paths=["a.tsx"], test_mode="automated")
    s["issues"] = [{"name": "t1"}]
    s["full_report"] = {"results": [{"status": "fail"}]}
    out = await nodes.node_review(s)
    assert seen["report"] == {"results": [{"status": "fail"}]}
    assert out["iteration"] == 1
    assert out.get("approve_iteration") is True


def test_sonar_detached_from_testing_nodes():
    # Sonar is a distinct feature - the testing nodes module must not carry it
    assert not hasattr(nodes, "node_sonar_enrich")




# ---------------------------------------------------------------------------
# node_compat_scan (agent-driven detection) + compat_check rewrite
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_compat_scan_agent_clean(monkeypatch):
    def fake_interrupt(p):
        assert p["gate"] == "compat_scan"
        return {"all_compatible": True, "findings": [{"path": "a.tsx", "compatible": True, "reasons": [], "required_changes": []}]}
    monkeypatch.setattr(nodes, "interrupt", fake_interrupt)
    s = make_initial_state(file_paths=["a.tsx"], test_mode="automated")
    s["test_type"] = "agent"
    out = await nodes.node_compat_scan(s)
    assert out["compat_findings"][0]["compatible"] is True
    assert nodes.route_after_scan({**s, **out}) == "config_gate"


@pytest.mark.asyncio
async def test_compat_scan_agent_finds_issue_routes_to_check(monkeypatch):
    def fake_interrupt(p):
        return {"all_compatible": False, "findings": [
            {"path": "Plain.tsx", "compatible": False, "reasons": ["no selector"],
             "required_changes": ["add data-testid to submit"]}]}
    monkeypatch.setattr(nodes, "interrupt", fake_interrupt)
    s = make_initial_state(file_paths=["Plain.tsx"], test_mode="automated")
    s["test_type"] = "agent"
    out = await nodes.node_compat_scan(s)
    assert nodes.route_after_scan({**s, **out}) == "compat_check"


@pytest.mark.asyncio
async def test_compat_scan_fallback_when_no_findings(monkeypatch, tmp_path):
    f = tmp_path / "Plain.tsx"
    f.write_text("export default function P(){return <div/>;}\n", encoding="utf-8")
    def fake_interrupt(p):
        return {}   # agent returned nothing -> ICX fallback
    monkeypatch.setattr(nodes, "interrupt", fake_interrupt)
    s = make_initial_state(file_paths=[str(f)], test_mode="ui")  # test_mode unused; set test_type
    s["test_type"] = "agent"
    out = await nodes.node_compat_scan(s)
    assert isinstance(out["compat_findings"], list) and len(out["compat_findings"]) == 1


@pytest.mark.asyncio
async def test_compat_check_consumes_agent_findings_drop(monkeypatch):
    def fake_interrupt(p):
        assert p["gate"] == "compat_check"
        return {"decision": "reject", "resolution": {"Plain.tsx": "drop"}}
    monkeypatch.setattr(nodes, "interrupt", fake_interrupt)
    s = make_initial_state(file_paths=["Plain.tsx"], test_mode="automated")
    s["test_type"] = "agent"
    s["compat_findings"] = [{"path": "Plain.tsx", "compatible": False, "reasons": ["x"], "required_changes": ["y"]}]
    out = await nodes.node_compat_check(s)
    assert "Plain.tsx" not in out["file_paths"]


@pytest.mark.asyncio
async def test_compat_check_approve_routes_back_to_scan(monkeypatch):
    def fake_interrupt(p):
        return {"decision": "approve"}
    monkeypatch.setattr(nodes, "interrupt", fake_interrupt)
    s = make_initial_state(file_paths=["Plain.tsx"], test_mode="automated")
    s["test_type"] = "agent"
    s["compat_findings"] = [{"path": "Plain.tsx", "compatible": False, "reasons": ["x"], "required_changes": ["y"]}]
    out = await nodes.node_compat_check(s)
    assert out["status"] == "compat_recheck"
    assert nodes.route_after_compat({**s, **out}) == "compat_scan"


@pytest.mark.asyncio
async def test_compat_scan_mandate_forbids_deferring_to_runner(monkeypatch):
    """The gate instruction must ban the 'the tool will cope' rationalization and
    must not hardcode blocker classes - completeness is the agent's own job."""
    captured = {}
    def fake_interrupt(p):
        captured["instruction"] = p.get("instruction", "")
        # ICX must not push a hardcoded rule/checklist or a pre-scan into the gate
        assert "rule" not in p and "suspected_blockers" not in p
        return {"all_compatible": True, "findings": [{"path": "a.tsx", "compatible": True}]}
    monkeypatch.setattr(nodes, "interrupt", fake_interrupt)
    s = make_initial_state(file_paths=["a.tsx"], test_mode="automated")
    s["test_type"] = "agent"
    await nodes.node_compat_scan(s)
    ins = captured["instruction"]
    assert "work around it" in ins and "less robust but fine" in ins
    assert "first principles" in ins
    assert "REPORT, DO NOT DECIDE" in ins
    for stale in ("B1", "B2", "B3", "suspected_blockers"):
        assert stale not in ins


async def test_compat_scan_mandate_requires_repo_wide_undefined_check(monkeypatch):
    # REGRESSION: compat_scan flagged a classic <script src=...> global (loaded via index.html,
    # legitimate no-import pattern) as "undefined" because it only checked the file's own imports.
    captured = {}
    def fake_interrupt(p):
        captured["instruction"] = p.get("instruction", "")
        return {"all_compatible": True, "findings": [{"path": "a.tsx", "compatible": True}]}
    monkeypatch.setattr(nodes, "interrupt", fake_interrupt)
    s = make_initial_state(file_paths=["a.tsx"], test_mode="automated")
    s["test_type"] = "agent"
    await nodes.node_compat_scan(s)
    ins = captured["instruction"]
    assert "index.html" in ins and "script src" in ins
    assert "grep for it" in ins


@pytest.mark.asyncio
async def test_compat_check_accept_keeps_file_in_set(monkeypatch):
    """User can knowingly accept a finding as-is: the file stays in the run, no change."""
    def fake_interrupt(p):
        return {"decision": "reject", "resolution": {"Plain.tsx": "accept"}}
    monkeypatch.setattr(nodes, "interrupt", fake_interrupt)
    s = make_initial_state(file_paths=["Plain.tsx"], test_mode="automated")
    s["test_type"] = "agent"
    s["compat_findings"] = [{"path": "Plain.tsx", "compatible": False, "reasons": ["x"], "required_changes": ["y"]}]
    out = await nodes.node_compat_check(s)
    assert "Plain.tsx" in out["file_paths"]
    assert out["compat_resolution"]["Plain.tsx"] == "accepted"


# ---------------------------------------------------------------------------
# node_profile_push -> agent-generate (2-interrupt flow)
# ---------------------------------------------------------------------------











# ---------------------------------------------------------------------------
# expand_scan agent-grep gate
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_expand_gate_message_states_confirmed_files_persist(monkeypatch):
    # REGRESSION: an agent excluded files at the expand gate via free text (no confirmed_files key)
    # and they silently reappeared at analyze_screen/compat_scan - the gate message must state the
    # persistence contract explicitly so the agent knows confirmed_files is what sticks.
    monkeypatch.setattr(nodes, "_load_querier", lambda paths: None)
    monkeypatch.setattr(nodes, "_expand_files_via_graph", lambda seeds, q, context=None: list(seeds))
    captured = {}
    def fake_interrupt(p):
        if p["gate"] == "expand":
            captured["message"] = p["message"]
        return {"confirmed_files": p.get("selected_files", [])} if p["gate"] == "expand" else {}
    monkeypatch.setattr(nodes, "interrupt", fake_interrupt)
    s = make_initial_state(file_paths=["Button.tsx"], test_mode="automated")
    await nodes.node_expand_files(s)
    msg = captured["message"]
    assert "confirmed_files" in msg
    assert "excluded for every later gate" in msg or "rest of this session" in msg


# ---------------------------------------------------------------------------
# node_known_screen_check: known-screen fast path
# ---------------------------------------------------------------------------

def _patch_screen_cache_store(monkeypatch, tmp_path):
    from icx_engine.testing import screen_cache as sc
    monkeypatch.setattr(sc, "store_path", lambda: tmp_path / "screens.json")
    return sc


@pytest.mark.asyncio
async def test_known_screen_check_no_cache_falls_through_silently(monkeypatch, tmp_path):
    _patch_screen_cache_store(monkeypatch, tmp_path)
    monkeypatch.setattr(nodes, "_resolve_project_id", lambda seeds: "proj1")

    def boom(p):
        raise AssertionError("must not interrupt on a cache miss")
    monkeypatch.setattr(nodes, "interrupt", boom)

    s = make_initial_state(file_paths=["Users.jsx"], test_mode="automated")
    s["test_type"] = "agent"
    out = await nodes.node_known_screen_check(s)
    assert out == {"known_screen_available": False}
    assert nodes.route_after_known_screen_check({**s, **out}) == "expand_files"


@pytest.mark.asyncio
async def test_known_screen_check_stale_file_falls_through_without_asking(monkeypatch, tmp_path):
    sc = _patch_screen_cache_store(monkeypatch, tmp_path)
    monkeypatch.setattr(nodes, "_resolve_project_id", lambda seeds: "proj1")
    f = tmp_path / "Users.jsx"
    f.write_text("original", encoding="utf-8")
    sc.save_screen("proj1", [str(f)], test_type="agent", url="http://x/users",
                   all_candidates=[str(f)], confirmed_files=[str(f)],
                   screen_model={"functionalities": []}, census_coverage=1.0,
                   analyzer_id="react", analyzer_family="ui")
    f.write_text("edited since caching", encoding="utf-8")   # the cached file changed

    def boom(p):
        raise AssertionError("a stale cache must never be offered as a choice")
    monkeypatch.setattr(nodes, "interrupt", boom)

    s = make_initial_state(file_paths=[str(f)], test_mode="automated")
    s["test_type"] = "agent"
    out = await nodes.node_known_screen_check(s)
    assert out == {"known_screen_available": False}


@pytest.mark.asyncio
async def test_known_screen_check_new_candidate_falls_through_without_asking(monkeypatch, tmp_path):
    # a file that used to not exist/be discoverable now appears in re-discovery -> treat as stale,
    # even though the cached files themselves are unchanged.
    sc = _patch_screen_cache_store(monkeypatch, tmp_path)
    monkeypatch.setattr(nodes, "_resolve_project_id", lambda seeds: "proj1")
    f = tmp_path / "Users.jsx"
    f.write_text("content", encoding="utf-8")
    sc.save_screen("proj1", [str(f)], test_type="agent", url=None,
                   all_candidates=[str(f)], confirmed_files=[str(f)],
                   screen_model={"functionalities": []}, census_coverage=1.0,
                   analyzer_id="react", analyzer_family="ui")
    new_file = str(tmp_path / "NewComponent.jsx")
    monkeypatch.setattr(nodes, "_deterministic_candidates", lambda seeds: {str(f), new_file})

    def boom(p):
        raise AssertionError("a newly-discovered candidate must force a rescan, no gate shown")
    monkeypatch.setattr(nodes, "interrupt", boom)

    s = make_initial_state(file_paths=[str(f)], test_mode="automated")
    s["test_type"] = "agent"
    out = await nodes.node_known_screen_check(s)
    assert out == {"known_screen_available": False}


@pytest.mark.asyncio
async def test_known_screen_check_fresh_offers_fast_path(monkeypatch, tmp_path):
    sc = _patch_screen_cache_store(monkeypatch, tmp_path)
    monkeypatch.setattr(nodes, "_resolve_project_id", lambda seeds: "proj1")
    f = tmp_path / "Users.jsx"
    f.write_text("content", encoding="utf-8")
    model = {"functionalities": [{"functionality": "Create"}]}
    sc.save_screen("proj1", [str(f)], test_type="agent", url="http://x/users",
                   all_candidates=[str(f)], confirmed_files=[str(f)],
                   screen_model=model, census_coverage=0.9,
                   analyzer_id="react", analyzer_family="ui",
                   compat_resolution={str(f): "accepted"})
    monkeypatch.setattr(nodes, "_deterministic_candidates", lambda seeds: {str(f)})

    captured = {}
    def fake_interrupt(p):
        captured["payload"] = p
        return {"decision": "fast_path"}
    monkeypatch.setattr(nodes, "interrupt", fake_interrupt)

    s = make_initial_state(file_paths=[str(f)], test_mode="automated")
    s["test_type"] = "agent"
    out = await nodes.node_known_screen_check(s)
    assert captured["payload"]["gate"] == "known_screen"
    assert out["known_screen_available"] is True
    assert out["file_paths"] == [str(f)]
    assert out["screen_model"] == model
    assert out["url"] == "http://x/users"
    assert out["compat_resolution"] == {str(f): "accepted"}
    assert nodes.route_after_known_screen_check({**s, **out}) == "config_gate"


@pytest.mark.asyncio
async def test_known_screen_check_fresh_but_user_declines_fast_path(monkeypatch, tmp_path):
    sc = _patch_screen_cache_store(monkeypatch, tmp_path)
    monkeypatch.setattr(nodes, "_resolve_project_id", lambda seeds: "proj1")
    f = tmp_path / "Users.jsx"
    f.write_text("content", encoding="utf-8")
    sc.save_screen("proj1", [str(f)], test_type="agent", url=None,
                   all_candidates=[str(f)], confirmed_files=[str(f)],
                   screen_model={"functionalities": []}, census_coverage=1.0,
                   analyzer_id="react", analyzer_family="ui")
    monkeypatch.setattr(nodes, "_deterministic_candidates", lambda seeds: {str(f)})
    monkeypatch.setattr(nodes, "interrupt", lambda p: {"decision": "rescan"})

    s = make_initial_state(file_paths=[str(f)], test_mode="automated")
    s["test_type"] = "agent"
    out = await nodes.node_known_screen_check(s)
    assert out == {"known_screen_available": False}
    assert nodes.route_after_known_screen_check({**s, **out}) == "expand_files"


@pytest.mark.asyncio
async def test_config_gate_saves_known_screen_cache(monkeypatch, tmp_path):
    sc = _patch_screen_cache_store(monkeypatch, tmp_path)
    monkeypatch.setattr(nodes, "_resolve_project_id", lambda seeds: "proj1")
    f = tmp_path / "Users.jsx"
    f.write_text("content", encoding="utf-8")
    monkeypatch.setattr(nodes, "interrupt", lambda p: {})   # user accepts gate 3
    s = make_initial_state(file_paths=[str(f)], test_mode="automated")
    s["test_type"] = "agent"
    s["url"] = "http://x/users"
    s["original_seeds"] = [str(f)]
    s["all_candidate_files"] = [str(f)]
    s["screen_model"] = {"functionalities": [{"functionality": "Create"}]}
    s["census_coverage"] = 0.85
    s["analyzer_id"] = "react"
    s["analyzer_family"] = "ui"

    await nodes.node_config_gate(s)

    entry = sc.load_screen("proj1", [str(f)])
    assert entry is not None
    assert entry.confirmed_files == [str(f)]
    assert entry.screen_model == {"functionalities": [{"functionality": "Create"}]}
    assert entry.census_coverage == 0.85


@pytest.mark.asyncio
async def test_config_gate_does_not_cache_non_agent_type(monkeypatch, tmp_path):
    sc = _patch_screen_cache_store(monkeypatch, tmp_path)
    monkeypatch.setattr(nodes, "_resolve_project_id", lambda seeds: "proj1")
    monkeypatch.setattr(nodes, "interrupt", lambda p: {})
    s = make_initial_state(file_paths=["a.py"], test_mode="automated")
    s["test_type"] = "unit"
    await nodes.node_config_gate(s)
    assert sc.load_screen("proj1", ["a.py"]) is None


@pytest.mark.asyncio
async def test_expand_uses_agent_grep(monkeypatch):
    monkeypatch.setattr(nodes, "_load_querier", lambda paths: None)
    monkeypatch.setattr(nodes, "_expand_files_via_graph", lambda seeds, q, context=None: list(seeds))
    # ICX grep must NOT be called when the agent supplies related_files
    def boom(*a, **k):
        raise AssertionError("expand_via_grep should not run when agent returns related_files")
    monkeypatch.setattr(nodes, "expand_via_grep", boom)

    gates = []
    def fake_interrupt(p):
        gates.append(p["gate"])
        if p["gate"] == "expand_scan":
            return {"related_files": ["Form.tsx"]}
        return {"confirmed_files": p["selected_files"]}
    monkeypatch.setattr(nodes, "interrupt", fake_interrupt)

    s = make_initial_state(file_paths=["Button.tsx"], test_mode="automated")
    s["test_type"] = "agent"
    out = await nodes.node_expand_files(s)
    assert "expand_scan" in gates and "expand" in gates
    assert out["file_sources"].get("Form.tsx") in ("grep", "both")  # agent grep result ranked


@pytest.mark.asyncio
async def test_expand_falls_back_to_icx_grep(monkeypatch, tmp_path):
    monkeypatch.setattr(nodes, "_load_querier", lambda paths: None)
    monkeypatch.setattr(nodes, "_expand_files_via_graph", lambda seeds, q, context=None: list(seeds))
    called = {"icx_grep": False}
    def fake_grep(seeds, root):
        called["icx_grep"] = True
        return ["IcxFound.tsx"]
    monkeypatch.setattr(nodes, "expand_via_grep", fake_grep)
    monkeypatch.setattr(nodes, "_project_root", lambda seeds: tmp_path)

    def fake_interrupt(p):
        if p["gate"] == "expand_scan":
            return {}   # agent returned nothing -> ICX fallback
        return {"confirmed_files": p["selected_files"]}
    monkeypatch.setattr(nodes, "interrupt", fake_interrupt)

    s = make_initial_state(file_paths=["Button.tsx"], test_mode="automated")
    s["test_type"] = "agent"
    out = await nodes.node_expand_files(s)
    assert called["icx_grep"] is True
    assert "IcxFound.tsx" in out["file_sources"]


# ---------------------------------------------------------------------------
# : re-read mandate + read_receipts
# ---------------------------------------------------------------------------

def test_reread_mandate_constant():
    m = nodes._REREAD_MANDATE.lower()
    assert "read" in m and ("stale" in m or "memory" in m)
    assert "read_receipts" in nodes._REREAD_MANDATE


@pytest.mark.asyncio
async def test_compat_scan_carries_mandate_and_records_receipts(monkeypatch):
    captured = {}
    def fake_interrupt(p):
        captured["instruction"] = p.get("instruction", "")
        return {"all_compatible": True,
                "findings": [{"path": "a.tsx", "compatible": True, "reasons": [], "required_changes": []}],
                "read_receipts": [{"path": "a.tsx", "line_count": 10, "last_line": "}"}]}
    monkeypatch.setattr(nodes, "interrupt", fake_interrupt)
    s = make_initial_state(file_paths=["a.tsx"], test_mode="automated")
    s["test_type"] = "agent"
    out = await nodes.node_compat_scan(s)
    assert "read_receipts" in captured["instruction"]
    assert nodes._REREAD_MANDATE.split(".")[0].lower()[:10] in captured["instruction"].lower()
    recs = out["read_receipts"]
    assert any(r.get("gate") == "compat_scan" and r["receipts"][0]["path"] == "a.tsx" for r in recs)




# -- project_id auth key + reuse robustness + profile file option --

def test_resolve_project_id_uses_project_id_then_hash_fallback(monkeypatch, tmp_path):
    from icx_engine.testing import nodes as _n
    # graph project found -> its project_id (not name)
    class _Info:
        name = "MyProject"
        path = str(tmp_path)
        project_id = "pid-abc123"
    import icx_engine.graph.storage as _st
    monkeypatch.setattr(_st, "lookup_for_file", lambda p: _Info())
    assert _n._resolve_project_id(["a.tsx"]) == "pid-abc123"
    # no graph project -> stable path-hash fallback (never None for real files)
    monkeypatch.setattr(_st, "lookup_for_file", lambda p: None)
    f = tmp_path / "x.tsx"; f.write_text("x", encoding="utf-8")
    pid = _n._resolve_project_id([str(f)])
    assert pid is not None and pid.startswith("path:")


@pytest.mark.asyncio
async def test_auth_gate_keys_on_project_id(monkeypatch, tmp_path):
    from icx_engine.testing import auth as _auth
    store = tmp_path / "auth.json"
    monkeypatch.setattr(_auth, "store_path", lambda: store)
    class _Info:
        name = "HumanName"
        path = str(tmp_path); project_id = "pid-xyz"
    import icx_engine.graph.storage as _st
    monkeypatch.setattr(_st, "lookup_for_file", lambda p: _Info())
    monkeypatch.setattr(nodes, "interrupt", lambda p: {"auth_mode": "capture", "session_id": "cap-9"})
    s = make_initial_state(file_paths=["a.tsx"], test_mode="automated")
    s["test_type"] = "agent"; s["url"] = "http://host-z/app"
    out = await nodes.node_auth_gate(s)
    assert out["project"] == "pid-xyz"                 # the id, not "HumanName"
    assert out["auth_ref"] == "pid-xyz::host-z"
    assert _auth.load_session("pid-xyz", "host-z", store=store).session_id == "cap-9"


# -- dev-server port-drift: surface + reuse a session from a different port ---------

def _fake_valid_storage_state(tmp_path, name="state.json"):
    p = tmp_path / name
    p.write_text('{"cookies": [{"name": "sid", "value": "x"}]}', encoding="utf-8")
    return str(p)


@pytest.mark.asyncio
async def test_auth_gate_no_hint_when_exact_host_matches(monkeypatch, tmp_path):
    from icx_engine.testing import auth as _auth
    store = tmp_path / "auth.json"
    monkeypatch.setattr(_auth, "store_path", lambda: store)
    monkeypatch.setattr(nodes, "_resolve_project_id", lambda seeds: "proj1")
    ss = _fake_valid_storage_state(tmp_path)
    _auth.save_session("proj1", "localhost:3000", "s1", store=store, storage_state=ss)

    captured = {}
    def fake_interrupt(p):
        captured["payload"] = p
        return {"auth_mode": "reuse"}
    monkeypatch.setattr(nodes, "interrupt", fake_interrupt)

    s = make_initial_state(file_paths=["a.tsx"], test_mode="automated")
    s["test_type"] = "agent"; s["url"] = "http://localhost:3000/app"
    await nodes.node_auth_gate(s)
    assert captured["payload"]["other_host_sessions"] == []
    assert "port change" not in captured["payload"]["message"]


@pytest.mark.asyncio
async def test_auth_gate_surfaces_port_drift_session(monkeypatch, tmp_path):
    from icx_engine.testing import auth as _auth
    store = tmp_path / "auth.json"
    monkeypatch.setattr(_auth, "store_path", lambda: store)
    monkeypatch.setattr(nodes, "_resolve_project_id", lambda seeds: "proj1")
    ss = _fake_valid_storage_state(tmp_path)
    _auth.save_session("proj1", "localhost:3000", "s1", store=store, storage_state=ss)

    captured = {}
    def fake_interrupt(p):
        captured["payload"] = p
        return {"auth_mode": "public"}
    monkeypatch.setattr(nodes, "interrupt", fake_interrupt)

    s = make_initial_state(file_paths=["a.tsx"], test_mode="automated")
    s["test_type"] = "agent"; s["url"] = "http://localhost:3001/app"   # port changed since capture
    await nodes.node_auth_gate(s)
    assert captured["payload"]["other_host_sessions"] == [
        {"host": "localhost:3000", "captured_at": captured["payload"]["other_host_sessions"][0]["captured_at"],
         "expires_at": captured["payload"]["other_host_sessions"][0]["expires_at"]}]
    assert "port change" in captured["payload"]["message"]
    assert "localStorage" in captured["payload"]["message"]


@pytest.mark.asyncio
async def test_auth_gate_does_not_offer_different_hostname(monkeypatch, tmp_path):
    from icx_engine.testing import auth as _auth
    store = tmp_path / "auth.json"
    monkeypatch.setattr(_auth, "store_path", lambda: store)
    monkeypatch.setattr(nodes, "_resolve_project_id", lambda seeds: "proj1")
    ss = _fake_valid_storage_state(tmp_path)
    _auth.save_session("proj1", "otherhost:3000", "s1", store=store, storage_state=ss)

    captured = {}
    monkeypatch.setattr(nodes, "interrupt", lambda p: captured.update(payload=p) or {"auth_mode": "public"})
    s = make_initial_state(file_paths=["a.tsx"], test_mode="automated")
    s["test_type"] = "agent"; s["url"] = "http://localhost:3001/app"
    await nodes.node_auth_gate(s)
    assert captured["payload"]["other_host_sessions"] == []


@pytest.mark.asyncio
async def test_auth_gate_reuse_host_aliases_session_under_current_host(monkeypatch, tmp_path):
    from icx_engine.testing import auth as _auth
    store = tmp_path / "auth.json"
    monkeypatch.setattr(_auth, "store_path", lambda: store)
    monkeypatch.setattr(nodes, "_resolve_project_id", lambda seeds: "proj1")
    ss = _fake_valid_storage_state(tmp_path)
    _auth.save_session("proj1", "localhost:3000", "s1", store=store, storage_state=ss)
    monkeypatch.setattr(nodes, "interrupt",
                        lambda p: {"auth_mode": "reuse", "reuse_host": "localhost:3000"})

    s = make_initial_state(file_paths=["a.tsx"], test_mode="automated")
    s["test_type"] = "agent"; s["url"] = "http://localhost:3001/app"
    out = await nodes.node_auth_gate(s)
    assert out["auth_mode"] == "reuse"
    aliased = _auth.load_session("proj1", "localhost:3001", store=store)
    assert aliased is not None and aliased.storage_state == ss


@pytest.mark.asyncio
async def test_auth_gate_reuse_host_invalid_falls_back_to_public(monkeypatch, tmp_path):
    from icx_engine.testing import auth as _auth
    store = tmp_path / "auth.json"
    monkeypatch.setattr(_auth, "store_path", lambda: store)
    monkeypatch.setattr(nodes, "_resolve_project_id", lambda seeds: "proj1")
    monkeypatch.setattr(nodes, "interrupt",
                        lambda p: {"auth_mode": "reuse", "reuse_host": "localhost:9999"})

    s = make_initial_state(file_paths=["a.tsx"], test_mode="automated")
    s["test_type"] = "agent"; s["url"] = "http://localhost:3001/app"
    out = await nodes.node_auth_gate(s)
    assert out["auth_mode"] == "public"
    assert out["auto_auth_recover"] is False


# -- runtime resolver memoization: one resolution per (lang) per run -----------------

async def test_runtime_resolver_memoizes_per_lang(monkeypatch):
    calls = {"n": 0}

    def _fake_resolve(key, repo):
        calls["n"] += 1
        return type("R", (), {"status": "resolved", "path": "/opt/py"})()

    monkeypatch.setattr("icx_engine.runtime_manager.resolve_runtime", _fake_resolve)
    resolver = await nodes._runtime_resolver("/repo")
    a = await resolver("python")
    b = await resolver("python")          # same lang -> cached, no second probe
    c = await resolver("typescript")      # different lang -> one more probe
    assert a == b == "/opt/py"
    assert c == "/opt/py"
    assert calls["n"] == 2                # python(1) + node-alias(1), python reused


async def test_runtime_resolver_ui_uses_harness_node(monkeypatch):
    # ui/agent must resolve the modern HARNESS node, never the project's node.
    monkeypatch.setattr("icx_engine.runtime_manager.resolve_harness_node", lambda: "/opt/node20")

    def _no_project(key, repo):
        raise AssertionError("ui/agent must not use the project runtime resolver")

    monkeypatch.setattr("icx_engine.runtime_manager.resolve_runtime", _no_project)
    resolver = await nodes._runtime_resolver("/repo")
    assert await resolver("ui") == "/opt/node20"
    assert await resolver("agent") == "/opt/node20"



