from __future__ import annotations
from pathlib import Path
import pytest

from icx_engine.git.manager import GitLifecycleManager, GitWorkflowError, ParentResolution


def test_validate_raises_for_non_repo(tmp_path):
    not_a_repo = tmp_path / "nope"
    not_a_repo.mkdir()
    mgr = GitLifecycleManager(not_a_repo)
    with pytest.raises(GitWorkflowError):
        mgr.validate()


def test_validate_returns_repo_root_for_real_repo(tmp_git_repo):
    mgr = GitLifecycleManager(tmp_git_repo)
    assert mgr.validate() == tmp_git_repo.resolve()


def test_check_leftover_state_clean(tmp_git_repo):
    mgr = GitLifecycleManager(tmp_git_repo)
    mgr.validate()
    state = mgr.check_leftover_state()
    assert state.is_clean is True


def test_resolve_parent_uses_stored_setting_when_still_valid(tmp_git_repo_with_remote, monkeypatch):
    monkeypatch.setattr("icx_engine.git.manager.read_repo_settings", lambda root: {"parent_branch": "main"})
    mgr = GitLifecycleManager(tmp_git_repo_with_remote)
    mgr.validate()
    result = mgr.resolve_parent_branch()
    assert result == ParentResolution(status="resolved", parent_branch="main")


def test_resolve_parent_reasks_when_stored_branch_gone(tmp_git_repo_with_remote, monkeypatch):
    monkeypatch.setattr("icx_engine.git.manager.read_repo_settings",
                         lambda root: {"parent_branch": "renamed-away"})
    mgr = GitLifecycleManager(tmp_git_repo_with_remote)
    mgr.validate()
    result = mgr.resolve_parent_branch()
    assert result.status in ("needs_confirmation", "needs_manual_pick")


def test_resolve_parent_no_stored_setting_proposes_development_if_it_exists(tmp_git_repo_with_remote, monkeypatch):
    import subprocess
    subprocess.run(["git", "branch", "development"], cwd=str(tmp_git_repo_with_remote), check=True)
    subprocess.run(["git", "push", "-u", "origin", "development"], cwd=str(tmp_git_repo_with_remote), check=True)
    monkeypatch.setattr("icx_engine.git.manager.read_repo_settings", lambda root: {})
    mgr = GitLifecycleManager(tmp_git_repo_with_remote)
    mgr.validate()
    result = mgr.resolve_parent_branch()
    assert result.status == "needs_confirmation"
    assert result.proposed_default == "development"


def test_resolve_parent_no_development_shows_available_branches(tmp_git_repo_with_remote, monkeypatch):
    monkeypatch.setattr("icx_engine.git.manager.read_repo_settings", lambda root: {})
    mgr = GitLifecycleManager(tmp_git_repo_with_remote)
    mgr.validate()
    result = mgr.resolve_parent_branch()
    assert result.status == "needs_manual_pick"
    assert "main" in result.available_branches


def test_confirm_parent_branch_persists_valid_choice(tmp_git_repo_with_remote, monkeypatch):
    written = {}
    monkeypatch.setattr("icx_engine.git.manager.write_repo_settings",
                         lambda root, **kw: written.update(kw))
    mgr = GitLifecycleManager(tmp_git_repo_with_remote)
    mgr.validate()
    mgr.confirm_parent_branch("main")
    assert written == {"parent_branch": "main"}


def test_confirm_parent_branch_rejects_nonexistent_branch(tmp_git_repo_with_remote):
    mgr = GitLifecycleManager(tmp_git_repo_with_remote)
    mgr.validate()
    with pytest.raises(GitWorkflowError):
        mgr.confirm_parent_branch("does-not-exist-anywhere")


# -- _auth_env: systemic fix - every network call routes through one place --

def test_manager_lazily_resolves_gitlab_conn_via_config_manager_when_not_constructed_with_one(
    tmp_git_repo_with_remote, monkeypatch,
):
    """The actual fix for "the auth fix only covered push, not fetch/ls-remote": a
    manager built with no gitlab_conn (every existing call site) must still resolve
    one itself, lazily, the first time _auth_env() is called - not rely on every
    caller remembering to pass one in."""
    from unittest.mock import patch
    from icx_engine.models.config import GitLabConnection

    conn = GitLabConnection(name="gitlab.example.com", url="https://gitlab.example.com", token="glpat-x")
    monkeypatch.setattr(
        "icx_engine.git.manager.remote_url",
        lambda repo, remote="origin": "https://gitlab.example.com/group/project.git",
    )
    mgr = GitLifecycleManager(tmp_git_repo_with_remote)
    mgr.validate()
    with patch("icx_engine.config_manager.ConfigManager") as mock_cfg_cls:
        mock_cfg_cls.load.return_value.active_gitlab_connection.return_value = conn
        env = mgr._auth_env()
    assert env is not None
    assert env["GIT_CONFIG_KEY_0"] == "http.extraheader"


def test_manager_auth_env_resolution_is_cached_after_first_call(tmp_git_repo_with_remote):
    """ConfigManager.load() must only be hit once per manager instance, not once per
    network call - resolve_parent_branch alone makes 3 network calls (fetch + two
    remote_branch_exists) that would otherwise triple the config lookups."""
    from unittest.mock import patch

    mgr = GitLifecycleManager(tmp_git_repo_with_remote)
    mgr.validate()
    with patch("icx_engine.config_manager.ConfigManager") as mock_cfg_cls:
        mock_cfg_cls.load.return_value.active_gitlab_connection.return_value = None
        mgr._auth_env()
        mgr._auth_env()
        mgr._auth_env()
    assert mock_cfg_cls.load.call_count == 1


def test_manager_constructed_with_explicit_gitlab_conn_skips_config_manager_lookup(tmp_git_repo_with_remote):
    from unittest.mock import patch
    from icx_engine.models.config import GitLabConnection

    conn = GitLabConnection(name="x", url="https://gitlab.example.com", token="glpat-x")
    mgr = GitLifecycleManager(tmp_git_repo_with_remote, gitlab_conn=conn)
    mgr.validate()
    with patch("icx_engine.config_manager.ConfigManager") as mock_cfg_cls:
        mgr._auth_env()
    mock_cfg_cls.load.assert_not_called()


def test_resolve_parent_branch_passes_auth_env_to_fetch_and_remote_branch_exists(
    tmp_git_repo_with_remote, monkeypatch,
):
    """The precise bug from the CCBSS incident: git_create_mr's own fetch/ls-remote
    calls (via resolve_parent_branch's fetch, and confirm_parent_branch's
    remote_branch_exists) were still running with no credential at all even after
    push was fixed. This proves both now receive the computed auth env."""
    from unittest.mock import Mock, patch
    from icx_engine.models.config import GitLabConnection

    conn = GitLabConnection(name="gitlab.example.com", url="https://gitlab.example.com", token="glpat-x")
    monkeypatch.setattr(
        "icx_engine.git.manager.remote_url",
        lambda repo, remote="origin": "https://gitlab.example.com/group/project.git",
    )
    mock_fetch = Mock()
    mock_remote_branch_exists = Mock(return_value=True)
    monkeypatch.setattr("icx_engine.git.manager.fetch", mock_fetch)
    monkeypatch.setattr("icx_engine.git.manager.remote_branch_exists", mock_remote_branch_exists)
    monkeypatch.setattr("icx_engine.git.manager.read_repo_settings", lambda root: {"parent_branch": "main"})

    mgr = GitLifecycleManager(tmp_git_repo_with_remote)
    mgr.validate()
    with patch("icx_engine.config_manager.ConfigManager") as mock_cfg_cls:
        mock_cfg_cls.load.return_value.active_gitlab_connection.return_value = conn
        mgr.resolve_parent_branch()

    fetch_env = mock_fetch.call_args.kwargs["extra_env"]
    exists_env = mock_remote_branch_exists.call_args.kwargs["extra_env"]
    assert fetch_env is not None and fetch_env["GIT_CONFIG_KEY_0"] == "http.extraheader"
    assert exists_env == fetch_env


def test_confirm_parent_branch_passes_auth_env_to_remote_branch_exists(tmp_git_repo_with_remote, monkeypatch):
    from unittest.mock import Mock, patch
    from icx_engine.models.config import GitLabConnection

    conn = GitLabConnection(name="gitlab.example.com", url="https://gitlab.example.com", token="glpat-x")
    monkeypatch.setattr(
        "icx_engine.git.manager.remote_url",
        lambda repo, remote="origin": "https://gitlab.example.com/group/project.git",
    )
    mock_exists = Mock(return_value=True)
    monkeypatch.setattr("icx_engine.git.manager.remote_branch_exists", mock_exists)
    monkeypatch.setattr("icx_engine.git.manager.write_repo_settings", lambda root, **kw: None)

    mgr = GitLifecycleManager(tmp_git_repo_with_remote)
    mgr.validate()
    with patch("icx_engine.config_manager.ConfigManager") as mock_cfg_cls:
        mock_cfg_cls.load.return_value.active_gitlab_connection.return_value = conn
        mgr.confirm_parent_branch("main")

    env = mock_exists.call_args.kwargs["extra_env"]
    assert env is not None and env["GIT_CONFIG_KEY_0"] == "http.extraheader"


