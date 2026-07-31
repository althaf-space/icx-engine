"""Backup branches and self-heal leftover-state detection (design spec
Sections 5 and 10). Backups are local-only branches, never tags, never
pushed - discoverable in a plain `git branch` list without knowing anything
about how ICX works internally."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from icx_engine.git.gitcmd import _run_git, _stdout, create_branch_from, checkout, local_branch_exists
from icx_engine.git.naming import slugify

_STASH_TAG_PREFIX = "icx:"


def create_backup(repo: Path, source_branch: str, ticket_key: str) -> str:
    """Create backup/<TICKET>-<slug>-<timestamp> pointing at source_branch's
    current commit, without switching to it. Returns the backup branch name.

    Backup names are second-resolution timestamps, so two calls for the same
    ticket+source_branch within the same second (e.g. reverse_merge_standard
    then start_conflict_resolution called back-to-back) would otherwise collide
    on `git branch <name> <start>`, which refuses to recreate an existing name.
    Since nothing can change source_branch's HEAD between two such calls, an
    existing same-name backup already captures the identical commit a new one
    would - skip creation and return the existing name instead of erroring."""
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    name = f"backup/{ticket_key}-{slugify(source_branch)}-{timestamp}"
    if not local_branch_exists(repo, name):
        create_branch_from(repo, name, source_branch)
    return name


def create_scratch_branch(repo: Path, source_branch: str, ticket_key: str) -> str:
    """Create AND switch to scratch/<TICKET>-<slug>-<timestamp> for conflict
    resolution (design spec Section 7.2). Unlike create_backup, this switches
    to the new branch since resolution work happens directly on it - the real
    feature branch is left untouched at source_branch's tip the whole time."""
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    name = f"scratch/{ticket_key}-{slugify(source_branch)}-{timestamp}"
    create_branch_from(repo, name, source_branch)
    checkout(repo, name)
    return name


def list_backups(repo: Path, ticket_key: str) -> list[str]:
    result = _run_git(repo, ["branch", "--list", f"backup/{ticket_key}-*", "--format=%(refname:short)"])
    out = _stdout(result)
    return [line for line in out.splitlines() if line]


def prune_old_backups(repo: Path, ticket_key: str, keep: int = 3) -> list[str]:
    """Deletes the oldest backups beyond `keep`, oldest first by name (names
    embed a sortable timestamp). Returns the list of deleted branch names."""
    backups = sorted(list_backups(repo, ticket_key))
    to_prune = backups[:len(backups) - keep] if len(backups) > keep else []
    for name in to_prune:
        _run_git(repo, ["branch", "-D", name])
    return to_prune


@dataclass
class LeftoverState:
    scratch_branches: list[str] = field(default_factory=list)
    icx_stashes: list[str] = field(default_factory=list)
    merge_in_progress: bool = False

    @property
    def is_clean(self) -> bool:
        return not self.scratch_branches and not self.icx_stashes and not self.merge_in_progress


def detect_leftover_state(repo: Path) -> LeftoverState:
    """Checks for anything left over from an interrupted prior ICX run - a
    scratch branch from the conflict-quarantine flow, an ICX-tagged stash, or
    a merge-in-progress marker. Called at the start of every lifecycle
    operation (design spec Section 10) - never just once at startup."""
    branches_result = _run_git(repo, ["branch", "--list", "scratch/*", "--format=%(refname:short)"])
    scratch_branches = [b for b in _stdout(branches_result).splitlines() if b]

    stash_result = _run_git(repo, ["stash", "list"])
    icx_stashes = [
        line for line in _stdout(stash_result).splitlines()
        if line.rpartition(": ")[2].startswith(_STASH_TAG_PREFIX)
    ]

    merge_in_progress = (repo / ".git" / "MERGE_HEAD").exists()

    return LeftoverState(
        scratch_branches=scratch_branches,
        icx_stashes=icx_stashes,
        merge_in_progress=merge_in_progress,
    )
