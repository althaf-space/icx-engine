from __future__ import annotations
import json
import pytest
from unittest.mock import AsyncMock, patch

from icx_engine.mcp_server import _ICX_FALLBACK


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
    mock_result = type("R", (), {"mr_iid": 5, "created": True, "merged": True, "refusal_reason": None})()
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

    with patch("icx_engine.git.mcp_tools.ConfigManager") as mock_cfg_cls:
        mock_cfg_cls.load.return_value.active_gitlab_connection.return_value = conn
        with patch("icx_engine.git.mcp_tools.project_path_from_remote_url", return_value="group/project"):
            with patch("icx_engine.git.mcp_tools.GitLabClient") as mock_client_cls:
                mock_client_cls.return_value.__aenter__.return_value = mock_client
                first = await dispatch_git_tool("git_create_tag", {
                    "repo_path": str(tmp_git_repo_with_remote), "environment": "qa", "branch": "main",
                    "tag_name_override": "v9.9.9-qa-custom",
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
