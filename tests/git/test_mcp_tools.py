from __future__ import annotations
import json
import pytest
from unittest.mock import AsyncMock, patch

from icx_engine.mcp_server import _ICX_FALLBACK

# Minimal real-shaped .gitlab-ci.yml fixture (dev/qa tag-trigger patterns) - used by
# git_create_tag tests so its live CI-pattern validation (added after live GitLab
# research) doesn't block tests that mock GitLabClient wholesale without configuring
# get_repository_file specifically.
_SAMPLE_CI_YAML = """
build_dev:
  only:
    - /^v\\d+\\.\\d+\\.\\d+-dev-(20\\d{2})(0[1-9]|1[0-2])(0[1-9]|[12][0-9]|3[01])(\\d{3})$/
build_qa:
  only:
    - /^v\\d+\\.\\d+\\.\\d+-qa-(20\\d{2})(0[1-9]|1[0-2])(0[1-9]|[12][0-9]|3[01])(\\d{3})$/
"""


async def test_dispatch_git_tool_returns_none_for_unknown_tool():
    from icx_engine.git.mcp_tools import dispatch_git_tool
    assert await dispatch_git_tool("sonar_status", {}) is None


async def test_git_repo_status_tool_reports_clean_repo(tmp_git_repo):
    from icx_engine.git.mcp_tools import dispatch_git_tool
    result = await dispatch_git_tool("git_repo_status", {"repo_path": str(tmp_git_repo)})
    assert result is not None
    payload = json.loads(result[0].text)
    assert payload["ok"] is True
    assert payload["current_branch"] == "main"
    assert payload["dirty"] is False


async def test_git_repo_status_reports_rich_structured_fields(tmp_git_repo):
    from icx_engine.git.mcp_tools import dispatch_git_tool
    from icx_engine.git.gitcmd import stage_files
    (tmp_git_repo / "staged.txt").write_text("x", encoding="utf-8")
    stage_files(tmp_git_repo, ["staged.txt"])
    (tmp_git_repo / "untracked.txt").write_text("y", encoding="utf-8")

    result = await dispatch_git_tool("git_repo_status", {"repo_path": str(tmp_git_repo)})
    payload = json.loads(result[0].text)
    assert payload["ok"] is True
    staged_paths = {e["path"] for e in payload["staged"]}
    assert staged_paths == {"staged.txt"}
    assert payload["untracked"] == ["untracked.txt"]
    assert payload["deleted"] == []
    assert payload["renamed"] == []
    assert payload["conflicted"] == []
    assert payload["ahead"] == 0
    assert payload["behind"] == 0
    assert payload["upstream"] is None


async def test_git_repo_status_attaches_safe_git_workflow_skill_hint(tmp_git_repo):
    from icx_engine.git.mcp_tools import dispatch_git_tool
    from icx_engine.skills.schema import SkillEntry

    class _FakeStorage:
        def read(self, name):
            assert name == "safe-git-workflow"
            return SkillEntry(name=name, description="Use for any git operation.", title="t",
                               when_to_use="w", procedure="p", pitfalls="x", verification="v")

    with patch("icx_engine.skills.hints.SkillStorage", _FakeStorage):
        result = await dispatch_git_tool("git_repo_status", {"repo_path": str(tmp_git_repo)})
    payload = json.loads(result[0].text)
    assert payload["skills"]["index"] == [{"name": "safe-git-workflow", "description": "Use for any git operation."}]


async def test_git_repo_status_appends_ranked_custom_skill(tmp_git_repo, tmp_path):
    from icx_engine.git.mcp_tools import dispatch_git_tool
    from icx_engine.skills.schema import SkillEntry
    from icx_engine.skills.storage import SkillStorage

    storage = SkillStorage(root=tmp_path)
    default_entry = SkillEntry(name="safe-git-workflow", description="Use for any git operation.",
                                title="t", when_to_use="w", procedure="p", pitfalls="x", verification="v")
    storage.write(default_entry)
    custom_entry = SkillEntry(name="branch-naming-helper", description="picks feature branch names",
                               tags=["branch"], title="Branch Naming Helper",
                               when_to_use="w", procedure="p", pitfalls="x", verification="v")
    storage.write(custom_entry)

    with patch("icx_engine.skills.hints.SkillStorage", lambda: storage):
        result = await dispatch_git_tool("git_repo_status", {"repo_path": str(tmp_git_repo)})
    payload = json.loads(result[0].text)
    names = [e["name"] for e in payload["skills"]["index"]]
    assert names[0] == "safe-git-workflow"
    assert "branch-naming-helper" in names


async def test_git_repo_status_omits_skill_hint_when_lookup_fails(tmp_git_repo):
    from icx_engine.git.mcp_tools import dispatch_git_tool

    class _BrokenStorage:
        def read(self, name):
            raise RuntimeError("boom")

    with patch("icx_engine.skills.hints.SkillStorage", _BrokenStorage):
        result = await dispatch_git_tool("git_repo_status", {"repo_path": str(tmp_git_repo)})
    payload = json.loads(result[0].text)
    assert payload["ok"] is True
    assert "skills" not in payload


async def test_git_stage_and_commit_returns_pending_confirmation_without_token(tmp_git_repo):
    from icx_engine.git.mcp_tools import dispatch_git_tool
    (tmp_git_repo / "x.txt").write_text("x", encoding="utf-8")
    result = await dispatch_git_tool("git_stage_and_commit", {
        "repo_path": str(tmp_git_repo), "files": ["x.txt"],
        "message": "Refactor helper", "ticket_key": None,
    })
    payload = json.loads(result[0].text)
    assert payload["status"] == "pending_confirmation"
    assert "token" in payload


async def test_git_stage_and_commit_executes_with_valid_token(tmp_git_repo):
    from icx_engine.git.mcp_tools import dispatch_git_tool
    (tmp_git_repo / "x.txt").write_text("x", encoding="utf-8")
    first = await dispatch_git_tool("git_stage_and_commit", {
        "repo_path": str(tmp_git_repo), "files": ["x.txt"],
        "message": "Refactor helper", "ticket_key": None,
    })
    token = json.loads(first[0].text)["token"]
    second = await dispatch_git_tool("git_stage_and_commit", {
        "repo_path": str(tmp_git_repo), "files": ["x.txt"],
        "message": "Refactor helper", "ticket_key": None, "confirm_token": token,
    })
    payload = json.loads(second[0].text)
    assert payload["ok"] is True
    assert len(payload["sha"]) == 40


async def test_git_stage_and_commit_warns_when_current_branch_is_stored_parent(tmp_git_repo, tmp_path, monkeypatch):
    from icx_engine.git.mcp_tools import dispatch_git_tool
    from icx_engine.git.settings import write_repo_settings
    monkeypatch.setattr("icx_engine.git.settings._git_settings_root", lambda: tmp_path / ".icx-test-home")
    write_repo_settings(tmp_git_repo, parent_branch="main")
    (tmp_git_repo / "x.txt").write_text("x", encoding="utf-8")
    result = await dispatch_git_tool("git_stage_and_commit", {
        "repo_path": str(tmp_git_repo), "files": ["x.txt"],
        "message": "Refactor helper", "ticket_key": None,
    })
    payload = json.loads(result[0].text)
    assert payload["status"] == "pending_confirmation"
    assert payload["on_parent_branch"] is True
    assert "git_start_branch" in payload["instruction"]
    assert "feature branch" in payload["instruction"].lower()


async def test_git_stage_and_commit_no_warning_when_no_parent_branch_stored(tmp_git_repo, tmp_path, monkeypatch):
    from icx_engine.git.mcp_tools import dispatch_git_tool
    monkeypatch.setattr("icx_engine.git.settings._git_settings_root", lambda: tmp_path / ".icx-test-home")
    (tmp_git_repo / "x.txt").write_text("x", encoding="utf-8")
    result = await dispatch_git_tool("git_stage_and_commit", {
        "repo_path": str(tmp_git_repo), "files": ["x.txt"],
        "message": "Refactor helper", "ticket_key": None,
    })
    payload = json.loads(result[0].text)
    assert payload["status"] == "pending_confirmation"
    assert payload["on_parent_branch"] is False
    assert payload["instruction"] == (
        "Show these exact files, message, AND branch to the human. Only call this "
        "tool again with confirm_token set once they explicitly agree."
    )


async def test_git_stage_and_commit_execution_unaffected_by_on_parent_branch_warning(tmp_git_repo, tmp_path, monkeypatch):
    """ICX warns about committing on the parent branch, it never blocks it - the
    confirm_token execute path must succeed identically whether on_parent_branch
    was true or false on the first call."""
    from icx_engine.git.mcp_tools import dispatch_git_tool
    from icx_engine.git.settings import write_repo_settings
    monkeypatch.setattr("icx_engine.git.settings._git_settings_root", lambda: tmp_path / ".icx-test-home")
    write_repo_settings(tmp_git_repo, parent_branch="main")
    (tmp_git_repo / "x.txt").write_text("x", encoding="utf-8")
    first = await dispatch_git_tool("git_stage_and_commit", {
        "repo_path": str(tmp_git_repo), "files": ["x.txt"],
        "message": "Refactor helper", "ticket_key": None,
    })
    first_payload = json.loads(first[0].text)
    assert first_payload["on_parent_branch"] is True
    token = first_payload["token"]
    second = await dispatch_git_tool("git_stage_and_commit", {
        "repo_path": str(tmp_git_repo), "files": ["x.txt"],
        "message": "Refactor helper", "ticket_key": None, "confirm_token": token,
    })
    second_payload = json.loads(second[0].text)
    assert second_payload["ok"] is True
    assert len(second_payload["sha"]) == 40


async def test_git_start_branch_creates_new_branch(tmp_git_repo_with_remote, tmp_path, monkeypatch):
    from icx_engine.git.mcp_tools import dispatch_git_tool
    monkeypatch.setattr("icx_engine.git.settings._git_settings_root", lambda: tmp_path / ".icx-test-home")
    result = await dispatch_git_tool("git_start_branch", {
        "repo_path": str(tmp_git_repo_with_remote), "ticket_key": "ABC-1",
        "summary_or_preferred_name": "Fix login timeout", "parent_branch": "main",
    })
    payload = json.loads(result[0].text)
    assert payload["ok"] is True
    assert payload["created"] is True
    assert payload["switched_to_existing"] is False
    assert payload["branch_name"] == "feature/fix-login-timeout-ABC-1"


async def test_git_start_branch_switches_to_existing(tmp_git_repo_with_remote, tmp_path, monkeypatch):
    from icx_engine.git.mcp_tools import dispatch_git_tool
    monkeypatch.setattr("icx_engine.git.settings._git_settings_root", lambda: tmp_path / ".icx-test-home")
    first = await dispatch_git_tool("git_start_branch", {
        "repo_path": str(tmp_git_repo_with_remote), "ticket_key": "ABC-1",
        "summary_or_preferred_name": "Fix login timeout", "parent_branch": "main",
    })
    assert json.loads(first[0].text)["created"] is True

    second = await dispatch_git_tool("git_start_branch", {
        "repo_path": str(tmp_git_repo_with_remote), "ticket_key": "ABC-1",
        "summary_or_preferred_name": "Fix login timeout", "parent_branch": "main",
    })
    payload = json.loads(second[0].text)
    assert payload["ok"] is True
    assert payload["created"] is False
    assert payload["switched_to_existing"] is True
    assert payload["branch_name"] == "feature/fix-login-timeout-ABC-1"


async def test_git_start_branch_ticketless(tmp_git_repo_with_remote, tmp_path, monkeypatch):
    from icx_engine.git.mcp_tools import dispatch_git_tool
    monkeypatch.setattr("icx_engine.git.settings._git_settings_root", lambda: tmp_path / ".icx-test-home")
    result = await dispatch_git_tool("git_start_branch", {
        "repo_path": str(tmp_git_repo_with_remote), "ticket_key": None,
        "summary_or_preferred_name": "Refactor auth module", "parent_branch": "main",
    })
    payload = json.loads(result[0].text)
    assert payload["ok"] is True
    assert payload["branch_name"] == "feature/refactor-auth-module"


async def test_git_start_branch_parent_omitted_with_saved_value_asks_confirm_remembered(tmp_git_repo_with_remote, monkeypatch):
    # A saved parent branch is never silently reused - it is offered back as
    # proposed_default for the human to confirm, every call.
    from icx_engine.git.mcp_tools import dispatch_git_tool
    from icx_engine.git.settings import write_repo_settings
    monkeypatch.setattr("icx_engine.git.settings._git_settings_root", lambda: tmp_git_repo_with_remote / ".icx-test-home")
    write_repo_settings(tmp_git_repo_with_remote, parent_branch="main")
    result = await dispatch_git_tool("git_start_branch", {
        "repo_path": str(tmp_git_repo_with_remote), "ticket_key": "ABC-1",
        "summary_or_preferred_name": "Fix login timeout",
    })
    payload = json.loads(result[0].text)
    assert payload["status"] == "confirm_remembered"
    assert payload["proposed_default"] == "main"

    confirmed = await dispatch_git_tool("git_start_branch", {
        "repo_path": str(tmp_git_repo_with_remote), "ticket_key": "ABC-1",
        "summary_or_preferred_name": "Fix login timeout", "parent_branch": "main",
    })
    confirmed_payload = json.loads(confirmed[0].text)
    assert confirmed_payload["ok"] is True
    assert confirmed_payload["created"] is True


