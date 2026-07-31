# src/icx_engine/gitlab/service.py
"""GitLab connection business logic - mirrors sonar/service.py's shape exactly:
named, multi-connection, one active, add/list/remove/set_active. Also owns
tag-name parsing/grouping and next-tag proposal (ParsedTag, parse_tag_name,
group_tags_by_environment, propose_next_tag)."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from icx_engine.config_manager import ConfigManager
from icx_engine.gitlab.client import GitLabClient
from icx_engine.models.config import GitLabConnection

_TAG_RE = re.compile(
    r"^v(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)-(?P<env>[a-zA-Z0-9]+)-(?P<date>\d{8})(?P<seq>\d{3})$"
)


@dataclass
class ParsedTag:
    name: str
    major: int
    minor: int
    patch: int
    environment: str
    date: str
    seq: int


def parse_tag_name(name: str) -> ParsedTag | None:
    """Parses `v<major>.<minor>.<patch>-<env>-<YYYYMMDD><seq:03d>`. Returns
    None for any tag not following this project's tagging convention -
    unrelated tags are simply excluded from grouping, never an error."""
    match = _TAG_RE.match(name)
    if not match:
        return None
    return ParsedTag(
        name=name,
        major=int(match["major"]), minor=int(match["minor"]), patch=int(match["patch"]),
        environment=match["env"], date=match["date"], seq=int(match["seq"]),
    )


def group_tags_by_environment(tags: list[dict]) -> dict[str, list[ParsedTag]]:
    """Groups GitLab tag API entries by environment label, newest-first per
    environment (by major.minor.patch, not by date - a same-day re-tag with a
    higher patch is 'newer' regardless of date/seq). 'Latest' is always scoped
    per environment - never computed globally across environments (design
    spec: show every environment's own lineage, let the human pick)."""
    grouped: dict[str, list[ParsedTag]] = {}
    for raw in tags:
        parsed = parse_tag_name(raw.get("name", ""))
        if parsed is None:
            continue
        grouped.setdefault(parsed.environment, []).append(parsed)
    for env in grouped:
        grouped[env].sort(key=lambda t: (t.major, t.minor, t.patch), reverse=True)
    return grouped


def propose_next_tag(environment: str, latest: ParsedTag | None, today: str | None = None) -> str:
    """Proposes the next tag for `environment`'s own lineage - a hard-gated
    SUGGESTION only, never auto-created. `today` is injectable for tests;
    production callers omit it and get the real current UTC date."""
    if latest is not None and latest.environment != environment:
        raise ValueError(
            f"propose_next_tag called with environment='{environment}' but latest tag "
            f"'{latest.name}' belongs to environment '{latest.environment}' - mismatched inputs."
        )
    if today is None:
        today = datetime.now(timezone.utc).strftime("%Y%m%d")
    if latest is None:
        return f"v0.0.1-{environment}-{today}001"
    new_patch = latest.patch + 1
    new_seq = latest.seq + 1 if latest.date == today else 1
    return f"v{latest.major}.{latest.minor}.{new_patch}-{latest.environment}-{today}{new_seq:03d}"


async def add_connection(
    name: str,
    url: str,
    token: str | None,
    verify_tls: bool = True,
    make_active: bool = False,
    cfg: Any | None = None,
) -> dict:
    """Add or update a named GitLab connection. The first connection added
    (or make_active=True) becomes the active one. Validates the connection live."""
    name = (name or "").strip()
    if not name:
        raise ValueError("Connection name is required.")
    cfg = cfg or ConfigManager.load()
    existing = cfg.gitlab_connections.get(name)
    conn = GitLabConnection(
        name=name,
        url=url or (existing.url if existing else ""),
        token=token or (existing.token if existing else None),
        verify_tls=verify_tls,
    )
    cfg.gitlab_connections[name] = conn
    if make_active or cfg.active_gitlab is None:
        cfg.active_gitlab = name
    ConfigManager.save(cfg)

    validation: dict = {"valid": None}
    if conn.url and conn.token:
        try:
            async with GitLabClient(conn.url, conn.token, conn.verify_tls) as client:
                validation = await client.validate()
        except Exception as exc:
            validation = {"valid": False, "error": str(exc)}
    return {
        "name": name,
        "url": conn.url,
        "verify_tls": conn.verify_tls,
        "active": cfg.active_gitlab == name,
        "validation": validation,
    }


async def status(cfg: Any | None = None) -> dict:
    cfg = cfg or ConfigManager.load()
    conn = cfg.active_gitlab_connection()
    out: dict = {
        "configured": conn is not None,
        "active": cfg.active_gitlab,
        "url": conn.url if conn else None,
        "verify_tls": conn.verify_tls if conn else None,
        "connection": None,
    }
    if conn and conn.token:
        try:
            async with GitLabClient(conn.url, conn.token, conn.verify_tls) as client:
                out["connection"] = await client.validate()
        except Exception as exc:
            out["connection"] = {"valid": False, "error": str(exc)}
    return out


def list_connections(cfg: Any | None = None) -> dict:
    cfg = cfg or ConfigManager.load()
    return {
        "active": cfg.active_gitlab,
        "connections": [
            {"name": c.name, "url": c.url, "verify_tls": c.verify_tls,
             "active": c.name == cfg.active_gitlab, "has_token": bool(c.token)}
            for c in cfg.gitlab_connections.values()
        ],
    }


def remove_connection(name: str, cfg: Any | None = None) -> dict:
    cfg = cfg or ConfigManager.load()
    if name not in cfg.gitlab_connections:
        raise KeyError(f"No GitLab connection named '{name}'.")
    cfg.gitlab_connections.pop(name, None)
    ConfigManager.delete_gitlab_connection_secret(name)
    if cfg.active_gitlab == name:
        cfg.active_gitlab = next(iter(cfg.gitlab_connections), None)
    ConfigManager.save(cfg)
    return {"removed": name, "active": cfg.active_gitlab}


def set_active(name: str, cfg: Any | None = None) -> dict:
    cfg = cfg or ConfigManager.load()
    if name not in cfg.gitlab_connections:
        raise KeyError(f"No GitLab connection named '{name}'.")
    cfg.active_gitlab = name
    ConfigManager.save(cfg)
    return {"active": cfg.active_gitlab}


async def create_and_merge_mr(
    conn: "GitLabConnection", project_path: str, source_branch: str, target_branch: str,
    title: str, description: str, assignee_id: int,
) -> dict:
    """Create an MR (or reuse an existing open one for this branch), then
    attempt one immediate merge (design spec Section 8.3-8.4). Never retries
    after a refusal - the caller reports the reason and stops."""
    async with GitLabClient(conn.url, conn.token, conn.verify_tls) as client:
        existing = await client.find_merge_request_for_branch(project_path, source_branch)
        if existing:
            mr_iid = existing["iid"]
            created = False
        else:
            mr = await client.create_merge_request(
                project_path, source_branch, target_branch, title, description,
                assignee_id=assignee_id, remove_source_branch=True,
            )
            mr_iid = mr["iid"]
            created = True
        merge_result = await client.attempt_merge(project_path, mr_iid)
        return {
            "mr_iid": mr_iid,
            "created": created,
            "merged": merge_result["merged"],
            "refusal_reason": merge_result.get("reason"),
        }
