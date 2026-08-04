from __future__ import annotations
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
import subprocess

from icx_engine.git.gitcmd import local_branch_exists, current_branch, head_sha
from icx_engine.git.safety import (
    create_backup, sync_backup, list_backups, prune_old_backups, detect_leftover_state,
)


def test_create_backup_creates_branch_without_switching(tmp_git_repo):
    name = create_backup(tmp_git_repo, "main", "ABC-1")
    assert name.startswith("backup/ABC-1-")
    assert local_branch_exists(tmp_git_repo, name) is True
    assert current_branch(tmp_git_repo) == "main"


def test_create_backup_same_second_second_call_does_not_raise(tmp_git_repo):
    """Regression test: two calls for the same ticket+source_branch within the
    same second (e.g. reverse_merge_standard then start_conflict_resolution
    called back-to-back) must not collide on the timestamp-based branch name.
    Clock is frozen so this is deterministic - it does not depend on both real
    calls happening to land within the same wall-clock second."""
    frozen = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    with patch("icx_engine.git.safety.datetime") as mock_datetime:
        mock_datetime.now.return_value = frozen
        first = create_backup(tmp_git_repo, "main", "ABC-1")
        second = create_backup(tmp_git_repo, "main", "ABC-1")
    assert first == second
    assert local_branch_exists(tmp_git_repo, first) is True


def test_sync_backup_creates_latest_branch_without_switching(tmp_git_repo):
    name = sync_backup(tmp_git_repo, "main", "ABC-1")
    assert name == "backup-latest/ABC-1"
    assert local_branch_exists(tmp_git_repo, name) is True
    assert current_branch(tmp_git_repo) == "main"


def test_sync_backup_moves_existing_pointer_to_new_commit(tmp_git_repo):
    """The core requirement this fixes: the backup must never trail behind -
    a second sync_backup call after a new commit must move the SAME branch
    name forward, not create a stale duplicate."""
    import subprocess
    first = sync_backup(tmp_git_repo, "main", "ABC-1")
    first_sha = head_sha(tmp_git_repo)

    (tmp_git_repo / "b.txt").write_text("b", encoding="utf-8")
    subprocess.run(["git", "add", "b.txt"], cwd=str(tmp_git_repo), check=True)
    subprocess.run(["git", "commit", "-m", "ABC-1 second commit"], cwd=str(tmp_git_repo), check=True)
    second_sha = head_sha(tmp_git_repo)
    assert second_sha != first_sha

    second = sync_backup(tmp_git_repo, "main", "ABC-1")
    assert second == first
    subprocess.run(["git", "rev-parse", "--verify", first], cwd=str(tmp_git_repo), check=True)
    backup_sha = subprocess.run(
        ["git", "rev-parse", first], cwd=str(tmp_git_repo), check=True, capture_output=True, text=True,
    ).stdout.strip()
    assert backup_sha == second_sha


def test_sync_backup_is_local_only_distinct_from_timestamped_create_backup(tmp_git_repo):
    latest = sync_backup(tmp_git_repo, "main", "ABC-1")
    timestamped = create_backup(tmp_git_repo, "main", "ABC-1")
    assert latest != timestamped
    assert latest == "backup-latest/ABC-1"
    assert timestamped.startswith("backup/ABC-1-")
    assert local_branch_exists(tmp_git_repo, latest) is True
    assert local_branch_exists(tmp_git_repo, timestamped) is True


def test_sync_backup_never_appears_in_list_backups_or_pruning(tmp_git_repo):
    """sync_backup's backup-latest/<key> prefix must never collide with
    list_backups/prune_old_backups' backup/{ticket_key}-* glob - it is a
    different mechanism (a single moving pointer, not timestamped history)
    and must never be counted or deleted by pruning logic meant for the other."""
    sync_backup(tmp_git_repo, "main", "ABC-1")
    create_backup(tmp_git_repo, "main", "ABC-1")
    backups = list_backups(tmp_git_repo, "ABC-1")
    assert len(backups) == 1
    assert all("latest" not in b for b in backups)


def test_list_backups_returns_only_matching_ticket(tmp_git_repo):
    create_backup(tmp_git_repo, "main", "ABC-1")
    create_backup(tmp_git_repo, "main", "XYZ-9")
    backups = list_backups(tmp_git_repo, "ABC-1")
    assert len(backups) == 1
    assert backups[0].startswith("backup/ABC-1-")


def test_prune_old_backups_keeps_only_newest_n(tmp_git_repo):
    import time
    names = []
    for _ in range(4):
        names.append(create_backup(tmp_git_repo, "main", "ABC-1"))
        time.sleep(1.1)  # timestamps are second-resolution; force distinct names
    pruned = prune_old_backups(tmp_git_repo, "ABC-1", keep=3)
    assert len(pruned) == 1
    assert pruned[0] == names[0]  # oldest one pruned
    remaining = list_backups(tmp_git_repo, "ABC-1")
    assert len(remaining) == 3
    assert names[0] not in remaining


def test_detect_leftover_state_clean_repo(tmp_git_repo):
    state = detect_leftover_state(tmp_git_repo)
    assert state.scratch_branches == []
    assert state.icx_stashes == []
    assert state.merge_in_progress is False


def test_detect_leftover_state_finds_scratch_branch(tmp_git_repo):
    subprocess.run(["git", "branch", "scratch/ABC-1-reverse-merge"], cwd=str(tmp_git_repo), check=True)
    state = detect_leftover_state(tmp_git_repo)
    assert state.scratch_branches == ["scratch/ABC-1-reverse-merge"]


def test_detect_leftover_state_finds_icx_stash(tmp_git_repo):
    (tmp_git_repo / "x.txt").write_text("x", encoding="utf-8")
    subprocess.run(["git", "stash", "push", "-u", "-m", "icx:ABC-1:2026-01-01"],
                    cwd=str(tmp_git_repo), check=True)
    state = detect_leftover_state(tmp_git_repo)
    assert len(state.icx_stashes) == 1
    assert "icx:ABC-1" in state.icx_stashes[0]


def test_prune_old_backups_keep_zero_prunes_all(tmp_git_repo):
    create_backup(tmp_git_repo, "main", "ABC-2")
    pruned = prune_old_backups(tmp_git_repo, "ABC-2", keep=0)
    assert len(pruned) == 1
    assert list_backups(tmp_git_repo, "ABC-2") == []


def test_detect_leftover_state_ignores_unrelated_stash_with_similar_substring(tmp_git_repo):
    (tmp_git_repo / "y.txt").write_text("y", encoding="utf-8")
    subprocess.run(["git", "stash", "push", "-u", "-m", "unrelated: mentions icx: in passing"],
                    cwd=str(tmp_git_repo), check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    state = detect_leftover_state(tmp_git_repo)
    assert state.icx_stashes == []


def test_create_scratch_branch_creates_and_switches_to_it(tmp_git_repo):
    from icx_engine.git.safety import create_scratch_branch
    name = create_scratch_branch(tmp_git_repo, "main", "ABC-1")
    assert name.startswith("scratch/ABC-1-")
    assert current_branch(tmp_git_repo) == name


def test_create_scratch_branch_matches_leftover_detection_glob(tmp_git_repo):
    from icx_engine.git.safety import create_scratch_branch, detect_leftover_state
    name = create_scratch_branch(tmp_git_repo, "main", "ABC-1")
    state = detect_leftover_state(tmp_git_repo)
    assert name in state.scratch_branches
