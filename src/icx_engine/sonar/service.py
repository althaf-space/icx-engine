"""SonarQube code-quality service (direct, read-only).

Assembles structured reports from a SonarQube server via its Web API. No proxy,
no LLM: every value comes straight from SonarQube GET endpoints and is returned
as typed, JSON-serializable data for an MCP agent or the CLI to consume.

Connection: `configure` stores only the server URL and token. Project and branch
are always chosen per request (discover them with `projects` and `branches`).
Files are supplied by the caller only - ICX never derives them.
"""
from __future__ import annotations

import logging
from typing import Any

from icx_engine.config_manager import ConfigManager
from icx_engine.models.config import SonarConnection
from icx_engine.models.sonar import (
    SonarReport,
    SonarScope,
    SonarTestGap,
)
from icx_engine.sonar import rules
from icx_engine.sonar.client import SonarClient, _BRANCH_LIST_CAP, _PROJECT_LIST_CAP
from icx_engine.sonar.parse import parse_sonar_url

_log = logging.getLogger(__name__)

DISABLED_MSG = "No active SonarQube connection. Run `icx sonar add` to add one."
NOT_CONFIGURED_MSG = "The active SonarQube connection has no stored token. Run `icx sonar add` to reconfigure it."


class SonarDisabled(Exception):
    pass


class SonarNotConfigured(Exception):
    pass


def sonar_enabled(cfg: Any) -> bool:
    """Sonar is 'on' when an active connection resolves - there is no separate flag."""
    return cfg.active_sonar_connection() is not None


def _require_enabled(cfg: Any) -> None:
    if not sonar_enabled(cfg):
        raise SonarDisabled(DISABLED_MSG)


def _make_client(cfg: Any) -> SonarClient:
    conn = cfg.active_sonar_connection()
    if conn is None:
        raise SonarDisabled(DISABLED_MSG)
    if not conn.url or not conn.token:
        raise SonarNotConfigured(NOT_CONFIGURED_MSG)
    return SonarClient(base_url=conn.url, token=conn.token, verify_tls=conn.verify_tls)


def _build_scope(
    project: str,
    branch: str | None,
    files: list[str] | None,
    types: list[str] | None,
    severities: list[str] | None,
    statuses: list[str] | None,
    author: str | None,
    assignee: str | None,
    new_code_only: bool,
    limit: int,
) -> SonarScope:
    return SonarScope(
        project=project,
        branch=branch,
        files=list(files or []),
        types=list(types or []),
        severities=list(severities or []),
        statuses=list(statuses or []),
        author=author,
        assignee=assignee,
        new_code_only=bool(new_code_only),
        limit=limit,
    )


# -- connection management (mirrors `icx model` profiles) ------------------

async def add_connection(
    name: str,
    url: str,
    token: str | None,
    verify_tls: bool = True,
    make_active: bool = False,
    cfg: Any | None = None,
) -> dict:
    """Add or update a named SonarQube connection. The first connection added
    (or make_active=True) becomes the active one. Validates the connection live."""
    name = (name or "").strip()
    if not name:
        raise ValueError("Connection name is required.")
    cfg = cfg or ConfigManager.load()
    existing = cfg.sonar_connections.get(name)
    base_url = parse_sonar_url(url).base_url if url else (existing.url if existing else "")
    conn = SonarConnection(
        name=name,
        url=base_url,
        token=token or (existing.token if existing else None),
        verify_tls=verify_tls,
    )
    cfg.sonar_connections[name] = conn
    if make_active or cfg.active_sonar is None:
        cfg.active_sonar = name
    ConfigManager.save(cfg)

    validation: dict = {"valid": None}
    if conn.url and conn.token:
        try:
            async with _make_client(cfg if cfg.active_sonar == name else _single(conn)) as client:
                validation = await client.validate()
        except Exception as exc:
            validation = {"valid": False, "error": str(exc)}
    return {
        "name": name,
        "url": conn.url,
        "verify_tls": conn.verify_tls,
        "active": cfg.active_sonar == name,
        "validation": validation,
    }


