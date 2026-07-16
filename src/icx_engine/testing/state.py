from __future__ import annotations

from typing import Any, TypedDict

_DEFAULT_MAX_ITERATIONS = 3


class TestingState(TypedDict):
    # Input
    file_paths: list[str]
    context: str | None

    # Detection (Gate 2)
    detection_mode: str | None          # "auto_detect" | "json_spec"
    url: str | None                     # target URL
    json_spec: str | None               # AI-generated JSON spec
    spec_warnings: list[str]            # required sections still missing after re-asks (2b)
    merge_files: bool                   # merge root + modal files into one spec
    scope: str                          # "ticket" | "full"

    # Submit config (Gate 3)
    test_type: str | None               # "agent" | "ui" | "api" | "unit" - set at pick_type
    headless: bool                      # UI/agent replay: hidden by default; visible when False

    # API test extras
    api_endpoint: str | None
    api_method: str | None
    api_payload: str | None
    api_payload_type: str | None
    api_headers: dict[str, str] | None

    # Classification + compatibility
    classified: list[dict[str, Any]]
    file_sources: dict[str, str]          # path -> "seed"|"graph"|"grep"|"both"
    compat_iteration: int
    max_compat_iterations: int
    compat_resolution: dict[str, str]     # path -> "applied"|"dropped"|"manual"
    edited_files: list[str]

    # Auth
    auth_mode: str | None                 # "public"|"capture"|"reuse"|"inline"
    auth_ref: str | None                  # "(project, host)" key into the auth store
    project: str | None
    host: str | None
    auto_auth_recover: bool

    # Coverage
    full_report: dict | None

    # Funnel - agent-driven detection/generation
    compat_findings: list[dict[str, Any]]

    # Audit - per-gate re-read receipts
    read_receipts: list[dict[str, Any]]

    # Mode selection (Gate "mode")
    test_mode: str | None              # "automated" | "manual"
    manual_result: dict[str, Any] | None  # manual path: user-reported result
    engine: str                        # "local" (in-process runner suite)

    # Runtime
    run_id: str | None
    iteration: int
    max_iterations: int
    issues: list[dict[str, Any]]
    fix_log: list[dict[str, Any]]
    status: str
    last_error: str | None
    approve_iteration: bool


def make_initial_state(
    file_paths: list[str],
    context: str | None = None,
    max_iterations: int | None = None,
    test_mode: str | None = None,
    engine: str = "local",
) -> TestingState:
    return TestingState(
        file_paths=list(file_paths),
        context=context,
        detection_mode=None,
        url=None,
        json_spec=None,
        spec_warnings=[],
        merge_files=len(file_paths) > 1,
        scope="ticket",
        test_type=None,
        headless=True,
        api_endpoint=None,
        api_method=None,
        api_payload=None,
        api_payload_type=None,
        api_headers=None,
        classified=[],
        file_sources={},
        compat_iteration=0,
        max_compat_iterations=3,
        compat_resolution={},
        edited_files=[],
        compat_findings=[],
        read_receipts=[],
        auth_mode=None,
        auth_ref=None,
        project=None,
        host=None,
        auto_auth_recover=True,
        full_report=None,
        test_mode=test_mode,
        manual_result=None,
        engine=engine,
        run_id=None,
        iteration=0,
        max_iterations=max_iterations if max_iterations is not None else _DEFAULT_MAX_ITERATIONS,
        issues=[],
        fix_log=[],
        status="pending",
        last_error=None,
        approve_iteration=True,
    )