def test_sync_with_remote_passes_auth_env_to_fetch(tmp_git_repo_with_remote, monkeypatch):
    from unittest.mock import Mock, patch
    from icx_engine.models.config import GitLabConnection

    conn = GitLabConnection(name="gitlab.example.com", url="https://gitlab.example.com", token="glpat-x")
    monkeypatch.setattr(
        "icx_engine.git.manager.remote_url",
        lambda repo, remote="origin": "https://gitlab.example.com/group/project.git",
    )
    mock_fetch = Mock()
    monkeypatch.setattr("icx_engine.git.manager.fetch", mock_fetch)

    mgr = GitLifecycleManager(tmp_git_repo_with_remote)
    mgr.validate()
    with patch("icx_engine.config_manager.ConfigManager") as mock_cfg_cls:
        mock_cfg_cls.load.return_value.active_gitlab_connection.return_value = conn
        try:
            mgr.sync_with_remote()
        except Exception:
            pass

    env = mock_fetch.call_args.kwargs["extra_env"]
    assert env is not None and env["GIT_CONFIG_KEY_0"] == "http.extraheader"


from icx_engine.git.gitcmd import current_branch, checkout, create_branch_from
from icx_engine.git.manager import DirtyTreeStatus, BranchStartResult


def test_check_dirty_tree_clean(tmp_git_repo):
    mgr = GitLifecycleManager(tmp_git_repo)
    mgr.validate()
    assert mgr.check_dirty_tree() == DirtyTreeStatus(dirty=False, files=[])


def test_check_dirty_tree_lists_files(tmp_git_repo):
    (tmp_git_repo / "x.txt").write_text("x", encoding="utf-8")
    mgr = GitLifecycleManager(tmp_git_repo)
    mgr.validate()
    status = mgr.check_dirty_tree()
    assert status.dirty is True
    assert status.files == ["x.txt"]


def test_stash_dirty_tree_clears_working_tree(tmp_git_repo):
    (tmp_git_repo / "x.txt").write_text("x", encoding="utf-8")
    mgr = GitLifecycleManager(tmp_git_repo)
    mgr.validate()
    mgr.stash_dirty_tree("ABC-1")
    assert mgr.check_dirty_tree().dirty is False


def test_start_branch_creates_and_switches_for_ticket(tmp_git_repo_with_remote):
    mgr = GitLifecycleManager(tmp_git_repo_with_remote)
    mgr.validate()
    result = mgr.start_branch("ABC-123", "Fix login timeout", "main")
    assert result.branch_name == "feature/fix-login-timeout-ABC-123"
    assert result.created is True
    assert result.switched_to_existing is False
    assert current_branch(tmp_git_repo_with_remote) == "feature/fix-login-timeout-ABC-123"


def test_start_branch_ticketless(tmp_git_repo_with_remote):
    mgr = GitLifecycleManager(tmp_git_repo_with_remote)
    mgr.validate()
    result = mgr.start_branch(None, "Refactor auth module", "main")
    assert result.branch_name == "feature/refactor-auth-module"


def test_start_branch_switches_to_existing_instead_of_recreating(tmp_git_repo_with_remote):
    create_branch_from(tmp_git_repo_with_remote, "feature/fix-login-timeout-ABC-123", "main")
    checkout(tmp_git_repo_with_remote, "main")
    mgr = GitLifecycleManager(tmp_git_repo_with_remote)
    mgr.validate()
    result = mgr.start_branch("ABC-123", "Fix login timeout", "main")
    assert result.created is False
    assert result.switched_to_existing is True
    assert current_branch(tmp_git_repo_with_remote) == "feature/fix-login-timeout-ABC-123"


def test_current_ticket_key_parses_from_branch_name(tmp_git_repo_with_remote):
    create_branch_from(tmp_git_repo_with_remote, "feature/fix-login-timeout-ABC-123", "main")
    checkout(tmp_git_repo_with_remote, "feature/fix-login-timeout-ABC-123")
    mgr = GitLifecycleManager(tmp_git_repo_with_remote)
    mgr.validate()
    assert mgr.current_ticket_key() == "ABC-123"


def test_current_ticket_key_none_for_ticketless_branch(tmp_git_repo_with_remote):
    create_branch_from(tmp_git_repo_with_remote, "feature/refactor-auth-module", "main")
    checkout(tmp_git_repo_with_remote, "feature/refactor-auth-module")
    mgr = GitLifecycleManager(tmp_git_repo_with_remote)
    mgr.validate()
    assert mgr.current_ticket_key() is None


import subprocess
from icx_engine.git.manager import SyncResult, DebugLeftover, CommitResult


def _push_new_commit(repo: Path, filename: str, content: str, message: str) -> None:
    (repo / filename).write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", filename], cwd=str(repo), check=True)
    subprocess.run(["git", "commit", "-m", message], cwd=str(repo), check=True)
    subprocess.run(["git", "push"], cwd=str(repo), check=True)


def test_sync_up_to_date_when_nothing_changed(tmp_git_repo_with_remote):
    mgr = GitLifecycleManager(tmp_git_repo_with_remote)
    mgr.validate()
    result = mgr.sync_with_remote()
    assert result.status == "up_to_date"


def test_sync_fast_forwards_when_remote_ahead(tmp_git_repo_with_remote, tmp_path):
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", str(tmp_git_repo_with_remote), str(clone)], check=True,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=str(clone), check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=str(clone), check=True)
    _push_new_commit(tmp_git_repo_with_remote, "new.txt", "x", "second")
    mgr = GitLifecycleManager(clone)
    mgr.validate()
    result = mgr.sync_with_remote()
    assert result.status == "fast_forwarded"
    assert (clone / "new.txt").exists()


def test_sync_diverged_needs_merge(tmp_git_repo_with_remote, tmp_path):
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", str(tmp_git_repo_with_remote), str(clone)], check=True,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=str(clone), check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=str(clone), check=True)
    _push_new_commit(tmp_git_repo_with_remote, "remote_side.txt", "x", "remote change")
    (clone / "local_side.txt").write_text("y", encoding="utf-8")
    subprocess.run(["git", "add", "local_side.txt"], cwd=str(clone), check=True)
    subprocess.run(["git", "commit", "-m", "local change"], cwd=str(clone), check=True)
    mgr = GitLifecycleManager(clone)
    mgr.validate()
    result = mgr.sync_with_remote()
    assert result.status == "diverged_needs_merge"


def test_scan_staged_debug_leftovers_finds_console_log(tmp_git_repo):
    (tmp_git_repo / "app.js").write_text("function f() {\n  console.log('debug');\n}\n", encoding="utf-8")
    mgr = GitLifecycleManager(tmp_git_repo)
    mgr.validate()
    from icx_engine.git.gitcmd import stage_files
    stage_files(tmp_git_repo, ["app.js"])
    findings = mgr.scan_staged_debug_leftovers()
    assert len(findings) == 1
    assert findings[0].file == "app.js"
    assert "console.log" in findings[0].line


def test_scan_staged_debug_leftovers_ignores_preexisting_lines(tmp_git_repo):
    (tmp_git_repo / "app.js").write_text("console.log('already here');\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.js"], cwd=str(tmp_git_repo), check=True)
    subprocess.run(["git", "commit", "-m", "pre-existing debug line"], cwd=str(tmp_git_repo), check=True)
    (tmp_git_repo / "app.js").write_text(
        "console.log('already here');\nconst x = 1;\n", encoding="utf-8")
    mgr = GitLifecycleManager(tmp_git_repo)
    mgr.validate()
    from icx_engine.git.gitcmd import stage_files
    stage_files(tmp_git_repo, ["app.js"])
    findings = mgr.scan_staged_debug_leftovers()
    assert findings == []


def test_strip_ai_attribution_removes_co_authored_by_line():
    from icx_engine.git.manager import strip_ai_attribution
    message = "ABC-1 fix login timeout\n\nCo-Authored-By: Claude <noreply@anthropic.com>"
    assert strip_ai_attribution(message) == "ABC-1 fix login timeout"


def test_strip_ai_attribution_removes_generated_with_line():
    from icx_engine.git.manager import strip_ai_attribution
    message = "ABC-1 fix login timeout\n\nGenerated with Claude Code"
    assert strip_ai_attribution(message) == "ABC-1 fix login timeout"


def test_strip_ai_attribution_leaves_clean_message_untouched():
    from icx_engine.git.manager import strip_ai_attribution
    assert strip_ai_attribution("ABC-1 fix login timeout") == "ABC-1 fix login timeout"


