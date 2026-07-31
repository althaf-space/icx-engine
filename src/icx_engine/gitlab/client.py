"""GitLab REST API v4 client - the repo-host connector's transport layer
(design spec Section 8.1). Mirrors sonar/client.py's async-context-manager
shape (one httpx.AsyncClient per batch of calls), but the token is protected
from cross-host leakage differently: the AsyncClient is constructed with
follow_redirects=False, so no redirect is ever followed at all - simpler
than Sonar's manual per-hop host-check loop, and at least as safe, since a
redirect response is always returned to the caller unfollowed rather than
re-issued with the auth header attached. PRIVATE-TOKEN header auth (GitLab's
personal-access-token convention, not Authorization: Bearer). Never used
directly by git/manager.py - only through gitlab/service.py."""
from __future__ import annotations

import re
from urllib.parse import quote, urlparse

import httpx

_TIMEOUT = 30.0


class GitLabError(RuntimeError):
    """Raised for any GitLab API failure - non-2xx response, network error,
    or malformed response body."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class GitLabClient:
    def __init__(self, base_url: str, token: str | None, verify_tls: bool = True):
        parsed = urlparse((base_url or "").strip())
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError(f"GitLab base_url must be an absolute http/https URL, got: {base_url!r}")
        if parsed.username or parsed.password:
            raise ValueError("GitLab base_url must not contain embedded credentials.")
        self._base = f"{parsed.scheme}://{parsed.netloc}"
        self._token = token
        self._verify = verify_tls
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "GitLabClient":
        self._client = httpx.AsyncClient(timeout=_TIMEOUT, verify=self._verify, follow_redirects=False)
        return self

    async def __aexit__(self, *exc) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _headers(self) -> dict[str, str]:
        return {"PRIVATE-TOKEN": self._token} if self._token else {}

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        """Centralized transport for every call this client makes - wraps
        transport/network errors as GitLabError. Non-2xx status handling is
        the caller's responsibility (each endpoint's success/refusal shape
        differs)."""
        assert self._client is not None, "GitLabClient must be used as an async context manager"
        try:
            resp = await self._client.request(method, f"{self._base}{path}", headers=self._headers(), **kwargs)
        except httpx.HTTPError as exc:
            raise GitLabError(f"GitLab request to {path} failed: {exc}") from exc
        return resp

    async def _get(self, path: str, params: dict | None = None) -> httpx.Response:
        return await self._request("GET", path, params=params)

    async def _post(self, path: str, json: dict | None = None) -> httpx.Response:
        return await self._request("POST", path, json=json)

    async def _put(self, path: str, **kwargs) -> httpx.Response:
        return await self._request("PUT", path, **kwargs)

    async def validate(self) -> dict:
        """Confirm the token is accepted and report the resolved user identity."""
        resp = await self._get("/api/v4/user")
        if resp.status_code != 200:
            return {"valid": False, "status_code": resp.status_code}
        return {"valid": True, "user": resp.json()}

    async def list_projects(self, query: str | None = None, limit: int = 20) -> list[dict]:
        params: dict = {"membership": "true", "per_page": limit}
        if query:
            params["search"] = query
        resp = await self._get("/api/v4/projects", params=params)
        if resp.status_code != 200:
            raise GitLabError(f"Listing projects failed (HTTP {resp.status_code}): {resp.text}", resp.status_code)
        return resp.json()

    async def get_project(self, path_or_id: str) -> dict:
        """`path_or_id` is either a numeric project ID or a `namespace/project`
        path - URL-encoded automatically for the path form."""
        encoded = path_or_id if path_or_id.isdigit() else quote(path_or_id, safe="")
        resp = await self._get(f"/api/v4/projects/{encoded}")
        if resp.status_code != 200:
            raise GitLabError(f"Project '{path_or_id}' not found (HTTP {resp.status_code}): {resp.text}", resp.status_code)
        return resp.json()

    async def create_merge_request(
        self, project: str, source_branch: str, target_branch: str, title: str,
        description: str, assignee_id: int, remove_source_branch: bool = True,
    ) -> dict:
        encoded = project if project.isdigit() else quote(project, safe="")
        resp = await self._post(
            f"/api/v4/projects/{encoded}/merge_requests",
            json={
                "source_branch": source_branch,
                "target_branch": target_branch,
                "title": title,
                "description": description,
                "assignee_id": assignee_id,
                "remove_source_branch": remove_source_branch,
            },
        )
        if resp.status_code not in (200, 201):
            raise GitLabError(f"Creating merge request failed (HTTP {resp.status_code}): {resp.text}", resp.status_code)
        return resp.json()

    async def get_merge_request(self, project: str, mr_iid: int) -> dict:
        encoded = project if project.isdigit() else quote(project, safe="")
        resp = await self._get(f"/api/v4/projects/{encoded}/merge_requests/{mr_iid}")
        if resp.status_code != 200:
            raise GitLabError(f"Fetching merge request !{mr_iid} failed (HTTP {resp.status_code}): {resp.text}", resp.status_code)
        return resp.json()

    async def attempt_merge(self, project: str, mr_iid: int) -> dict:
        """Attempt an immediate merge. Refusal is reported as a normal result
        (merged=False + reason), never raised - the server refusing per its
        own protection rules is an expected outcome, not an error condition.
        A 5xx or a malformed body is a genuine transport/API failure, not a
        refusal, and raises GitLabError instead."""
        encoded = project if project.isdigit() else quote(project, safe="")
        resp = await self._put(f"/api/v4/projects/{encoded}/merge_requests/{mr_iid}/merge")
        if resp.status_code >= 500:
            raise GitLabError(f"Merge attempt for !{mr_iid} failed (HTTP {resp.status_code}): {resp.text}", resp.status_code)
        if resp.status_code == 200:
            try:
                body = resp.json()
            except Exception as exc:
                raise GitLabError(
                    f"Merge attempt for !{mr_iid} returned a malformed response body (HTTP 200): {exc}", resp.status_code
                ) from exc
            return {"merged": body.get("state") == "merged", "state": body.get("state")}
        try:
            reason = resp.json().get("message", resp.text)
        except Exception:
            reason = resp.text
        return {"merged": False, "reason": str(reason)}

    async def find_merge_request_for_branch(self, project: str, source_branch: str) -> dict | None:
        """Look up an already-open MR for this source branch, if one exists -
        used to avoid creating a duplicate MR on a retry."""
        encoded = project if project.isdigit() else quote(project, safe="")
        resp = await self._get(
            f"/api/v4/projects/{encoded}/merge_requests",
            params={"source_branch": source_branch, "state": "opened"},
        )
        if resp.status_code != 200:
            raise GitLabError(f"Looking up merge requests failed (HTTP {resp.status_code}): {resp.text}", resp.status_code)
        results = resp.json()
        return results[0] if results else None

    async def list_tags(self, project: str) -> list[dict]:
        encoded = project if project.isdigit() else quote(project, safe="")
        resp = await self._get(f"/api/v4/projects/{encoded}/repository/tags", params={"per_page": 100})
        if resp.status_code != 200:
            raise GitLabError(f"Listing tags failed (HTTP {resp.status_code}): {resp.text}", resp.status_code)
        return resp.json()

    async def create_tag(self, project: str, tag_name: str, ref: str, message: str = "") -> dict:
        """Creates the tag server-side against `ref` (a branch name or SHA) -
        GitLab resolves the branch's current tip on its own server, so no
        local push is required."""
        encoded = project if project.isdigit() else quote(project, safe="")
        body: dict = {"tag_name": tag_name, "ref": ref}
        if message:
            body["message"] = message
        resp = await self._post(f"/api/v4/projects/{encoded}/repository/tags", json=body)
        if resp.status_code not in (200, 201):
            raise GitLabError(f"Creating tag '{tag_name}' failed (HTTP {resp.status_code}): {resp.text}", resp.status_code)
        return resp.json()

    async def list_merge_requests(
        self, project: str, state: str = "merged", target_branch: str | None = None, limit: int = 20,
    ) -> list[dict]:
        """GitLab's MR list response already includes `merged_by`/`merged_at`
        fields natively when state=merged - no extra processing needed."""
        encoded = project if project.isdigit() else quote(project, safe="")
        params: dict = {"state": state, "per_page": limit}
        if target_branch:
            params["target_branch"] = target_branch
        resp = await self._get(f"/api/v4/projects/{encoded}/merge_requests", params=params)
        if resp.status_code != 200:
            raise GitLabError(f"Listing merge requests failed (HTTP {resp.status_code}): {resp.text}", resp.status_code)
        return resp.json()

    async def get_merge_request_changes(self, project: str, mr_iid: int) -> dict:
        encoded = project if project.isdigit() else quote(project, safe="")
        resp = await self._get(f"/api/v4/projects/{encoded}/merge_requests/{mr_iid}/changes")
        if resp.status_code != 200:
            raise GitLabError(
                f"Fetching merge request !{mr_iid} changes failed (HTTP {resp.status_code}): {resp.text}", resp.status_code
            )
        return resp.json()

    async def list_commits(
        self, project: str, ref: str | None = None, path: str | None = None, since: str | None = None, limit: int = 20,
    ) -> list[dict]:
        encoded = project if project.isdigit() else quote(project, safe="")
        params: dict = {"per_page": limit}
        if ref:
            params["ref_name"] = ref
        if path:
            params["path"] = path
        if since:
            params["since"] = since
        resp = await self._get(f"/api/v4/projects/{encoded}/repository/commits", params=params)
        if resp.status_code != 200:
            raise GitLabError(f"Listing commits failed (HTTP {resp.status_code}): {resp.text}", resp.status_code)
        return resp.json()

    async def compare(self, project: str, from_ref: str, to_ref: str) -> dict:
        encoded = project if project.isdigit() else quote(project, safe="")
        resp = await self._get(
            f"/api/v4/projects/{encoded}/repository/compare", params={"from": from_ref, "to": to_ref}
        )
        if resp.status_code != 200:
            raise GitLabError(
                f"Comparing '{from_ref}'..'{to_ref}' failed (HTTP {resp.status_code}): {resp.text}", resp.status_code
            )
        return resp.json()


_SSH_REMOTE_RE = re.compile(r"^[\w.-]+@[\w.-]+:(?P<path>.+?)(?:\.git)?/?$")
_HTTPS_REMOTE_RE = re.compile(r"^https?://[^/]+/(?P<path>.+?)(?:\.git)?/?$")


def project_path_from_remote_url(remote_url: str) -> str | None:
    """Parse a git remote URL (SSH or HTTPS form) into a GitLab
    `namespace/project` path. Returns None for anything unrecognized -
    callers must handle that (never assume a repo is on GitLab)."""
    remote_url = (remote_url or "").strip()
    match = _SSH_REMOTE_RE.match(remote_url) or _HTTPS_REMOTE_RE.match(remote_url)
    return match.group("path") if match else None