async def test_git_start_branch_confirmed_once_still_asks_again_next_call(tmp_git_repo_with_remote, monkeypatch):
    # Once confirmed for this repo (via an explicit parent_branch on a prior call),
    # a later call that omits parent_branch must still ask - never proceed silently.
    from icx_engine.git.mcp_tools import dispatch_git_tool
    monkeypatch.setattr("icx_engine.git.settings._git_settings_root", lambda: tmp_git_repo_with_remote / ".icx-test-home")
    first = await dispatch_git_tool("git_start_branch", {
        "repo_path": str(tmp_git_repo_with_remote), "ticket_key": "ABC-1",
        "summary_or_preferred_name": "Fix login timeout", "parent_branch": "main",
    })
    assert json.loads(first[0].text)["ok"] is True

    second = await dispatch_git_tool("git_start_branch", {
        "repo_path": str(tmp_git_repo_with_remote), "ticket_key": "ABC-2",
        "summary_or_preferred_name": "Another feature",
    })
    payload = json.loads(second[0].text)
    assert payload["status"] == "confirm_remembered"
    assert payload["proposed_default"] == "main"


async def test_git_start_branch_parent_omitted_needs_confirmation(tmp_git_repo_with_remote, monkeypatch):
    from icx_engine.git.mcp_tools import dispatch_git_tool
    from icx_engine.git.gitcmd import create_branch_from
    import subprocess
    create_branch_from(tmp_git_repo_with_remote, "development", "main")
    subprocess.run(["git", "push", "origin", "development"], cwd=str(tmp_git_repo_with_remote), check=True)
    monkeypatch.setattr("icx_engine.git.settings._git_settings_root", lambda: tmp_git_repo_with_remote / ".icx-test-home")
    result = await dispatch_git_tool("git_start_branch", {
        "repo_path": str(tmp_git_repo_with_remote), "ticket_key": "ABC-1",
        "summary_or_preferred_name": "Fix login timeout",
    })
    payload = json.loads(result[0].text)
    assert payload["status"] == "needs_confirmation"
    assert payload["proposed_default"] == "development"


async def test_git_start_branch_missing_repo_path_returns_named_error():
    from icx_engine.git.mcp_tools import dispatch_git_tool
    result = await dispatch_git_tool("git_start_branch", {"summary_or_preferred_name": "Fix login"})
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert "repo_path" in payload["error"]
    assert payload["error"] != "'repo_path'"


async def test_git_start_branch_missing_summary_returns_named_error():
    from icx_engine.git.mcp_tools import dispatch_git_tool
    result = await dispatch_git_tool("git_start_branch", {"repo_path": "/fake/repo"})
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert "summary_or_preferred_name" in payload["error"]
    assert payload["error"] != "'summary_or_preferred_name'"


async def test_git_push_requires_confirm_token_then_puts_branch_on_remote(tmp_git_repo_with_remote):
    from icx_engine.git.mcp_tools import dispatch_git_tool
    from icx_engine.git.gitcmd import create_branch_from, checkout, remote_branch_exists

    create_branch_from(tmp_git_repo_with_remote, "feature/push-me", "main")
    checkout(tmp_git_repo_with_remote, "feature/push-me")
    assert remote_branch_exists(tmp_git_repo_with_remote, "feature/push-me") is False

    first = await dispatch_git_tool("git_push", {"repo_path": str(tmp_git_repo_with_remote)})
    first_payload = json.loads(first[0].text)
    assert first_payload["status"] == "pending_confirmation"
    assert first_payload["branch"] == "feature/push-me"
    assert first_payload["remote"] == "origin"
    token = first_payload["token"]
    assert remote_branch_exists(tmp_git_repo_with_remote, "feature/push-me") is False  # not pushed yet

    second = await dispatch_git_tool("git_push", {
        "repo_path": str(tmp_git_repo_with_remote), "confirm_token": token,
    })
    second_payload = json.loads(second[0].text)
    assert second_payload["ok"] is True
    assert second_payload["branch"] == "feature/push-me"
    assert second_payload["remote"] == "origin"
    assert second_payload["pushed"] is True
    assert remote_branch_exists(tmp_git_repo_with_remote, "feature/push-me") is True


async def test_git_push_invalid_token_does_not_push(tmp_git_repo_with_remote):
    from icx_engine.git.mcp_tools import dispatch_git_tool
    from icx_engine.git.gitcmd import create_branch_from, checkout, remote_branch_exists

    create_branch_from(tmp_git_repo_with_remote, "feature/push-me", "main")
    checkout(tmp_git_repo_with_remote, "feature/push-me")

    result = await dispatch_git_tool("git_push", {
        "repo_path": str(tmp_git_repo_with_remote), "confirm_token": "bogus",
    })
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert remote_branch_exists(tmp_git_repo_with_remote, "feature/push-me") is False


async def test_git_push_missing_repo_path_returns_named_error():
    from icx_engine.git.mcp_tools import dispatch_git_tool
    result = await dispatch_git_tool("git_push", {})
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert "repo_path" in payload["error"]
    assert payload["error"] != "'repo_path'"


async def test_git_push_injects_gitlab_auth_env_for_matching_https_origin(tmp_git_repo_with_remote, monkeypatch):
    """The actual push-credential fix, at the standalone git_push tool: this tool calls
    gitcmd.push directly (never through create_mr_for_ticket), so it needs its own
    resolve-connection-and-compute-auth-env wiring - this proves it's there."""
    from icx_engine.git.mcp_tools import dispatch_git_tool
    from icx_engine.git.gitcmd import create_branch_from, checkout
    from icx_engine.models.config import GitLabConnection
    from unittest.mock import Mock

    create_branch_from(tmp_git_repo_with_remote, "feature/push-auth", "main")
    checkout(tmp_git_repo_with_remote, "feature/push-auth")

    conn = GitLabConnection(name="gitlab.example.com", url="https://gitlab.example.com", token="glpat-x")
    mock_push = Mock()
    monkeypatch.setattr("icx_engine.git.manager.remote_url", lambda repo, remote="origin": "https://gitlab.example.com/group/project.git")
    monkeypatch.setattr("icx_engine.git.gitcmd.push", mock_push)

    with patch("icx_engine.config_manager.ConfigManager") as mock_cfg_cls:
        mock_cfg_cls.load.return_value.active_gitlab_connection.return_value = conn
        first = await dispatch_git_tool("git_push", {"repo_path": str(tmp_git_repo_with_remote)})
        token = json.loads(first[0].text)["token"]
        await dispatch_git_tool("git_push", {
            "repo_path": str(tmp_git_repo_with_remote), "confirm_token": token,
        })

    mock_push.assert_called_once()
    auth_env = mock_push.call_args.kwargs["extra_env"]
    assert auth_env is not None
    assert auth_env["GIT_CONFIG_KEY_0"] == "http.extraheader"


async def test_git_push_no_gitlab_connection_still_pushes_with_no_extra_env(tmp_git_repo_with_remote):
    """No regression for the common case (no GitLab connection configured at all) -
    push must still behave exactly as before: extra_env=None, relying on whatever
    git credential already exists."""
    from icx_engine.git.mcp_tools import dispatch_git_tool
    from icx_engine.git.gitcmd import create_branch_from, checkout, remote_branch_exists

    create_branch_from(tmp_git_repo_with_remote, "feature/push-no-conn", "main")
    checkout(tmp_git_repo_with_remote, "feature/push-no-conn")

    with patch("icx_engine.config_manager.ConfigManager") as mock_cfg_cls:
        mock_cfg_cls.load.return_value.active_gitlab_connection.return_value = None
        first = await dispatch_git_tool("git_push", {"repo_path": str(tmp_git_repo_with_remote)})
        token = json.loads(first[0].text)["token"]
        second = await dispatch_git_tool("git_push", {
            "repo_path": str(tmp_git_repo_with_remote), "confirm_token": token,
        })
    payload = json.loads(second[0].text)
    assert payload["ok"] is True
    assert remote_branch_exists(tmp_git_repo_with_remote, "feature/push-no-conn") is True


async def test_git_reverse_merge_reports_clean(tmp_git_repo_with_remote, tmp_path, monkeypatch):
    from icx_engine.git.mcp_tools import dispatch_git_tool
    from icx_engine.git.gitcmd import create_branch_from, checkout, stage_files, commit
    monkeypatch.setattr("icx_engine.git.settings._git_settings_root", lambda: tmp_path / ".icx-test-home")
    create_branch_from(tmp_git_repo_with_remote, "feature/x-ABC-1", "main")
    checkout(tmp_git_repo_with_remote, "feature/x-ABC-1")
    (tmp_git_repo_with_remote / "feature_only.txt").write_text("f", encoding="utf-8")
    stage_files(tmp_git_repo_with_remote, ["feature_only.txt"])
    commit(tmp_git_repo_with_remote, "ABC-1 feature change")

    result = await dispatch_git_tool("git_reverse_merge", {
        "repo_path": str(tmp_git_repo_with_remote), "parent_branch": "main", "ticket_key": "ABC-1",
    })
    payload = json.loads(result[0].text)
    assert payload["ok"] is True
    assert payload["status"] == "clean"


async def test_git_reverse_merge_nullable_ticket_key_succeeds(tmp_git_repo_with_remote, tmp_path, monkeypatch):
    from icx_engine.git.mcp_tools import dispatch_git_tool
    from icx_engine.git.gitcmd import create_branch_from, checkout, stage_files, commit
    monkeypatch.setattr("icx_engine.git.settings._git_settings_root", lambda: tmp_path / ".icx-test-home")
    create_branch_from(tmp_git_repo_with_remote, "feature/x-nope", "main")
    checkout(tmp_git_repo_with_remote, "feature/x-nope")
    (tmp_git_repo_with_remote / "feature_only.txt").write_text("f", encoding="utf-8")
    stage_files(tmp_git_repo_with_remote, ["feature_only.txt"])
    commit(tmp_git_repo_with_remote, "fix: feature change")

    result = await dispatch_git_tool("git_reverse_merge", {
        "repo_path": str(tmp_git_repo_with_remote), "parent_branch": "main", "ticket_key": None,
    })
    payload = json.loads(result[0].text)
    assert payload["ok"] is True
    assert payload["status"] == "clean"


async def test_git_reverse_merge_ticket_key_wrong_type_returns_named_error(tmp_git_repo_with_remote):
    from icx_engine.git.mcp_tools import dispatch_git_tool
    result = await dispatch_git_tool("git_reverse_merge", {
        "repo_path": str(tmp_git_repo_with_remote), "parent_branch": "main", "ticket_key": 123,
    })
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert "ticket_key" in payload["error"]


async def test_git_reverse_merge_reports_conflict_with_scratch_branch(tmp_git_repo_with_remote, tmp_path, monkeypatch):
    from icx_engine.git.mcp_tools import dispatch_git_tool
    from icx_engine.git.gitcmd import create_branch_from, checkout, stage_files, commit
    import subprocess
    monkeypatch.setattr("icx_engine.git.settings._git_settings_root", lambda: tmp_path / ".icx-test-home")
    create_branch_from(tmp_git_repo_with_remote, "feature/x-ABC-1", "main")
    checkout(tmp_git_repo_with_remote, "feature/x-ABC-1")
    (tmp_git_repo_with_remote / "README.md").write_text("feature version\n", encoding="utf-8")
    stage_files(tmp_git_repo_with_remote, ["README.md"])
    commit(tmp_git_repo_with_remote, "ABC-1 feature change")
    checkout(tmp_git_repo_with_remote, "main")
    (tmp_git_repo_with_remote / "README.md").write_text("parent version\n", encoding="utf-8")
    stage_files(tmp_git_repo_with_remote, ["README.md"])
    commit(tmp_git_repo_with_remote, "PARENT-1 parent change")
    subprocess.run(["git", "push"], cwd=str(tmp_git_repo_with_remote), check=True)
    checkout(tmp_git_repo_with_remote, "feature/x-ABC-1")

    result = await dispatch_git_tool("git_reverse_merge", {
        "repo_path": str(tmp_git_repo_with_remote), "parent_branch": "main", "ticket_key": "ABC-1",
    })
    payload = json.loads(result[0].text)
    assert payload["ok"] is True
    assert payload["status"] == "conflict"
    assert payload["conflicted_files"] == ["README.md"]
    assert payload["scratch_branch"].startswith("scratch/ABC-1-")

    conflict_result = await dispatch_git_tool("git_get_conflict", {
        "repo_path": str(tmp_git_repo_with_remote), "file": "README.md",
    })
    conflict_payload = json.loads(conflict_result[0].text)
    assert conflict_payload["ours"] == "feature version\n"
    assert conflict_payload["theirs"] == "parent version\n"

    (tmp_git_repo_with_remote / "README.md").write_text("resolved\n", encoding="utf-8")

    complete_first = await dispatch_git_tool("git_complete_resolution", {
        "repo_path": str(tmp_git_repo_with_remote), "files": ["README.md"], "message": "ABC-1 resolve conflict",
    })
    complete_payload = json.loads(complete_first[0].text)
    assert complete_payload["status"] == "pending_confirmation"
    token = complete_payload["token"]

    complete_second = await dispatch_git_tool("git_complete_resolution", {
        "repo_path": str(tmp_git_repo_with_remote), "files": ["README.md"], "message": "ABC-1 resolve conflict",
        "confirm_token": token,
    })
    assert json.loads(complete_second[0].text)["ok"] is True

    adopt_first = await dispatch_git_tool("git_adopt_resolution", {
        "repo_path": str(tmp_git_repo_with_remote), "feature_branch": "feature/x-ABC-1",
        "scratch_branch": payload["scratch_branch"],
    })
    adopt_token = json.loads(adopt_first[0].text)["token"]
    adopt_second = await dispatch_git_tool("git_adopt_resolution", {
        "repo_path": str(tmp_git_repo_with_remote), "feature_branch": "feature/x-ABC-1",
        "scratch_branch": payload["scratch_branch"], "confirm_token": adopt_token,
    })
    adopt_payload = json.loads(adopt_second[0].text)
    assert adopt_payload["ok"] is True
    assert len(adopt_payload["sha"]) == 40


