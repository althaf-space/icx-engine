from __future__ import annotations
import click
import typer.testing
from unittest.mock import AsyncMock, patch


def test_git_status_reports_clean_repo(tmp_git_repo, monkeypatch):
    from icx_engine.git.cli_commands import git_app
    monkeypatch.setattr("icx_engine.git.cli_commands._resolve_cwd", lambda: tmp_git_repo)
    runner = typer.testing.CliRunner()
    result = runner.invoke(git_app, ["status"])
    assert result.exit_code == 0
    assert "main" in result.stdout
    assert "clean" in result.stdout.lower()


def test_git_status_reports_dirty_files(tmp_git_repo, monkeypatch):
    (tmp_git_repo / "x.txt").write_text("x", encoding="utf-8")
    from icx_engine.git.cli_commands import git_app
    monkeypatch.setattr("icx_engine.git.cli_commands._resolve_cwd", lambda: tmp_git_repo)
    runner = typer.testing.CliRunner()
    result = runner.invoke(git_app, ["status"])
    assert result.exit_code == 0
    assert "x.txt" in result.stdout


def test_git_status_errors_outside_repo(tmp_path, monkeypatch):
    from icx_engine.git.cli_commands import git_app
    monkeypatch.setattr("icx_engine.git.cli_commands._resolve_cwd", lambda: tmp_path)
    runner = typer.testing.CliRunner()
    result = runner.invoke(git_app, ["status"])
    assert result.exit_code != 0


def test_git_status_handles_post_validate_failure_cleanly(tmp_git_repo, monkeypatch):
    from icx_engine.git.cli_commands import git_app
    from icx_engine.git.gitcmd import GitCommandError
    monkeypatch.setattr("icx_engine.git.cli_commands._resolve_cwd", lambda: tmp_git_repo)

    def _boom(*args, **kwargs):
        raise GitCommandError("simulated git failure")
    monkeypatch.setattr("icx_engine.git.manager.GitLifecycleManager.check_dirty_tree", _boom)

    runner = typer.testing.CliRunner()
    result = runner.invoke(git_app, ["status"])
    assert result.exit_code != 0
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_git_branch_creates_and_checks_out_new_branch(tmp_git_repo_with_remote, tmp_path, monkeypatch):
    from icx_engine.git.cli_commands import git_app
    from icx_engine.git.gitcmd import current_branch
    monkeypatch.setattr("icx_engine.git.cli_commands._resolve_cwd", lambda: tmp_git_repo_with_remote)
    monkeypatch.setattr("icx_engine.git.settings._git_settings_root", lambda: tmp_path / ".icx-test-home")

    runner = typer.testing.CliRunner()
    result = runner.invoke(git_app, [
        "branch", "--ticket", "ABC-1", "--name", "Fix login timeout", "--parent", "main",
    ])
    assert result.exit_code == 0
    assert "feature/fix-login-timeout-ABC-1" in result.stdout
    assert current_branch(tmp_git_repo_with_remote) == "feature/fix-login-timeout-ABC-1"


def test_git_branch_switches_to_existing_branch(tmp_git_repo_with_remote, tmp_path, monkeypatch):
    from icx_engine.git.cli_commands import git_app
    from icx_engine.git.gitcmd import current_branch, checkout
    monkeypatch.setattr("icx_engine.git.cli_commands._resolve_cwd", lambda: tmp_git_repo_with_remote)
    monkeypatch.setattr("icx_engine.git.settings._git_settings_root", lambda: tmp_path / ".icx-test-home")

    runner = typer.testing.CliRunner()
    first = runner.invoke(git_app, [
        "branch", "--ticket", "ABC-1", "--name", "Fix login timeout", "--parent", "main",
    ])
    assert first.exit_code == 0
    checkout(tmp_git_repo_with_remote, "main")

    second = runner.invoke(git_app, [
        "branch", "--ticket", "ABC-1", "--name", "Fix login timeout", "--parent", "main",
    ])
    assert second.exit_code == 0
    assert "already exists" in second.stdout.lower()
    assert current_branch(tmp_git_repo_with_remote) == "feature/fix-login-timeout-ABC-1"


