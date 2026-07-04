"""Direct SonarQube Web API client (read-only).

Talks to a SonarQube server over its documented Web API. Only HTTP GET requests
are issued - the client cannot mutate the server. Authentication uses a user
token sent as HTTP Basic (token as username, empty password), which every
SonarQube version accepts.

Security:
- GET only; no POST/PUT/DELETE method exists here.
- http and https are both allowed (internal Sonar servers are often plain http
  on a private network); the target is operator-configured trusted infra.
- The auth token is sent only to the exact configured host. On a cross-host
  redirect the token is dropped, preventing it leaking via SSRF-style redirects.
- TLS certificates are verified by default; verification is disabled only when
  the operator explicitly opts in.
"""
from __future__ import annotations

import logging
from urllib.parse import urljoin, urlparse

import httpx

from icx_engine.exceptions import AuthError, RateLimited, SourceUnavailable
from icx_engine.models.sonar import (
    MEASURE_METRIC_KEYS,
    SonarDuplication,
    SonarDupBlock,
    SonarFinding,
    SonarGateCondition,
    SonarMeasures,
    SonarQualityGate,
    SonarScope,
    rating_letter,
)

_log = logging.getLogger(__name__)

_TIMEOUT = 30.0
_MAX_REDIRECTS = 3
_PAGE_SIZE = 500          # SonarQube max page size
_MAX_PAGES = 20           # hard ceiling: 20 * 500 = 10000 findings (SonarQube caps p*ps at 10000)
_ABS_MAX_FINDINGS = 10000
_PROJECT_LIST_CAP = 50    # discovery: never hand back more than this without a query
_BRANCH_LIST_CAP = 50


def _sonar_error_text(resp: httpx.Response) -> str:
    """SonarQube returns errors as {"errors":[{"msg":...}]}; surface them verbatim."""
    try:
        msgs = [e.get("msg", "") for e in resp.json().get("errors", []) if e.get("msg")]
        if msgs:
            return ": " + "; ".join(msgs)
    except Exception:
        pass
    return ""


def _raise_for_sonar(resp: httpx.Response) -> None:
    """Map SonarQube HTTP errors to ICX exceptions with Sonar-appropriate guidance."""
    code = resp.status_code
    if code < 400:
        return
    if code == 401:
        raise AuthError("SonarQube authentication failed. Check the token and run `icx sonar add` to update the connection.")
    if code == 403:
        raise AuthError("SonarQube permission denied. The token lacks access to this project or resource.")
    if code == 404:
        raise SourceUnavailable(
            f"SonarQube resource not found. Check the project key, branch, or file path{_sonar_error_text(resp)}."
        )
    if code == 429:
        raise RateLimited("SonarQube rate limited the request. Wait a moment and try again.")
    if code >= 500:
        raise SourceUnavailable("SonarQube server is unavailable. Try again later.")
    raise SourceUnavailable(f"SonarQube request failed (HTTP {code}){_sonar_error_text(resp)}.")