async def test_git_create_mr_confirmation_gated_and_executes(tmp_git_repo_with_remote, tmp_path, monkeypatch):
    from icx_engine.git.mcp_tools import dispatch_git_tool
    mock_result = type("R", (), {"mr_iid": 5, "created": True, "merged": True, "merge_status": "MERGEABLE", "refusal_reason": None})()
    from icx_engine.models.config import GitLabConnection
    conn = GitLabConnection(name="gitlab.example.com", url="https://gitlab.example.com", token="glpat-x")
    monkeypatch.setattr("icx_engine.git.settings._git_settings_root", lambda: tmp_path / ".icx-test-home")

    with patch("icx_engine.git.mcp_tools.ConfigManager") as mock_cfg_cls:
        mock_cfg_cls.load.return_value.active_gitlab_connection.return_value = conn
        with patch("icx_engine.git.manager.GitLifecycleManager.create_mr_for_ticket", new=AsyncMock(return_value=mock_result)):
            first = await dispatch_git_tool("git_create_mr", {
                "repo_path": str(tmp_git_repo_with_remote), "parent_branch": "main",
                "ticket_key": "ABC-1", "ticket_summary": "Fix login",
            })
            token = json.loads(first[0].text)["token"]
            second = await dispatch_git_tool("git_create_mr", {
                "repo_path": str(tmp_git_repo_with_remote), "parent_branch": "main",
                "ticket_key": "ABC-1", "ticket_summary": "Fix login", "confirm_token": token,
            })
    payload = json.loads(second[0].text)
    assert payload["ok"] is True
    assert payload["merged"] is True


async def test_git_create_mr_nullable_ticket_key_executes(tmp_git_repo_with_remote, tmp_path, monkeypatch):
    from icx_engine.git.mcp_tools import dispatch_git_tool
    mock_result = type("R", (), {"mr_iid": 6, "created": True, "merged": False, "merge_status": "BLOCKED", "refusal_reason": None})()
    from icx_engine.models.config import GitLabConnection
    conn = GitLabConnection(name="gitlab.example.com", url="https://gitlab.example.com", token="glpat-x")
    monkeypatch.setattr("icx_engine.git.settings._git_settings_root", lambda: tmp_path / ".icx-test-home")

    with patch("icx_engine.git.mcp_tools.ConfigManager") as mock_cfg_cls:
        mock_cfg_cls.load.return_value.active_gitlab_connection.return_value = conn
        with patch("icx_engine.git.manager.GitLifecycleManager.create_mr_for_ticket", new=AsyncMock(return_value=mock_result)) as mock_create:
            first = await dispatch_git_tool("git_create_mr", {
                "repo_path": str(tmp_git_repo_with_remote), "parent_branch": "main",
                "ticket_key": None, "ticket_summary": "Fix login",
            })
            token = json.loads(first[0].text)["token"]
            second = await dispatch_git_tool("git_create_mr", {
                "repo_path": str(tmp_git_repo_with_remote), "parent_branch": "main",
                "ticket_key": None, "ticket_summary": "Fix login", "confirm_token": token,
            })
    payload = json.loads(second[0].text)
    assert payload["ok"] is True
    mock_create.assert_called_once()
    assert mock_create.call_args.args[1] is None


async def test_git_create_mr_ticket_key_wrong_type_returns_named_error():
    from icx_engine.git.mcp_tools import dispatch_git_tool
    result = await dispatch_git_tool("git_create_mr", {
        "repo_path": "/fake/repo", "ticket_key": 123, "ticket_summary": "Fix login",
    })
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert "ticket_key" in payload["error"]


async def test_git_create_mr_pending_confirmation_shows_source_branch(tmp_git_repo_with_remote, tmp_path, monkeypatch):
    """Real bug fixed: the confirmation payload used to show only parent_branch
    (the target) and never the source (current feature branch) - the instruction
    text even said 'show ticket, summary, and target branch', source omitted."""
    from icx_engine.git.mcp_tools import dispatch_git_tool
    from icx_engine.git.gitcmd import create_branch_from, checkout
    monkeypatch.setattr("icx_engine.git.settings._git_settings_root", lambda: tmp_path / ".icx-test-home")
    create_branch_from(tmp_git_repo_with_remote, "feature/x-ABC-1", "main")
    checkout(tmp_git_repo_with_remote, "feature/x-ABC-1")

    result = await dispatch_git_tool("git_create_mr", {
        "repo_path": str(tmp_git_repo_with_remote), "parent_branch": "main",
        "ticket_key": "ABC-1", "ticket_summary": "Fix login",
    })
    payload = json.loads(result[0].text)
    assert payload["status"] == "pending_confirmation"
    assert payload["source_branch"] == "feature/x-ABC-1"
    assert payload["parent_branch"] == "main"


async def test_git_create_mr_confirmed_once_still_asks_again_next_call(tmp_git_repo_with_remote, monkeypatch):
    # Once confirmed for this repo, a later call that omits parent_branch must
    # still ask - never proceed silently, even mid-confirmation-gate.
    from icx_engine.git.mcp_tools import dispatch_git_tool
    from icx_engine.git.manager import GitLifecycleManager
    from pathlib import Path
    monkeypatch.setattr("icx_engine.git.settings._git_settings_root", lambda: tmp_git_repo_with_remote / ".icx-test-home")
    mgr = GitLifecycleManager(Path(str(tmp_git_repo_with_remote)))
    mgr.validate()
    mgr.confirm_parent_branch("main")

    result = await dispatch_git_tool("git_create_mr", {
        "repo_path": str(tmp_git_repo_with_remote),
        "ticket_key": "ABC-1", "ticket_summary": "Fix login",
    })
    payload = json.loads(result[0].text)
    assert payload["status"] == "confirm_remembered"
    assert payload["proposed_default"] == "main"


async def test_git_finish_ticket_confirmation_gated_and_executes(tmp_git_repo_with_remote, tmp_path, monkeypatch):
    from icx_engine.git.mcp_tools import dispatch_git_tool
    mock_result = type("R", (), {"parent_branch": "main", "feature_branch_deleted": True, "backups_deleted": []})()
    from icx_engine.models.config import GitLabConnection
    conn = GitLabConnection(name="gitlab.example.com", url="https://gitlab.example.com", token="glpat-x")
    monkeypatch.setattr("icx_engine.git.settings._git_settings_root", lambda: tmp_path / ".icx-test-home")

    with patch("icx_engine.git.mcp_tools.ConfigManager") as mock_cfg_cls:
        mock_cfg_cls.load.return_value.active_gitlab_connection.return_value = conn
        with patch("icx_engine.git.manager.GitLifecycleManager.post_merge_cleanup", return_value=mock_result):
            first = await dispatch_git_tool("git_finish_ticket", {
                "repo_path": str(tmp_git_repo_with_remote), "parent_branch": "main",
                "feature_branch": "feature/x-ABC-1", "ticket_key": "ABC-1",
                "delete_backups": True, "mr_iid": 5,
            })
            token = json.loads(first[0].text)["token"]
            second = await dispatch_git_tool("git_finish_ticket", {
                "repo_path": str(tmp_git_repo_with_remote), "parent_branch": "main",
                "feature_branch": "feature/x-ABC-1", "ticket_key": "ABC-1",
                "delete_backups": True, "mr_iid": 5, "confirm_token": token,
            })
    payload = json.loads(second[0].text)
    assert payload["ok"] is True
    assert payload["feature_branch_deleted"] is True


async def test_git_finish_ticket_nullable_ticket_key_executes(tmp_git_repo_with_remote, tmp_path, monkeypatch):
    from icx_engine.git.mcp_tools import dispatch_git_tool
    mock_result = type("R", (), {"parent_branch": "main", "feature_branch_deleted": True, "backups_deleted": []})()
    from icx_engine.models.config import GitLabConnection
    conn = GitLabConnection(name="gitlab.example.com", url="https://gitlab.example.com", token="glpat-x")
    monkeypatch.setattr("icx_engine.git.settings._git_settings_root", lambda: tmp_path / ".icx-test-home")

    with patch("icx_engine.git.mcp_tools.ConfigManager") as mock_cfg_cls:
        mock_cfg_cls.load.return_value.active_gitlab_connection.return_value = conn
        with patch("icx_engine.git.manager.GitLifecycleManager.post_merge_cleanup", return_value=mock_result) as mock_cleanup:
            first = await dispatch_git_tool("git_finish_ticket", {
                "repo_path": str(tmp_git_repo_with_remote), "parent_branch": "main",
                "feature_branch": "feature/x-nope", "ticket_key": None,
                "delete_backups": True, "mr_iid": 5,
            })
            token = json.loads(first[0].text)["token"]
            second = await dispatch_git_tool("git_finish_ticket", {
                "repo_path": str(tmp_git_repo_with_remote), "parent_branch": "main",
                "feature_branch": "feature/x-nope", "ticket_key": None,
                "delete_backups": True, "mr_iid": 5, "confirm_token": token,
            })
    payload = json.loads(second[0].text)
    assert payload["ok"] is True
    mock_cleanup.assert_called_once()
    assert mock_cleanup.call_args.args[2] is None


async def test_git_finish_ticket_ticket_key_wrong_type_returns_named_error(tmp_git_repo_with_remote):
    from icx_engine.git.mcp_tools import dispatch_git_tool
    result = await dispatch_git_tool("git_finish_ticket", {
        "repo_path": str(tmp_git_repo_with_remote), "parent_branch": "main",
        "feature_branch": "feature/x-ABC-1", "ticket_key": 123, "mr_iid": 5,
    })
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert "ticket_key" in payload["error"]


async def test_git_create_mr_invalid_token_returns_error(tmp_git_repo_with_remote):
    from icx_engine.git.mcp_tools import dispatch_git_tool
    result = await dispatch_git_tool("git_create_mr", {
        "repo_path": str(tmp_git_repo_with_remote), "parent_branch": "main",
        "ticket_key": "ABC-1", "ticket_summary": "Fix login", "confirm_token": "bogus",
    })
    payload = json.loads(result[0].text)
    assert payload["ok"] is False


async def test_git_finish_ticket_invalid_token_returns_error(tmp_git_repo_with_remote):
    from icx_engine.git.mcp_tools import dispatch_git_tool
    result = await dispatch_git_tool("git_finish_ticket", {
        "repo_path": str(tmp_git_repo_with_remote), "parent_branch": "main",
        "feature_branch": "feature/x-ABC-1", "ticket_key": "ABC-1",
        "delete_backups": True, "mr_iid": 5, "confirm_token": "bogus",
    })
    payload = json.loads(result[0].text)
    assert payload["ok"] is False


async def test_git_create_mr_missing_field_without_token_returns_clean_error(tmp_git_repo_with_remote, tmp_path, monkeypatch):
    from icx_engine.git.mcp_tools import dispatch_git_tool
    monkeypatch.setattr("icx_engine.git.settings._git_settings_root", lambda: tmp_path / ".icx-test-home")
    result = await dispatch_git_tool("git_create_mr", {
        "repo_path": str(tmp_git_repo_with_remote), "parent_branch": "main",
        "ticket_key": "ABC-1",
    })
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert "error" in payload