def test_git_sync_reports_clean_merge(tmp_git_repo_with_remote, tmp_path, monkeypatch):
    from icx_engine.git.cli_commands import git_app
    from icx_engine.git.gitcmd import create_branch_from, checkout, stage_files, commit
    create_branch_from(tmp_git_repo_with_remote, "feature/x-ABC-1", "main")
    checkout(tmp_git_repo_with_remote, "feature/x-ABC-1")
    (tmp_git_repo_with_remote / "feature_only.txt").write_text("f", encoding="utf-8")
    stage_files(tmp_git_repo_with_remote, ["feature_only.txt"])
    commit(tmp_git_repo_with_remote, "ABC-1 feature change")

    monkeypatch.setattr("icx_engine.git.cli_commands._resolve_cwd", lambda: tmp_git_repo_with_remote)
    monkeypatch.setattr("icx_engine.git.settings._git_settings_root", lambda: tmp_path / ".icx-test-home")
    runner = typer.testing.CliRunner()
    result = runner.invoke(git_app, ["sync", "--parent", "main", "--ticket", "ABC-1"])
    assert result.exit_code == 0
    assert "clean" in result.stdout.lower()


def test_git_push_prompts_then_puts_current_branch_on_remote(tmp_git_repo_with_remote, monkeypatch):
    from icx_engine.git.cli_commands import git_app
    from icx_engine.git.gitcmd import create_branch_from, checkout, remote_branch_exists

    create_branch_from(tmp_git_repo_with_remote, "feature/push-me", "main")
    checkout(tmp_git_repo_with_remote, "feature/push-me")
    assert remote_branch_exists(tmp_git_repo_with_remote, "feature/push-me") is False

    monkeypatch.setattr("icx_engine.git.cli_commands._resolve_cwd", lambda: tmp_git_repo_with_remote)
    monkeypatch.setattr("typer.confirm", lambda *a, **k: True)
    runner = typer.testing.CliRunner()
    result = runner.invoke(git_app, ["push"])
    assert result.exit_code == 0
    assert "feature/push-me" in result.stdout
    assert remote_branch_exists(tmp_git_repo_with_remote, "feature/push-me") is True


def test_git_push_cancelled_does_not_push(tmp_git_repo_with_remote, monkeypatch):
    from icx_engine.git.cli_commands import git_app
    from icx_engine.git.gitcmd import create_branch_from, checkout, remote_branch_exists

    create_branch_from(tmp_git_repo_with_remote, "feature/push-me", "main")
    checkout(tmp_git_repo_with_remote, "feature/push-me")

    monkeypatch.setattr("icx_engine.git.cli_commands._resolve_cwd", lambda: tmp_git_repo_with_remote)
    monkeypatch.setattr("typer.confirm", lambda *a, **k: False)
    runner = typer.testing.CliRunner()
    result = runner.invoke(git_app, ["push"])
    assert result.exit_code == 0
    assert "cancelled" in result.stdout.lower()
    assert remote_branch_exists(tmp_git_repo_with_remote, "feature/push-me") is False


def test_git_push_cli_injects_gitlab_auth_env_for_matching_https_origin(tmp_git_repo_with_remote, monkeypatch):
    from icx_engine.git.cli_commands import git_app
    from icx_engine.git.gitcmd import create_branch_from, checkout
    from icx_engine.models.config import GitLabConnection
    from unittest.mock import Mock

    create_branch_from(tmp_git_repo_with_remote, "feature/push-auth-cli", "main")
    checkout(tmp_git_repo_with_remote, "feature/push-auth-cli")

    conn = GitLabConnection(name="gitlab.example.com", url="https://gitlab.example.com", token="glpat-x")
    mock_push = Mock()
    monkeypatch.setattr("icx_engine.git.cli_commands._resolve_cwd", lambda: tmp_git_repo_with_remote)
    monkeypatch.setattr("typer.confirm", lambda *a, **k: True)
    monkeypatch.setattr("icx_engine.git.manager.remote_url", lambda repo, remote="origin": "https://gitlab.example.com/group/project.git")
    monkeypatch.setattr("icx_engine.git.gitcmd.push", mock_push)

    with patch("icx_engine.config_manager.ConfigManager") as mock_cfg_cls:
        mock_cfg_cls.load.return_value.active_gitlab_connection.return_value = conn
        runner = typer.testing.CliRunner()
        result = runner.invoke(git_app, ["push"])

    assert result.exit_code == 0
    mock_push.assert_called_once()
    auth_env = mock_push.call_args.kwargs["extra_env"]
    assert auth_env is not None
    assert auth_env["GIT_CONFIG_KEY_0"] == "http.extraheader"


