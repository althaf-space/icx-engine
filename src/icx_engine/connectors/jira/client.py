from __future__ import annotations
import asyncio
import httpx
from contextlib import asynccontextmanager
from urllib.parse import urlparse, urljoin, quote

from icx_engine.connectors.http import check_http_status
from icx_engine.connectors.jira.parser import parse_issue_response
from icx_engine.exceptions import JiraValidationError, SourceUnavailable
from icx_engine.models.output import RawIssueData

_MAX_RETRIES = 3
_MAX_REDIRECT_HOPS = 3
_RETRY_STATUSES = {429, 500, 502, 503, 504}
_MAX_RETRY_DELAY = 60
_MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024  # 50 MB

_JIRA_FIELDS = (
    "summary,description,comment,attachment,"
    "issuetype,priority,status,reporter,assignee,duedate"
)


class JiraClient:
    def __init__(self, base_url: str, auth_header: str, allowed_hosts: set[str]):
        parsed_base = urlparse(base_url)
        if not parsed_base.scheme or not parsed_base.netloc:
            raise ValueError(f"base_url must be an absolute HTTPS URL, got: {base_url!r}")
        if parsed_base.scheme != "https":
            raise ValueError(f"base_url must use HTTPS, got scheme '{parsed_base.scheme}'.")
        if parsed_base.netloc not in allowed_hosts:
            raise ValueError(
                f"base_url host '{parsed_base.netloc}' is not in allowed_hosts."
            )
        self._base_url = base_url
        self._auth_header = auth_header
        self._allowed_hosts = allowed_hosts
        # Optional shared client for a batch of attachment downloads. When set
        # (via attachment_session), downloads reuse one connection pool instead
        # of opening a fresh client per attachment. None = per-call client.
        self._dl_client: httpx.AsyncClient | None = None

    async def fetch(self, issue_key: str) -> RawIssueData:
        async with httpx.AsyncClient(timeout=30.0) as client:
            for attempt in range(_MAX_RETRIES):
                response = await client.get(
                    f"{self._base_url}/issue/{quote(issue_key, safe='')}",
                    headers={
                        "Authorization": self._auth_header,
                        "Accept": "application/json",
                    },
                    params={"fields": _JIRA_FIELDS},
                )

                if response.status_code in _RETRY_STATUSES and attempt < _MAX_RETRIES - 1:
                    raw_retry = response.headers.get("Retry-After", "")
                    try:
                        delay = min(int(raw_retry), _MAX_RETRY_DELAY)
                    except (ValueError, TypeError):
                        delay = min(2 ** attempt, _MAX_RETRY_DELAY)
                    await asyncio.sleep(delay)
                    continue

                check_http_status(response)
                return parse_issue_response(issue_key, response.json())

        raise SourceUnavailable("Source is unavailable. Try again later.")

    async def get_transitions(self, issue_key: str) -> list[dict]:
        """GET .../issue/{key}/transitions?expand=transitions.fields - the
        available workflow transitions for this issue, each with its
        per-transition required fields (present only via this expand)."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(
                f"{self._base_url}/issue/{quote(issue_key, safe='')}/transitions",
                headers={
                    "Authorization": self._auth_header,
                    "Accept": "application/json",
                },
                params={"expand": "transitions.fields"},
            )
            check_http_status(response)
            return response.json().get("transitions", [])

    async def get_editmeta(self, issue_key: str) -> dict:
        """GET .../issue/{key}/editmeta - the fields editable on this issue,
        each with its `required`/`schema`/`allowedValues`."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(
                f"{self._base_url}/issue/{quote(issue_key, safe='')}/editmeta",
                headers={
                    "Authorization": self._auth_header,
                    "Accept": "application/json",
                },
            )
            check_http_status(response)
            return response.json().get("fields", {})

    async def transition_issue(
        self,
        issue_key: str,
        transition_id: str | None = None,
        fields: dict | None = None,
        comment_adf: dict | None = None,
    ) -> None:
        """Move the issue through a workflow transition, optionally setting
        fields and adding a comment in the same call - POST .../transitions.
        When `transition_id` is None (a field-only update, no transition),
        falls through to PUT .../issue/{key} instead - Jira's standard path
        for a field-only change. `comment_adf` is ignored in that branch;
        there is no comment-only write endpoint here yet."""
        key = quote(issue_key, safe="")
        headers = {
            "Authorization": self._auth_header,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            if transition_id is not None:
                body: dict = {"transition": {"id": transition_id}}
                if fields:
                    body["fields"] = fields
                if comment_adf:
                    body["update"] = {"comment": [{"add": {"body": comment_adf}}]}
                response = await client.post(
                    f"{self._base_url}/issue/{key}/transitions",
                    headers=headers,
                    json=body,
                )
            else:
                response = await client.put(
                    f"{self._base_url}/issue/{key}",
                    headers=headers,
                    json={"fields": fields or {}},
                )
            self._check_write_status(response)

    async def update_fields(self, issue_key: str, fields: dict) -> None:
        """PUT .../issue/{key} - update fields with no workflow transition."""
        key = quote(issue_key, safe="")
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.put(
                f"{self._base_url}/issue/{key}",
                headers={
                    "Authorization": self._auth_header,
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                json={"fields": fields},
            )
            self._check_write_status(response)

    async def list_issuetypes(self, project: str) -> list[dict]:
        """GET .../issue/createmeta/{project}/issuetypes - the issue types
        available for creation in this project, each with at least id/name."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(
                f"{self._base_url}/issue/createmeta/{quote(project, safe='')}/issuetypes",
                headers={
                    "Authorization": self._auth_header,
                    "Accept": "application/json",
                },
            )
            check_http_status(response)
            return response.json().get("values", [])

    async def get_createmeta_fields(self, project: str, issuetype_id: str) -> dict:
        """GET .../issue/createmeta/{project}/issuetypes/{issuetype_id} - the
        create-time analog of get_editmeta: fields required/available to
        create an issue of this type. Jira returns these as a `values` list;
        re-keyed here by `fieldId` so the returned shape mirrors
        get_editmeta's (dict of field-key -> field-info with 'required')."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(
                f"{self._base_url}/issue/createmeta/{quote(project, safe='')}"
                f"/issuetypes/{quote(issuetype_id, safe='')}",
                headers={
                    "Authorization": self._auth_header,
                    "Accept": "application/json",
                },
            )
            check_http_status(response)
            values = response.json().get("values", [])
            return {v["fieldId"]: v for v in values if "fieldId" in v}

    async def create_issue(
        self, project: str, issuetype: str, summary: str, extra_fields: dict | None = None,
    ) -> str:
        """POST .../issue - minimal required fields are project/issuetype/
        summary; extra_fields (if given) are merged in alongside them.
        `issuetype` is matched by name (e.g. 'Bug'), not id. Returns the
        created issue's key."""
        fields: dict = {
            "project": {"key": project},
            "issuetype": {"name": issuetype},
            "summary": summary,
        }
        if extra_fields:
            fields.update(extra_fields)
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{self._base_url}/issue",
                headers={
                    "Authorization": self._auth_header,
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                json={"fields": fields},
            )
            self._check_write_status(response)
            return response.json()["key"]

    async def delete_issue(self, issue_key: str, delete_subtasks: bool = False) -> None:
        """DELETE .../issue/{key} - permanent, Jira Cloud has no recycle bin
        for issues. `deleteSubtasks=true` is required if the issue has
        subtasks, else Jira 400s."""
        key = quote(issue_key, safe="")
        params = {"deleteSubtasks": "true"} if delete_subtasks else {}
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.delete(
                f"{self._base_url}/issue/{key}",
                headers={
                    "Authorization": self._auth_header,
                    "Accept": "application/json",
                },
                params=params,
            )
            self._check_write_status(response)

    async def list_comments(self, issue_key: str) -> list[dict]:
        """GET .../issue/{key}/comment - a plain read, so `check_http_status`
        alone (no per-field validator body to parse on a 4xx, unlike the
        write calls below)."""
        key = quote(issue_key, safe="")
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(
                f"{self._base_url}/issue/{key}/comment",
                headers={
                    "Authorization": self._auth_header,
                    "Accept": "application/json",
                },
            )
            check_http_status(response)
            return response.json().get("comments", [])

    async def add_comment(self, issue_key: str, body_adf: dict) -> dict:
        """POST .../issue/{key}/comment - body {"body": <ADF>}. Returns the
        created comment object (id/body/author/created/...)."""
        key = quote(issue_key, safe="")
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{self._base_url}/issue/{key}/comment",
                headers={
                    "Authorization": self._auth_header,
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                json={"body": body_adf},
            )
            self._check_write_status(response)
            return response.json()

    async def edit_comment(self, issue_key: str, comment_id: str, body_adf: dict) -> dict:
        """PUT .../issue/{key}/comment/{id} - body {"body": <ADF>}. Returns
        the updated comment object."""
        key = quote(issue_key, safe="")
        cid = quote(comment_id, safe="")
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.put(
                f"{self._base_url}/issue/{key}/comment/{cid}",
                headers={
                    "Authorization": self._auth_header,
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                json={"body": body_adf},
            )
            self._check_write_status(response)
            return response.json()

    async def search_issues(
        self, jql: str, fields: list[str] | None = None, max_results: int = 50,
        page_token: str | None = None,
    ) -> dict:
        """POST .../search/jql - the old GET/POST /search endpoint is fully
        decommissioned (2025); pagination here is token-based (`nextPageToken`),
        not `startAt`. `nextPageToken` is omitted from the body entirely when
        `page_token` is None - Jira rejects an explicit null there. This method
        does not itself cap `max_results`/default `fields` - that ICX-side cost
        discipline lives in jira/service.py's `search`, not the transport layer."""
        body: dict = {"jql": jql, "fields": fields, "maxResults": max_results}
        if page_token is not None:
            body["nextPageToken"] = page_token
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{self._base_url}/search/jql",
                headers={
                    "Authorization": self._auth_header,
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                json=body,
            )
            check_http_status(response)
            data = response.json()
            return {
                "issues": data.get("issues", []),
                "next_page_token": data.get("nextPageToken"),
                "is_last": data.get("isLast", True),
            }

    async def get_issue_raw(self, issue_key: str, fields: list[str] | None = None) -> dict:
        """GET .../issue/{key} - a plain, lightweight raw fetch returning Jira's
        bare field JSON. Deliberately NOT the RawIssueData shape `fetch()`
        produces - named distinctly to avoid confusion with the read pipeline.
        `fields` (if given) is joined into a comma-list query param; omitted
        entirely when not given, so Jira returns its own default field set."""
        key = quote(issue_key, safe="")
        params = {"fields": ",".join(fields)} if fields else {}
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(
                f"{self._base_url}/issue/{key}",
                headers={
                    "Authorization": self._auth_header,
                    "Accept": "application/json",
                },
                params=params,
            )
            check_http_status(response)
            return response.json()

    async def delete_comment(self, issue_key: str, comment_id: str) -> None:
        """DELETE .../issue/{key}/comment/{id} - permanent. Verified against
        Jira Cloud documentation/community reports: there is no recovery
        mechanism for a deleted comment (no recycle bin, no undo) - the same
        permanence as delete_issue, just with no issue-level trash concept
        to reference for comments specifically."""
        key = quote(issue_key, safe="")
        cid = quote(comment_id, safe="")
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.delete(
                f"{self._base_url}/issue/{key}/comment/{cid}",
                headers={
                    "Authorization": self._auth_header,
                    "Accept": "application/json",
                },
            )
            self._check_write_status(response)

    async def list_link_types(self) -> list[dict]:
        """GET .../issueLinkType - the link types available for creating an
        issue link (e.g. 'Blocks'/'Relates to'), each with id/name/inward/
        outward. Call this before create_link if the link type name isn't
        already known, to offer real options rather than a guess. A plain
        read, so `check_http_status` alone (no per-field validator body)."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(
                f"{self._base_url}/issueLinkType",
                headers={
                    "Authorization": self._auth_header,
                    "Accept": "application/json",
                },
            )
            check_http_status(response)
            return response.json().get("issueLinkTypes", [])

    async def create_link(self, link_type_name: str, inward_key: str, outward_key: str) -> None:
        """POST .../issueLink - body {"type": {"name": ...}, "inwardIssue":
        {"key": ...}, "outwardIssue": {"key": ...}}. Jira returns 201 with
        no body on success."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{self._base_url}/issueLink",
                headers={
                    "Authorization": self._auth_header,
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                json={
                    "type": {"name": link_type_name},
                    "inwardIssue": {"key": inward_key},
                    "outwardIssue": {"key": outward_key},
                },
            )
            self._check_write_status(response)

    async def delete_link(self, link_id: str) -> None:
        """DELETE .../issueLink/{link_id} - a GLOBAL endpoint, unlike every
        other write method in this class: it is not scoped under
        /issue/{key}/ at all, and takes no issue key."""
        link = quote(link_id, safe="")
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.delete(
                f"{self._base_url}/issueLink/{link}",
                headers={
                    "Authorization": self._auth_header,
                    "Accept": "application/json",
                },
            )
            self._check_write_status(response)

    async def set_assignee(self, issue_key: str, account_id: str | None) -> None:
        """PUT .../issue/{key}/assignee - body {"accountId": account_id}.
        Three distinct cases, all handled by a plain pass-through into the
        JSON body (httpx's json= serializes None to JSON null on its own -
        no special-casing needed): account_id="-1" sends the literal string
        "-1" (Jira's default-assignee sentinel); account_id=None serializes
        to JSON null (unassigns the issue); any other string is sent as-is
        (assigns that account). This has its own endpoint deliberately -
        needs only the "Assign Issues" permission, not full "Edit Issues" -
        so it is never folded into update_fields."""
        key = quote(issue_key, safe="")
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.put(
                f"{self._base_url}/issue/{key}/assignee",
                headers={
                    "Authorization": self._auth_header,
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                json={"accountId": account_id},
            )
            self._check_write_status(response)

    async def upload_attachment(
        self, issue_key: str, filename: str, content_bytes: bytes, content_type: str | None = None,
    ) -> list[dict]:
        """POST .../issue/{key}/attachments - multipart/form-data, field name
        'file'. REQUIRES the X-Atlassian-Token: no-check header - Jira's XSRF
        check silently 403s this endpoint without it; this is a real,
        documented gotcha, not optional. Built with httpx's files= param
        (not json=) - Content-Type is left for httpx to set from the
        multipart boundary, never set explicitly on the request itself.
        Returns the created attachment metadata array."""
        key = quote(issue_key, safe="")
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                f"{self._base_url}/issue/{key}/attachments",
                headers={
                    "Authorization": self._auth_header,
                    "Accept": "application/json",
                    "X-Atlassian-Token": "no-check",
                },
                files={"file": (filename, content_bytes, content_type or "application/octet-stream")},
            )
            self._check_write_status(response)
            return response.json()

    async def get_current_user(self) -> dict:
        """GET .../myself - the authenticated user's own identity (accountId,
        displayName, ...). Exists specifically so the self-vs-other gating
        decision for watcher/worklog mutations can be made for real - see
        jira/mcp_tools.py's jira_set_watcher/jira_worklog_edit/
        jira_worklog_delete. A plain read, check_http_status alone."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(
                f"{self._base_url}/myself",
                headers={
                    "Authorization": self._auth_header,
                    "Accept": "application/json",
                },
            )
            check_http_status(response)
            return response.json()

    async def list_watchers(self, issue_key: str) -> dict:
        """GET .../issue/{key}/watchers -> {"watchers": [...], "watchCount": N}.
        A plain read, check_http_status alone."""
        key = quote(issue_key, safe="")
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(
                f"{self._base_url}/issue/{key}/watchers",
                headers={
                    "Authorization": self._auth_header,
                    "Accept": "application/json",
                },
            )
            check_http_status(response)
            return response.json()

    async def add_watcher(self, issue_key: str, account_id: str) -> None:
        """POST .../issue/{key}/watchers - the body is a BARE JSON STRING (the
        accountId), NOT {"accountId": ...} like every other write body in this
        class - a real, easy-to-get-wrong Jira API shape. `json=account_id`
        (not `json={"accountId": account_id}`) is what achieves this: httpx
        serializes a bare str via json.dumps into a JSON string literal."""
        key = quote(issue_key, safe="")
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{self._base_url}/issue/{key}/watchers",
                headers={
                    "Authorization": self._auth_header,
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                json=account_id,
            )
            self._check_write_status(response)

    async def remove_watcher(self, issue_key: str, account_id: str) -> None:
        """DELETE .../issue/{key}/watchers?accountId=<id> - a query param, not
        a body, unlike add_watcher's POST."""
        key = quote(issue_key, safe="")
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.delete(
                f"{self._base_url}/issue/{key}/watchers",
                headers={
                    "Authorization": self._auth_header,
                    "Accept": "application/json",
                },
                params={"accountId": account_id},
            )
            self._check_write_status(response)

    async def list_worklogs(self, issue_key: str) -> dict:
        """GET .../issue/{key}/worklog -> {"worklogs": [...], ...}. A plain
        read, check_http_status alone."""
        key = quote(issue_key, safe="")
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(
                f"{self._base_url}/issue/{key}/worklog",
                headers={
                    "Authorization": self._auth_header,
                    "Accept": "application/json",
                },
            )
            check_http_status(response)
            return response.json()

    async def add_worklog(
        self, issue_key: str, time_spent_seconds: int, started: str, comment_adf: dict | None = None,
    ) -> dict:
        """POST .../issue/{key}/worklog - body {"timeSpentSeconds": ...,
        "started": ...}, optionally with "comment" (ADF). `started` must
        already be formatted the way Jira requires (ISO 8601 with a numeric
        timezone offset, no trailing 'Z') - callers go through
        jira/service.py's `_format_started_for_jira` helper, not raw
        ISO-formatting on their own. Returns the created worklog object."""
        key = quote(issue_key, safe="")
        body: dict = {"timeSpentSeconds": time_spent_seconds, "started": started}
        if comment_adf is not None:
            body["comment"] = comment_adf
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{self._base_url}/issue/{key}/worklog",
                headers={
                    "Authorization": self._auth_header,
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                json=body,
            )
            self._check_write_status(response)
            return response.json()

    async def edit_worklog(
        self, issue_key: str, worklog_id: str, time_spent_seconds: int | None = None,
        started: str | None = None, comment_adf: dict | None = None,
    ) -> dict:
        """PUT .../issue/{key}/worklog/{id} - same body shape as add_worklog,
        a partial update: only the given fields are included in the body.
        Returns the updated worklog object."""
        key = quote(issue_key, safe="")
        wid = quote(worklog_id, safe="")
        body: dict = {}
        if time_spent_seconds is not None:
            body["timeSpentSeconds"] = time_spent_seconds
        if started is not None:
            body["started"] = started
        if comment_adf is not None:
            body["comment"] = comment_adf
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.put(
                f"{self._base_url}/issue/{key}/worklog/{wid}",
                headers={
                    "Authorization": self._auth_header,
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                json=body,
            )
            self._check_write_status(response)
            return response.json()

    async def delete_worklog(self, issue_key: str, worklog_id: str) -> None:
        """DELETE .../issue/{key}/worklog/{id}."""
        key = quote(issue_key, safe="")
        wid = quote(worklog_id, safe="")
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.delete(
                f"{self._base_url}/issue/{key}/worklog/{wid}",
                headers={
                    "Authorization": self._auth_header,
                    "Accept": "application/json",
                },
            )
            self._check_write_status(response)

    async def delete_attachment(self, attachment_id: str) -> None:
        """DELETE .../attachment/{id} - a GLOBAL endpoint, unlike
        upload_attachment above: not scoped under /issue/{key}/ at all,
        mirroring delete_link's shape. Needs "Delete own attachments" or
        "Delete all attachments" permission - Jira enforces this
        server-side, ICX does not need to distinguish."""
        aid = quote(attachment_id, safe="")
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.delete(
                f"{self._base_url}/attachment/{aid}",
                headers={
                    "Authorization": self._auth_header,
                    "Accept": "application/json",
                },
            )
            self._check_write_status(response)

    def _check_write_status(self, response: httpx.Response) -> None:
        """A write call's 400 carries per-field validator info (`errors`)
        that a generic `check_http_status` 4xx fallback would discard - parse
        and raise it as JiraValidationError before falling through to the
        shared 401/403/404/429/5xx/other-4xx handling."""
        if response.status_code == 400:
            try:
                body = response.json()
            except ValueError:
                body = {}
            errors = body.get("errors") or {}
            messages = body.get("errorMessages") or []
            message = "; ".join(messages) if messages else "Jira rejected the request (validation error)."
            raise JiraValidationError(message, errors=errors)
        check_http_status(response)

    @asynccontextmanager
    async def attachment_session(self):
        """Open one shared httpx client for a batch of attachment downloads so they
        reuse connections (keep-alive) instead of one TLS handshake per attachment.
        Downloads outside a session keep the original per-call client behavior."""
        if self._dl_client is not None:
            # Already in a session (nested) - reuse the outer one, do not double-close.
            yield
            return
        client = httpx.AsyncClient(timeout=60.0)
        self._dl_client = client
        try:
            yield
        finally:
            self._dl_client = None
            await client.aclose()

    async def download_attachment(self, content_url: str) -> bytes:
        shared = self._dl_client
        if shared is not None:
            return await self._download_attachment_with(shared, content_url)
        async with httpx.AsyncClient(timeout=60.0) as client:
            return await self._download_attachment_with(client, content_url)

    async def _download_attachment_with(self, client: httpx.AsyncClient, content_url: str) -> bytes:
        current_url = content_url
        send_auth = True

        for _hop in range(_MAX_REDIRECT_HOPS + 1):
            parsed = urlparse(current_url)
            if parsed.scheme != "https" or parsed.netloc not in self._allowed_hosts:
                raise SourceUnavailable(
                    f"Attachment download blocked: the redirect target '{parsed.netloc}' is outside "
                    f"the allowed hosts for this connection. "
                    f"This safeguard prevents server-side request forgery (SSRF). "
                    f"Contact your Jira administrator if this attachment URL is legitimate."
                )

            headers = {"Authorization": self._auth_header} if send_auth else {}

            async with client.stream(
                "GET", current_url,
                headers=headers,
                follow_redirects=False,
            ) as response:
                if response.status_code not in (301, 302, 303, 307, 308):
                    response.raise_for_status()
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in response.aiter_bytes(65536):
                        total += len(chunk)
                        if total > _MAX_ATTACHMENT_BYTES:
                            raise SourceUnavailable(
                                f"Attachment exceeds the {_MAX_ATTACHMENT_BYTES // (1024 * 1024)} MB size limit - skipped."
                            )
                        chunks.append(chunk)
                    return b"".join(chunks)

                location = response.headers.get("Location", "")
                if not location:
                    raise SourceUnavailable(
                        "Attachment download failed: redirect response is missing the Location header. "
                        "The attachment URL may be broken."
                    )
                # Resolve relative Location headers against the current URL so a
                # legitimate same-host relative redirect is not misread as a
                # host change. The loop re-validates scheme/host on the next hop.
                resolved = urljoin(current_url, location)
                next_netloc = urlparse(resolved).netloc
                send_auth = (
                    bool(next_netloc)
                    and next_netloc.split(":")[0].lower() == parsed.netloc.split(":")[0].lower()
                )
                current_url = resolved

        raise SourceUnavailable(
            "Attachment download failed: exceeded the maximum redirect limit. "
            "The attachment URL may be in a redirect loop."
        )