async def test_git_finish_ticket_missing_field_without_token_returns_clean_error(tmp_git_repo_with_remote, tmp_path, monkeypatch):
    from icx_engine.git.mcp_tools import dispatch_git_tool
    monkeypatch.setattr("icx_engine.git.settings._git_settings_root", lambda: tmp_path / ".icx-test-home")
    result = await dispatch_git_tool("git_finish_ticket", {
        "repo_path": str(tmp_git_repo_with_remote), "parent_branch": "main",
        "ticket_key": "ABC-1", "mr_iid": 5,
    })
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert "error" in payload


async def test_git_reverse_merge_reports_needs_confirmation_when_development_exists(tmp_git_repo_with_remote, monkeypatch):
    from icx_engine.git.mcp_tools import dispatch_git_tool
    from icx_engine.git.gitcmd import create_branch_from
    import subprocess
    create_branch_from(tmp_git_repo_with_remote, "development", "main")
    subprocess.run(["git", "push", "origin", "development"], cwd=str(tmp_git_repo_with_remote), check=True)
    monkeypatch.setattr("icx_engine.git.settings._git_settings_root", lambda: tmp_git_repo_with_remote / ".icx-test-home")
    result = await dispatch_git_tool("git_reverse_merge", {
        "repo_path": str(tmp_git_repo_with_remote), "ticket_key": "ABC-1",
    })
    payload = json.loads(result[0].text)
    assert payload["status"] == "needs_confirmation"
    assert payload["proposed_default"] == "development"


async def test_git_reverse_merge_with_explicit_parent_branch_proceeds_and_persists(tmp_git_repo_with_remote, monkeypatch):
    from icx_engine.git.mcp_tools import dispatch_git_tool
    monkeypatch.setattr("icx_engine.git.settings._git_settings_root", lambda: tmp_git_repo_with_remote / ".icx-test-home")
    result = await dispatch_git_tool("git_reverse_merge", {
        "repo_path": str(tmp_git_repo_with_remote), "ticket_key": "ABC-1", "parent_branch": "main",
    })
    payload = json.loads(result[0].text)
    assert payload["ok"] is True
    from icx_engine.git.settings import read_repo_settings
    assert read_repo_settings(tmp_git_repo_with_remote).get("parent_branch") == "main"


async def test_git_reverse_merge_parent_omitted_with_saved_value_asks_confirm_remembered(tmp_git_repo_with_remote, monkeypatch):
    # A saved parent branch is never silently reused - it is offered back as
    # proposed_default for the human to confirm, every call.
    from icx_engine.git.mcp_tools import dispatch_git_tool
    from icx_engine.git.settings import write_repo_settings
    monkeypatch.setattr("icx_engine.git.settings._git_settings_root", lambda: tmp_git_repo_with_remote / ".icx-test-home")
    write_repo_settings(tmp_git_repo_with_remote, parent_branch="main")
    result = await dispatch_git_tool("git_reverse_merge", {
        "repo_path": str(tmp_git_repo_with_remote), "ticket_key": "ABC-1",
    })
    payload = json.loads(result[0].text)
    assert payload["status"] == "confirm_remembered"
    assert payload["proposed_default"] == "main"


async def test_git_stage_and_commit_pending_confirmation_shows_branch(tmp_git_repo):
    from icx_engine.git.mcp_tools import dispatch_git_tool
    (tmp_git_repo / "a.txt").write_text("a", encoding="utf-8")
    result = await dispatch_git_tool("git_stage_and_commit", {
        "repo_path": str(tmp_git_repo), "files": ["a.txt"], "message": "ABC-1 add a.txt", "ticket_key": "ABC-1",
    })
    payload = json.loads(result[0].text)
    assert payload["status"] == "pending_confirmation"
    assert payload["branch"] == "main"


async def test_git_complete_resolution_pending_confirmation_shows_branch(tmp_git_repo):
    from icx_engine.git.gitcmd import create_branch_from, checkout
    from icx_engine.git.safety import create_scratch_branch
    from icx_engine.git.mcp_tools import dispatch_git_tool
    scratch = create_scratch_branch(tmp_git_repo, "main", "ABC-1")
    (tmp_git_repo / "a.txt").write_text("a", encoding="utf-8")
    result = await dispatch_git_tool("git_complete_resolution", {
        "repo_path": str(tmp_git_repo), "files": ["a.txt"], "message": "ABC-1 resolve conflict",
    })
    payload = json.loads(result[0].text)
    assert payload["status"] == "pending_confirmation"
    assert payload["branch"] == scratch


async def test_git_create_tag_confirmation_gated_and_executes(tmp_git_repo_with_remote):
    from icx_engine.git.mcp_tools import dispatch_git_tool
    from icx_engine.gitlab.service import propose_next_tag as real_propose_next_tag
    from icx_engine.models.config import GitLabConnection
    conn = GitLabConnection(name="gitlab.example.com", url="https://gitlab.example.com", token="glpat-x")

    mock_client = AsyncMock()
    mock_client.list_tags.return_value = [{"name": "v0.0.184-qa-20260727002"}]
    mock_client.create_tag.return_value = {"name": "v0.0.185-qa-20260727003"}
    mock_client.get_repository_file.return_value = _SAMPLE_CI_YAML

    # Pins the "today" the production code would otherwise take from the real
    # UTC clock, so this test's outcome never depends on the calendar date it
    # happens to run on - only propose_next_tag's own tested logic (Task 5).
    def _fixed_propose_next_tag(environment, latest, today=None):
        return real_propose_next_tag(environment, latest, today="20260727")

    with patch("icx_engine.gitlab.service.propose_next_tag", side_effect=_fixed_propose_next_tag):
        with patch("icx_engine.git.mcp_tools.ConfigManager") as mock_cfg_cls:
            mock_cfg_cls.load.return_value.active_gitlab_connection.return_value = conn
            with patch("icx_engine.git.mcp_tools.project_path_from_remote_url", return_value="group/project"):
                with patch("icx_engine.git.mcp_tools.GitLabClient") as mock_client_cls:
                    mock_client_cls.return_value.__aenter__.return_value = mock_client
                    first = await dispatch_git_tool("git_create_tag", {
                        "repo_path": str(tmp_git_repo_with_remote), "environment": "qa", "branch": "main",
                    })
                    first_payload = json.loads(first[0].text)
                    assert first_payload["status"] == "pending_confirmation"
                    assert first_payload["previous_tag"] == "v0.0.184-qa-20260727002"
                    assert first_payload["proposed_tag"] == "v0.0.185-qa-20260727003"

                    token = first_payload["token"]
                    second = await dispatch_git_tool("git_create_tag", {
                        "repo_path": str(tmp_git_repo_with_remote), "environment": "qa", "branch": "main",
                        "confirm_token": token,
                    })
    payload = json.loads(second[0].text)
    assert payload["ok"] is True
    assert payload["tag"] == "v0.0.185-qa-20260727003"


# -- git_create_tag: real CI-pattern validation (GIT-2/3/4) -------------------

async def test_git_create_tag_rejects_environment_not_in_real_ci_patterns(tmp_git_repo_with_remote):
    """GIT-2: a free-text environment with no relation to any real one (e.g.
    'STAGING' when only dev/qa exist) must be rejected - the real bug was that
    ICX accepted any string silently and the resulting tag triggered no pipeline."""
    from icx_engine.git.mcp_tools import dispatch_git_tool
    from icx_engine.models.config import GitLabConnection
    conn = GitLabConnection(name="gitlab.example.com", url="https://gitlab.example.com", token="glpat-x")

    mock_client = AsyncMock()
    mock_client.list_tags.return_value = []
    mock_client.get_repository_file.return_value = _SAMPLE_CI_YAML

    with patch("icx_engine.git.mcp_tools.ConfigManager") as mock_cfg_cls:
        mock_cfg_cls.load.return_value.active_gitlab_connection.return_value = conn
        with patch("icx_engine.git.mcp_tools.project_path_from_remote_url", return_value="group/project"):
            with patch("icx_engine.git.mcp_tools.GitLabClient") as mock_client_cls:
                mock_client_cls.return_value.__aenter__.return_value = mock_client
                result = await dispatch_git_tool("git_create_tag", {
                    "repo_path": str(tmp_git_repo_with_remote), "environment": "STAGING", "branch": "main",
                })
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert "does not match any tag-triggering environment" in payload["error"]
    assert "'dev'" in payload["error"] and "'qa'" in payload["error"]


async def test_git_create_tag_normalizes_wrong_case_environment_instead_of_producing_a_dead_tag(tmp_git_repo_with_remote):
    """The precise real-world bug this closes: passing 'DEV' (wrong case) used to
    either get accepted verbatim (a tag with no matching CI pattern, a silent
    no-op) or now would fail the pattern check a second time for the same reason.
    Normalizing to the real observed casing here means it just works instead."""
    from icx_engine.git.mcp_tools import dispatch_git_tool
    from icx_engine.gitlab.service import propose_next_tag as real_propose_next_tag
    from icx_engine.models.config import GitLabConnection
    conn = GitLabConnection(name="gitlab.example.com", url="https://gitlab.example.com", token="glpat-x")

    mock_client = AsyncMock()
    mock_client.list_tags.return_value = [{"name": "v0.0.184-qa-20260727002"}]
    mock_client.get_repository_file.return_value = _SAMPLE_CI_YAML

    def _fixed_propose_next_tag(environment, latest, today=None):
        return real_propose_next_tag(environment, latest, today="20260727")

    with patch("icx_engine.gitlab.service.propose_next_tag", side_effect=_fixed_propose_next_tag):
        with patch("icx_engine.git.mcp_tools.ConfigManager") as mock_cfg_cls:
            mock_cfg_cls.load.return_value.active_gitlab_connection.return_value = conn
            with patch("icx_engine.git.mcp_tools.project_path_from_remote_url", return_value="group/project"):
                with patch("icx_engine.git.mcp_tools.GitLabClient") as mock_client_cls:
                    mock_client_cls.return_value.__aenter__.return_value = mock_client
                    result = await dispatch_git_tool("git_create_tag", {
                        "repo_path": str(tmp_git_repo_with_remote), "environment": "QA", "branch": "main",
                    })
    payload = json.loads(result[0].text)
    assert payload["status"] == "pending_confirmation"
    assert payload["ci_pipeline_will_trigger"] is True
    assert "-qa-" in payload["proposed_tag"]
    assert "-QA-" not in payload["proposed_tag"]


async def test_git_create_tag_refuses_when_proposed_tag_matches_no_ci_pattern(tmp_git_repo_with_remote):
    """GIT-3: creating a tag that triggers no pipeline is a silent no-op - must
    refuse rather than succeed, unless override_ci_check is explicitly passed."""
    from icx_engine.git.mcp_tools import dispatch_git_tool
    from icx_engine.models.config import GitLabConnection
    conn = GitLabConnection(name="gitlab.example.com", url="https://gitlab.example.com", token="glpat-x")

    mock_client = AsyncMock()
    mock_client.list_tags.return_value = []
    mock_client.get_repository_file.return_value = _SAMPLE_CI_YAML

    with patch("icx_engine.git.mcp_tools.ConfigManager") as mock_cfg_cls:
        mock_cfg_cls.load.return_value.active_gitlab_connection.return_value = conn
        with patch("icx_engine.git.mcp_tools.project_path_from_remote_url", return_value="group/project"):
            with patch("icx_engine.git.mcp_tools.GitLabClient") as mock_client_cls:
                mock_client_cls.return_value.__aenter__.return_value = mock_client
                result = await dispatch_git_tool("git_create_tag", {
                    "repo_path": str(tmp_git_repo_with_remote), "environment": "qa", "branch": "main",
                    "tag_name_override": "v0.0.1-qa-not-a-real-date",
                })
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert "does not match any tag-triggering pattern" in payload["error"]


async def test_git_create_tag_override_ci_check_bypasses_refusal(tmp_git_repo_with_remote):
    from icx_engine.git.mcp_tools import dispatch_git_tool
    from icx_engine.models.config import GitLabConnection
    conn = GitLabConnection(name="gitlab.example.com", url="https://gitlab.example.com", token="glpat-x")

    mock_client = AsyncMock()
    mock_client.list_tags.return_value = []
    mock_client.get_repository_file.return_value = _SAMPLE_CI_YAML

    with patch("icx_engine.git.mcp_tools.ConfigManager") as mock_cfg_cls:
        mock_cfg_cls.load.return_value.active_gitlab_connection.return_value = conn
        with patch("icx_engine.git.mcp_tools.project_path_from_remote_url", return_value="group/project"):
            with patch("icx_engine.git.mcp_tools.GitLabClient") as mock_client_cls:
                mock_client_cls.return_value.__aenter__.return_value = mock_client
                result = await dispatch_git_tool("git_create_tag", {
                    "repo_path": str(tmp_git_repo_with_remote), "environment": "qa", "branch": "main",
                    "tag_name_override": "v0.0.1-qa-not-a-real-date", "override_ci_check": True,
                })
    payload = json.loads(result[0].text)
    assert payload["status"] == "pending_confirmation"
    assert payload["ci_pipeline_will_trigger"] is False