def test_git_sync_reports_conflict_and_scratch_branch(tmp_git_repo_with_remote, tmp_path, monkeypatch):
    from icx_engine.git.cli_commands import git_app
    from icx_engine.git.gitcmd import create_branch_from, checkout, stage_files, commit
    import subprocess
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

    monkeypatch.setattr("icx_engine.git.cli_commands._resolve_cwd", lambda: tmp_git_repo_with_remote)
    monkeypatch.setattr("icx_engine.git.settings._git_settings_root", lambda: tmp_path / ".icx-test-home")
    runner = typer.testing.CliRunner()
    result = runner.invoke(git_app, ["sync", "--parent", "main", "--ticket", "ABC-1"])
    assert result.exit_code == 0
    assert "conflict" in result.stdout.lower()
    assert "scratch/ABC-1-" in result.stdout
    assert "README.md" in result.stdout


def test_git_mr_creates_and_reports_merge(tmp_git_repo_with_remote, tmp_path, monkeypatch):
    from icx_engine.git.cli_commands import git_app
    from icx_engine.git.gitcmd import create_branch_from, checkout, stage_files, commit
    create_branch_from(tmp_git_repo_with_remote, "feature/x-ABC-1", "main")
    checkout(tmp_git_repo_with_remote, "feature/x-ABC-1")
    (tmp_git_repo_with_remote / "a.txt").write_text("a", encoding="utf-8")
    stage_files(tmp_git_repo_with_remote, ["a.txt"])
    commit(tmp_git_repo_with_remote, "ABC-1 add a.txt")

    monkeypatch.setattr("icx_engine.git.cli_commands._resolve_cwd", lambda: tmp_git_repo_with_remote)
    monkeypatch.setattr("icx_engine.git.settings._git_settings_root", lambda: tmp_path / ".icx-test-home")
    monkeypatch.setattr("typer.confirm", lambda *a, **k: True)
    mock_result = type("R", (), {"mr_iid": 5, "created": True, "merged": True, "refusal_reason": None})()
    from icx_engine.models.config import GitLabConnection
    conn = GitLabConnection(name="gitlab.example.com", url="https://gitlab.example.com", token="glpat-x")

    with patch("icx_engine.git.cli_commands.ConfigManager") as mock_cfg_cls:
        mock_cfg_cls.load.return_value.active_gitlab_connection.return_value = conn
        with patch("icx_engine.git.manager.GitLifecycleManager.create_mr_for_ticket", new=AsyncMock(return_value=mock_result)):
            runner = typer.testing.CliRunner()
            result = runner.invoke(git_app, ["mr", "--parent", "main", "--ticket", "ABC-1", "--summary", "Fix login"])
    assert result.exit_code == 0
    assert "merged" in result.stdout.lower()


def test_git_mr_errors_when_no_gitlab_connection(tmp_git_repo_with_remote, tmp_path, monkeypatch):
    from icx_engine.git.cli_commands import git_app
    monkeypatch.setattr("icx_engine.git.cli_commands._resolve_cwd", lambda: tmp_git_repo_with_remote)
    monkeypatch.setattr("icx_engine.git.settings._git_settings_root", lambda: tmp_path / ".icx-test-home")
    with patch("icx_engine.git.cli_commands.ConfigManager") as mock_cfg_cls:
        mock_cfg_cls.load.return_value.active_gitlab_connection.return_value = None
        runner = typer.testing.CliRunner()
        result = runner.invoke(git_app, ["mr", "--parent", "main", "--ticket", "ABC-1", "--summary", "Fix login"])
    assert result.exit_code != 0


def test_git_finish_reports_cleanup(tmp_git_repo_with_remote, tmp_path, monkeypatch):
    from icx_engine.git.cli_commands import git_app
    monkeypatch.setattr("icx_engine.git.cli_commands._resolve_cwd", lambda: tmp_git_repo_with_remote)
    monkeypatch.setattr("icx_engine.git.settings._git_settings_root", lambda: tmp_path / ".icx-test-home")
    monkeypatch.setattr("typer.confirm", lambda *a, **k: True)
    mock_result = type("R", (), {
        "parent_branch": "main", "feature_branch_deleted": True, "remote_branch_deleted": True,
        "backup_latest_deleted": True, "backups_deleted": [],
    })()
    from icx_engine.models.config import GitLabConnection
    conn = GitLabConnection(name="gitlab.example.com", url="https://gitlab.example.com", token="glpat-x")

    with patch("icx_engine.git.cli_commands.ConfigManager") as mock_cfg_cls:
        mock_cfg_cls.load.return_value.active_gitlab_connection.return_value = conn
        with patch("icx_engine.git.manager.GitLifecycleManager.post_merge_cleanup", new=AsyncMock(return_value=mock_result)):
            runner = typer.testing.CliRunner()
            result = runner.invoke(git_app, [
                "finish", "--parent", "main", "--feature", "feature/x-ABC-1",
                "--ticket", "ABC-1", "--mr-iid", "5",
            ])
    assert result.exit_code == 0
    assert "true" in result.stdout.lower()


