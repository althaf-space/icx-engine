from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from icx_engine.testing.classify import FileClass
from icx_engine.testing.compat import CompatVerdict, check_compat


class TestModeHandler(ABC):
    mode: str = ""

    @abstractmethod
    def relevant_layers(self) -> set[str]: ...

    def compat(self, fc: FileClass) -> CompatVerdict:
        return check_compat(fc, self.mode)

    @abstractmethod
    async def submit(self, client: Any, state: dict) -> dict: ...


class UiHandler(TestModeHandler):
    mode = "ui"

    def relevant_layers(self) -> set[str]:
        return {"frontend", "shared"}

    async def submit(self, client: Any, state: dict) -> dict:
        return await client.submit_ui_test(
            url=state.get("url") or "",
            profile_screen=state.get("profile_screen"),
            headless=state.get("headless", True),
            agent=state.get("agent_provider", "openai"),
            session_id=state.get("_auth_session_id"),
            auto_auth_recover=state.get("auto_auth_recover", True),
        )


class AgentHandler(TestModeHandler):
    mode = "agent"

    def relevant_layers(self) -> set[str]:
        return {"frontend", "shared"}

    async def submit(self, client: Any, state: dict) -> dict:
        goal = state.get("json_spec") or state.get("context") or ""
        return await client.submit_agent_run(
            url=state.get("url") or "",
            goal=goal,
            headless=state.get("headless", True),
            agent=state.get("agent_provider", "openai"),
            max_steps=state.get("agent_max_steps", 50),
            session_id=state.get("_auth_session_id"),
            auto_auth_recover=state.get("auto_auth_recover", True),
        )


class ApiHandler(TestModeHandler):
    mode = "api"

    def relevant_layers(self) -> set[str]:
        return {"backend", "shared"}

    async def submit(self, client: Any, state: dict) -> dict:
        return await client.submit_api_test(
            endpoint=state.get("api_endpoint") or "",
            method=state.get("api_method") or "POST",
            payload=state.get("api_payload") or "",
            payload_type=state.get("api_payload_type") or "json",
            headers=state.get("api_headers"),
        )


_REGISTRY: dict[str, TestModeHandler] = {
    "ui": UiHandler(),
    "agent": AgentHandler(),
    "api": ApiHandler(),
}


def get_handler(mode: str) -> TestModeHandler:
    try:
        return _REGISTRY[mode]
    except KeyError:
        raise ValueError(f"unknown test mode: {mode!r}") from None
