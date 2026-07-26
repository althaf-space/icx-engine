from __future__ import annotations

from abc import ABC, abstractmethod

from icx_engine.testing.classify import FileClass
from icx_engine.testing.compat import CompatVerdict, check_compat


class TestModeHandler(ABC):
    """Per-test-type metadata: which code layers a test type cares about, and how to judge a
    file's compatibility with it. Execution is handled by the local runner suite
    (testing/runners + local_executor), not here."""
    mode: str = ""

    @abstractmethod
    def relevant_layers(self) -> set[str]: ...

    def compat(self, fc: FileClass) -> CompatVerdict:
        return check_compat(fc, self.mode)


class AgentHandler(TestModeHandler):
    mode = "agent"

    def relevant_layers(self) -> set[str]:
        return {"frontend", "shared"}


class ApiHandler(TestModeHandler):
    mode = "api"

    def relevant_layers(self) -> set[str]:
        return {"backend", "shared"}


_REGISTRY: dict[str, TestModeHandler] = {
    "agent": AgentHandler(),
    "api": ApiHandler(),
}


def get_handler(mode: str) -> TestModeHandler:
    try:
        return _REGISTRY[mode]
    except KeyError:
        raise ValueError(f"unknown test mode: {mode!r}") from None