async def test_git_create_tag_no_previous_tag_carries_explicit_warning(tmp_git_repo_with_remote):
    """GIT-4: previous_tag: null must be a hard-to-miss warning, not a footnote -
    it can mean the environment name itself is wrong, not 'first tag ever'."""
    from icx_engine.git.mcp_tools import dispatch_git_tool
    from icx_engine.gitlab.service import propose_next_tag as real_propose_next_tag
    from icx_engine.models.config import GitLabConnection
    conn = GitLabConnection(name="gitlab.example.com", url="https://gitlab.example.com", token="glpat-x")

    mock_client = AsyncMock()
    mock_client.list_tags.return_value = []  # no tags at all for this environment
    mock_client.get_repository_file.return_value = _SAMPLE_CI_YAML

    def _fixed_propose_next_tag(environment, latest, today=None):
        return real_propose_next_tag(environment, latest, today="20260727")

    with patch("icx_engine.gitlab.service.propose_next_tag", side_effect=_fixed_propose_next_tag):
        with patch("icx_engine.git.mcp_tools.ConfigManager") as mock_cfg_cls:
            mock_cfg_cls.load.return_value.active_gitlab_connection.return_value = conn
            with patch("icx_engine.git.mcp_tools.project_path_from_remote_url", return_value="group/project"):
                with patch("icx_engine.git.mcp_tools.GitLabClient") as mock_client_cls:
                    mock_client_cls.return_value.__aenter__.return_value = mock_client
                    result = await dispatch_git_tool("git_create_tag", {
                        "repo_path": str(tmp_git_repo_with_remote), "environment": "qa", "branch": "main",
                    })
    payload = json.loads(result[0].text)
    assert payload["previous_tag"] is None
    assert "warning" in payload
    assert "environment name is wrong" in payload["warning"]


async def test_git_create_tag_degrades_to_warning_when_ci_file_unfetchable(tmp_git_repo_with_remote):
    """If .gitlab-ci.yml can't be fetched, this must degrade to a surfaced warning,
    never silently proceed as if validated and never hard-block on an
    infrastructure hiccup."""
    from icx_engine.git.mcp_tools import dispatch_git_tool
    from icx_engine.gitlab.service import propose_next_tag as real_propose_next_tag
    from icx_engine.models.config import GitLabConnection
    from icx_engine.gitlab.client import GitLabError
    conn = GitLabConnection(name="gitlab.example.com", url="https://gitlab.example.com", token="glpat-x")

    mock_client = AsyncMock()
    mock_client.list_tags.return_value = [{"name": "v0.0.184-qa-20260727002"}]
    mock_client.get_repository_file.side_effect = GitLabError("File not found", 404)

    def _fixed_propose_next_tag(environment, latest, today=None):
        return real_propose_next_tag(environment, latest, today="20260727")

    with patch("icx_engine.gitlab.service.propose_next_tag", side_effect=_fixed_propose_next_tag):
        with patch("icx_engine.git.mcp_tools.ConfigManager") as mock_cfg_cls:
            mock_cfg_cls.load.return_value.active_gitlab_connection.return_value = conn
            with patch("icx_engine.git.mcp_tools.project_path_from_remote_url", return_value="group/project"):
                with patch("icx_engine.git.mcp_tools.GitLabClient") as mock_client_cls:
                    mock_client_cls.return_value.__aenter__.return_value = mock_client
                    result = await dispatch_git_tool("git_create_tag", {
                        "repo_path": str(tmp_git_repo_with_remote), "environment": "qa", "branch": "main",
                    })
    payload = json.loads(result[0].text)
    assert payload["status"] == "pending_confirmation"  # never hard-blocked
    assert "ci_check_error" in payload
    assert payload["ci_pipeline_will_trigger"] is None


async def test_git_repo_status_missing_repo_path_returns_named_error():
    from icx_engine.git.mcp_tools import dispatch_git_tool
    result = await dispatch_git_tool("git_repo_status", {})
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert "repo_path" in payload["error"]
    assert payload["error"] != "'repo_path'"


async def test_git_stage_and_commit_missing_message_returns_named_error():
    from icx_engine.git.mcp_tools import dispatch_git_tool
    result = await dispatch_git_tool("git_stage_and_commit", {
        "repo_path": "/fake/repo", "files": ["x.txt"], "ticket_key": None,
    })
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert "message" in payload["error"]
    assert payload["error"] != "'message'"


async def test_git_reverse_merge_missing_ticket_key_returns_named_error():
    from icx_engine.git.mcp_tools import dispatch_git_tool
    result = await dispatch_git_tool("git_reverse_merge", {"repo_path": "/fake/repo"})
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert "ticket_key" in payload["error"]
    assert payload["error"] != "'ticket_key'"


async def test_git_get_conflict_missing_file_returns_named_error():
    from icx_engine.git.mcp_tools import dispatch_git_tool
    result = await dispatch_git_tool("git_get_conflict", {"repo_path": "/fake/repo"})
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert "file" in payload["error"]
    assert payload["error"] != "'file'"


async def test_git_complete_resolution_missing_files_returns_named_error():
    from icx_engine.git.mcp_tools import dispatch_git_tool
    result = await dispatch_git_tool("git_complete_resolution", {
        "repo_path": "/fake/repo", "message": "ABC-1 resolve",
    })
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert "files" in payload["error"]
    assert payload["error"] != "'files'"


async def test_git_adopt_resolution_missing_scratch_branch_returns_named_error():
    from icx_engine.git.mcp_tools import dispatch_git_tool
    result = await dispatch_git_tool("git_adopt_resolution", {
        "repo_path": "/fake/repo", "feature_branch": "feature/x-ABC-1",
    })
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert "scratch_branch" in payload["error"]
    assert payload["error"] != "'scratch_branch'"


async def test_git_adopt_resolution_missing_repo_path_returns_named_error():
    # Regression: repo_path is schema-required and gets baked into the
    # confirm_token payload, read back as payload["repo_path"] at confirm
    # time - omitting it must fail clearly on the FIRST call, not surface as
    # a bare KeyError on the second (confirm_token) call.
    from icx_engine.git.mcp_tools import dispatch_git_tool
    result = await dispatch_git_tool("git_adopt_resolution", {
        "feature_branch": "feature/x-ABC-1", "scratch_branch": "scratch/ABC-1-xyz",
    })
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert "repo_path" in payload["error"]
    assert payload["error"] != "'repo_path'"


async def test_git_discard_scratch_requires_confirm_token_then_deletes(tmp_git_repo_with_remote, tmp_path, monkeypatch):
    """Regression: discard_scratch force-deletes a branch that may hold in-progress
    conflict-resolution work - it must be confirmation-gated like its sibling
    resolution tools, not execute immediately on the first call."""
    from icx_engine.git.mcp_tools import dispatch_git_tool
    from icx_engine.git.gitcmd import create_branch_from, checkout, stage_files, commit, local_branch_exists
    import subprocess
    monkeypatch.setattr("icx_engine.git.settings._git_settings_root", lambda: tmp_path / ".icx-test-home")
    create_branch_from(tmp_git_repo_with_remote, "feature/x-ABC-9", "main")
    checkout(tmp_git_repo_with_remote, "feature/x-ABC-9")
    (tmp_git_repo_with_remote / "README.md").write_text("feature version\n", encoding="utf-8")
    stage_files(tmp_git_repo_with_remote, ["README.md"])
    commit(tmp_git_repo_with_remote, "ABC-9 feature change")
    checkout(tmp_git_repo_with_remote, "main")
    (tmp_git_repo_with_remote / "README.md").write_text("parent version\n", encoding="utf-8")
    stage_files(tmp_git_repo_with_remote, ["README.md"])
    commit(tmp_git_repo_with_remote, "PARENT-1 parent change")
    subprocess.run(["git", "push"], cwd=str(tmp_git_repo_with_remote), check=True)
    checkout(tmp_git_repo_with_remote, "feature/x-ABC-9")

    merge_result = await dispatch_git_tool("git_reverse_merge", {
        "repo_path": str(tmp_git_repo_with_remote), "parent_branch": "main", "ticket_key": "ABC-9",
    })
    scratch_branch = json.loads(merge_result[0].text)["scratch_branch"]

    first = await dispatch_git_tool("git_discard_scratch", {
        "repo_path": str(tmp_git_repo_with_remote),
        "feature_branch": "feature/x-ABC-9", "scratch_branch": scratch_branch,
    })
    first_payload = json.loads(first[0].text)
    assert first_payload["status"] == "pending_confirmation"
    token = first_payload["token"]
    assert local_branch_exists(tmp_git_repo_with_remote, scratch_branch)  # not deleted yet

    second = await dispatch_git_tool("git_discard_scratch", {
        "repo_path": str(tmp_git_repo_with_remote),
        "feature_branch": "feature/x-ABC-9", "scratch_branch": scratch_branch,
        "confirm_token": token,
    })
    second_payload = json.loads(second[0].text)
    assert second_payload["ok"] is True
    assert second_payload["discarded"] == scratch_branch
    assert not local_branch_exists(tmp_git_repo_with_remote, scratch_branch)


async def test_git_discard_scratch_missing_feature_branch_returns_named_error():
    from icx_engine.git.mcp_tools import dispatch_git_tool
    result = await dispatch_git_tool("git_discard_scratch", {
        "repo_path": "/fake/repo", "scratch_branch": "scratch/ABC-1-xyz",
    })
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert "feature_branch" in payload["error"]
    assert payload["error"] != "'feature_branch'"


async def test_git_create_mr_missing_ticket_summary_returns_named_error():
    from icx_engine.git.mcp_tools import dispatch_git_tool
    result = await dispatch_git_tool("git_create_mr", {
        "repo_path": "/fake/repo", "ticket_key": "ABC-1",
    })
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert "ticket_summary" in payload["error"]
    assert payload["error"] != "'ticket_summary'"


async def test_git_finish_ticket_missing_mr_iid_returns_named_error():
    from icx_engine.git.mcp_tools import dispatch_git_tool
    result = await dispatch_git_tool("git_finish_ticket", {
        "repo_path": "/fake/repo", "feature_branch": "feature/x-ABC-1", "ticket_key": "ABC-1",
    })
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert "mr_iid" in payload["error"]
    assert payload["error"] != "'mr_iid'"


async def test_git_create_tag_missing_environment_returns_named_error():
    from icx_engine.git.mcp_tools import dispatch_git_tool
    result = await dispatch_git_tool("git_create_tag", {
        "repo_path": "/fake/repo", "branch": "main",
    })
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert "environment" in payload["error"]
    assert payload["error"] != "'environment'"


async def test_git_create_tag_with_override_uses_exact_name(tmp_git_repo_with_remote):
    from icx_engine.git.mcp_tools import dispatch_git_tool
    from icx_engine.models.config import GitLabConnection
    conn = GitLabConnection(name="gitlab.example.com", url="https://gitlab.example.com", token="glpat-x")

    mock_client = AsyncMock()
    mock_client.list_tags.return_value = [{"name": "v0.0.184-qa-20260727002"}]
    mock_client.create_tag.return_value = {"name": "v9.9.9-qa-custom"}
    mock_client.get_repository_file.return_value = _SAMPLE_CI_YAML

    with patch("icx_engine.git.mcp_tools.ConfigManager") as mock_cfg_cls:
        mock_cfg_cls.load.return_value.active_gitlab_connection.return_value = conn
        with patch("icx_engine.git.mcp_tools.project_path_from_remote_url", return_value="group/project"):
            with patch("icx_engine.git.mcp_tools.GitLabClient") as mock_client_cls:
                mock_client_cls.return_value.__aenter__.return_value = mock_client
                first = await dispatch_git_tool("git_create_tag", {
                    "repo_path": str(tmp_git_repo_with_remote), "environment": "qa", "branch": "main",
                    "tag_name_override": "v9.9.9-qa-custom", "override_ci_check": True,
                })
                first_payload = json.loads(first[0].text)
                assert first_payload["proposed_tag"] == "v9.9.9-qa-custom"
                token = first_payload["token"]
                second = await dispatch_git_tool("git_create_tag", {
                    "repo_path": str(tmp_git_repo_with_remote), "environment": "qa", "branch": "main",
                    "tag_name_override": "v9.9.9-qa-custom", "confirm_token": token,
                })
    payload = json.loads(second[0].text)
    assert payload["tag"] == "v9.9.9-qa-custom"


async def test_git_repo_status_not_a_repo_passes_through_git_workflow_error_message(tmp_path):
    # Regression guard: GitWorkflowError's own message must reach the caller
    # unchanged - no redundant re-wrapping tier should alter it (Task 3).
    from icx_engine.git.mcp_tools import dispatch_git_tool
    not_a_repo = tmp_path / "nope"
    not_a_repo.mkdir()
    result = await dispatch_git_tool("git_repo_status", {"repo_path": str(not_a_repo)})
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert payload["error"] == f"'{not_a_repo}' is not a git repository. Run this from inside one."