def test_stage_and_commit_requires_ticket_key_prefix(tmp_git_repo):
    (tmp_git_repo / "x.txt").write_text("x", encoding="utf-8")
    mgr = GitLifecycleManager(tmp_git_repo)
    mgr.validate()
    with pytest.raises(GitWorkflowError):
        mgr.stage_and_commit(["x.txt"], "wrong prefix message", ticket_key="ABC-1")


def test_stage_and_commit_rejects_empty_description(tmp_git_repo):
    (tmp_git_repo / "x.txt").write_text("x", encoding="utf-8")
    mgr = GitLifecycleManager(tmp_git_repo)
    mgr.validate()
    with pytest.raises(GitWorkflowError):
        mgr.stage_and_commit(["x.txt"], "ABC-1", ticket_key="ABC-1")


def test_stage_and_commit_succeeds_and_strips_attribution(tmp_git_repo):
    (tmp_git_repo / "x.txt").write_text("x", encoding="utf-8")
    mgr = GitLifecycleManager(tmp_git_repo)
    mgr.validate()
    result = mgr.stage_and_commit(
        ["x.txt"], "ABC-1 add x.txt\n\nCo-Authored-By: Claude <noreply@anthropic.com>",
        ticket_key="ABC-1",
    )
    assert isinstance(result, CommitResult)
    assert len(result.sha) == 40
    log = subprocess.run(["git", "log", "-1", "--pretty=%B"], cwd=str(tmp_git_repo),
                          check=True, stdout=subprocess.PIPE).stdout.decode()
    assert "Co-Authored-By" not in log


def test_stage_and_commit_ticketless_needs_no_key(tmp_git_repo):
    (tmp_git_repo / "x.txt").write_text("x", encoding="utf-8")
    mgr = GitLifecycleManager(tmp_git_repo)
    mgr.validate()
    result = mgr.stage_and_commit(["x.txt"], "Refactor helper for clarity", ticket_key=None)
    assert len(result.sha) == 40


def test_stage_and_commit_syncs_backup_latest_to_new_commit(tmp_git_repo):
    """The actual requirement this fixes: the backup must always be in sync with
    the latest commit, not just refreshed before a risky reverse-merge attempt."""
    from icx_engine.git.gitcmd import local_branch_exists
    (tmp_git_repo / "x.txt").write_text("x", encoding="utf-8")
    mgr = GitLifecycleManager(tmp_git_repo)
    mgr.validate()
    result = mgr.stage_and_commit(["x.txt"], "ABC-1 add x.txt", ticket_key="ABC-1")
    assert local_branch_exists(tmp_git_repo, "backup-latest/ABC-1") is True
    backup_sha = subprocess.run(
        ["git", "rev-parse", "backup-latest/ABC-1"], cwd=str(tmp_git_repo),
        check=True, stdout=subprocess.PIPE, text=True,
    ).stdout.strip()
    assert backup_sha == result.sha


def test_stage_and_commit_second_commit_moves_backup_latest_forward(tmp_git_repo):
    (tmp_git_repo / "x.txt").write_text("x", encoding="utf-8")
    mgr = GitLifecycleManager(tmp_git_repo)
    mgr.validate()
    mgr.stage_and_commit(["x.txt"], "ABC-1 add x.txt", ticket_key="ABC-1")

    (tmp_git_repo / "y.txt").write_text("y", encoding="utf-8")
    result2 = mgr.stage_and_commit(["y.txt"], "ABC-1 add y.txt", ticket_key="ABC-1")

    backup_sha = subprocess.run(
        ["git", "rev-parse", "backup-latest/ABC-1"], cwd=str(tmp_git_repo),
        check=True, stdout=subprocess.PIPE, text=True,
    ).stdout.strip()
    assert backup_sha == result2.sha


def test_stage_and_commit_ticketless_uses_branch_slug_for_backup_key(tmp_git_repo):
    from icx_engine.git.gitcmd import local_branch_exists
    (tmp_git_repo / "x.txt").write_text("x", encoding="utf-8")
    mgr = GitLifecycleManager(tmp_git_repo)
    mgr.validate()
    mgr.stage_and_commit(["x.txt"], "Refactor helper for clarity", ticket_key=None)
    assert local_branch_exists(tmp_git_repo, "backup-latest/main") is True


from icx_engine.git.manager import ReverseMergeResult
from icx_engine.git.gitcmd import (
    create_branch_from, checkout, stage_files, commit as gitcmd_commit,
)


def _feature_branch_diverged_from_parent(tmp_git_repo_with_remote, conflicting=True):
    """Set up: feature branch with a local commit; origin's main also moved
    ahead with a commit. If conflicting=True, both touch the same line of
    README.md; otherwise they touch different files."""
    create_branch_from(tmp_git_repo_with_remote, "feature/x-ABC-1", "main")
    checkout(tmp_git_repo_with_remote, "feature/x-ABC-1")
    if conflicting:
        (tmp_git_repo_with_remote / "README.md").write_text("feature version\n", encoding="utf-8")
        stage_files(tmp_git_repo_with_remote, ["README.md"])
    else:
        (tmp_git_repo_with_remote / "feature_only.txt").write_text("f", encoding="utf-8")
        stage_files(tmp_git_repo_with_remote, ["feature_only.txt"])
    gitcmd_commit(tmp_git_repo_with_remote, "ABC-1 feature change")

    checkout(tmp_git_repo_with_remote, "main")
    if conflicting:
        (tmp_git_repo_with_remote / "README.md").write_text("parent version\n", encoding="utf-8")
        stage_files(tmp_git_repo_with_remote, ["README.md"])
    else:
        (tmp_git_repo_with_remote / "parent_only.txt").write_text("p", encoding="utf-8")
        stage_files(tmp_git_repo_with_remote, ["parent_only.txt"])
    gitcmd_commit(tmp_git_repo_with_remote, "PARENT-1 parent change")
    subprocess.run(["git", "push"], cwd=str(tmp_git_repo_with_remote), check=True)
    checkout(tmp_git_repo_with_remote, "feature/x-ABC-1")


def test_reverse_merge_standard_clean_path_merges_and_reports_clean(tmp_git_repo_with_remote):
    _feature_branch_diverged_from_parent(tmp_git_repo_with_remote, conflicting=False)
    mgr = GitLifecycleManager(tmp_git_repo_with_remote)
    mgr.validate()
    result = mgr.reverse_merge_standard("main", "ABC-1")
    assert result == ReverseMergeResult(status="clean", conflicted_files=[])
    assert (tmp_git_repo_with_remote / "feature_only.txt").exists()
    assert (tmp_git_repo_with_remote / "parent_only.txt").exists()


def test_reverse_merge_standard_conflict_path_aborts_cleanly(tmp_git_repo_with_remote):
    _feature_branch_diverged_from_parent(tmp_git_repo_with_remote, conflicting=True)
    mgr = GitLifecycleManager(tmp_git_repo_with_remote)
    mgr.validate()
    result = mgr.reverse_merge_standard("main", "ABC-1")
    assert result.status == "conflict"
    assert result.conflicted_files == ["README.md"]
    # feature branch must be back to a clean, non-conflicted state
    from icx_engine.git.gitcmd import conflicted_files as _conflicted, is_dirty
    assert _conflicted(tmp_git_repo_with_remote) == []
    assert not (tmp_git_repo_with_remote / ".git" / "MERGE_HEAD").exists()


def test_reverse_merge_standard_conflict_path_restores_prior_dirty_work(tmp_git_repo_with_remote):
    _feature_branch_diverged_from_parent(tmp_git_repo_with_remote, conflicting=True)
    (tmp_git_repo_with_remote / "uncommitted.txt").write_text("still here", encoding="utf-8")
    mgr = GitLifecycleManager(tmp_git_repo_with_remote)
    mgr.validate()
    mgr.reverse_merge_standard("main", "ABC-1")
    assert (tmp_git_repo_with_remote / "uncommitted.txt").read_text(encoding="utf-8") == "still here"


def test_reverse_merge_standard_clean_path_restores_prior_dirty_work(tmp_git_repo_with_remote):
    _feature_branch_diverged_from_parent(tmp_git_repo_with_remote, conflicting=False)
    (tmp_git_repo_with_remote / "uncommitted.txt").write_text("still here too", encoding="utf-8")
    mgr = GitLifecycleManager(tmp_git_repo_with_remote)
    mgr.validate()
    mgr.reverse_merge_standard("main", "ABC-1")
    assert (tmp_git_repo_with_remote / "uncommitted.txt").read_text(encoding="utf-8") == "still here too"


def test_reverse_merge_standard_stash_pop_conflict_raises_clear_error(tmp_git_repo_with_remote):
    _feature_branch_diverged_from_parent(tmp_git_repo_with_remote, conflicting=False)
    # Stash an uncommitted change to the SAME file the merge is about to bring
    # in (parent_only.txt is created fresh on main by the fixture helper), so
    # popping the stash back after the merge collides with the merged content.
    (tmp_git_repo_with_remote / "parent_only.txt").write_text("stashed conflicting content", encoding="utf-8")
    mgr = GitLifecycleManager(tmp_git_repo_with_remote)
    mgr.validate()
    with pytest.raises(GitWorkflowError, match="stash"):
        mgr.reverse_merge_standard("main", "ABC-1")
    # Confirm the stash was NOT dropped - the developer's work is still recoverable.
    from icx_engine.git.gitcmd import _run_git, _stdout
    stash_list = _stdout(_run_git(tmp_git_repo_with_remote, ["stash", "list"]))
    assert stash_list != ""