class SonarClient:
    """Async context manager wrapping one httpx client for a batch of reads."""

    def __init__(self, base_url: str, token: str | None, verify_tls: bool = True):
        parsed = urlparse((base_url or "").strip())
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError(f"Sonar base_url must be an absolute http/https URL, got: {base_url!r}")
        if parsed.username or parsed.password:
            raise ValueError("Sonar base_url must not contain embedded credentials.")
        self._base = f"{parsed.scheme}://{parsed.netloc}"
        self._host = parsed.netloc
        self._auth = httpx.BasicAuth(token, "") if token else None
        self._verify = verify_tls
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "SonarClient":
        self._client = httpx.AsyncClient(timeout=_TIMEOUT, verify=self._verify, follow_redirects=False)
        return self

    async def __aexit__(self, *exc) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # -- transport ---------------------------------------------------------

    async def _get_json(self, path: str, params: dict | None = None) -> dict:
        assert self._client is not None, "SonarClient must be used as an async context manager"
        current = self._base + path
        send_auth = True
        for _hop in range(_MAX_REDIRECTS + 1):
            parsed = urlparse(current)
            if parsed.scheme not in ("http", "https"):
                raise SourceUnavailable("Sonar request blocked: unsupported redirect scheme.")
            same_host = parsed.netloc == self._host
            auth = self._auth if (send_auth and same_host) else None
            resp = await self._client.get(current, params=params, auth=auth)
            params = None  # only applied to the first hop
            if resp.status_code in (301, 302, 303, 307, 308):
                location = resp.headers.get("Location", "")
                if not location:
                    break
                current = urljoin(current, location)
                send_auth = urlparse(current).netloc == self._host
                continue
            _raise_for_sonar(resp)
            return resp.json()
        raise SourceUnavailable("Sonar request exceeded the redirect limit.")

    # -- discovery ---------------------------------------------------------

    async def validate(self) -> dict:
        """Confirm the token is accepted and report server status/version."""
        auth_ok = False
        try:
            auth_ok = bool((await self._get_json("/api/authentication/validate")).get("valid"))
        except SourceUnavailable:
            raise
        status = {}
        try:
            status = await self._get_json("/api/system/status")
        except Exception:
            status = {}
        return {
            "valid": auth_ok,
            "server_status": status.get("status", ""),
            "version": status.get("version", ""),
        }

    async def projects(self, query: str | None = None, limit: int = _PROJECT_LIST_CAP) -> tuple[list[dict], int]:
        """Return (up to `limit` projects the token can browse, total match count).

        A `query` narrows by key/name substring (SonarQube `q`). One request only:
        SonarQube reports the full `paging.total` regardless of page size, so the
        caller can tell whether more matches exist without paging through them all.
        """
        params: dict = {"qualifiers": "TRK", "ps": min(max(limit, 1), _PAGE_SIZE), "p": 1}
        if query:
            params["q"] = query
        data = await self._get_json("/api/components/search", params)
        total = int(data.get("paging", {}).get("total", 0))
        items = [{"key": c.get("key", ""), "name": c.get("name", "")}
                 for c in data.get("components", [])]
        return items, total

    async def branches(self, project: str, query: str | None = None) -> list[dict]:
        data = await self._get_json("/api/project_branches/list", {"project": project})
        result = []
        for b in data.get("branches", []):
            result.append({
                "name": b.get("name", ""),
                "is_main": bool(b.get("isMain")),
                "type": b.get("type", ""),
                "quality_gate": (b.get("status") or {}).get("qualityGateStatus", ""),
                "analysis_date": b.get("analysisDate", ""),
            })
        if query:
            q = query.lower()
            result = [b for b in result if q in b["name"].lower()]
        return result

    # -- measures ----------------------------------------------------------

    async def measures(self, component: str, branch: str | None = None) -> SonarMeasures:
        params = {"component": component, "metricKeys": ",".join(MEASURE_METRIC_KEYS)}
        if branch:
            params["branch"] = branch
        data = await self._get_json("/api/measures/component", params)
        comp = data.get("component", {})
        return _parse_measures(comp.get("key", component), comp.get("measures", []))

    # -- quality gate ------------------------------------------------------

    async def quality_gate(self, project: str, branch: str | None = None) -> SonarQualityGate:
        params = {"projectKey": project}
        if branch:
            params["branch"] = branch
        data = await self._get_json("/api/qualitygates/project_status", params)
        status = data.get("projectStatus", {})
        conditions = [
            SonarGateCondition(
                metric=c.get("metricKey", ""),
                comparator=c.get("comparator", ""),
                error_threshold=str(c.get("errorThreshold", "")),
                actual_value=str(c.get("actualValue", "")),
                status=c.get("status", ""),
            )
            for c in status.get("conditions", [])
        ]
        return SonarQualityGate(status=status.get("status", "NONE"), conditions=conditions)

    # -- findings ----------------------------------------------------------

    async def issues(self, scope: SonarScope) -> tuple[list[SonarFinding], int, bool]:
        # SECURITY_HOTSPOT is not an issues/search type - hotspots have their own
        # API. If the caller asked for hotspots only, there is nothing to fetch here.
        issue_types = [t for t in scope.types if t != "SECURITY_HOTSPOT"]
        if scope.types and not issue_types:
            return [], 0, False

        component_keys = _component_keys(scope)
        params: dict = {
            "componentKeys": ",".join(component_keys),
            "ps": _PAGE_SIZE,
        }
        if scope.branch:
            params["branch"] = scope.branch
        if issue_types:
            params["types"] = ",".join(issue_types)
        if scope.severities:
            params["severities"] = ",".join(scope.severities)
        params["statuses"] = ",".join(scope.statuses) if scope.statuses else "OPEN,CONFIRMED,REOPENED"
        if scope.rules:
            params["rules"] = ",".join(scope.rules)
        if scope.tags:
            params["tags"] = ",".join(scope.tags)
        if scope.author:
            params["author"] = scope.author
        if scope.assignee:
            params["assignees"] = scope.assignee
        if scope.new_code_only:
            params["inNewCodePeriod"] = "true"

        cap = min(scope.limit if scope.limit > 0 else _ABS_MAX_FINDINGS, _ABS_MAX_FINDINGS)
        findings: list[SonarFinding] = []
        total = 0
        page = 1
        while page <= _MAX_PAGES:
            params["p"] = page
            data = await self._get_json("/api/issues/search", dict(params))
            total = int(data.get("total", data.get("paging", {}).get("total", 0)))
            for raw in data.get("issues", []):
                findings.append(_parse_issue(raw, scope.project))
                if len(findings) >= cap:
                    break
            if len(findings) >= cap or page * _PAGE_SIZE >= total or not data.get("issues"):
                break
            page += 1
        truncated = total > len(findings)
        return findings, total, truncated

    async def hotspots(self, scope: SonarScope) -> list[SonarFinding]:
        params: dict = {"projectKey": scope.project, "ps": _PAGE_SIZE}
        if scope.branch:
            params["branch"] = scope.branch
        if scope.new_code_only:
            params["inNewCodePeriod"] = "true"
        file_set = set(scope.files)
        out: list[SonarFinding] = []
        page = 1
        while page <= _MAX_PAGES:
            params["p"] = page
            data = await self._get_json("/api/hotspots/search", dict(params))
            hotspots = data.get("hotspots", [])
            for raw in hotspots:
                finding = _parse_hotspot(raw, scope.project)
                if file_set and (finding.file not in file_set):
                    continue
                out.append(finding)
            total = int(data.get("paging", {}).get("total", 0))
            if page * _PAGE_SIZE >= total or not hotspots:
                break
            page += 1
        return out

    # -- duplications ------------------------------------------------------

    async def duplications(self, project: str, files: list[str], branch: str | None = None) -> list[SonarDuplication]:
        out: list[SonarDuplication] = []
        for path in files:
            params: dict = {"key": f"{project}:{path}"}
            if branch:
                params["branch"] = branch
            try:
                data = await self._get_json("/api/duplications/show", params)
            except Exception as exc:
                # A missing/unanalyzed file (404) or any per-file error must not
                # abort the whole report - skip this file and continue.
                _log.debug("[sonar] duplications lookup failed for %s: %s", path, exc)
                continue
            file_refs = data.get("files", {})
            blocks: list[SonarDupBlock] = []
            for dup in data.get("duplications", []):
                for block in dup.get("blocks", []):
                    ref = block.get("_ref")
                    ref_component = (file_refs.get(ref) or {}).get("key") if ref else None
                    ref_path = _strip_project(ref_component, project) if ref_component else None
                    blocks.append(SonarDupBlock(
                        from_line=int(block.get("from", 0)),
                        size=int(block.get("size", 0)),
                        ref_file=ref_path,
                    ))
            if blocks:
                out.append(SonarDuplication(file=path, blocks=blocks))
        return out


