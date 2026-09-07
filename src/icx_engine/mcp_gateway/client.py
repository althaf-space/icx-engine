"""ExternalMcpClient: owns exactly one external MCP server's subprocess + ClientSession for the
lifetime of ICX's own MCP server process. Never raises past its own boundary - a hung, crashed,
or never-installed external server must never break ICX's own MCP loop, mirroring the "never
raise past this boundary" convention already used by telemetry/logger.py and workstatus/client.py.

Runs the entire `async with stdio_client(...): async with ClientSession(...):` block inside one
dedicated background task (`_run`) for the client's whole lifetime, and talks to it only through
asyncio.Event/the session object itself - never by holding an AsyncExitStack open across tasks.
anyio (which the mcp SDK's stdio transport is built on) requires a cancel scope to be entered and
exited in the same task; a naive AsyncExitStack kept open across the calling task and a later
close() call would violate that. Keeping one task alive for the whole session and having it
`await` its own shutdown event sidesteps the problem entirely."""
from __future__ import annotations

import asyncio
from datetime import timedelta

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.types import Tool

_REQUEST_TIMEOUT = timedelta(seconds=30)
_START_TIMEOUT_SECONDS = 20.0


class ExternalMcpClient:
    def __init__(self, name: str, command: str, args: list[str], env: dict[str, str] | None = None) -> None:
        self.name = name
        self._command = command
        self._args = list(args or [])
        self._env = dict(env) if env else None
        self._task: asyncio.Task | None = None
        self._ready = asyncio.Event()
        self._closed = asyncio.Event()
        self._session: ClientSession | None = None
        self.start_error: str | None = None

    @property
    def started(self) -> bool:
        return self._session is not None

    async def ensure_started(self) -> bool:
        """Idempotent lazy start - the subprocess is spawned on first real use, never at
        registration time. Returns True once a live session exists. A server that already failed
        to start this process lifetime is not retried on every call (that would retry-storm a
        genuinely broken/missing command); call close() then a fresh ensure_started() to retry."""
        if self._session is not None:
            return True
        if self.start_error is not None:
            return False
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name=f"ext-mcp:{self.name}")
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=_START_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            self.start_error = self.start_error or "startup timed out"
        return self.start_error is None

    async def _run(self) -> None:
        """The one task that owns the subprocess+session context managers end to end. Sets
        `_ready` exactly once, whether startup succeeded or failed, then blocks on `_closed`
        until close() is called - keeping both `async with` blocks open and exited in this same
        task, never any other."""
        try:
            params = StdioServerParameters(command=self._command, args=self._args, env=self._env)
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write, read_timeout_seconds=_REQUEST_TIMEOUT) as session:
                    await asyncio.wait_for(session.initialize(), timeout=_START_TIMEOUT_SECONDS)
                    self._session = session
                    self._ready.set()
                    await self._closed.wait()
        except Exception as exc:
            self.start_error = f"{type(exc).__name__}: {exc}"
            self._ready.set()
        finally:
            self._session = None

    async def list_tools(self) -> list[Tool]:
        if not await self.ensure_started():
            return []
        try:
            result = await self._session.list_tools()
            return list(result.tools)
        except Exception:
            return []

    async def call_tool(self, tool_name: str, arguments: dict) -> dict:
        """Never raises - returns {"ok": False, "error": ...} on any failure so
        mcp_gateway/mcp_tools.py's dispatcher can always hand a real payload back to the caller."""
        if not await self.ensure_started():
            return {
                "ok": False,
                "error": f"external MCP server {self.name!r} is not reachable: {self.start_error}",
            }
        try:
            result = await self._session.call_tool(tool_name, arguments)
        except Exception as exc:
            return {"ok": False, "error": f"external MCP server {self.name!r} call failed: {exc}"}
        content = [c.model_dump(mode="json", exclude_none=True) for c in (result.content or [])]
        payload = {"ok": not result.isError, "content": content}
        if result.structuredContent is not None:
            payload["structuredContent"] = result.structuredContent
        return payload

    async def close(self) -> None:
        """Signals the owning task to exit its context managers and waits for it. Safe to call
        even if the client never started."""
        self._closed.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=10.0)
            except Exception:
                self._task.cancel()
        self._task = None
        self._session = None
        self._ready = asyncio.Event()
        self._closed = asyncio.Event()
        self.start_error = None