def test_reverse_merge_standard_nullable_ticket_key_falls_back_to_branch_slug(tmp_git_repo_with_remote):
    _feature_branch_diverged_from_parent(tmp_git_repo_with_remote, conflicting=False)
    mgr = GitLifecycleManager(tmp_git_repo_with_remote)
    mgr.validate()
    result = mgr.reverse_merge_standard("main", None)
    assert result == ReverseMergeResult(status="clean", conflicted_files=[])
    from icx_engine.git.safety import list_backups
    assert list_backups(tmp_git_repo_with_remote, "feature-x-abc-1") != []


from icx_engine.git.manager import ScratchSession, ConflictPayload


def test_start_conflict_resolution_creates_scratch_and_reports_conflicts(tmp_git_repo_with_remote):
    _feature_branch_diverged_from_parent(tmp_git_repo_with_remote, conflicting=True)
    mgr = GitLifecycleManager(tmp_git_repo_with_remote)
    mgr.validate()
    session = mgr.start_conflict_resolution("main", "ABC-1")
    assert session.scratch_branch.startswith("scratch/ABC-1-")
    assert session.conflicted_files == ["README.md"]
    from icx_engine.git.gitcmd import current_branch
    assert current_branch(tmp_git_repo_with_remote) == session.scratch_branch


def test_start_conflict_resolution_leaves_feature_branch_untouched(tmp_git_repo_with_remote):
    _feature_branch_diverged_from_parent(tmp_git_repo_with_remote, conflicting=True)
    from icx_engine.git.gitcmd import head_sha, checkout, _run_git, _stdout
    checkout(tmp_git_repo_with_remote, "feature/x-ABC-1")
    feature_sha_before = head_sha(tmp_git_repo_with_remote)
    mgr = GitLifecycleManager(tmp_git_repo_with_remote)
    mgr.validate()
    mgr.start_conflict_resolution("main", "ABC-1")
    # start_conflict_resolution leaves a genuinely unresolved conflict on the scratch
    # branch (by design - that's what the human/AI resolves next), and real git refuses
    # any checkout away from the currently-checked-out branch while that's unresolved.
    # Read the feature branch's ref directly instead of checking it out, to confirm its
    # tip never moved without fighting that (correct) git safety behavior.
    feature_sha_after = _stdout(_run_git(tmp_git_repo_with_remote, ["rev-parse", "feature/x-ABC-1"]))
    assert feature_sha_after == feature_sha_before


def test_start_conflict_resolution_nullable_ticket_key_falls_back_to_branch_slug(tmp_git_repo_with_remote):
    _feature_branch_diverged_from_parent(tmp_git_repo_with_remote, conflicting=True)
    mgr = GitLifecycleManager(tmp_git_repo_with_remote)
    mgr.validate()
    session = mgr.start_conflict_resolution("main", None)
    assert session.scratch_branch.startswith("scratch/feature-x-abc-1-")
    assert session.conflicted_files == ["README.md"]


def test_start_conflict_resolution_handles_dirty_tree_overlapping_the_conflict(tmp_git_repo_with_remote):
    _feature_branch_diverged_from_parent(tmp_git_repo_with_remote, conflicting=True)
    # Dirty edit to the SAME file the merge conflicts on - this is what broke escalation before the fix.
    (tmp_git_repo_with_remote / "README.md").write_text("dirty uncommitted edit\n", encoding="utf-8")
    mgr = GitLifecycleManager(tmp_git_repo_with_remote)
    mgr.validate()
    standard_result = mgr.reverse_merge_standard("main", "ABC-1")
    assert standard_result.status == "conflict"
    # Before the fix, this raised a raw GitCommandError from merge_ref itself ("Your local
    # changes to the following files would be overwritten by merge") - the merge never even
    # started, and the repo was left stranded on the scratch branch with no conflict info at
    # all. With the fix, the tree is stashed before the checkout+merge, so the merge starts
    # cleanly and produces a real, resolvable content conflict instead.
    #
    # Popping the stash back afterward then hits a separate, unavoidable git constraint: git
    # cannot reapply a stashed change onto a path that already has unmerged (conflicted) index
    # entries for that exact same path (verified directly against real git: "error: could not
    # write index" / "README.md: needs merge"). That surfaces as the pre-existing, already
    # covered `_pop_stash_or_explain` friendly error (see
    # test_reverse_merge_standard_stash_pop_conflict_raises_clear_error) - never a raw
    # GitCommandError, and never a lost stash.
    from icx_engine.git.gitcmd import current_branch, GitCommandError, _run_git, _stdout
    with pytest.raises(GitWorkflowError) as excinfo:
        mgr.start_conflict_resolution("main", "ABC-1")
    assert isinstance(excinfo.value.__cause__, GitCommandError)
    assert "would be overwritten by merge" not in str(excinfo.value)
    assert "not lost" in str(excinfo.value).lower()
    # Repo is not stranded: still on the scratch branch, the merge conflict is genuine and
    # resolvable, and the stashed dirty work is preserved (git keeps it on a failed pop).
    branch = current_branch(tmp_git_repo_with_remote)
    assert branch.startswith("scratch/ABC-1-")
    assert mgr.list_conflicted_files() == ["README.md"]
    stash_list = _stdout(_run_git(tmp_git_repo_with_remote, ["stash", "list"]))
    assert stash_list != ""


def test_get_conflict_returns_ours_and_theirs(tmp_git_repo_with_remote):
    _feature_branch_diverged_from_parent(tmp_git_repo_with_remote, conflicting=True)
    mgr = GitLifecycleManager(tmp_git_repo_with_remote)
    mgr.validate()
    mgr.start_conflict_resolution("main", "ABC-1")
    payload = mgr.get_conflict("README.md")
    assert payload == ConflictPayload(file="README.md", ours="feature version\n", theirs="parent version\n")


def test_list_conflicted_files_matches_session(tmp_git_repo_with_remote):
    _feature_branch_diverged_from_parent(tmp_git_repo_with_remote, conflicting=True)
    mgr = GitLifecycleManager(tmp_git_repo_with_remote)
    mgr.validate()
    session = mgr.start_conflict_resolution("main", "ABC-1")
    assert mgr.list_conflicted_files() == session.conflicted_files


def test_complete_scratch_resolution_blocks_when_markers_remain(tmp_git_repo_with_remote):
    _feature_branch_diverged_from_parent(tmp_git_repo_with_remote, conflicting=True)
    mgr = GitLifecycleManager(tmp_git_repo_with_remote)
    mgr.validate()
    mgr.start_conflict_resolution("main", "ABC-1")
    # Leave the file with real markers still in it (simulating an unresolved edit).
    with pytest.raises(GitWorkflowError):
        mgr.complete_scratch_resolution(["README.md"], "ABC-1 resolve conflict")


def test_complete_scratch_resolution_succeeds_once_markers_removed(tmp_git_repo_with_remote):
    _feature_branch_diverged_from_parent(tmp_git_repo_with_remote, conflicting=True)
    mgr = GitLifecycleManager(tmp_git_repo_with_remote)
    mgr.validate()
    mgr.start_conflict_resolution("main", "ABC-1")
    (tmp_git_repo_with_remote / "README.md").write_text("resolved content\n", encoding="utf-8")
    sha = mgr.complete_scratch_resolution(["README.md"], "ABC-1 resolve conflict")
    assert len(sha) == 40
    from icx_engine.git.gitcmd import conflicted_files as _conflicted
    assert _conflicted(tmp_git_repo_with_remote) == []


def test_adopt_scratch_resolution_fast_forwards_feature_and_deletes_scratch(tmp_git_repo_with_remote):
    _feature_branch_diverged_from_parent(tmp_git_repo_with_remote, conflicting=True)
    mgr = GitLifecycleManager(tmp_git_repo_with_remote)
    mgr.validate()
    session = mgr.start_conflict_resolution("main", "ABC-1")
    (tmp_git_repo_with_remote / "README.md").write_text("resolved content\n", encoding="utf-8")
    mgr.complete_scratch_resolution(["README.md"], "ABC-1 resolve conflict")

    adopted_sha = mgr.adopt_scratch_resolution("feature/x-ABC-1", session.scratch_branch)
    from icx_engine.git.gitcmd import current_branch, head_sha, local_branch_exists
    assert current_branch(tmp_git_repo_with_remote) == "feature/x-ABC-1"
    assert head_sha(tmp_git_repo_with_remote) == adopted_sha
    assert local_branch_exists(tmp_git_repo_with_remote, session.scratch_branch) is False
    assert (tmp_git_repo_with_remote / "README.md").read_text(encoding="utf-8") == "resolved content\n"