# ---------------------------------------------------------------------------
# Normalizers (module-level, pure functions - easy to test in isolation)
# ---------------------------------------------------------------------------

def _component_keys(scope: SonarScope) -> list[str]:
    if scope.files:
        return [f"{scope.project}:{f}" for f in scope.files]
    return [scope.project]


def _strip_project(component: str | None, project: str) -> str | None:
    if not component:
        return None
    prefix = f"{project}:"
    if component.startswith(prefix):
        return component[len(prefix):]
    return component.split(":", 1)[-1]


def _effort_to_minutes(effort: str | None) -> int | None:
    """Parse a SonarQube effort string ('1h30min', '10min', '2d') to minutes."""
    if not effort:
        return None
    total = 0
    num = ""
    unit_minutes = {"d": 8 * 60, "h": 60, "min": 1}
    i = 0
    text = effort.strip()
    while i < len(text):
        ch = text[i]
        if ch.isdigit():
            num += ch
            i += 1
            continue
        if text[i:i + 3] == "min":
            total += int(num or 0) * 1
            num = ""
            i += 3
            continue
        if ch in unit_minutes:
            total += int(num or 0) * unit_minutes[ch]
            num = ""
            i += 1
            continue
        i += 1
    return total or None


def _parse_issue(raw: dict, project: str) -> SonarFinding:
    return SonarFinding(
        key=raw.get("key", ""),
        type=raw.get("type", ""),
        severity=raw.get("severity", ""),
        rule=raw.get("rule", ""),
        message=raw.get("message", ""),
        file=_strip_project(raw.get("component"), project),
        line=raw.get("line"),
        status=raw.get("status", ""),
        effort=raw.get("effort") or raw.get("debt"),
        effort_minutes=_effort_to_minutes(raw.get("effort") or raw.get("debt")),
        author=raw.get("author", ""),
        assignee=raw.get("assignee", ""),
        tags=list(raw.get("tags", []) or []),
        new_code=bool(raw.get("inNewCodePeriod")),
        creation_date=raw.get("creationDate", ""),
        update_date=raw.get("updateDate", ""),
    )