class _single:
    """Tiny cfg-like shim so a specific connection can be validated even when it
    is not the active one."""
    def __init__(self, conn: SonarConnection):
        self._conn = conn

    def active_sonar_connection(self):
        return self._conn


def list_connections(cfg: Any | None = None) -> dict:
    cfg = cfg or ConfigManager.load()
    return {
        "active": cfg.active_sonar,
        "connections": [
            {"name": c.name, "url": c.url, "verify_tls": c.verify_tls,
             "active": c.name == cfg.active_sonar, "has_token": bool(c.token)}
            for c in cfg.sonar_connections.values()
        ],
    }


def remove_connection(name: str, cfg: Any | None = None) -> dict:
    cfg = cfg or ConfigManager.load()
    if name not in cfg.sonar_connections:
        raise KeyError(f"No SonarQube connection named '{name}'.")
    cfg.sonar_connections.pop(name, None)
    ConfigManager.delete_sonar_connection_secret(name)
    if cfg.active_sonar == name:
        cfg.active_sonar = next(iter(cfg.sonar_connections), None)
    ConfigManager.save(cfg)
    return {"removed": name, "active": cfg.active_sonar}


def set_active(name: str, cfg: Any | None = None) -> dict:
    cfg = cfg or ConfigManager.load()
    if name not in cfg.sonar_connections:
        raise KeyError(f"No SonarQube connection named '{name}'.")
    cfg.active_sonar = name
    ConfigManager.save(cfg)
    return {"active": name}


async def status(cfg: Any | None = None) -> dict:
    cfg = cfg or ConfigManager.load()
    conn = cfg.active_sonar_connection()
    out: dict = {
        "enabled": conn is not None,
        "active": cfg.active_sonar,
        "count": len(cfg.sonar_connections),
        "url": conn.url if conn else None,
        "verify_tls": conn.verify_tls if conn else None,
        "configured": bool(conn and conn.token),
        "connection": None,
    }
    if conn and conn.token:
        try:
            async with _make_client(cfg) as client:
                out["connection"] = await client.validate()
        except Exception as exc:
            out["connection"] = {"valid": False, "error": str(exc)}
    return out


def _selection_instructions(kind: str, total: int, shown: int, truncated: bool, query: str | None) -> str:
    if truncated and shown == 0:
        status = (
            f"{total} {kind}s exist on the server - too many to list, so the list has been "
            f"withheld. Ask the user to paste the exact {kind}, or give a search term to pass "
            f"as `query`."
        )
    elif truncated:
        status = (
            f"Showing {shown} of {total} matching {kind}s. More matches exist - if none fit, "
            f"ask the user to refine the `query` or paste the exact {kind}."
        )
    else:
        status = f"{total} {kind}(s) returned. Ask the user which one, or let them paste the exact {kind}."
    body = rules.selection_rules()
    return f"{status}\n\n{body}" if body else status


async def projects(query: str | None = None, cfg: Any | None = None) -> dict:
    cfg = cfg or ConfigManager.load()
    _require_enabled(cfg)
    async with _make_client(cfg) as client:
        items, total = await client.projects(query=query, limit=_PROJECT_LIST_CAP)
    # Withhold a giant unfiltered list: force the user to narrow with `query` or paste a key.
    show = bool(query) or total <= _PROJECT_LIST_CAP
    shown = items if show else []
    truncated = total > len(shown)
    return {
        "total": total,
        "returned": len(shown),
        "truncated": truncated,
        "query": query or "",
        "projects": shown,
        "instructions": _selection_instructions("project", total, len(shown), truncated, query),
    }


async def branches(project: str, query: str | None = None, cfg: Any | None = None) -> dict:
    cfg = cfg or ConfigManager.load()
    _require_enabled(cfg)
    async with _make_client(cfg) as client:
        items = await client.branches(project, query=query)
    total = len(items)
    if total > _BRANCH_LIST_CAP:
        shown = items[:_BRANCH_LIST_CAP] if query else []
    else:
        shown = items
    truncated = total > len(shown)
    return {
        "project": project,
        "total": total,
        "returned": len(shown),
        "truncated": truncated,
        "query": query or "",
        "branches": shown,
        "instructions": _selection_instructions("branch", total, len(shown), truncated, query),
    }