def test_git_finish_errors_when_no_gitlab_connection(tmp_git_repo_with_remote, tmp_path, monkeypatch):
    from icx_engine.git.cli_commands import git_app
    monkeypatch.setattr("icx_engine.git.cli_commands._resolve_cwd", lambda: tmp_git_repo_with_remote)
    monkeypatch.setattr("icx_engine.git.settings._git_settings_root", lambda: tmp_path / ".icx-test-home")
    with patch("icx_engine.git.cli_commands.ConfigManager") as mock_cfg_cls:
        mock_cfg_cls.load.return_value.active_gitlab_connection.return_value = None
        runner = typer.testing.CliRunner()
        result = runner.invoke(git_app, [
            "finish", "--parent", "main", "--feature", "feature/x-ABC-1",
            "--ticket", "ABC-1", "--mr-iid", "5",
        ])
    assert result.exit_code != 0


def test_git_sync_confirms_proposed_default_when_none_saved(tmp_git_repo_with_remote, tmp_path, monkeypatch):
    from icx_engine.git.cli_commands import git_app
    from icx_engine.git.gitcmd import create_branch_from, checkout
    create_branch_from(tmp_git_repo_with_remote, "development", "main")
    monkeypatch.setattr("icx_engine.git.cli_commands._resolve_cwd", lambda: tmp_git_repo_with_remote)
    monkeypatch.setattr("icx_engine.git.settings._git_settings_root", lambda: tmp_path / ".icx-test-home")
    import subprocess
    subprocess.run(["git", "push", "origin", "development"], cwd=str(tmp_git_repo_with_remote), check=True)
    monkeypatch.setattr("typer.confirm", lambda *a, **k: True)

    runner = typer.testing.CliRunner()
    result = runner.invoke(git_app, ["sync", "--ticket", "ABC-1"])
    assert result.exit_code == 0
    from icx_engine.git.settings import read_repo_settings
    assert read_repo_settings(tmp_git_repo_with_remote).get("parent_branch") == "development"


def test_git_sync_confirms_saved_parent_every_call_and_accepts_default(tmp_git_repo_with_remote, tmp_path, monkeypatch):
    # A saved parent branch is never reused silently - it is offered as a
    # fast one-tap default, and accepting it proceeds with that value.
    from icx_engine.git.cli_commands import git_app
    from icx_engine.git.settings import write_repo_settings
    monkeypatch.setattr("icx_engine.git.cli_commands._resolve_cwd", lambda: tmp_git_repo_with_remote)
    monkeypatch.setattr("icx_engine.git.settings._git_settings_root", lambda: tmp_path / ".icx-test-home")
    write_repo_settings(tmp_git_repo_with_remote, parent_branch="main")

    confirm_calls = []

    def _confirm(prompt, *a, **k):
        confirm_calls.append(prompt)
        return True
    monkeypatch.setattr("typer.confirm", _confirm)

    def _fail_if_prompted(*a, **k):
        raise AssertionError("Should not fall back to typer.prompt - the default was accepted.")
    monkeypatch.setattr("typer.prompt", _fail_if_prompted)

    runner = typer.testing.CliRunner()
    result = runner.invoke(git_app, ["sync", "--ticket", "ABC-1"])
    assert result.exit_code == 0
    assert confirm_calls
    assert "main" in confirm_calls[0]