def _parse_hotspot(raw: dict, project: str) -> SonarFinding:
    return SonarFinding(
        key=raw.get("key", ""),
        type="SECURITY_HOTSPOT",
        severity=raw.get("vulnerabilityProbability", ""),
        rule=raw.get("ruleKey", ""),
        message=raw.get("message", ""),
        file=_strip_project(raw.get("component"), project),
        line=raw.get("line"),
        status=raw.get("status", ""),
        author=raw.get("author", ""),
        assignee=raw.get("assignee", ""),
        security_category=raw.get("securityCategory", ""),
        new_code=bool(raw.get("inNewCodePeriod")),
        creation_date=raw.get("creationDate", ""),
        update_date=raw.get("updateDate", ""),
    )


def _to_int(value: str | None) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (ValueError, TypeError):
        return None


def _to_float(value: str | None) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def _debt_human(minutes: int | None) -> str:
    if not minutes:
        return ""
    days, rem = divmod(minutes, 8 * 60)
    hours, mins = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if mins:
        parts.append(f"{mins}min")
    return " ".join(parts)


def _parse_measures(component: str, measures: list[dict]) -> SonarMeasures:
    raw = {m.get("metric", ""): str(m.get("value", "")) for m in measures if m.get("metric")}
    debt_minutes = _to_int(raw.get("sqale_index"))
    return SonarMeasures(
        component=component,
        bugs=_to_int(raw.get("bugs")),
        vulnerabilities=_to_int(raw.get("vulnerabilities")),
        code_smells=_to_int(raw.get("code_smells")),
        security_hotspots=_to_int(raw.get("security_hotspots")),
        coverage=_to_float(raw.get("coverage")),
        line_coverage=_to_float(raw.get("line_coverage")),
        uncovered_lines=_to_int(raw.get("uncovered_lines")),
        duplicated_lines_density=_to_float(raw.get("duplicated_lines_density")),
        duplicated_lines=_to_int(raw.get("duplicated_lines")),
        duplicated_blocks=_to_int(raw.get("duplicated_blocks")),
        duplicated_files=_to_int(raw.get("duplicated_files")),
        ncloc=_to_int(raw.get("ncloc")),
        technical_debt_minutes=debt_minutes,
        technical_debt=_debt_human(debt_minutes),
        reliability_rating=rating_letter(raw.get("reliability_rating")),
        security_rating=rating_letter(raw.get("security_rating")),
        maintainability_rating=rating_letter(raw.get("sqale_rating")),
        security_review_rating=rating_letter(raw.get("security_review_rating")),
        tests=_to_int(raw.get("tests")),
        test_failures=_to_int(raw.get("test_failures")),
        test_errors=_to_int(raw.get("test_errors")),
        skipped_tests=_to_int(raw.get("skipped_tests")),
        test_success_density=_to_float(raw.get("test_success_density")),
        test_execution_time_ms=_to_int(raw.get("test_execution_time")),
        new_bugs=_to_int(raw.get("new_bugs")),
        new_vulnerabilities=_to_int(raw.get("new_vulnerabilities")),
        new_code_smells=_to_int(raw.get("new_code_smells")),
        new_coverage=_to_float(raw.get("new_coverage")),
        new_duplicated_lines_density=_to_float(raw.get("new_duplicated_lines_density")),
        raw=raw,
    )
