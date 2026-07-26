from icx_engine.testing.classify import FileClass
from icx_engine.testing.compat import check_compat, build_report, CompatVerdict


def _frontend(ok_selector=True, route=True):
    return FileClass(
        path="src/pages/CreateUser.tsx", layer="frontend", role="container",
        artifacts=["component", "route"] if route else ["component"],
        testability={"renderable": True, "has_stable_selector": ok_selector, "has_route": route,
                     "exposes_endpoint": False, "has_request_schema": False},
    )


def _backend(schema=True):
    return FileClass(
        path="src/UserController.java", layer="backend", role="controller",
        artifacts=["endpoint", "controller"],
        testability={"exposes_endpoint": True, "has_request_schema": schema,
                     "renderable": False, "has_stable_selector": False, "has_route": False},
    )


def test_agent_mode_frontend_ready_is_compatible():
    v = check_compat(_frontend(), "agent")
    assert v.compatible is True
    assert v.required_changes == []


def test_agent_mode_frontend_missing_selector_needs_change():
    v = check_compat(_frontend(ok_selector=False), "agent")
    assert v.compatible is False
    assert any("data-testid" in c for c in v.required_changes)


def test_agent_mode_backend_file_off_type():
    v = check_compat(_backend(), "agent")
    assert v.compatible is False
    assert any("backend" in r.lower() for r in v.reasons)


def test_api_mode_backend_with_schema_compatible():
    v = check_compat(_backend(schema=True), "api")
    assert v.compatible is True


def test_api_mode_missing_schema_needs_change():
    v = check_compat(_backend(schema=False), "api")
    assert v.compatible is False
    assert any("schema" in c.lower() for c in v.required_changes)


def test_api_mode_frontend_file_off_type():
    v = check_compat(_frontend(), "api")
    assert v.compatible is False
    assert any("frontend" in r.lower() for r in v.reasons)


def test_build_report_one_verdict_per_file():
    report = build_report([_frontend(), _backend()], "agent")
    assert len(report) == 2
    assert all(isinstance(v, CompatVerdict) for v in report)