def test_git_sync_rejects_saved_parent_default_and_prompts_for_new_one(tmp_git_repo_with_remote, tmp_path, monkeypatch):
    from icx_engine.git.cli_commands import git_app
    from icx_engine.git.settings import write_repo_settings, read_repo_settings
    monkeypatch.setattr("icx_engine.git.cli_commands._resolve_cwd", lambda: tmp_git_repo_with_remote)
    monkeypatch.setattr("icx_engine.git.settings._git_settings_root", lambda: tmp_path / ".icx-test-home")
    write_repo_settings(tmp_git_repo_with_remote, parent_branch="development")

    from icx_engine.git.gitcmd import create_branch_from
    import subprocess
    create_branch_from(tmp_git_repo_with_remote, "development", "main")
    subprocess.run(["git", "push", "origin", "development"], cwd=str(tmp_git_repo_with_remote), check=True)

    monkeypatch.setattr("typer.confirm", lambda *a, **k: False)
    monkeypatch.setattr("typer.prompt", lambda *a, **k: "main")

    runner = typer.testing.CliRunner()
    result = runner.invoke(git_app, ["sync", "--ticket", "ABC-1"])
    assert result.exit_code == 0
    assert read_repo_settings(tmp_git_repo_with_remote).get("parent_branch") == "main"


def test_git_sync_explicit_parent_flag_overrides_and_persists(tmp_git_repo_with_remote, tmp_path, monkeypatch):
    from icx_engine.git.cli_commands import git_app
    from icx_engine.git.settings import write_repo_settings, read_repo_settings
    monkeypatch.setattr("icx_engine.git.cli_commands._resolve_cwd", lambda: tmp_git_repo_with_remote)
    monkeypatch.setattr("icx_engine.git.settings._git_settings_root", lambda: tmp_path / ".icx-test-home")
    write_repo_settings(tmp_git_repo_with_remote, parent_branch="development")

    runner = typer.testing.CliRunner()
    result = runner.invoke(git_app, ["sync", "--ticket", "ABC-1", "--parent", "main"])
    assert result.exit_code == 0
    assert read_repo_settings(tmp_git_repo_with_remote).get("parent_branch") == "main"


def test_resolve_parent_or_ask_confirms_saved_value_and_returns_it(tmp_git_repo_with_remote, tmp_path, monkeypatch):
    from icx_engine.git.cli_commands import _resolve_parent_or_ask
    from icx_engine.git.manager import GitLifecycleManager
    from icx_engine.git.settings import write_repo_settings
    monkeypatch.setattr("icx_engine.git.settings._git_settings_root", lambda: tmp_path / ".icx-test-home")
    write_repo_settings(tmp_git_repo_with_remote, parent_branch="main")
    mgr = GitLifecycleManager(tmp_git_repo_with_remote)
    mgr.validate()

    monkeypatch.setattr("typer.confirm", lambda *a, **k: True)

    def _fail_if_prompted(*a, **k):
        raise AssertionError("Should not fall back to typer.prompt - the default was accepted.")
    monkeypatch.setattr("typer.prompt", _fail_if_prompted)

    assert _resolve_parent_or_ask(mgr, None) == "main"


def test_resolve_parent_or_ask_rejects_default_and_confirms_new_value(tmp_git_repo_with_remote, tmp_path, monkeypatch):
    from icx_engine.git.cli_commands import _resolve_parent_or_ask
    from icx_engine.git.manager import GitLifecycleManager
    from icx_engine.git.settings import write_repo_settings
    from icx_engine.git.gitcmd import create_branch_from
    import subprocess
    monkeypatch.setattr("icx_engine.git.settings._git_settings_root", lambda: tmp_path / ".icx-test-home")
    create_branch_from(tmp_git_repo_with_remote, "development", "main")
    subprocess.run(["git", "push", "origin", "development"], cwd=str(tmp_git_repo_with_remote), check=True)
    write_repo_settings(tmp_git_repo_with_remote, parent_branch="development")
    mgr = GitLifecycleManager(tmp_git_repo_with_remote)
    mgr.validate()

    monkeypatch.setattr("typer.confirm", lambda *a, **k: False)
    monkeypatch.setattr("typer.prompt", lambda *a, **k: "main")

    with patch("icx_engine.git.manager.GitLifecycleManager.confirm_parent_branch") as mock_confirm:
        result = _resolve_parent_or_ask(mgr, None)
    mock_confirm.assert_called_once_with("main")
    assert result == "main"


def test_git_sync_explicit_parent_flag_rejects_nonexistent_branch(tmp_git_repo_with_remote, tmp_path, monkeypatch):
    from icx_engine.git.cli_commands import git_app
    monkeypatch.setattr("icx_engine.git.cli_commands._resolve_cwd", lambda: tmp_git_repo_with_remote)
    monkeypatch.setattr("icx_engine.git.settings._git_settings_root", lambda: tmp_path / ".icx-test-home")

    runner = typer.testing.CliRunner()
    result = runner.invoke(git_app, ["sync", "--ticket", "ABC-1", "--parent", "does-not-exist"])
    assert result.exit_code != 0