async def measures(project: str, branch: str | None = None, cfg: Any | None = None) -> dict:
    cfg = cfg or ConfigManager.load()
    _require_enabled(cfg)
    async with _make_client(cfg) as client:
        return (await client.measures(project, branch)).model_dump()


async def quality_gate(project: str, branch: str | None = None, cfg: Any | None = None) -> dict:
    cfg = cfg or ConfigManager.load()
    _require_enabled(cfg)
    async with _make_client(cfg) as client:
        return (await client.quality_gate(project, branch)).model_dump()


def _summarize(findings: list) -> dict:
    by_type: dict[str, int] = {}
    by_severity: dict[str, int] = {}
    for f in findings:
        by_type[f.type] = by_type.get(f.type, 0) + 1
        by_severity[f.severity] = by_severity.get(f.severity, 0) + 1
    return {"total": len(findings), "by_type": by_type, "by_severity": by_severity}


async def findings(
    project: str,
    branch: str | None = None,
    files: list[str] | None = None,
    types: list[str] | None = None,
    severities: list[str] | None = None,
    statuses: list[str] | None = None,
    author: str | None = None,
    assignee: str | None = None,
    new_code_only: bool = False,
    limit: int = 1000,
    cfg: Any | None = None,
) -> dict:
    cfg = cfg or ConfigManager.load()
    _require_enabled(cfg)
    scope = _build_scope(project, branch, files, types, severities, statuses,
                         author, assignee, new_code_only, limit)
    async with _make_client(cfg) as client:
        issues, total, truncated = await client.issues(scope)
        want_hotspots = (not scope.types) or ("SECURITY_HOTSPOT" in scope.types)
        hotspots = await client.hotspots(scope) if want_hotspots else []
    all_findings = issues + hotspots
    return {
        "project": project,
        "branch": branch,
        "findings": [f.model_dump() for f in all_findings],
        "summary": _summarize(all_findings),
        "total_findings": total + len(hotspots),
        "truncated": truncated,
    }


async def report(
    project: str,
    branch: str | None = None,
    files: list[str] | None = None,
    types: list[str] | None = None,
    severities: list[str] | None = None,
    statuses: list[str] | None = None,
    author: str | None = None,
    assignee: str | None = None,
    new_code_only: bool = False,
    limit: int = 1000,
    cfg: Any | None = None,
) -> dict:
    cfg = cfg or ConfigManager.load()
    _require_enabled(cfg)
    scope = _build_scope(project, branch, files, types, severities, statuses,
                         author, assignee, new_code_only, limit)

    _active = cfg.active_sonar_connection()
    server_url = _active.url if _active else ""
    async with _make_client(cfg) as client:
        gate = await client.quality_gate(project, branch)
        project_measures = await client.measures(project, branch)
        issues, total, truncated = await client.issues(scope)
        want_hotspots = (not scope.types) or ("SECURITY_HOTSPOT" in scope.types)
        hotspots = await client.hotspots(scope) if want_hotspots else []

        file_measures: dict = {}
        for path in scope.files:
            try:
                fm = await client.measures(f"{project}:{path}", branch)
                file_measures[path] = fm
            except Exception as exc:
                _log.debug("[sonar] per-file measures failed for %s: %s", path, exc)

        duplications = await client.duplications(project, scope.files, branch) if scope.files else []

    test_gaps: list[SonarTestGap] = []
    for path, fm in file_measures.items():
        if fm.coverage is not None and fm.coverage == 0.0:
            test_gaps.append(SonarTestGap(file=path, coverage=fm.coverage, has_tests=False))

    all_findings = issues + hotspots
    result = SonarReport(
        project=project,
        branch=branch,
        server_url=server_url,
        quality_gate=gate,
        measures=project_measures,
        file_measures=file_measures,
        findings=all_findings,
        duplications=duplications,
        test_gaps=test_gaps,
        summary=_summarize(all_findings),
        total_findings=total + len(hotspots),
        truncated=truncated,
    )
    return result.model_dump()