def test_discard_scratch_resolution_deletes_scratch_leaves_feature_alone(tmp_git_repo_with_remote):
    _feature_branch_diverged_from_parent(tmp_git_repo_with_remote, conflicting=True)
    from icx_engine.git.gitcmd import head_sha, checkout
    checkout(tmp_git_repo_with_remote, "feature/x-ABC-1")
    feature_sha_before = head_sha(tmp_git_repo_with_remote)
    mgr = GitLifecycleManager(tmp_git_repo_with_remote)
    mgr.validate()
    session = mgr.start_conflict_resolution("main", "ABC-1")
    mgr.discard_scratch_resolution("feature/x-ABC-1", session.scratch_branch)
    from icx_engine.git.gitcmd import current_branch, local_branch_exists
    assert current_branch(tmp_git_repo_with_remote) == "feature/x-ABC-1"
    assert head_sha(tmp_git_repo_with_remote) == feature_sha_before
    assert local_branch_exists(tmp_git_repo_with_remote, session.scratch_branch) is False


# Task 6: MR description template builder
from icx_engine.git.manager import MrDescription


def test_build_mr_description_flags_migration_files(tmp_git_repo_with_remote):
    from icx_engine.git.gitcmd import create_branch_from, checkout, stage_files, commit as gitcmd_commit
    create_branch_from(tmp_git_repo_with_remote, "feature/x-ABC-1", "main")
    checkout(tmp_git_repo_with_remote, "feature/x-ABC-1")
    (tmp_git_repo_with_remote / "migrations").mkdir()
    (tmp_git_repo_with_remote / "migrations" / "0001_init.sql").write_text("CREATE TABLE x;", encoding="utf-8")
    stage_files(tmp_git_repo_with_remote, ["migrations/0001_init.sql"])
    gitcmd_commit(tmp_git_repo_with_remote, "ABC-1 add migration")
    mgr = GitLifecycleManager(tmp_git_repo_with_remote)
    mgr.validate()
    desc = mgr.build_mr_description("main")
    assert "0001_init.sql" in desc.db_changes


def test_build_mr_description_flags_config_files(tmp_git_repo_with_remote):
    from icx_engine.git.gitcmd import create_branch_from, checkout, stage_files, commit as gitcmd_commit
    create_branch_from(tmp_git_repo_with_remote, "feature/x-ABC-1", "main")
    checkout(tmp_git_repo_with_remote, "feature/x-ABC-1")
    (tmp_git_repo_with_remote / "config.yml").write_text("key: value", encoding="utf-8")
    stage_files(tmp_git_repo_with_remote, ["config.yml"])
    gitcmd_commit(tmp_git_repo_with_remote, "ABC-1 add config")
    mgr = GitLifecycleManager(tmp_git_repo_with_remote)
    mgr.validate()
    desc = mgr.build_mr_description("main")
    assert "config.yml" in desc.config_changes


def test_build_mr_description_no_signal_files_shows_dash(tmp_git_repo_with_remote):
    from icx_engine.git.gitcmd import create_branch_from, checkout, stage_files, commit as gitcmd_commit
    create_branch_from(tmp_git_repo_with_remote, "feature/x-ABC-1", "main")
    checkout(tmp_git_repo_with_remote, "feature/x-ABC-1")
    (tmp_git_repo_with_remote / "readme_note.txt").write_text("x", encoding="utf-8")
    stage_files(tmp_git_repo_with_remote, ["readme_note.txt"])
    gitcmd_commit(tmp_git_repo_with_remote, "ABC-1 note")
    mgr = GitLifecycleManager(tmp_git_repo_with_remote)
    mgr.validate()
    desc = mgr.build_mr_description("main")
    assert desc.db_changes == "-"
    assert desc.config_changes == "-"
    assert desc.api_impact == "-"


def test_build_mr_description_change_summary_lists_commits(tmp_git_repo_with_remote):
    from icx_engine.git.gitcmd import create_branch_from, checkout, stage_files, commit as gitcmd_commit
    create_branch_from(tmp_git_repo_with_remote, "feature/x-ABC-1", "main")
    checkout(tmp_git_repo_with_remote, "feature/x-ABC-1")
    (tmp_git_repo_with_remote / "a.txt").write_text("a", encoding="utf-8")
    stage_files(tmp_git_repo_with_remote, ["a.txt"])
    gitcmd_commit(tmp_git_repo_with_remote, "ABC-1 add a.txt")
    mgr = GitLifecycleManager(tmp_git_repo_with_remote)
    mgr.validate()
    desc = mgr.build_mr_description("main")
    assert "add a.txt" in desc.change_summary
    assert desc.deployment_notes == "-"
    assert desc.rollback_notes == "-"


import respx
import httpx
from unittest.mock import AsyncMock, patch
from icx_engine.git.manager import CreateMrResult, CleanupResult
from icx_engine.models.config import GitLabConnection


def _gitlab_conn() -> GitLabConnection:
    return GitLabConnection(name="gitlab.example.com", url="https://gitlab.example.com", token="glpat-x")


# -- _gitlab_push_auth_env: the actual push-credential fix -------------------

def test_gitlab_push_auth_env_builds_basic_auth_header_for_matching_https_origin():
    from icx_engine.git.manager import _gitlab_push_auth_env
    import base64
    env = _gitlab_push_auth_env(_gitlab_conn(), "https://gitlab.example.com/group/project.git")
    assert env is not None
    assert env["GIT_CONFIG_COUNT"] == "1"
    assert env["GIT_CONFIG_KEY_0"] == "http.extraheader"
    expected = "Authorization: Basic " + base64.b64encode(b"oauth2:glpat-x").decode()
    assert env["GIT_CONFIG_VALUE_0"] == expected


def test_gitlab_push_auth_env_none_for_ssh_origin():
    from icx_engine.git.manager import _gitlab_push_auth_env
    env = _gitlab_push_auth_env(_gitlab_conn(), "git@gitlab.example.com:group/project.git")
    assert env is None


def test_gitlab_push_auth_env_none_for_mismatched_host():
    from icx_engine.git.manager import _gitlab_push_auth_env
    env = _gitlab_push_auth_env(_gitlab_conn(), "https://some-other-gitlab.internal/group/project.git")
    assert env is None


def test_gitlab_push_auth_env_none_when_no_token():
    from icx_engine.git.manager import _gitlab_push_auth_env
    conn = GitLabConnection(name="x", url="https://gitlab.example.com", token=None)
    env = _gitlab_push_auth_env(conn, "https://gitlab.example.com/group/project.git")
    assert env is None


def test_gitlab_push_auth_env_none_when_gitlab_conn_is_none():
    from icx_engine.git.manager import _gitlab_push_auth_env
    assert _gitlab_push_auth_env(None, "https://gitlab.example.com/group/project.git") is None


def test_gitlab_push_auth_env_adds_ssl_verify_false_entry_when_verify_tls_disabled():
    from icx_engine.git.manager import _gitlab_push_auth_env
    conn = GitLabConnection(name="x", url="https://gitlab.example.com", token="glpat-x", verify_tls=False)
    env = _gitlab_push_auth_env(conn, "https://gitlab.example.com/group/project.git")
    assert env["GIT_CONFIG_COUNT"] == "2"
    assert env["GIT_CONFIG_KEY_1"] == "http.sslVerify"
    assert env["GIT_CONFIG_VALUE_1"] == "false"


