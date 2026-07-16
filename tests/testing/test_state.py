from icx_engine.models.config import AppConfig


def test_test_max_iterations_default():
    cfg = AppConfig()
    assert cfg.test_max_iterations == 3


def test_testing_fields_accept_values():
    cfg = AppConfig(test_max_iterations=5)
    assert cfg.test_max_iterations == 5


def test_config_clamps_out_of_range_testing_knobs():
    # Hand-edited config.json must never crash load or drive an unbounded loop - values are clamped.
    assert AppConfig(test_max_iterations=0).test_max_iterations == 1
    assert AppConfig(test_max_iterations=99999).test_max_iterations == 100
    # in-range values pass through untouched
    assert AppConfig(test_max_iterations=7).test_max_iterations == 7


def test_legacy_keys_ignored_on_construct():
    # Old keys (magik_*, retired agent_max_steps) must not crash load; pydantic ignores unknown.
    cfg = AppConfig(magik_base_url="http://x", magik_api_key="k", agent_max_steps=99)
    assert not hasattr(cfg, "magik_base_url")
    assert not hasattr(cfg, "agent_max_steps")


# TestingState tests
from icx_engine.testing.state import TestingState, make_initial_state


def test_make_initial_state_defaults():
    state = make_initial_state(
        file_paths=["src/auth/login.py"],
        context="Fix login bug",
        max_iterations=3,
    )
    assert state["file_paths"] == ["src/auth/login.py"]
    assert state["context"] == "Fix login bug"
    assert state["iteration"] == 0
    assert state["max_iterations"] == 3
    assert state["issues"] == []
    assert state["fix_log"] == []
    assert state["status"] == "pending"
    assert state["test_type"] is None
    assert state["headless"] is True
    assert state["scope"] == "ticket"
    assert state["merge_files"] is False  # single file
    assert state["run_id"] is None
    assert state["last_error"] is None
    assert state["detection_mode"] is None
    assert state["json_spec"] is None


def test_make_initial_state_multi_file_sets_merge_true():
    state = make_initial_state(file_paths=["a.py", "b.py"], context=None)
    assert state["merge_files"] is True


def test_make_initial_state_max_iterations_none_uses_default():
    state = make_initial_state(file_paths=["x.py"], context=None, max_iterations=None)
    assert state["max_iterations"] == 3


def test_make_initial_state_test_mode_none_by_default():
    state = make_initial_state(file_paths=["a.py"], context=None)
    assert state["test_mode"] is None


def test_make_initial_state_test_mode_automated():
    state = make_initial_state(file_paths=["a.py"], context=None, test_mode="automated")
    assert state["test_mode"] == "automated"


def test_make_initial_state_test_mode_manual():
    state = make_initial_state(file_paths=["a.py"], context=None, test_mode="manual")
    assert state["test_mode"] == "manual"


def test_make_initial_state_manual_result_none():
    state = make_initial_state(file_paths=["a.py"], context=None)
    assert state["manual_result"] is None


def test_testing_state_is_total_dict():
    from typing import get_type_hints
    hints = get_type_hints(TestingState)
    required_keys = {
        "file_paths", "context", "detection_mode", "json_spec",
        "test_type", "headless", "scope", "merge_files",
        "url", "run_id",
        "test_mode", "manual_result",
        "iteration", "max_iterations", "issues", "fix_log", "status", "last_error",
    }
    assert required_keys.issubset(set(hints.keys()))


def test_initial_state_has_compat_fields():
    s = make_initial_state(file_paths=["a.tsx"], test_mode="automated")
    assert s["test_type"] is None          # never auto-picked
    assert s["classified"] == []
    assert s["file_sources"] == {}
    assert s["compat_iteration"] == 0
    assert s["max_compat_iterations"] == 3
    assert s["compat_resolution"] == {}
    assert s["edited_files"] == []


def test_initial_state_has_auth_fields():
    s = make_initial_state(file_paths=["a.tsx"], test_mode="automated")
    assert s["auth_mode"] is None
    assert s["auth_ref"] is None
    assert s["project"] is None
    assert s["host"] is None
    assert s["auto_auth_recover"] is True


def test_initial_state_has_coverage_fields():
    s = make_initial_state(file_paths=["a.tsx"], test_mode="automated")
    assert s["full_report"] is None


def test_initial_state_has_funnel_fields():
    s = make_initial_state(file_paths=["a.tsx"], test_mode="automated")
    assert s["compat_findings"] == []


def test_config_gate_visible_option_sets_headless(monkeypatch):
    import icx_engine.testing.nodes as nodes
    s = make_initial_state(file_paths=["a.jsx"], test_mode="automated")
    s["test_type"] = "ui"
    s["url"] = "http://x/login"
    monkeypatch.setattr(nodes, "interrupt", lambda p: {"visible": True})
    import asyncio as _a
    out = _a.run(nodes.node_config_gate(s))
    assert out["headless"] is False        # visible:true -> headed replay


def test_initial_state_has_read_receipts():
    s = make_initial_state(file_paths=["a.tsx"], test_mode="automated")
    assert s["read_receipts"] == []