async def test_git_create_mr_no_gitlab_connection_returns_fallback_hint(tmp_git_repo_with_remote, tmp_path, monkeypatch):
    from icx_engine.git.mcp_tools import dispatch_git_tool
    monkeypatch.setattr("icx_engine.git.settings._git_settings_root", lambda: tmp_path / ".icx-test-home")

    with patch("icx_engine.git.mcp_tools.ConfigManager") as mock_cfg_cls:
        mock_cfg_cls.load.return_value.active_gitlab_connection.return_value = None
        first = await dispatch_git_tool("git_create_mr", {
            "repo_path": str(tmp_git_repo_with_remote), "parent_branch": "main",
            "ticket_key": "ABC-1", "ticket_summary": "Fix login",
        })
        token = json.loads(first[0].text)["token"]
        second = await dispatch_git_tool("git_create_mr", {
            "repo_path": str(tmp_git_repo_with_remote), "parent_branch": "main",
            "ticket_key": "ABC-1", "ticket_summary": "Fix login", "confirm_token": token,
        })
    payload = json.loads(second[0].text)
    assert payload["ok"] is False
    assert payload["error"] == "No active GitLab connection. Run `icx gitlab --add` first."
    assert payload["fallback"] == _ICX_FALLBACK("GitLab", "icx gitlab --add")


async def test_git_finish_ticket_no_gitlab_connection_returns_fallback_hint(tmp_git_repo_with_remote, tmp_path, monkeypatch):
    from icx_engine.git.mcp_tools import dispatch_git_tool
    monkeypatch.setattr("icx_engine.git.settings._git_settings_root", lambda: tmp_path / ".icx-test-home")

    with patch("icx_engine.git.mcp_tools.ConfigManager") as mock_cfg_cls:
        mock_cfg_cls.load.return_value.active_gitlab_connection.return_value = None
        first = await dispatch_git_tool("git_finish_ticket", {
            "repo_path": str(tmp_git_repo_with_remote), "parent_branch": "main",
            "feature_branch": "feature/x-ABC-1", "ticket_key": "ABC-1",
            "delete_backups": True, "mr_iid": 5,
        })
        token = json.loads(first[0].text)["token"]
        second = await dispatch_git_tool("git_finish_ticket", {
            "repo_path": str(tmp_git_repo_with_remote), "parent_branch": "main",
            "feature_branch": "feature/x-ABC-1", "ticket_key": "ABC-1",
            "delete_backups": True, "mr_iid": 5, "confirm_token": token,
        })
    payload = json.loads(second[0].text)
    assert payload["ok"] is False
    assert payload["error"] == "No active GitLab connection. Run `icx gitlab --add` first."
    assert payload["fallback"] == _ICX_FALLBACK("GitLab", "icx gitlab --add")


async def test_git_create_tag_no_gitlab_connection_returns_fallback_hint_before_confirm(tmp_git_repo_with_remote):
    from icx_engine.git.mcp_tools import dispatch_git_tool

    with patch("icx_engine.git.mcp_tools.ConfigManager") as mock_cfg_cls:
        mock_cfg_cls.load.return_value.active_gitlab_connection.return_value = None
        result = await dispatch_git_tool("git_create_tag", {
            "repo_path": str(tmp_git_repo_with_remote), "environment": "qa", "branch": "main",
        })
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert payload["error"] == "No active GitLab connection. Run `icx gitlab --add` first."
    assert payload["fallback"] == _ICX_FALLBACK("GitLab", "icx gitlab --add")


