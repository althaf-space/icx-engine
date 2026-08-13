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
from icx_engine.gitlab.client import GitLabClient, GitLabError
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


_TRANSITIONAL_MERGE_STATUSES = {"checking", "unchecked", "preparing"}
_MERGEABLE_MERGE_STATUSES = {"can_be_merged", "mergeable"}
_CONFLICTED_MERGE_STATUSES = {"cannot_be_merged", "cannot_be_merged_recheck", "conflict"}


def classify_merge_status(mr: dict) -> str:
    """Buckets a GitLab MR's mergeability into MERGEABLE/CONFLICTED/CHECKING/
    BLOCKED/UNKNOWN. Prefers `detailed_merge_status` (newer GitLab - names the
    real reason: ci_still_running/not_approved/need_rebase/
    discussions_not_resolved/draft_status/policies_denied/
    external_status_checks/broken_status/...) over the legacy `merge_status`
    field when both are present. Any named reason that is not itself a
    conflict is BLOCKED - never silently reported as CONFLICTED (a different,
    misleading diagnosis) or dropped."""
    status = mr.get("detailed_merge_status") or mr.get("merge_status")
    if status is None:
        return "UNKNOWN"
    status = str(status).lower()
    if status in _TRANSITIONAL_MERGE_STATUSES:
        return "CHECKING"
    if status in _MERGEABLE_MERGE_STATUSES:
        return "MERGEABLE"
    if status in _CONFLICTED_MERGE_STATUSES:
        return "CONFLICTED"
    if status == "unknown":
        return "UNKNOWN"
    return "BLOCKED"


async def wait_for_mergeable(
    client: GitLabClient, project_path: str, mr_iid: int,
    max_attempts: int = 5, delay_seconds: float = 2.0,
) -> dict:
    """Bounded poll of one MR's mergeability - GitLab computes merge_status
    asynchronously after creation/push, so a refusal immediately after create
    can be a stale CHECKING snapshot, not a real failure. Stops as soon as a
    terminal state (MERGEABLE/CONFLICTED/BLOCKED/UNKNOWN) is reached, or after
    max_attempts if it never leaves CHECKING - never polls indefinitely.
    Returns {"status": <bucket>, "attempts": <n>, "mr": <raw MR body>}."""
    import asyncio
    mr: dict = {}
    for attempt in range(1, max_attempts + 1):
        mr = await client.get_merge_request(project_path, mr_iid)
        bucket = classify_merge_status(mr)
        if bucket != "CHECKING":
            return {"status": bucket, "attempts": attempt, "mr": mr}
        if attempt < max_attempts:
            await asyncio.sleep(delay_seconds)
    return {"status": "CHECKING", "attempts": max_attempts, "mr": mr}


async def _pipeline_summary(client: GitLabClient, project_path: str, source_branch: str) -> dict | None:
    """Latest pipeline for source_branch, plus the failed job's name/id if the
    pipeline itself failed - lets a BLOCKED/failed merge refusal name the
    real reason (a specific failed job) instead of leaving the human to dig
    through gitlab_pipeline_status/gitlab_job_log by hand. Returns None if no
    pipeline exists yet or the lookup itself fails - never raises, this is
    diagnostic best-effort, not required for the merge result itself."""
    try:
        pipelines = await client.list_pipelines(project_path, ref=source_branch)
    except GitLabError:
        return None
    if not pipelines:
        return None
    latest = pipelines[0]
    summary: dict = {"id": latest.get("id"), "status": latest.get("status")}
    if latest.get("status") == "failed" and latest.get("id") is not None:
        try:
            detail = await client.get_pipeline(project_path, latest["id"])
        except GitLabError:
            return summary
        failed_jobs = [j for j in detail.get("jobs", []) if j.get("status") == "failed"]
        if failed_jobs:
            summary["failed_job_name"] = failed_jobs[0].get("name")
            summary["failed_job_id"] = failed_jobs[0].get("id")
    return summary


async def create_and_merge_mr(
    conn: "GitLabConnection", project_path: str, source_branch: str, target_branch: str,
    title: str, description: str, assignee_id: int,
    max_poll_attempts: int = 5, poll_delay_seconds: float = 2.0,
) -> dict:
    """Create an MR (or reuse an existing open one for this branch), then
    attempt one immediate merge (design spec Section 8.3-8.4). A refusal
    right after creation is not treated as permanent: GitLab computes
    mergeability asynchronously, so this checks whether the MR was still
    CHECKING at refusal time and, if so, polls (bounded by max_poll_attempts/
    poll_delay_seconds) until a terminal state is reached, retrying the merge
    exactly once if it settles on MERGEABLE. A genuine CONFLICTED/BLOCKED/
    UNKNOWN terminal state is never retried - the caller reports it and
    stops."""
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
        if merge_result["merged"]:
            return {
                "mr_iid": mr_iid, "created": created, "merged": True,
                "merge_status": "MERGEABLE", "refusal_reason": None,
            }

        poll = await wait_for_mergeable(
            client, project_path, mr_iid, max_attempts=max_poll_attempts, delay_seconds=poll_delay_seconds,
        )
        if poll["status"] != "MERGEABLE":
            return {
                "mr_iid": mr_iid, "created": created, "merged": False,
                "merge_status": poll["status"], "refusal_reason": merge_result.get("reason"),
                "has_conflicts": poll["mr"].get("has_conflicts"),
                "pipeline": await _pipeline_summary(client, project_path, source_branch),
            }
        # Mergeability finished computing since the first attempt - retry exactly once now that it's terminal.
        merge_result = await client.attempt_merge(project_path, mr_iid)
        if merge_result["merged"]:
            return {
                "mr_iid": mr_iid, "created": created, "merged": True,
                "merge_status": "MERGEABLE", "refusal_reason": None,
            }
        final_mr = await client.get_merge_request(project_path, mr_iid)
        return {
            "mr_iid": mr_iid, "created": created, "merged": False,
            "merge_status": classify_merge_status(final_mr),
            "refusal_reason": merge_result.get("reason"),
            "has_conflicts": final_mr.get("has_conflicts"),
            "pipeline": await _pipeline_summary(client, project_path, source_branch),
        }