from unittest.mock import AsyncMock, patch as mock_patch


def test_git_tag_shows_proposal_and_creates_on_approval(tmp_git_repo_with_remote, monkeypatch):
    from icx_engine.git.cli_commands import git_app
    from icx_engine.models.config import GitLabConnection
    conn = GitLabConnection(name="gitlab.example.com", url="https://gitlab.example.com", token="glpat-x")

    monkeypatch.setattr("icx_engine.git.cli_commands._resolve_cwd", lambda: tmp_git_repo_with_remote)
    monkeypatch.setattr("typer.confirm", lambda *a, **k: True)
    monkeypatch.setattr("typer.prompt", lambda *a, **k: "main")

    mock_client = AsyncMock()
    mock_client.list_tags.return_value = [{"name": "v0.0.184-qa-20260727002"}]
    mock_client.create_tag.return_value = {"name": "v0.0.185-qa-20260727003"}

    with mock_patch("icx_engine.git.cli_commands.ConfigManager") as mock_cfg_cls:
        mock_cfg_cls.load.return_value.active_gitlab_connection.return_value = conn
        with mock_patch("icx_engine.git.cli_commands.project_path_from_remote_url", return_value="group/project"):
            with mock_patch("icx_engine.git.cli_commands.GitLabClient") as mock_client_cls:
                mock_client_cls.return_value.__aenter__.return_value = mock_client
                runner = typer.testing.CliRunner()
                result = runner.invoke(git_app, ["tag", "--env", "qa"])

    assert result.exit_code == 0
    assert "v0.0.185-qa-20260727003" in result.stdout
    mock_client.create_tag.assert_called_once()


# ---------------------------------------------------------------------------
# Task 5: @_guarded / DebugOpt / TracebackOpt retrofit
#
# For each of the 5 commands: (a) --debug/--traceback must be recognized
# options (checked via --help, matching tests/test_cli_errors.py's existing
# pattern for the same flags on `icx analyze`), and (b) a forced exception
# must render through render_icx_error's real Panel shape - "What:"/"Why:"/
# "How:" body text (see src/icx_engine/error_display.py:77-81), not a raw
# traceback and not the old hand-rolled "[red]{exc}[/red]" line.
# ---------------------------------------------------------------------------

def _assert_flags_recognized(result) -> None:
    out = click.unstyle(result.output)
    assert "no such option" not in out.lower()
    assert "--debug" in out
    assert "--traceback" in out


def _assert_renders_via_render_icx_error(result, expected_message: str) -> None:
    assert result.exit_code == 1
    out = click.unstyle(result.output)
    assert expected_message in out
    assert "What:" in out
    assert "Why:" in out
    assert "How:" in out
    assert "Traceback (most recent call last)" not in out
    assert f"[red]{expected_message}[/red]" not in result.output


def test_git_status_debug_and_traceback_flags_recognized():
    from icx_engine.git.cli_commands import git_app
    runner = typer.testing.CliRunner()
    result = runner.invoke(git_app, ["status", "--help"])
    _assert_flags_recognized(result)


def test_git_status_exception_renders_through_render_icx_error(tmp_git_repo, monkeypatch):
    from icx_engine.git.cli_commands import git_app
    from icx_engine.git.manager import GitWorkflowError
    monkeypatch.setattr("icx_engine.git.cli_commands._resolve_cwd", lambda: tmp_git_repo)

    def _boom(self):
        raise GitWorkflowError("forced status boom")
    monkeypatch.setattr("icx_engine.git.manager.GitLifecycleManager.validate", _boom)

    runner = typer.testing.CliRunner()
    result = runner.invoke(git_app, ["status"])
    _assert_renders_via_render_icx_error(result, "forced status boom")


def test_git_branch_debug_and_traceback_flags_recognized():
    from icx_engine.git.cli_commands import git_app
    runner = typer.testing.CliRunner()
    result = runner.invoke(git_app, ["branch", "--help"])
    _assert_flags_recognized(result)


