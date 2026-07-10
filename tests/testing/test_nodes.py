from __future__ import annotations
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
# node_config_gate (Gate 3) - URL-required validation, agent_max_steps
# clamping, and headless parsing.
# ---------------------------------------------------------------------------

async def test_node_config_gate_ui_without_url_raises():
    from icx_engine.testing.nodes import node_config_gate
    state = _base_state(url=None)
    with patch("icx_engine.testing.nodes.interrupt", return_value={"test_type": "ui"}):
        with pytest.raises(ValueError, match="requires a URL"):
            await node_config_gate(state)


async def test_node_config_gate_agent_without_url_raises():
    from icx_engine.testing.nodes import node_config_gate
    state = _base_state(url=None)
    with patch("icx_engine.testing.nodes.interrupt", return_value={"test_type": "agent"}):
        with pytest.raises(ValueError, match="requires a URL"):
            await node_config_gate(state)


async def test_node_config_gate_agent_max_steps_clamps_high(monkeypatch):
    from icx_engine.testing.nodes import node_config_gate
    from icx_engine.models.config import AppConfig
    fake_cfg = AppConfig()
    fake_cfg.magik_agent_step_cap = 60
    fake_cfg.magik_agent_max_steps = 50
    class _CM:
        @staticmethod
        def load(): return fake_cfg
    import icx_engine.config_manager as _cm_mod
    monkeypatch.setattr(_cm_mod, "ConfigManager", _CM)
    state = _base_state()
    state["test_type"] = "api"
    with patch("icx_engine.testing.nodes.interrupt",
               return_value={"agent_max_steps": 999}):
        result = await node_config_gate(state)
    assert result["agent_max_steps"] == 60


async def test_node_config_gate_agent_max_steps_clamps_low(monkeypatch):
    from icx_engine.testing.nodes import node_config_gate
    from icx_engine.models.config import AppConfig
    fake_cfg = AppConfig()
    fake_cfg.magik_agent_step_cap = 60
    fake_cfg.magik_agent_max_steps = 50
    class _CM:
        @staticmethod
        def load(): return fake_cfg
    import icx_engine.config_manager as _cm_mod
    monkeypatch.setattr(_cm_mod, "ConfigManager", _CM)
    state = _base_state()
    state["test_type"] = "api"
    with patch("icx_engine.testing.nodes.interrupt",
               return_value={"agent_max_steps": 0}):
        result = await node_config_gate(state)
    assert result["agent_max_steps"] == 1


async def test_node_config_gate_agent_max_steps_non_int_falls_back(monkeypatch):
    from icx_engine.testing.nodes import node_config_gate
    from icx_engine.models.config import AppConfig
    fake_cfg = AppConfig()
    fake_cfg.magik_agent_step_cap = 60
    fake_cfg.magik_agent_max_steps = 50
    class _CM:
        @staticmethod
        def load(): return fake_cfg
    import icx_engine.config_manager as _cm_mod
    monkeypatch.setattr(_cm_mod, "ConfigManager", _CM)
    state = _base_state(agent_max_steps=42)
    state["test_type"] = "api"
    with patch("icx_engine.testing.nodes.interrupt",
               return_value={"agent_max_steps": "abc"}):
        result = await node_config_gate(state)
    assert result["agent_max_steps"] == 42


async def test_node_config_gate_headless_no_parses_false():
    from icx_engine.testing.nodes import node_config_gate
    state = _base_state()
    state["test_type"] = "api"
    with patch("icx_engine.testing.nodes.interrupt",
               return_value={"headless": "2"}):
        result = await node_config_gate(state)
    assert result["headless"] is False


import pytest
from icx_engine.testing.state import make_initial_state
from icx_engine.testing import nodes


