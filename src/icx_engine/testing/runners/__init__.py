"""ICX polyglot test-runner plugins.

Importing this package registers the built-in unit-runner adapters (Wave 1). Adding a language =
create an adapter and call register_runner - no core change.

UI (agent-type) tests are NOT a runner adapter here - the connected agent writes and runs its own
Playwright test directly; ICX only parses the JUnit report it produces. There is no ICX-invoked UI
runner/adapter to register.
"""
from icx_engine.testing.runners.base import (
    TestCase,
    TestReport,
    RunSpec,
    register_runner,
    get_runner,
    detect_runners,
    list_runners,
)
from icx_engine.testing.runners.junit import parse_junit_xml
from icx_engine.testing.runners.ephemeral import run_ephemeral_repro
from icx_engine.testing.runners.security import (
    SecurityFinding,
    build_security_plan,
    check_security_headers,
    REQUIRED_SECURITY_HEADERS,
)

from icx_engine.testing.runners.executor import run_spec, run_plan

# Import for side effect: registers the built-in adapters (unit + api + systems).
from icx_engine.testing.runners import unit as _unit        # noqa: F401
from icx_engine.testing.runners import api as _api          # noqa: F401
from icx_engine.testing.runners import systems as _systems  # noqa: F401

__all__ = [
    "TestCase", "TestReport", "RunSpec",
    "register_runner", "get_runner", "detect_runners", "list_runners",
    "parse_junit_xml", "run_ephemeral_repro",
    "SecurityFinding", "build_security_plan", "check_security_headers",
    "REQUIRED_SECURITY_HEADERS",
    "run_spec", "run_plan",
]
