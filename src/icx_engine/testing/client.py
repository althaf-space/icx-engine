from __future__ import annotations

import httpx
from typing import Any
from urllib.parse import quote


class MagikError(Exception):
    pass


class MagikUnreachable(MagikError):
    pass


class MagikRunLost(MagikError):
    pass


class MagikReportNotReady(MagikError):
    pass


class MagikAuthError(MagikError):
    pass


class MagikClient:
    def __init__(self, base_url: str, api_key: str | None = None) -> None:
        self._base = base_url.rstrip("/")
        headers: dict[str, str] = {}
        if api_key:
            headers["X-API-Key"] = api_key
        self._client = httpx.AsyncClient(headers=headers, timeout=30.0)

    def _raise_for_status(self, response: httpx.Response) -> None:
        if response.status_code in (401, 403):
            raise MagikAuthError(f"Magik-AI auth failed: {response.status_code}")
        if response.status_code == 404:
            raise MagikRunLost("Run not found - Magik may have restarted")
        if response.status_code == 425:
            raise MagikReportNotReady("Report not ready yet - run still in progress")
        if response.status_code in (503, 502, 504):
            raise MagikUnreachable(
                f"Magik-AI service unavailable ({response.status_code}) - "
                "the test service may not be running. Restart Magik-AI and retry."
            )
        response.raise_for_status()

    def _unwrap(self, response: httpx.Response) -> dict:
        """Check HTTP status, verify ok flag, and return the data payload.

        Magik API has three response shapes:
          1. Wrapped:     {"ok": true,  "data": {...}}  - UI run status (apiGateway.ok)
          2. Flat status: {runId, kind, state, ...}     - agent-run status (raw res.end)
          3. Flat report: raw JSON file content         - both UI and agent report endpoints
        Formats 2 and 3 have no "ok" or "data" keys - detected by absence of "ok".
        """
        self._raise_for_status(response)
        body = response.json()
        # Application-level error envelope
        if not body.get("ok", True):
            error = body.get("error") or {}
            msg = error.get("message") or str(error) or "unknown Magik error"
            raise MagikError(f"Magik-AI rejected request: {msg}")
        # Wrapped format: {"ok": true, "data": {...}}
        if "data" in body:
            return body["data"]
        # Flat format: agent-run, report, and legacy sonar endpoints return payload directly
        if "ok" not in body:
            return body
        # ok: true but no data key - legacy sonar endpoints return body as-is
        return body

    async def health_check(self) -> dict:
        try:
            r = await self._client.get(f"{self._base}/api/health")
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            raise MagikUnreachable(f"Magik-AI unreachable at {self._base}: {exc}") from exc
        try:
            r.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise MagikUnreachable(
                f"Magik-AI returned {r.status_code} at {self._base}/api/health - "
                "wrong URL or Magik API not started"
            ) from exc
        return self._unwrap(r)

    async def get_active_profile(self) -> dict | None:
        try:
            r = await self._client.get(f"{self._base}/api/v1/profiles/active")
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            raise MagikUnreachable(f"Magik-AI unreachable: {exc}") from exc
        if r.status_code == 404:
            return None
        return self._unwrap(r)

    async def submit_ui_test(
        self,
        url: str,
        profile_screen: str | None = None,
        headless: bool = True,
        agent: str = "openai",
        session_id: str | None = None,
        auto_auth_recover: bool = True,
    ) -> dict:
        body: dict[str, Any] = {"url": url, "headless": headless, "agent": agent}
        if profile_screen:
            body["profileScreen"] = profile_screen
        if session_id:
            body["sessionId"] = session_id
        body["autoAuthRecover"] = auto_auth_recover
        try:
            r = await self._client.post(f"{self._base}/api/v1/ui-tests", json=body)
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            raise MagikUnreachable(f"Magik-AI unreachable: {exc}") from exc
        return self._unwrap(r)

    async def submit_api_test(
        self,
        endpoint: str,
        method: str,
        payload: str,
        payload_type: str,
        headers: dict[str, str] | None = None,
    ) -> dict:
        body: dict[str, Any] = {
            "endpoint": endpoint,
            "method": method,
            "payload": payload,
            "payloadType": payload_type,
        }
        if headers:
            body["headers"] = headers
        try:
            r = await self._client.post(f"{self._base}/api/v1/api-tests", json=body)
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            raise MagikUnreachable(f"Magik-AI unreachable: {exc}") from exc
        return self._unwrap(r)

    async def submit_agent_run(
        self,
        url: str,
        goal: str,
        success_criteria: str | None = None,
        max_steps: int = 50,
        headless: bool = True,
        agent: str = "openai",
        session_id: str | None = None,
        auto_auth_recover: bool = True,
    ) -> dict:
        body: dict[str, Any] = {
            "url": url,
            "goal": goal,
            "headless": headless,
            "agent": agent,
            "maxSteps": max_steps,
        }
        if success_criteria:
            body["successCriteria"] = success_criteria
        if session_id:
            body["sessionId"] = session_id
        body["autoAuthRecover"] = auto_auth_recover
        try:
            r = await self._client.post(f"{self._base}/api/v1/agent-runs", json=body)
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            raise MagikUnreachable(f"Magik-AI unreachable: {exc}") from exc
        return self._unwrap(r)

    async def get_run_status(self, run_id: str) -> dict:
        rid = quote(run_id, safe="")
        if run_id.startswith("agent-"):
            url = f"{self._base}/api/v1/agent-runs/{rid}"
        else:
            url = f"{self._base}/api/v1/runs/{rid}"
        try:
            r = await self._client.get(url)
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            raise MagikUnreachable(f"Magik-AI unreachable: {exc}") from exc
        return self._unwrap(r)

    async def get_run_report(self, run_id: str, fmt: str = "json") -> dict:
        rid = quote(run_id, safe="")
        if run_id.startswith("agent-"):
            url = f"{self._base}/api/v1/agent-runs/{rid}/report"
        else:
            url = f"{self._base}/api/v1/runs/{rid}/report"
        try:
            r = await self._client.get(url, params={"format": fmt})
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            raise MagikUnreachable(f"Magik-AI unreachable: {exc}") from exc
        return self._unwrap(r)

    async def _post(self, path: str, body: dict) -> dict:
        try:
            r = await self._client.post(f"{self._base}{path}", json=body)
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            raise MagikUnreachable(f"Magik-AI unreachable: {exc}") from exc
        return self._unwrap(r)

    async def parse_spec(self, payload: str, payload_type: str) -> dict:
        return await self._post("/api/v1/api-tests/parse-spec",
                                {"payload": payload, "payloadType": payload_type})

    async def submit_profile(self, markdown: str) -> dict:
        return await self._post("/api/v1/profiles", {"markdown": markdown})

    async def sonar_config(self, url: str, token: str, project_key: str) -> dict:
        return await self._post("/api/v1/sonar/config",
                                {"url": url, "token": token, "projectKey": project_key})

    async def sonar_pull(self, project_key: str, types: str | None = None,
                         severities: str | None = None) -> dict:
        body: dict[str, Any] = {"projectKey": project_key}
        if types:
            body["types"] = types
        if severities:
            body["severities"] = severities
        return await self._post("/api/v1/sonar-pulls", body)

    async def _get(self, path: str, params: dict | None = None) -> dict:
        try:
            r = await self._client.get(f"{self._base}{path}", params=params or {})
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            raise MagikUnreachable(f"Magik-AI unreachable: {exc}") from exc
        return self._unwrap(r)

    async def get_sonar_config(self) -> dict:
        return await self._get("/api/v1/sonar/config")

    async def sonar_ping(self, url: str | None = None, token: str | None = None) -> dict:
        body: dict[str, Any] = {}
        if url:
            body["url"] = url
        if token:
            body["token"] = token
        return await self._post("/api/v1/sonar/ping", body)

    async def sonar_projects(self) -> dict:
        return await self._get("/api/v1/sonar/projects")

    async def sonar_scanner_check(self) -> dict:
        return await self._get("/api/v1/sonar/scanner-check")

    async def sonar_scan(self, project_folder: str, project_key: str | None = None,
                         sources: str | None = None, exclusions: str | None = None) -> dict:
        body: dict[str, Any] = {"projectFolder": project_folder}
        if project_key:
            body["projectKey"] = project_key
        if sources:
            body["sources"] = sources
        if exclusions:
            body["exclusions"] = exclusions
        return await self._post("/api/v1/sonar-scans", body)

    async def sonar_last_scan(self) -> dict:
        return await self._get("/api/v1/sonar/last-scan")

    async def sonar_report(self, fmt: str = "json") -> dict | str:
        # Magik serves raw HTML for format=html and JSON for format=json.
        # Do not route through _unwrap (which forces response.json()) - decode
        # by format so an HTML body never triggers a JSON parse error.
        try:
            r = await self._client.get(f"{self._base}/api/v1/sonar/report",
                                       params={"format": fmt})
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            raise MagikUnreachable(f"Magik-AI unreachable: {exc}") from exc
        self._raise_for_status(r)
        if fmt == "json":
            return r.json()
        return r.text

    async def sonar_scan_stream(self, job_id: str):
        import json as _json
        url = f"{self._base}/api/v1/sonar-scans/stream"
        event = "message"
        async with self._client.stream("GET", url, params={"jobId": job_id}) as resp:
            async for line in resp.aiter_lines():
                if line.startswith("event:"):
                    event = line[6:].strip()
                elif line.startswith("data:"):
                    raw = line[5:].strip()
                    try:
                        yield event, _json.loads(raw)
                    except ValueError:
                        yield event, {"raw": raw}
                    event = "message"

    async def stream_run(self, run_id: str):
        import json as _json
        rid = quote(run_id, safe="")
        base = "agent-runs" if run_id.startswith("agent-") else "runs"
        url = f"{self._base}/api/v1/{base}/{rid}/stream"
        event = "message"
        async with self._client.stream("GET", url) as resp:
            async for line in resp.aiter_lines():
                if line.startswith("event:"):
                    event = line[6:].strip()
                elif line.startswith("data:"):
                    raw = line[5:].strip()
                    try:
                        yield event, _json.loads(raw)
                    except ValueError:
                        yield event, {"raw": raw}
                    event = "message"

    async def interactive_login_start(self, login_url: str) -> dict:
        return await self._post("/api/v1/login/interactive/start", {"loginUrl": login_url})

    async def interactive_login_capture(self, interactive_id: str, label: str | None = None) -> dict:
        body = {"interactiveId": interactive_id}
        if label:
            body["label"] = label
        return await self._post("/api/v1/login/interactive/capture", body)

    async def interactive_login_cancel(self, interactive_id: str) -> dict:
        return await self._post("/api/v1/login/interactive/cancel", {"interactiveId": interactive_id})

    async def inline_login(self, login_url: str, username: str, password: str, timeout: int = 30000) -> dict:
        return await self._post("/api/v1/login", {
            "loginUrl": login_url, "username": username, "password": password, "timeout": timeout,
        })

    async def logout(self, session_id: str) -> dict:
        return await self._post("/api/v1/logout", {"sessionId": session_id})

    async def get_profile_creation_prompt(self) -> str:
        try:
            r = await self._client.get(f"{self._base}/profile/creation-prompt")
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            raise MagikUnreachable(f"Magik-AI unreachable: {exc}") from exc
        self._raise_for_status(r)
        return r.text

    async def aclose(self) -> None:
        await self._client.aclose()
