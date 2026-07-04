from icx_engine.models.config import AppConfig


def test_magik_base_url_default():
    cfg = AppConfig()
    assert cfg.magik_base_url == "http://localhost:7646"


def test_magik_api_key_default_none():
    cfg = AppConfig()
    assert cfg.magik_api_key is None


def test_magik_max_iterations_default():
    cfg = AppConfig()
    assert cfg.magik_max_iterations == 3


def test_magik_api_key_excluded_from_serialization():
    cfg = AppConfig(magik_api_key="secret-key-123")
    serialized = cfg.model_dump()
    assert "magik_api_key" not in serialized or serialized.get("magik_api_key") is None


def test_magik_fields_accept_values():
    cfg = AppConfig(
        magik_base_url="http://localhost:3000",
        magik_api_key="mykey",
        magik_max_iterations=5,
    )
    assert cfg.magik_base_url == "http://localhost:3000"
    assert cfg.magik_api_key == "mykey"
    assert cfg.magik_max_iterations == 5


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
    assert state["agent_provider"] == "openai"
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
        "test_type", "agent_provider", "headless", "scope", "merge_files",
        "url", "profile_screen", "run_id",
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
    assert s["profile_pushed"] is False


def test_initial_state_has_coverage_fields():
    s = make_initial_state(file_paths=["a.tsx"], test_mode="automated")
    assert s["full_report"] is None


def test_initial_state_has_funnel_fields():
    s = make_initial_state(file_paths=["a.tsx"], test_mode="automated")
    assert s["compat_findings"] == []
    assert s["profile_markdown"] is None


def test_initial_state_has_read_receipts():
    s = make_initial_state(file_paths=["a.tsx"], test_mode="automated")
    assert s["read_receipts"] == []