@pytest.mark.asyncio
async def test_pick_type_sets_test_type(monkeypatch):
    captured = {}

    def fake_interrupt(payload):
        captured["payload"] = payload
        return {"test_type": "2"}   # numbered -> ui

    monkeypatch.setattr(nodes, "interrupt", fake_interrupt)
    out = await nodes.node_pick_type(make_initial_state(file_paths=["a.tsx"], test_mode="automated"))
    assert out["test_type"] == "ui"
    assert captured["payload"]["gate"] == "pick_type"


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
    s["test_type"] = "ui"
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
    s["test_type"] = "ui"
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
    s["test_type"] = "ui"
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
    s["test_type"] = "ui"
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
    s["test_type"] = "ui"
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
    s["test_type"] = "ui"
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
    s["test_type"] = "ui"; s["url"] = "http://host-x/app"
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
    s["test_type"] = "ui"; s["url"] = "http://host-x/app"; s["project"] = "proj-a"
    monkeypatch.setattr(nodes, "interrupt", lambda p: {"auth_mode": "capture", "session_id": "cap-1"})
    out = await nodes.node_auth_gate(s)
    assert out["auth_mode"] == "capture"
    assert _auth.load_session("proj-a", "host-x", store=store).session_id == "cap-1"
    assert "session_id" not in out and "_auth_session_id" not in out


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
    s["test_type"] = "ui"
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
    s["test_type"] = "ui"
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
    s["test_type"] = "ui"
    out = await nodes.node_compat_scan(s)
    assert isinstance(out["compat_findings"], list) and len(out["compat_findings"]) == 1


@pytest.mark.asyncio
async def test_compat_check_consumes_agent_findings_drop(monkeypatch):
    def fake_interrupt(p):
        assert p["gate"] == "compat_check"
        return {"decision": "reject", "resolution": {"Plain.tsx": "drop"}}
    monkeypatch.setattr(nodes, "interrupt", fake_interrupt)
    s = make_initial_state(file_paths=["Plain.tsx"], test_mode="automated")
    s["test_type"] = "ui"
    s["compat_findings"] = [{"path": "Plain.tsx", "compatible": False, "reasons": ["x"], "required_changes": ["y"]}]
    out = await nodes.node_compat_check(s)
    assert "Plain.tsx" not in out["file_paths"]


@pytest.mark.asyncio
async def test_compat_check_approve_routes_back_to_scan(monkeypatch):
    def fake_interrupt(p):
        return {"decision": "approve"}
    monkeypatch.setattr(nodes, "interrupt", fake_interrupt)
    s = make_initial_state(file_paths=["Plain.tsx"], test_mode="automated")
    s["test_type"] = "ui"
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
    s["test_type"] = "ui"
    await nodes.node_compat_scan(s)
    ins = captured["instruction"]
    assert "work around it" in ins and "less robust but fine" in ins
    assert "first principles" in ins
    assert "REPORT, DO NOT DECIDE" in ins
    for stale in ("B1", "B2", "B3", "suspected_blockers"):
        assert stale not in ins


@pytest.mark.asyncio
async def test_compat_check_accept_keeps_file_in_set(monkeypatch):
    """User can knowingly accept a finding as-is: the file stays in the run, no change."""
    def fake_interrupt(p):
        return {"decision": "reject", "resolution": {"Plain.tsx": "accept"}}
    monkeypatch.setattr(nodes, "interrupt", fake_interrupt)
    s = make_initial_state(file_paths=["Plain.tsx"], test_mode="automated")
    s["test_type"] = "ui"
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
    s["test_type"] = "ui"
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
    s["test_type"] = "ui"
    out = await nodes.node_expand_files(s)
    assert called["icx_grep"] is True
    assert "IcxFound.tsx" in out["file_sources"]


# ---------------------------------------------------------------------------
# configurable agent step cap in node_config_gate
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_config_gate_clamps_to_configured_cap(monkeypatch):
    from icx_engine.models.config import AppConfig
    fake_cfg = AppConfig()
    fake_cfg.magik_agent_step_cap = 40   # custom cap
    class _CM:
        @staticmethod
        def load(): return fake_cfg
    import icx_engine.config_manager as _cm_mod
    monkeypatch.setattr(_cm_mod, "ConfigManager", _CM)
    monkeypatch.setattr(nodes, "interrupt", lambda p: {"agent_max_steps": 999})
    s = make_initial_state(file_paths=["a.tsx"], test_mode="automated")
    s["test_type"] = "api"   # api skips URL requirement
    out = await nodes.node_config_gate(s)
    assert out["agent_max_steps"] == 40   # clamped to configured cap, not 60


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
    s["test_type"] = "ui"
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
    s["test_type"] = "ui"; s["url"] = "http://host-z/app"
    out = await nodes.node_auth_gate(s)
    assert out["project"] == "pid-xyz"                 # the id, not "HumanName"
    assert out["auth_ref"] == "pid-xyz::host-z"
    assert _auth.load_session("pid-xyz", "host-z", store=store).session_id == "cap-9"