def test_git_branch_exception_renders_through_render_icx_error(tmp_git_repo, monkeypatch):
    from icx_engine.git.cli_commands import git_app
    from icx_engine.git.manager import GitWorkflowError
    monkeypatch.setattr("icx_engine.git.cli_commands._resolve_cwd", lambda: tmp_git_repo)

    def _boom(self):
        raise GitWorkflowError("forced branch boom")
    monkeypatch.setattr("icx_engine.git.manager.GitLifecycleManager.validate", _boom)

    runner = typer.testing.CliRunner()
    result = runner.invoke(git_app, ["branch", "--name", "Fix login timeout"])
    _assert_renders_via_render_icx_error(result, "forced branch boom")


def test_git_sync_debug_and_traceback_flags_recognized():
    from icx_engine.git.cli_commands import git_app
    runner = typer.testing.CliRunner()
    result = runner.invoke(git_app, ["sync", "--help"])
    _assert_flags_recognized(result)


def test_git_sync_exception_renders_through_render_icx_error(tmp_git_repo, monkeypatch):
    from icx_engine.git.cli_commands import git_app
    from icx_engine.git.manager import GitWorkflowError
    monkeypatch.setattr("icx_engine.git.cli_commands._resolve_cwd", lambda: tmp_git_repo)

    def _boom(self):
        raise GitWorkflowError("forced sync boom")
    monkeypatch.setattr("icx_engine.git.manager.GitLifecycleManager.validate", _boom)

    runner = typer.testing.CliRunner()
    result = runner.invoke(git_app, ["sync", "--ticket", "ABC-1"])
    _assert_renders_via_render_icx_error(result, "forced sync boom")


def test_git_mr_debug_and_traceback_flags_recognized():
    from icx_engine.git.cli_commands import git_app
    runner = typer.testing.CliRunner()
    result = runner.invoke(git_app, ["mr", "--help"])
    _assert_flags_recognized(result)


def test_git_mr_exception_renders_through_render_icx_error(tmp_git_repo, monkeypatch):
    from icx_engine.git.cli_commands import git_app
    from icx_engine.git.manager import GitWorkflowError
    monkeypatch.setattr("icx_engine.git.cli_commands._resolve_cwd", lambda: tmp_git_repo)

    def _boom(self):
        raise GitWorkflowError("forced mr boom")
    monkeypatch.setattr("icx_engine.git.manager.GitLifecycleManager.validate", _boom)

    runner = typer.testing.CliRunner()
    result = runner.invoke(git_app, ["mr", "--ticket", "ABC-1", "--summary", "Fix login"])
    _assert_renders_via_render_icx_error(result, "forced mr boom")


def test_git_finish_debug_and_traceback_flags_recognized():
    from icx_engine.git.cli_commands import git_app
    runner = typer.testing.CliRunner()
    result = runner.invoke(git_app, ["finish", "--help"])
    _assert_flags_recognized(result)


def test_git_finish_exception_renders_through_render_icx_error(tmp_git_repo, monkeypatch):
    from icx_engine.git.cli_commands import git_app
    from icx_engine.git.manager import GitWorkflowError
    monkeypatch.setattr("icx_engine.git.cli_commands._resolve_cwd", lambda: tmp_git_repo)

    def _boom(self):
        raise GitWorkflowError("forced finish boom")
    monkeypatch.setattr("icx_engine.git.manager.GitLifecycleManager.validate", _boom)

    runner = typer.testing.CliRunner()
    result = runner.invoke(git_app, [
        "finish", "--feature", "feature/x-ABC-1", "--ticket", "ABC-1", "--mr-iid", "5",
    ])
    _assert_renders_via_render_icx_error(result, "forced finish boom")


def test_git_tag_debug_and_traceback_flags_recognized():
    from icx_engine.git.cli_commands import git_app
    runner = typer.testing.CliRunner()
    result = runner.invoke(git_app, ["tag", "--help"])
    _assert_flags_recognized(result)


def test_git_tag_exception_renders_through_render_icx_error(tmp_git_repo, monkeypatch):
    from icx_engine.git.cli_commands import git_app
    from icx_engine.git.manager import GitWorkflowError
    monkeypatch.setattr("icx_engine.git.cli_commands._resolve_cwd", lambda: tmp_git_repo)

    def _boom(self):
        raise GitWorkflowError("forced tag boom")
    monkeypatch.setattr("icx_engine.git.manager.GitLifecycleManager.validate", _boom)

    runner = typer.testing.CliRunner()
    result = runner.invoke(git_app, ["tag"])
    _assert_renders_via_render_icx_error(result, "forced tag boom")
