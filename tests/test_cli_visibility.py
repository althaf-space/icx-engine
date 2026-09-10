"""cli_visibility.AGENT_ONLY_CLI_HIDDEN - a code-level flag hiding the operational/feature-work
CLI surface from --help while keeping every hidden command fully runnable. See developer.md
Section 8's "CLI help visibility" note."""
from typer.testing import CliRunner

from icx_engine.cli import _FULL_HELP, app
from icx_engine.cli_visibility import AGENT_ONLY_CLI_HIDDEN

runner = CliRunner()


def test_agent_only_cli_hidden_flag_is_on_by_default():
    assert AGENT_ONLY_CLI_HIDDEN is True


def test_full_help_omits_hidden_commands():
    hidden_samples = [
        "icx git branch", "icx git status", "icx boost brief", "icx boost benchmark",
        "icx logs report", "icx jira create", "icx jira delete", "icx jira comment",
        "icx skills create", "icx skills delete", "icx sonar projects", "icx sonar report",
        "icx memory save", "icx memory search", "icx memory delete", "icx memory migrate",
        "icx test configure", "icx test rules", "icx test cancel", "icx test analytics",
        "icx workstatus profile", "icx workstatus timesheets", "icx gitlab mrs", "icx gitlab commits",
    ]
    for snippet in hidden_samples:
        assert snippet not in _FULL_HELP, f"{snippet!r} should be hidden from _FULL_HELP"


def test_full_help_keeps_visible_commands():
    visible_samples = [
        "icx setup", "icx connection --add", "icx model --add", "icx analyze <KEY>",
        "icx status", "icx logout", "icx uninstall",
        "icx memory list", "icx memory export", "icx memory import", "icx memory status",
        "icx graph add", "icx graph build", "icx graph list", "icx graph status",
        "icx graph remove", "icx test setup", "icx test sessions",
        "icx sonar --add", "icx sonar status", "icx skills list",
        "icx jira whoami", "icx gitlab --add", "icx gitlab verify", "icx gitlab status",
        "icx workstatus --add", "icx workstatus status",
        "icx mcp run", "icx mcp setup", "icx mcp-external --add --preset <name>",
        "icx mcp-external --list", "icx mcp-external test", "icx langfuse",
    ]
    for snippet in visible_samples:
        assert snippet in _FULL_HELP, f"{snippet!r} should still be visible in _FULL_HELP"


def test_hidden_commands_still_run_by_exact_name():
    """hidden=True only affects --help listing - dispatch is untouched."""
    result = runner.invoke(app, ["skills", "list", "--help"])
    assert result.exit_code == 0
    result = runner.invoke(app, ["boost", "brief", "--help"])
    assert result.exit_code == 0
    result = runner.invoke(app, ["git", "--help"])
    assert result.exit_code == 0
    result = runner.invoke(app, ["jira", "worklog", "--help"])
    assert result.exit_code == 0


def test_hidden_subcommand_omitted_from_parent_help_but_visible_one_kept():
    result = runner.invoke(app, ["workstatus", "--help"])
    assert result.exit_code == 0
    assert "profile" not in result.stdout
    assert "status" in result.stdout


def test_hidden_whole_group_omitted_from_top_level_command_tree_listing():
    import typer as _typer
    cli = _typer.main.get_command(app)
    # Click's Command tree still contains hidden groups (dispatch must keep working) -
    # `hidden` is a rendering-only flag, checked here directly rather than via --help text.
    assert cli.commands["git"].hidden is True
    assert cli.commands["boost"].hidden is True
    assert cli.commands["logs"].hidden is True
    assert cli.commands["mcp-external"].hidden is not True
    assert cli.commands["langfuse"].hidden is not True