async def test_git_blame_reports_commit_and_author_per_line(tmp_git_repo):
    from icx_engine.git.mcp_tools import dispatch_git_tool
    from icx_engine.git.gitcmd import stage_files, commit
    (tmp_git_repo / "target.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
    stage_files(tmp_git_repo, ["target.txt"])
    sha = commit(tmp_git_repo, "ABC-1 add target")

    result = await dispatch_git_tool("git_blame", {
        "repo_path": str(tmp_git_repo), "relpath": "target.txt",
    })
    payload = json.loads(result[0].text)
    assert payload["ok"] is True
    assert len(payload["lines"]) == 3
    assert payload["lines"][0]["commit_sha"] == sha
    assert payload["lines"][0]["content"] == "one"


async def test_git_blame_line_range_narrows_results(tmp_git_repo):
    from icx_engine.git.mcp_tools import dispatch_git_tool
    from icx_engine.git.gitcmd import stage_files, commit
    (tmp_git_repo / "target.txt").write_text("one\ntwo\nthree\nfour\n", encoding="utf-8")
    stage_files(tmp_git_repo, ["target.txt"])
    commit(tmp_git_repo, "ABC-1 add target")

    result = await dispatch_git_tool("git_blame", {
        "repo_path": str(tmp_git_repo), "relpath": "target.txt", "line_start": 2, "line_end": 3,
    })
    payload = json.loads(result[0].text)
    assert payload["ok"] is True
    assert [entry["line_no"] for entry in payload["lines"]] == [2, 3]


async def test_git_blame_only_line_start_returns_named_error(tmp_git_repo):
    from icx_engine.git.mcp_tools import dispatch_git_tool
    result = await dispatch_git_tool("git_blame", {
        "repo_path": str(tmp_git_repo), "relpath": "target.txt", "line_start": 2,
    })
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert "line_start" in payload["error"]
    assert "line_end" in payload["error"]


async def test_git_blame_only_line_end_returns_named_error(tmp_git_repo):
    from icx_engine.git.mcp_tools import dispatch_git_tool
    result = await dispatch_git_tool("git_blame", {
        "repo_path": str(tmp_git_repo), "relpath": "target.txt", "line_end": 3,
    })
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert "line_start" in payload["error"]
    assert "line_end" in payload["error"]


async def test_git_blame_missing_repo_path_returns_named_error():
    from icx_engine.git.mcp_tools import dispatch_git_tool
    result = await dispatch_git_tool("git_blame", {"relpath": "target.txt"})
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert "repo_path" in payload["error"]
    assert payload["error"] != "'repo_path'"


async def test_git_log_returns_commits_newest_first(tmp_git_repo):
    from icx_engine.git.mcp_tools import dispatch_git_tool
    from icx_engine.git.gitcmd import stage_files, commit
    (tmp_git_repo / "a.txt").write_text("a", encoding="utf-8")
    stage_files(tmp_git_repo, ["a.txt"])
    commit(tmp_git_repo, "ABC-1 first")
    (tmp_git_repo / "b.txt").write_text("b", encoding="utf-8")
    stage_files(tmp_git_repo, ["b.txt"])
    sha2 = commit(tmp_git_repo, "ABC-1 second")

    result = await dispatch_git_tool("git_log", {"repo_path": str(tmp_git_repo)})
    payload = json.loads(result[0].text)
    assert payload["ok"] is True
    assert payload["commits"][0]["sha"] == sha2
    assert payload["commits"][0]["subject"] == "ABC-1 second"


async def test_git_log_missing_repo_path_returns_named_error():
    from icx_engine.git.mcp_tools import dispatch_git_tool
    result = await dispatch_git_tool("git_log", {})
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert "repo_path" in payload["error"]
    assert payload["error"] != "'repo_path'"


async def test_git_show_commit_returns_message_author_and_files(tmp_git_repo):
    from icx_engine.git.mcp_tools import dispatch_git_tool
    from icx_engine.git.gitcmd import stage_files, commit
    (tmp_git_repo / "new.txt").write_text("x", encoding="utf-8")
    stage_files(tmp_git_repo, ["new.txt"])
    sha = commit(tmp_git_repo, "ABC-1 add new.txt")

    result = await dispatch_git_tool("git_show_commit", {
        "repo_path": str(tmp_git_repo), "sha": sha,
    })
    payload = json.loads(result[0].text)
    assert payload["ok"] is True
    assert payload["sha"] == sha
    assert payload["subject"] == "ABC-1 add new.txt"
    assert payload["files"] == [{"path": "new.txt", "status": "A"}]


async def test_git_show_commit_bogus_sha_returns_error_not_crash(tmp_git_repo):
    from icx_engine.git.mcp_tools import dispatch_git_tool
    result = await dispatch_git_tool("git_show_commit", {
        "repo_path": str(tmp_git_repo), "sha": "0" * 40,
    })
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert "error" in payload


async def test_git_show_commit_missing_repo_path_returns_named_error():
    from icx_engine.git.mcp_tools import dispatch_git_tool
    result = await dispatch_git_tool("git_show_commit", {"sha": "abc123"})
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert "repo_path" in payload["error"]
    assert payload["error"] != "'repo_path'"


async def test_git_diff_reports_per_file_status_and_counts(tmp_git_repo):
    from icx_engine.git.mcp_tools import dispatch_git_tool
    from icx_engine.git.gitcmd import stage_files, commit, create_branch_from, checkout
    create_branch_from(tmp_git_repo, "feature/diff-b", "main")
    create_branch_from(tmp_git_repo, "feature/diff-a", "main")
    checkout(tmp_git_repo, "feature/diff-a")
    (tmp_git_repo / "added.txt").write_text("brand new\n", encoding="utf-8")
    stage_files(tmp_git_repo, ["added.txt"])
    commit(tmp_git_repo, "ABC-1 add added.txt")

    result = await dispatch_git_tool("git_diff", {
        "repo_path": str(tmp_git_repo), "ref_a": "feature/diff-b", "ref_b": "feature/diff-a",
    })
    payload = json.loads(result[0].text)
    assert payload["ok"] is True
    by_path = {entry["path"]: entry for entry in payload["files"]}
    assert by_path["added.txt"]["status"] == "A"
    assert by_path["added.txt"]["insertions"] == 1


async def test_git_diff_missing_repo_path_returns_named_error():
    from icx_engine.git.mcp_tools import dispatch_git_tool
    result = await dispatch_git_tool("git_diff", {"ref_a": "main", "ref_b": "main"})
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert "repo_path" in payload["error"]
    assert payload["error"] != "'repo_path'"


async def test_git_diff_worktree_staged_mode_reports_only_index_changes(tmp_git_repo):
    from icx_engine.git.mcp_tools import dispatch_git_tool
    from icx_engine.git.gitcmd import stage_files
    (tmp_git_repo / "staged.txt").write_text("staged\n", encoding="utf-8")
    stage_files(tmp_git_repo, ["staged.txt"])
    (tmp_git_repo / "README.md").write_text("unstaged change\n", encoding="utf-8")

    result = await dispatch_git_tool("git_diff_worktree", {
        "repo_path": str(tmp_git_repo), "mode": "staged",
    })
    payload = json.loads(result[0].text)
    assert payload["ok"] is True
    paths = {f["path"] for f in payload["files"]}
    assert paths == {"staged.txt"}


async def test_git_diff_worktree_combined_mode_scoped_to_relpath(tmp_git_repo):
    from icx_engine.git.mcp_tools import dispatch_git_tool
    from icx_engine.git.gitcmd import stage_files
    (tmp_git_repo / "staged.txt").write_text("staged\n", encoding="utf-8")
    stage_files(tmp_git_repo, ["staged.txt"])
    (tmp_git_repo / "README.md").write_text("unstaged change\n", encoding="utf-8")

    result = await dispatch_git_tool("git_diff_worktree", {
        "repo_path": str(tmp_git_repo), "mode": "combined", "relpath": "README.md",
    })
    payload = json.loads(result[0].text)
    assert payload["ok"] is True
    paths = {f["path"] for f in payload["files"]}
    assert paths == {"README.md"}


async def test_git_diff_worktree_invalid_mode_returns_named_error(tmp_git_repo):
    from icx_engine.git.mcp_tools import dispatch_git_tool
    result = await dispatch_git_tool("git_diff_worktree", {
        "repo_path": str(tmp_git_repo), "mode": "bogus",
    })
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert "mode" in payload["error"]


async def test_git_diff_worktree_missing_repo_path_returns_named_error():
    from icx_engine.git.mcp_tools import dispatch_git_tool
    result = await dispatch_git_tool("git_diff_worktree", {"mode": "combined"})
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert "repo_path" in payload["error"]


async def test_git_read_file_at_ref_returns_content_at_head(tmp_git_repo):
    from icx_engine.git.mcp_tools import dispatch_git_tool
    result = await dispatch_git_tool("git_read_file_at_ref", {
        "repo_path": str(tmp_git_repo), "ref": "HEAD", "path": "README.md",
    })
    payload = json.loads(result[0].text)
    assert payload["ok"] is True
    assert payload["content"] == "hello\n"
    assert payload["ref"] == "HEAD"
    assert payload["path"] == "README.md"


async def test_git_read_file_at_ref_missing_path_raises_named_error(tmp_git_repo):
    from icx_engine.git.mcp_tools import dispatch_git_tool
    result = await dispatch_git_tool("git_read_file_at_ref", {
        "repo_path": str(tmp_git_repo), "ref": "HEAD", "path": "does_not_exist.txt",
    })
    payload = json.loads(result[0].text)
    assert payload["ok"] is False


async def test_git_read_file_at_ref_missing_ref_returns_named_error(tmp_git_repo):
    from icx_engine.git.mcp_tools import dispatch_git_tool
    result = await dispatch_git_tool("git_read_file_at_ref", {
        "repo_path": str(tmp_git_repo), "path": "README.md",
    })
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert "ref" in payload["error"]


async def test_git_create_tag_no_gitlab_connection_returns_fallback_hint_after_confirm(tmp_git_repo_with_remote):
    # Connection is active at token-issue time but revoked before the human
    # confirms - the second (confirm_token) branch re-checks independently
    # and must surface the same fallback hint, not a bare KeyError/crash.
    from icx_engine.git.mcp_tools import dispatch_git_tool
    from icx_engine.gitlab.service import propose_next_tag as real_propose_next_tag
    from icx_engine.models.config import GitLabConnection
    conn = GitLabConnection(name="gitlab.example.com", url="https://gitlab.example.com", token="glpat-x")

    mock_client = AsyncMock()
    mock_client.list_tags.return_value = [{"name": "v0.0.184-qa-20260727002"}]
    mock_client.get_repository_file.return_value = _SAMPLE_CI_YAML

    def _fixed_propose_next_tag(environment, latest, today=None):
        return real_propose_next_tag(environment, latest, today="20260727")

    with patch("icx_engine.gitlab.service.propose_next_tag", side_effect=_fixed_propose_next_tag):
        with patch("icx_engine.git.mcp_tools.ConfigManager") as mock_cfg_cls:
            mock_cfg_cls.load.return_value.active_gitlab_connection.side_effect = [conn, None]
            with patch("icx_engine.git.mcp_tools.project_path_from_remote_url", return_value="group/project"):
                with patch("icx_engine.git.mcp_tools.GitLabClient") as mock_client_cls:
                    mock_client_cls.return_value.__aenter__.return_value = mock_client
                    first = await dispatch_git_tool("git_create_tag", {
                        "repo_path": str(tmp_git_repo_with_remote), "environment": "qa", "branch": "main",
                    })
                    token = json.loads(first[0].text)["token"]
                    second = await dispatch_git_tool("git_create_tag", {
                        "repo_path": str(tmp_git_repo_with_remote), "environment": "qa", "branch": "main",
                        "confirm_token": token,
                    })
    payload = json.loads(second[0].text)
    assert payload["ok"] is False
    assert payload["error"] == "No active GitLab connection. Run `icx gitlab --add` first."
    assert payload["fallback"] == _ICX_FALLBACK("GitLab", "icx gitlab --add")


# -- git_delete_tag (GIT-5) ----------------------------------------------------

async def test_git_delete_tag_confirmation_gated_and_executes(tmp_git_repo_with_remote):
    from icx_engine.git.mcp_tools import dispatch_git_tool
    from icx_engine.models.config import GitLabConnection
    conn = GitLabConnection(name="gitlab.example.com", url="https://gitlab.example.com", token="glpat-x")

    mock_client = AsyncMock()
    mock_client.get_tag.return_value = {"name": "zz-icx-verify-1", "commit": {"id": "abc123"}}

    with patch("icx_engine.git.mcp_tools.ConfigManager") as mock_cfg_cls:
        mock_cfg_cls.load.return_value.active_gitlab_connection.return_value = conn
        with patch("icx_engine.git.mcp_tools.project_path_from_remote_url", return_value="group/project"):
            with patch("icx_engine.git.mcp_tools.GitLabClient") as mock_client_cls:
                mock_client_cls.return_value.__aenter__.return_value = mock_client
                first = await dispatch_git_tool("git_delete_tag", {
                    "repo_path": str(tmp_git_repo_with_remote), "tag_name": "zz-icx-verify-1",
                })
                first_payload = json.loads(first[0].text)
                assert first_payload["status"] == "pending_confirmation"
                assert first_payload["tag_name"] == "zz-icx-verify-1"
                assert first_payload["target_commit"] == "abc123"

                token = first_payload["token"]
                second = await dispatch_git_tool("git_delete_tag", {
                    "repo_path": str(tmp_git_repo_with_remote), "tag_name": "zz-icx-verify-1",
                    "confirm_token": token,
                })
    payload = json.loads(second[0].text)
    assert payload["ok"] is True
    assert payload["deleted"] == "zz-icx-verify-1"
    mock_client.delete_tag.assert_awaited_once_with("group/project", "zz-icx-verify-1")


async def test_git_delete_tag_nonexistent_tag_fails_before_confirmation(tmp_git_repo_with_remote):
    from icx_engine.git.mcp_tools import dispatch_git_tool
    from icx_engine.gitlab.client import GitLabError
    from icx_engine.models.config import GitLabConnection
    conn = GitLabConnection(name="gitlab.example.com", url="https://gitlab.example.com", token="glpat-x")

    mock_client = AsyncMock()
    mock_client.get_tag.side_effect = GitLabError("Tag 'nope' not found (HTTP 404).", 404)

    with patch("icx_engine.git.mcp_tools.ConfigManager") as mock_cfg_cls:
        mock_cfg_cls.load.return_value.active_gitlab_connection.return_value = conn
        with patch("icx_engine.git.mcp_tools.project_path_from_remote_url", return_value="group/project"):
            with patch("icx_engine.git.mcp_tools.GitLabClient") as mock_client_cls:
                mock_client_cls.return_value.__aenter__.return_value = mock_client
                result = await dispatch_git_tool("git_delete_tag", {
                    "repo_path": str(tmp_git_repo_with_remote), "tag_name": "nope",
                })
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert "not found" in payload["error"]
    mock_client.delete_tag.assert_not_called()


async def test_git_delete_tag_missing_tag_name_returns_named_error():
    from icx_engine.git.mcp_tools import dispatch_git_tool
    result = await dispatch_git_tool("git_delete_tag", {"repo_path": "/fake/repo"})
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert "tag_name" in payload["error"]


# -- git_retag (GIT-6) ---------------------------------------------------------

async def test_git_retag_confirmation_gated_and_executes(tmp_git_repo_with_remote):
    from icx_engine.git.mcp_tools import dispatch_git_tool
    from icx_engine.models.config import GitLabConnection
    conn = GitLabConnection(name="gitlab.example.com", url="https://gitlab.example.com", token="glpat-x")

    mock_client = AsyncMock()
    mock_client.get_tag.return_value = {"name": "zz-icx-verify-1", "commit": {"id": "old111"}}
    mock_client.list_branches.return_value = [{"name": "main", "commit": {"id": "new222"}}]
    mock_client.get_repository_file.return_value = _SAMPLE_CI_YAML
    mock_client.create_tag.return_value = {"name": "zz-icx-verify-1"}

    with patch("icx_engine.git.mcp_tools.ConfigManager") as mock_cfg_cls:
        mock_cfg_cls.load.return_value.active_gitlab_connection.return_value = conn
        with patch("icx_engine.git.mcp_tools.project_path_from_remote_url", return_value="group/project"):
            with patch("icx_engine.git.mcp_tools.GitLabClient") as mock_client_cls:
                mock_client_cls.return_value.__aenter__.return_value = mock_client
                first = await dispatch_git_tool("git_retag", {
                    "repo_path": str(tmp_git_repo_with_remote), "tag_name": "zz-icx-verify-1", "branch": "main",
                })
                first_payload = json.loads(first[0].text)
                assert first_payload["status"] == "pending_confirmation"
                assert first_payload["previous_target"] == "old111"
                assert first_payload["new_target"] == "new222"
                assert first_payload["no_op"] is False

                token = first_payload["token"]
                second = await dispatch_git_tool("git_retag", {
                    "repo_path": str(tmp_git_repo_with_remote), "tag_name": "zz-icx-verify-1", "branch": "main",
                    "confirm_token": token,
                })
    payload = json.loads(second[0].text)
    assert payload["ok"] is True
    assert payload["tag"] == "zz-icx-verify-1"
    assert payload["previous_target"] == "old111"
    mock_client.delete_tag.assert_awaited_once_with("group/project", "zz-icx-verify-1")
    mock_client.create_tag.assert_awaited_once_with("group/project", "zz-icx-verify-1", "main")


async def test_git_retag_detects_no_op_when_branch_tip_unchanged(tmp_git_repo_with_remote):
    from icx_engine.git.mcp_tools import dispatch_git_tool
    from icx_engine.models.config import GitLabConnection
    conn = GitLabConnection(name="gitlab.example.com", url="https://gitlab.example.com", token="glpat-x")

    mock_client = AsyncMock()
    mock_client.get_tag.return_value = {"name": "zz-icx-verify-1", "commit": {"id": "same111"}}
    mock_client.list_branches.return_value = [{"name": "main", "commit": {"id": "same111"}}]
    mock_client.get_repository_file.return_value = _SAMPLE_CI_YAML

    with patch("icx_engine.git.mcp_tools.ConfigManager") as mock_cfg_cls:
        mock_cfg_cls.load.return_value.active_gitlab_connection.return_value = conn
        with patch("icx_engine.git.mcp_tools.project_path_from_remote_url", return_value="group/project"):
            with patch("icx_engine.git.mcp_tools.GitLabClient") as mock_client_cls:
                mock_client_cls.return_value.__aenter__.return_value = mock_client
                result = await dispatch_git_tool("git_retag", {
                    "repo_path": str(tmp_git_repo_with_remote), "tag_name": "zz-icx-verify-1", "branch": "main",
                })
    payload = json.loads(result[0].text)
    assert payload["no_op"] is True


async def test_git_retag_branch_not_found_fails_before_confirmation(tmp_git_repo_with_remote):
    from icx_engine.git.mcp_tools import dispatch_git_tool
    from icx_engine.models.config import GitLabConnection
    conn = GitLabConnection(name="gitlab.example.com", url="https://gitlab.example.com", token="glpat-x")

    mock_client = AsyncMock()
    mock_client.get_tag.return_value = {"name": "zz-icx-verify-1", "commit": {"id": "old111"}}
    mock_client.list_branches.return_value = []

    with patch("icx_engine.git.mcp_tools.ConfigManager") as mock_cfg_cls:
        mock_cfg_cls.load.return_value.active_gitlab_connection.return_value = conn
        with patch("icx_engine.git.mcp_tools.project_path_from_remote_url", return_value="group/project"):
            with patch("icx_engine.git.mcp_tools.GitLabClient") as mock_client_cls:
                mock_client_cls.return_value.__aenter__.return_value = mock_client
                result = await dispatch_git_tool("git_retag", {
                    "repo_path": str(tmp_git_repo_with_remote), "tag_name": "zz-icx-verify-1", "branch": "ghost",
                })
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert "not found" in payload["error"]


async def test_git_retag_reports_previous_target_when_recreate_fails_after_delete(tmp_git_repo_with_remote):
    """Partial-failure case: delete succeeds, recreate then fails - the tag no
    longer exists, so the error MUST carry the exact commit to recover it manually."""
    from icx_engine.git.mcp_tools import dispatch_git_tool
    from icx_engine.models.config import GitLabConnection
    conn = GitLabConnection(name="gitlab.example.com", url="https://gitlab.example.com", token="glpat-x")

    mock_client = AsyncMock()
    mock_client.get_tag.return_value = {"name": "zz-icx-verify-1", "commit": {"id": "old111"}}
    mock_client.list_branches.return_value = [{"name": "main", "commit": {"id": "new222"}}]
    mock_client.get_repository_file.return_value = _SAMPLE_CI_YAML
    mock_client.create_tag.side_effect = RuntimeError("network blip")

    with patch("icx_engine.git.mcp_tools.ConfigManager") as mock_cfg_cls:
        mock_cfg_cls.load.return_value.active_gitlab_connection.return_value = conn
        with patch("icx_engine.git.mcp_tools.project_path_from_remote_url", return_value="group/project"):
            with patch("icx_engine.git.mcp_tools.GitLabClient") as mock_client_cls:
                mock_client_cls.return_value.__aenter__.return_value = mock_client
                first = await dispatch_git_tool("git_retag", {
                    "repo_path": str(tmp_git_repo_with_remote), "tag_name": "zz-icx-verify-1", "branch": "main",
                })
                token = json.loads(first[0].text)["token"]
                second = await dispatch_git_tool("git_retag", {
                    "repo_path": str(tmp_git_repo_with_remote), "tag_name": "zz-icx-verify-1", "branch": "main",
                    "confirm_token": token,
                })
    payload = json.loads(second[0].text)
    assert payload["ok"] is False
    assert "old111" in payload["error"]
    mock_client.delete_tag.assert_awaited_once()


async def test_git_retag_missing_branch_returns_named_error():
    from icx_engine.git.mcp_tools import dispatch_git_tool
    result = await dispatch_git_tool("git_retag", {"repo_path": "/fake/repo", "tag_name": "x"})
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert "branch" in payload["error"]


# Task: git_stash_create/list/apply/pop/drop tools

async def test_git_stash_create_and_list_round_trip(tmp_git_repo):
    from icx_engine.git.mcp_tools import dispatch_git_tool
    (tmp_git_repo / "new.txt").write_text("x", encoding="utf-8")
    result = await dispatch_git_tool("git_stash_create", {
        "repo_path": str(tmp_git_repo), "message": "before sync",
    })
    payload = json.loads(result[0].text)
    assert payload["ok"] is True

    listed = await dispatch_git_tool("git_stash_list", {"repo_path": str(tmp_git_repo)})
    stashes = json.loads(listed[0].text)["stashes"]
    assert len(stashes) == 1
    assert stashes[0]["ref"] == "stash@{0}"
    assert stashes[0]["message"].endswith("before sync")


async def test_git_stash_create_missing_message_returns_named_error(tmp_git_repo):
    from icx_engine.git.mcp_tools import dispatch_git_tool
    result = await dispatch_git_tool("git_stash_create", {"repo_path": str(tmp_git_repo)})
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert "message" in payload["error"]


async def test_git_stash_apply_restores_without_removing(tmp_git_repo):
    from icx_engine.git.mcp_tools import dispatch_git_tool
    (tmp_git_repo / "new.txt").write_text("x", encoding="utf-8")
    await dispatch_git_tool("git_stash_create", {"repo_path": str(tmp_git_repo), "message": "keep"})

    result = await dispatch_git_tool("git_stash_apply", {"repo_path": str(tmp_git_repo)})
    payload = json.loads(result[0].text)
    assert payload["ok"] is True
    assert (tmp_git_repo / "new.txt").exists()

    listed = await dispatch_git_tool("git_stash_list", {"repo_path": str(tmp_git_repo)})
    assert len(json.loads(listed[0].text)["stashes"]) == 1


async def test_git_stash_pop_removes_after_restoring(tmp_git_repo):
    from icx_engine.git.mcp_tools import dispatch_git_tool
    (tmp_git_repo / "new.txt").write_text("x", encoding="utf-8")
    await dispatch_git_tool("git_stash_create", {"repo_path": str(tmp_git_repo), "message": "pop me"})

    result = await dispatch_git_tool("git_stash_pop", {"repo_path": str(tmp_git_repo)})
    payload = json.loads(result[0].text)
    assert payload["ok"] is True
    assert (tmp_git_repo / "new.txt").exists()

    listed = await dispatch_git_tool("git_stash_list", {"repo_path": str(tmp_git_repo)})
    assert json.loads(listed[0].text)["stashes"] == []


async def test_git_stash_drop_confirmation_gated_and_executes(tmp_git_repo):
    from icx_engine.git.mcp_tools import dispatch_git_tool
    (tmp_git_repo / "new.txt").write_text("x", encoding="utf-8")
    await dispatch_git_tool("git_stash_create", {"repo_path": str(tmp_git_repo), "message": "drop me"})

    first = await dispatch_git_tool("git_stash_drop", {"repo_path": str(tmp_git_repo)})
    first_payload = json.loads(first[0].text)
    assert first_payload["status"] == "pending_confirmation"
    assert first_payload["message"].endswith("drop me")
    token = first_payload["token"]

    second = await dispatch_git_tool("git_stash_drop", {
        "repo_path": str(tmp_git_repo), "confirm_token": token,
    })
    assert json.loads(second[0].text)["ok"] is True

    listed = await dispatch_git_tool("git_stash_list", {"repo_path": str(tmp_git_repo)})
    assert json.loads(listed[0].text)["stashes"] == []


async def test_git_stash_drop_without_confirmation_does_not_drop(tmp_git_repo):
    from icx_engine.git.mcp_tools import dispatch_git_tool
    (tmp_git_repo / "new.txt").write_text("x", encoding="utf-8")
    await dispatch_git_tool("git_stash_create", {"repo_path": str(tmp_git_repo), "message": "still here"})
    await dispatch_git_tool("git_stash_drop", {"repo_path": str(tmp_git_repo)})
    listed = await dispatch_git_tool("git_stash_list", {"repo_path": str(tmp_git_repo)})
    assert len(json.loads(listed[0].text)["stashes"]) == 1


async def test_git_stash_drop_unknown_ref_returns_named_error(tmp_git_repo):
    from icx_engine.git.mcp_tools import dispatch_git_tool
    result = await dispatch_git_tool("git_stash_drop", {
        "repo_path": str(tmp_git_repo), "ref": "stash@{5}",
    })
    payload = json.loads(result[0].text)
    assert payload["ok"] is False


async def test_git_stash_drop_invalid_token_returns_error(tmp_git_repo):
    from icx_engine.git.mcp_tools import dispatch_git_tool
    result = await dispatch_git_tool("git_stash_drop", {
        "repo_path": str(tmp_git_repo), "confirm_token": "bogus",
    })
    payload = json.loads(result[0].text)
    assert payload["ok"] is False


# Task: git_fetch/git_pull/git_sync tools

async def test_git_fetch_succeeds_against_real_remote(tmp_git_repo_with_remote):
    from icx_engine.git.mcp_tools import dispatch_git_tool
    result = await dispatch_git_tool("git_fetch", {"repo_path": str(tmp_git_repo_with_remote)})
    payload = json.loads(result[0].text)
    assert payload["ok"] is True
    assert payload["fetched"] is True


async def test_git_fetch_missing_repo_path_returns_named_error():
    from icx_engine.git.mcp_tools import dispatch_git_tool
    result = await dispatch_git_tool("git_fetch", {})
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert "repo_path" in payload["error"]


async def test_git_pull_ff_only_reports_up_to_date(tmp_git_repo_with_remote):
    from icx_engine.git.mcp_tools import dispatch_git_tool
    result = await dispatch_git_tool("git_pull", {"repo_path": str(tmp_git_repo_with_remote)})
    payload = json.loads(result[0].text)
    assert payload["ok"] is True
    assert payload["status"] == "up_to_date"


async def test_git_pull_invalid_strategy_returns_named_error(tmp_git_repo):
    from icx_engine.git.mcp_tools import dispatch_git_tool
    result = await dispatch_git_tool("git_pull", {
        "repo_path": str(tmp_git_repo), "strategy": "rebase",
    })
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert "strategy" in payload["error"]


async def test_git_pull_ticket_key_wrong_type_returns_named_error(tmp_git_repo):
    from icx_engine.git.mcp_tools import dispatch_git_tool
    result = await dispatch_git_tool("git_pull", {
        "repo_path": str(tmp_git_repo), "strategy": "merge", "ticket_key": 123,
    })
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert "ticket_key" in payload["error"]


async def test_git_sync_merges_diverged_own_remote_branch(tmp_git_repo_with_remote, tmp_path):
    from icx_engine.git.mcp_tools import dispatch_git_tool
    import subprocess
    bare_origin = tmp_path / "origin.git"
    other_clone = tmp_path / "sync_other_clone"
    subprocess.run(["git", "clone", str(bare_origin), str(other_clone)], check=True,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=str(other_clone), check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=str(other_clone), check=True)
    (other_clone / "remote_change.txt").write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "remote_change.txt"], cwd=str(other_clone), check=True)
    subprocess.run(["git", "commit", "-m", "remote change"], cwd=str(other_clone), check=True)
    subprocess.run(["git", "push"], cwd=str(other_clone), check=True)

    (tmp_git_repo_with_remote / "local_change.txt").write_text("y", encoding="utf-8")

    result = await dispatch_git_tool("git_sync", {
        "repo_path": str(tmp_git_repo_with_remote), "ticket_key": "ABC-1",
    })
    payload = json.loads(result[0].text)
    assert payload["ok"] is True
    assert payload["status"] == "merged"
    assert (tmp_git_repo_with_remote / "remote_change.txt").exists()
    assert (tmp_git_repo_with_remote / "local_change.txt").exists()  # stashed dirty tree restored