async def test_create_mr_for_ticket_resolves_project_and_calls_gitlab(tmp_git_repo_with_remote, monkeypatch):
    from icx_engine.git.gitcmd import create_branch_from, checkout, stage_files, commit as gitcmd_commit, remote_url
    import subprocess
    create_branch_from(tmp_git_repo_with_remote, "feature/x-ABC-1", "main")
    checkout(tmp_git_repo_with_remote, "feature/x-ABC-1")
    (tmp_git_repo_with_remote / "a.txt").write_text("a", encoding="utf-8")
    stage_files(tmp_git_repo_with_remote, ["a.txt"])
    gitcmd_commit(tmp_git_repo_with_remote, "ABC-1 add a.txt")

    monkeypatch.setattr(
        "icx_engine.git.manager.project_path_from_remote_url",
        lambda url: "group/project",
    )
    mock_result = {"mr_iid": 5, "created": True, "merged": True, "refusal_reason": None}
    with patch("icx_engine.git.manager.create_and_merge_mr", new=AsyncMock(return_value=mock_result)) as mock_call:
        with patch("icx_engine.git.manager.GitLabClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.validate.return_value = {"valid": True, "user": {"id": 42}}
            mock_client_cls.return_value.__aenter__.return_value = mock_client
            mgr = GitLifecycleManager(tmp_git_repo_with_remote)
            mgr.validate()
            result = await mgr.create_mr_for_ticket("main", "ABC-1", "Fix login timeout", _gitlab_conn())

    assert result.mr_iid == 5
    assert result.merged is True
    mock_call.assert_called_once()
    call_kwargs = mock_call.call_args
    assert call_kwargs.args[1] == "group/project"
    assert call_kwargs.args[2] == "feature/x-ABC-1"
    assert call_kwargs.args[3] == "main"
    assert call_kwargs.args[4] == "ABC-1 Fix login timeout"
    assert call_kwargs.args[6] == 42
    assert "add a.txt" in call_kwargs.args[5]


async def test_create_mr_for_ticket_nullable_ticket_key_omits_prefix_from_title(tmp_git_repo_with_remote, monkeypatch):
    from icx_engine.git.gitcmd import create_branch_from, checkout, stage_files, commit as gitcmd_commit
    create_branch_from(tmp_git_repo_with_remote, "feature/x-nope", "main")
    checkout(tmp_git_repo_with_remote, "feature/x-nope")
    (tmp_git_repo_with_remote / "a.txt").write_text("a", encoding="utf-8")
    stage_files(tmp_git_repo_with_remote, ["a.txt"])
    gitcmd_commit(tmp_git_repo_with_remote, "fix: add a.txt")

    monkeypatch.setattr("icx_engine.git.manager.project_path_from_remote_url", lambda url: "group/project")
    mock_result = {"mr_iid": 7, "created": True, "merged": False, "refusal_reason": None}
    with patch("icx_engine.git.manager.create_and_merge_mr", new=AsyncMock(return_value=mock_result)) as mock_call:
        with patch("icx_engine.git.manager.GitLabClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.validate.return_value = {"valid": True, "user": {"id": 42}}
            mock_client_cls.return_value.__aenter__.return_value = mock_client
            mgr = GitLifecycleManager(tmp_git_repo_with_remote)
            mgr.validate()
            await mgr.create_mr_for_ticket("main", None, "Fix login timeout", _gitlab_conn())

    # No manufactured ticket id prefix - the title is exactly ticket_summary.
    assert mock_call.call_args.args[4] == "Fix login timeout"


async def test_create_mr_for_ticket_raises_when_remote_not_gitlab(tmp_git_repo_with_remote, monkeypatch):
    monkeypatch.setattr("icx_engine.git.manager.project_path_from_remote_url", lambda url: None)
    mgr = GitLifecycleManager(tmp_git_repo_with_remote)
    mgr.validate()
    with pytest.raises(GitWorkflowError):
        await mgr.create_mr_for_ticket("main", "ABC-1", "Fix login timeout", _gitlab_conn())


async def test_create_mr_for_ticket_pushes_feature_branch_before_creating_mr(tmp_git_repo_with_remote, monkeypatch):
    """Proves push() is called with the feature branch before the actual MR-creation call - an
    MR cannot be created from a branch that does not yet exist on the remote. The GitLab
    connection check (a cheap, fast-fail validate() call) legitimately runs before push - it's
    not creating the MR, just confirming the token works before doing any git work at all."""
    from unittest.mock import Mock
    from icx_engine.git.gitcmd import create_branch_from, checkout, stage_files, commit as gitcmd_commit, GitCommandError

    create_branch_from(tmp_git_repo_with_remote, "feature/x-ABC-2", "main")
    checkout(tmp_git_repo_with_remote, "feature/x-ABC-2")
    (tmp_git_repo_with_remote / "b.txt").write_text("b", encoding="utf-8")
    stage_files(tmp_git_repo_with_remote, ["b.txt"])
    gitcmd_commit(tmp_git_repo_with_remote, "ABC-2 add b.txt")

    monkeypatch.setattr(
        "icx_engine.git.manager.project_path_from_remote_url",
        lambda url: "group/project",
    )

    def _boom_push(repo, branch, **kwargs):
        raise GitCommandError("simulated push failure")
    mock_push = Mock(side_effect=_boom_push)
    monkeypatch.setattr("icx_engine.git.manager.push", mock_push)

    with patch("icx_engine.git.manager.create_and_merge_mr", new=AsyncMock()) as mock_call:
        with patch("icx_engine.git.manager.GitLabClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.validate.return_value = {"valid": True, "user": {"id": 42}}
            mock_client_cls.return_value.__aenter__.return_value = mock_client
            mgr = GitLifecycleManager(tmp_git_repo_with_remote)
            mgr.validate()
            with pytest.raises(GitCommandError):
                await mgr.create_mr_for_ticket("main", "ABC-2", "Fix thing", _gitlab_conn())

    mock_push.assert_called_once_with(mgr.repo_root, "feature/x-ABC-2", extra_env=None)
    mock_call.assert_not_called()


async def test_create_mr_for_ticket_validates_connection_before_any_git_work(tmp_git_repo_with_remote, monkeypatch):
    """Regression: the connection check used to run LAST, after fetch+push - an invalid token was
    only discovered after the user waited through the entire git operation. It must now run
    first, before push (or fetch) is ever called."""
    from unittest.mock import Mock

    monkeypatch.setattr(
        "icx_engine.git.manager.project_path_from_remote_url",
        lambda url: "group/project",
    )
    mock_push = Mock()
    monkeypatch.setattr("icx_engine.git.manager.push", mock_push)

    with patch("icx_engine.git.manager.create_and_merge_mr", new=AsyncMock()) as mock_call:
        with patch("icx_engine.git.manager.GitLabClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.validate.return_value = {"valid": False, "status_code": 401}
            mock_client_cls.return_value.__aenter__.return_value = mock_client
            mgr = GitLifecycleManager(tmp_git_repo_with_remote)
            mgr.validate()
            with pytest.raises(GitWorkflowError, match="401"):
                await mgr.create_mr_for_ticket("main", "ABC-3", "Fix thing", _gitlab_conn())

    mock_push.assert_not_called()
    mock_call.assert_not_called()


async def test_create_mr_for_ticket_passes_gitlab_auth_env_to_fetch_and_push_for_matching_origin(
    tmp_git_repo_with_remote, monkeypatch,
):
    """The actual push-credential fix: when the repo's origin host matches the connected
    GitLab connection's host, fetch/push must receive a non-None extra_env (the injected
    auth header) instead of running with no credential at all."""
    from unittest.mock import Mock

    monkeypatch.setattr(
        "icx_engine.git.manager.remote_url",
        lambda repo, remote="origin": "https://gitlab.example.com/group/project.git",
    )
    monkeypatch.setattr(
        "icx_engine.git.manager.project_path_from_remote_url",
        lambda url: "group/project",
    )
    mock_fetch = Mock()
    mock_push = Mock()
    monkeypatch.setattr("icx_engine.git.manager.fetch", mock_fetch)
    monkeypatch.setattr("icx_engine.git.manager.push", mock_push)

    mock_result = {"mr_iid": 9, "created": True, "merged": True, "refusal_reason": None}
    with patch("icx_engine.git.manager.create_and_merge_mr", new=AsyncMock(return_value=mock_result)):
        with patch("icx_engine.git.manager.GitLabClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.validate.return_value = {"valid": True, "user": {"id": 42}}
            mock_client_cls.return_value.__aenter__.return_value = mock_client
            mgr = GitLifecycleManager(tmp_git_repo_with_remote)
            mgr.validate()
            await mgr.create_mr_for_ticket("main", "ABC-9", "Fix thing", _gitlab_conn())

    fetch_env = mock_fetch.call_args.kwargs["extra_env"]
    push_env = mock_push.call_args.kwargs["extra_env"]
    assert fetch_env is not None and fetch_env["GIT_CONFIG_KEY_0"] == "http.extraheader"
    assert push_env == fetch_env


def test_post_merge_cleanup_raises_when_mr_not_actually_merged(tmp_git_repo_with_remote, monkeypatch):
    with patch("icx_engine.git.manager.GitLabClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.get_merge_request.return_value = {"state": "opened"}
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        monkeypatch.setattr("icx_engine.git.manager.project_path_from_remote_url", lambda url: "group/project")
        mgr = GitLifecycleManager(tmp_git_repo_with_remote)
        mgr.validate()
        with pytest.raises(GitWorkflowError, match="not.*merged"):
            mgr.post_merge_cleanup("main", "feature/x-ABC-1", "ABC-1", delete_backups=False, gitlab_conn=_gitlab_conn(), mr_iid=5)


def test_post_merge_cleanup_deletes_feature_branch_when_mr_confirmed_merged(tmp_git_repo_with_remote, monkeypatch):
    from icx_engine.git.gitcmd import create_branch_from, checkout, stage_files, commit as gitcmd_commit, local_branch_exists
    import subprocess
    create_branch_from(tmp_git_repo_with_remote, "feature/x-ABC-1", "main")
    checkout(tmp_git_repo_with_remote, "feature/x-ABC-1")
    (tmp_git_repo_with_remote / "a.txt").write_text("a", encoding="utf-8")
    stage_files(tmp_git_repo_with_remote, ["a.txt"])
    gitcmd_commit(tmp_git_repo_with_remote, "ABC-1 add a.txt")
    # Simulate the MR having merged: push feature's content to main on the remote,
    # so cleanup's "verify changes present" check finds a.txt after fast-forwarding.
    subprocess.run(["git", "push", "origin", "feature/x-ABC-1:main"], cwd=str(tmp_git_repo_with_remote), check=True)
    checkout(tmp_git_repo_with_remote, "main")
    # Brief's test omitted this - the fixture's origin is a local bare-repo path, which
    # project_path_from_remote_url legitimately can't resolve to a GitLab project path.
    monkeypatch.setattr("icx_engine.git.manager.project_path_from_remote_url", lambda url: "group/project")

    with patch("icx_engine.git.manager.GitLabClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.get_merge_request.return_value = {"state": "merged"}
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        with patch("icx_engine.git.manager.asyncio") as mock_asyncio:
            import asyncio as real_asyncio
            mock_asyncio.run = real_asyncio.run
            mgr = GitLifecycleManager(tmp_git_repo_with_remote)
            mgr.validate()
            checkout(tmp_git_repo_with_remote, "feature/x-ABC-1")
            result = mgr.post_merge_cleanup(
                "main", "feature/x-ABC-1", "ABC-1", delete_backups=False,
                gitlab_conn=_gitlab_conn(), mr_iid=5,
            )

    assert result.feature_branch_deleted is True
    assert local_branch_exists(tmp_git_repo_with_remote, "feature/x-ABC-1") is False


def test_post_merge_cleanup_nullable_ticket_key_uses_branch_slug_for_backup_prune(tmp_git_repo_with_remote, monkeypatch):
    from icx_engine.git.gitcmd import create_branch_from, checkout, stage_files, commit as gitcmd_commit
    import subprocess
    create_branch_from(tmp_git_repo_with_remote, "feature/x-nope", "main")
    checkout(tmp_git_repo_with_remote, "feature/x-nope")
    (tmp_git_repo_with_remote / "a.txt").write_text("a", encoding="utf-8")
    stage_files(tmp_git_repo_with_remote, ["a.txt"])
    gitcmd_commit(tmp_git_repo_with_remote, "fix: add a.txt")
    subprocess.run(["git", "push", "origin", "feature/x-nope:main"], cwd=str(tmp_git_repo_with_remote), check=True)
    checkout(tmp_git_repo_with_remote, "main")
    monkeypatch.setattr("icx_engine.git.manager.project_path_from_remote_url", lambda url: "group/project")

    with patch("icx_engine.git.manager.GitLabClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.get_merge_request.return_value = {"state": "merged"}
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        with patch("icx_engine.git.manager.asyncio") as mock_asyncio:
            import asyncio as real_asyncio
            mock_asyncio.run = real_asyncio.run
            mgr = GitLifecycleManager(tmp_git_repo_with_remote)
            mgr.validate()
            checkout(tmp_git_repo_with_remote, "feature/x-nope")
            # No ticket_key - must not raise (e.g. globbing "backup/None-*") and must
            # complete cleanup using a slug of feature_branch instead.
            result = mgr.post_merge_cleanup(
                "main", "feature/x-nope", None, delete_backups=True,
                gitlab_conn=_gitlab_conn(), mr_iid=5,
            )

    assert result.feature_branch_deleted is True
    assert result.backups_deleted == []


def test_post_merge_cleanup_raises_when_file_genuinely_missing_after_fast_forward(tmp_git_repo_with_remote, monkeypatch):
    """GitLab's API can say state=merged while the local fast-forward genuinely does not
    bring in the feature branch's content (e.g. the MR was merged against a different
    remote state than what this clone has fetched). The content check must catch this -
    it must not be dead code that always sees an empty diff."""
    from icx_engine.git.gitcmd import create_branch_from, checkout, stage_files, commit as gitcmd_commit, local_branch_exists
    create_branch_from(tmp_git_repo_with_remote, "feature/x-ABC-1", "main")
    checkout(tmp_git_repo_with_remote, "feature/x-ABC-1")
    (tmp_git_repo_with_remote / "b.txt").write_text("b", encoding="utf-8")
    stage_files(tmp_git_repo_with_remote, ["b.txt"])
    gitcmd_commit(tmp_git_repo_with_remote, "ABC-1 add b.txt")
    # Deliberately do NOT push this commit anywhere - origin/main never receives b.txt,
    # so a fast-forward of local main against origin/main is a no-op that still lacks it.
    checkout(tmp_git_repo_with_remote, "main")
    monkeypatch.setattr("icx_engine.git.manager.project_path_from_remote_url", lambda url: "group/project")

    with patch("icx_engine.git.manager.GitLabClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.get_merge_request.return_value = {"state": "merged"}
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        with patch("icx_engine.git.manager.asyncio") as mock_asyncio:
            import asyncio as real_asyncio
            mock_asyncio.run = real_asyncio.run
            mgr = GitLifecycleManager(tmp_git_repo_with_remote)
            mgr.validate()
            checkout(tmp_git_repo_with_remote, "feature/x-ABC-1")
            with pytest.raises(GitWorkflowError, match="b.txt"):
                mgr.post_merge_cleanup(
                    "main", "feature/x-ABC-1", "ABC-1", delete_backups=False,
                    gitlab_conn=_gitlab_conn(), mr_iid=5,
                )

    # Safety check must fire before the feature branch is deleted.
    assert local_branch_exists(tmp_git_repo_with_remote, "feature/x-ABC-1") is True


def test_post_merge_cleanup_deletes_feature_branch_after_squash_merge(tmp_git_repo_with_remote, monkeypatch):
    """GitLab's squash-merge gives parent a NEW commit that is not a descendant of the
    feature branch's tip - `git branch -d` (force=False) legitimately refuses this as
    'not fully merged' even though the content genuinely landed. Before the fix, this
    call raised GitCommandError here and left the feature branch undeleted; force=True
    is required once the merged-state + content checks have already vouched for it."""
    from icx_engine.git.gitcmd import create_branch_from, checkout, stage_files, commit as gitcmd_commit, local_branch_exists
    import subprocess
    create_branch_from(tmp_git_repo_with_remote, "feature/x-ABC-1", "main")
    checkout(tmp_git_repo_with_remote, "feature/x-ABC-1")
    (tmp_git_repo_with_remote / "a.txt").write_text("a", encoding="utf-8")
    stage_files(tmp_git_repo_with_remote, ["a.txt"])
    gitcmd_commit(tmp_git_repo_with_remote, "ABC-1 add a.txt")

    # Simulate a squash-merge: main gets an unrelated NEW commit with the same net
    # content, rather than fast-forwarding onto feature's own commit. Feature's tip
    # is deliberately NOT an ancestor of this new main commit.
    checkout(tmp_git_repo_with_remote, "main")
    (tmp_git_repo_with_remote / "a.txt").write_text("a", encoding="utf-8")
    stage_files(tmp_git_repo_with_remote, ["a.txt"])
    gitcmd_commit(tmp_git_repo_with_remote, "ABC-1 squash: add a.txt")
    subprocess.run(["git", "push", "origin", "main"], cwd=str(tmp_git_repo_with_remote), check=True)

    monkeypatch.setattr("icx_engine.git.manager.project_path_from_remote_url", lambda url: "group/project")

    with patch("icx_engine.git.manager.GitLabClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.get_merge_request.return_value = {"state": "merged"}
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        with patch("icx_engine.git.manager.asyncio") as mock_asyncio:
            import asyncio as real_asyncio
            mock_asyncio.run = real_asyncio.run
            mgr = GitLifecycleManager(tmp_git_repo_with_remote)
            mgr.validate()
            checkout(tmp_git_repo_with_remote, "feature/x-ABC-1")
            result = mgr.post_merge_cleanup(
                "main", "feature/x-ABC-1", "ABC-1", delete_backups=False,
                gitlab_conn=_gitlab_conn(), mr_iid=5,
            )

    assert result.feature_branch_deleted is True
    assert local_branch_exists(tmp_git_repo_with_remote, "feature/x-ABC-1") is False


# Task: pull() - ff-only/merge strategies, scoped to the current branch's own remote
from icx_engine.git.manager import PullResult


def test_pull_ff_only_up_to_date_when_nothing_changed(tmp_git_repo_with_remote):
    mgr = GitLifecycleManager(tmp_git_repo_with_remote)
    mgr.validate()
    result = mgr.pull(strategy="ff-only")
    assert result == PullResult(status="up_to_date")


def test_pull_ff_only_fast_forwards_when_remote_is_ahead(tmp_git_repo_with_remote, tmp_path):
    import subprocess
    bare_origin = tmp_path / "origin.git"
    other_clone = tmp_path / "other_clone_ff"
    subprocess.run(["git", "clone", str(bare_origin), str(other_clone)], check=True,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=str(other_clone), check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=str(other_clone), check=True)
    (other_clone / "remote_change.txt").write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "remote_change.txt"], cwd=str(other_clone), check=True)
    subprocess.run(["git", "commit", "-m", "remote change"], cwd=str(other_clone), check=True)
    subprocess.run(["git", "push"], cwd=str(other_clone), check=True)

    mgr = GitLifecycleManager(tmp_git_repo_with_remote)
    mgr.validate()
    result = mgr.pull(strategy="ff-only")
    assert result.status == "fast_forwarded"
    assert (tmp_git_repo_with_remote / "remote_change.txt").exists()


def _diverge_current_branch_from_own_remote(tmp_git_repo_with_remote, tmp_path, clone_name, conflicting):
    import subprocess
    bare_origin = tmp_path / "origin.git"
    other_clone = tmp_path / clone_name
    subprocess.run(["git", "clone", str(bare_origin), str(other_clone)], check=True,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=str(other_clone), check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=str(other_clone), check=True)
    remote_file = "README.md" if conflicting else "remote_change.txt"
    remote_content = "remote version\n" if conflicting else "x"
    (other_clone / remote_file).write_text(remote_content, encoding="utf-8")
    subprocess.run(["git", "add", remote_file], cwd=str(other_clone), check=True)
    subprocess.run(["git", "commit", "-m", "remote change"], cwd=str(other_clone), check=True)
    subprocess.run(["git", "push"], cwd=str(other_clone), check=True)

    from icx_engine.git.gitcmd import stage_files as _stage, commit as _commit
    local_file = "README.md" if conflicting else "local_change.txt"
    local_content = "local version\n" if conflicting else "y"
    (tmp_git_repo_with_remote / local_file).write_text(local_content, encoding="utf-8")
    _stage(tmp_git_repo_with_remote, [local_file])
    _commit(tmp_git_repo_with_remote, "ABC-1 local change")


def test_pull_ff_only_diverged_reports_status_without_merging(tmp_git_repo_with_remote, tmp_path):
    _diverge_current_branch_from_own_remote(tmp_git_repo_with_remote, tmp_path, "other_clone_diverge", conflicting=False)
    mgr = GitLifecycleManager(tmp_git_repo_with_remote)
    mgr.validate()
    result = mgr.pull(strategy="ff-only")
    assert result.status == "diverged_needs_merge"
    assert not (tmp_git_repo_with_remote / "remote_change.txt").exists()


def test_pull_merge_strategy_merges_diverged_branches_cleanly(tmp_git_repo_with_remote, tmp_path):
    _diverge_current_branch_from_own_remote(tmp_git_repo_with_remote, tmp_path, "other_clone_merge", conflicting=False)
    mgr = GitLifecycleManager(tmp_git_repo_with_remote)
    mgr.validate()
    result = mgr.pull(strategy="merge", ticket_key="ABC-1")
    assert result.status == "merged"
    assert (tmp_git_repo_with_remote / "remote_change.txt").exists()
    assert (tmp_git_repo_with_remote / "local_change.txt").exists()


def test_pull_merge_strategy_conflict_quarantines_onto_scratch_branch(tmp_git_repo_with_remote, tmp_path):
    _diverge_current_branch_from_own_remote(tmp_git_repo_with_remote, tmp_path, "other_clone_conflict", conflicting=True)
    mgr = GitLifecycleManager(tmp_git_repo_with_remote)
    mgr.validate()
    result = mgr.pull(strategy="merge", ticket_key="ABC-1")
    assert result.status == "conflict"
    assert result.conflicted_files == ["README.md"]
    assert result.scratch_branch is not None
    assert result.scratch_branch.startswith("scratch/ABC-1-")


def test_pull_invalid_strategy_raises(tmp_git_repo):
    mgr = GitLifecycleManager(tmp_git_repo)
    mgr.validate()
    with pytest.raises(GitWorkflowError):
        mgr.pull(strategy="rebase")


# Task: delete_branch_safely - merged-only safety mode, force override, current-branch protection
from icx_engine.git.manager import BranchDeleteResult
from icx_engine.git.gitcmd import local_branch_exists as _local_branch_exists, delete_branch as _delete_branch, push as _push


def test_delete_branch_safely_merged_branch_deletes_locally(tmp_git_repo):
    create_branch_from(tmp_git_repo, "feature/merged-ABC-1", "main")
    mgr = GitLifecycleManager(tmp_git_repo)
    mgr.validate()
    result = mgr.delete_branch_safely("feature/merged-ABC-1", "main")
    assert result == BranchDeleteResult(
        branch="feature/merged-ABC-1", local_deleted=True, remote_deleted=False, unique_commits=0,
    )
    assert _local_branch_exists(tmp_git_repo, "feature/merged-ABC-1") is False


def test_delete_branch_safely_refuses_unmerged_branch_without_force(tmp_git_repo):
    create_branch_from(tmp_git_repo, "feature/unmerged-ABC-1", "main")
    checkout(tmp_git_repo, "feature/unmerged-ABC-1")
    (tmp_git_repo / "only_here.txt").write_text("x", encoding="utf-8")
    stage_files(tmp_git_repo, ["only_here.txt"])
    gitcmd_commit(tmp_git_repo, "ABC-1 unique commit")
    checkout(tmp_git_repo, "main")
    mgr = GitLifecycleManager(tmp_git_repo)
    mgr.validate()
    with pytest.raises(GitWorkflowError, match="cannot be safely deleted"):
        mgr.delete_branch_safely("feature/unmerged-ABC-1", "main")
    assert _local_branch_exists(tmp_git_repo, "feature/unmerged-ABC-1") is True


def test_delete_branch_safely_force_deletes_unmerged_branch(tmp_git_repo):
    create_branch_from(tmp_git_repo, "feature/unmerged-ABC-1", "main")
    checkout(tmp_git_repo, "feature/unmerged-ABC-1")
    (tmp_git_repo / "only_here.txt").write_text("x", encoding="utf-8")
    stage_files(tmp_git_repo, ["only_here.txt"])
    gitcmd_commit(tmp_git_repo, "ABC-1 unique commit")
    checkout(tmp_git_repo, "main")
    mgr = GitLifecycleManager(tmp_git_repo)
    mgr.validate()
    result = mgr.delete_branch_safely("feature/unmerged-ABC-1", "main", force=True)
    assert result.local_deleted is True
    assert result.unique_commits == 1
    assert _local_branch_exists(tmp_git_repo, "feature/unmerged-ABC-1") is False


def test_delete_branch_safely_refuses_current_branch_even_with_force(tmp_git_repo):
    create_branch_from(tmp_git_repo, "feature/current-ABC-1", "main")
    checkout(tmp_git_repo, "feature/current-ABC-1")
    mgr = GitLifecycleManager(tmp_git_repo)
    mgr.validate()
    with pytest.raises(GitWorkflowError, match="currently checked-out"):
        mgr.delete_branch_safely("feature/current-ABC-1", "main", force=True)
    assert _local_branch_exists(tmp_git_repo, "feature/current-ABC-1") is True


def test_delete_branch_safely_raises_when_local_branch_missing(tmp_git_repo):
    mgr = GitLifecycleManager(tmp_git_repo)
    mgr.validate()
    with pytest.raises(GitWorkflowError, match="does not exist"):
        mgr.delete_branch_safely("feature/does-not-exist", "main")


def test_delete_branch_safely_deletes_remote_branch_too(tmp_git_repo_with_remote):
    create_branch_from(tmp_git_repo_with_remote, "feature/remote-cleanup-ABC-1", "main")
    checkout(tmp_git_repo_with_remote, "feature/remote-cleanup-ABC-1")
    _push(tmp_git_repo_with_remote, "feature/remote-cleanup-ABC-1")
    checkout(tmp_git_repo_with_remote, "main")
    mgr = GitLifecycleManager(tmp_git_repo_with_remote)
    mgr.validate()
    result = mgr.delete_branch_safely("feature/remote-cleanup-ABC-1", "main", delete_remote=True)
    assert result.remote_deleted is True
    from icx_engine.git.gitcmd import remote_branch_exists
    assert remote_branch_exists(tmp_git_repo_with_remote, "feature/remote-cleanup-ABC-1") is False


def test_delete_branch_safely_remote_only_skips_unique_commit_check(tmp_git_repo_with_remote):
    create_branch_from(tmp_git_repo_with_remote, "feature/remote-only-ABC-1", "main")
    checkout(tmp_git_repo_with_remote, "feature/remote-only-ABC-1")
    _push(tmp_git_repo_with_remote, "feature/remote-only-ABC-1")
    checkout(tmp_git_repo_with_remote, "main")
    _delete_branch(tmp_git_repo_with_remote, "feature/remote-only-ABC-1")
    mgr = GitLifecycleManager(tmp_git_repo_with_remote)
    mgr.validate()
    result = mgr.delete_branch_safely(
        "feature/remote-only-ABC-1", "main", delete_local=False, delete_remote=True,
    )
    assert result.local_deleted is False
    assert result.remote_deleted is True
    assert result.unique_commits == 0