async def test_git_sync_missing_repo_path_returns_named_error():
    from icx_engine.git.mcp_tools import dispatch_git_tool
    result = await dispatch_git_tool("git_sync", {})
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert "repo_path" in payload["error"]


# Task: git_delete_branch tool

async def test_git_delete_branch_merged_branch_confirmation_gated_and_executes(tmp_git_repo):
    from icx_engine.git.mcp_tools import dispatch_git_tool
    from icx_engine.git.gitcmd import create_branch_from, local_branch_exists
    create_branch_from(tmp_git_repo, "feature/merged-ABC-1", "main")

    first = await dispatch_git_tool("git_delete_branch", {
        "repo_path": str(tmp_git_repo), "branch": "feature/merged-ABC-1", "target": "main",
    })
    first_payload = json.loads(first[0].text)
    assert first_payload["status"] == "pending_confirmation"
    assert first_payload["unique_commits"] == 0
    token = first_payload["token"]

    second = await dispatch_git_tool("git_delete_branch", {
        "repo_path": str(tmp_git_repo), "branch": "feature/merged-ABC-1", "target": "main",
        "confirm_token": token,
    })
    payload = json.loads(second[0].text)
    assert payload["ok"] is True
    assert payload["local_deleted"] is True
    assert local_branch_exists(tmp_git_repo, "feature/merged-ABC-1") is False


async def test_git_delete_branch_refuses_unmerged_branch_before_issuing_token(tmp_git_repo):
    from icx_engine.git.mcp_tools import dispatch_git_tool
    from icx_engine.git.gitcmd import create_branch_from, checkout, stage_files, commit, local_branch_exists
    create_branch_from(tmp_git_repo, "feature/unmerged-ABC-1", "main")
    checkout(tmp_git_repo, "feature/unmerged-ABC-1")
    (tmp_git_repo / "only_here.txt").write_text("x", encoding="utf-8")
    stage_files(tmp_git_repo, ["only_here.txt"])
    commit(tmp_git_repo, "ABC-1 unique commit")
    checkout(tmp_git_repo, "main")

    result = await dispatch_git_tool("git_delete_branch", {
        "repo_path": str(tmp_git_repo), "branch": "feature/unmerged-ABC-1", "target": "main",
    })
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert "cannot be safely deleted" in payload["error"]
    assert "token" not in payload  # never issued - refused outright
    assert local_branch_exists(tmp_git_repo, "feature/unmerged-ABC-1") is True


async def test_git_delete_branch_force_true_issues_token_for_unmerged_branch(tmp_git_repo):
    from icx_engine.git.mcp_tools import dispatch_git_tool
    from icx_engine.git.gitcmd import create_branch_from, checkout, stage_files, commit
    create_branch_from(tmp_git_repo, "feature/unmerged-ABC-1", "main")
    checkout(tmp_git_repo, "feature/unmerged-ABC-1")
    (tmp_git_repo / "only_here.txt").write_text("x", encoding="utf-8")
    stage_files(tmp_git_repo, ["only_here.txt"])
    commit(tmp_git_repo, "ABC-1 unique commit")
    checkout(tmp_git_repo, "main")

    first = await dispatch_git_tool("git_delete_branch", {
        "repo_path": str(tmp_git_repo), "branch": "feature/unmerged-ABC-1", "target": "main", "force": True,
    })
    first_payload = json.loads(first[0].text)
    assert first_payload["status"] == "pending_confirmation"
    assert first_payload["unique_commits"] == 1
    token = first_payload["token"]

    second = await dispatch_git_tool("git_delete_branch", {
        "repo_path": str(tmp_git_repo), "branch": "feature/unmerged-ABC-1", "target": "main",
        "confirm_token": token,
    })
    assert json.loads(second[0].text)["ok"] is True


async def test_git_delete_branch_refuses_current_branch(tmp_git_repo):
    from icx_engine.git.mcp_tools import dispatch_git_tool
    from icx_engine.git.gitcmd import create_branch_from, checkout
    create_branch_from(tmp_git_repo, "feature/current-ABC-1", "main")
    checkout(tmp_git_repo, "feature/current-ABC-1")

    result = await dispatch_git_tool("git_delete_branch", {
        "repo_path": str(tmp_git_repo), "branch": "feature/current-ABC-1", "target": "main", "force": True,
    })
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert "currently checked-out" in payload["error"]


async def test_git_delete_branch_missing_target_returns_named_error(tmp_git_repo):
    from icx_engine.git.mcp_tools import dispatch_git_tool
    result = await dispatch_git_tool("git_delete_branch", {
        "repo_path": str(tmp_git_repo), "branch": "feature/x-ABC-1",
    })
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert "target" in payload["error"]


async def test_git_delete_branch_invalid_token_returns_error(tmp_git_repo):
    from icx_engine.git.mcp_tools import dispatch_git_tool
    result = await dispatch_git_tool("git_delete_branch", {
        "repo_path": str(tmp_git_repo), "confirm_token": "bogus",
    })
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
