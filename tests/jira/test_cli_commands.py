from __future__ import annotations

import click
import typer.testing
from unittest.mock import AsyncMock, Mock, patch


def _requirements(transitions=None, editable_fields=None):
    return {
        "issue_key": "TEST-1",
        "transitions": transitions if transitions is not None else [],
        "editable_fields": editable_fields if editable_fields is not None else {},
    }


# ---------------------------------------------------------------------------
# Happy path: transition chosen, required field prompted, comment given,
# confirmed, applied.
# ---------------------------------------------------------------------------

def test_jira_update_happy_path_transition_field_comment_confirmed_applied():
    from icx_engine.jira.cli_commands import jira_app
    transitions = [{"id": "31", "name": "Done", "fields": {"resolution": {"required": True}}}]
    requirements = _requirements(transitions=transitions, editable_fields={"summary": {"required": False}})

    prompt_mock = Mock(side_effect=["1", "Fixed the bug", "Closing this out"])
    confirm_mock = Mock(side_effect=[True, True])  # Add a comment? -> yes, Proceed? -> yes

    with patch("icx_engine.jira.cli_commands.service.get_close_requirements",
               new=AsyncMock(return_value=requirements)), \
         patch("icx_engine.jira.cli_commands.service.apply_update",
               new=AsyncMock(return_value={"ok": True, "issue_key": "TEST-1"})) as mock_apply, \
         patch("typer.prompt", prompt_mock), \
         patch("typer.confirm", confirm_mock):
        runner = typer.testing.CliRunner()
        result = runner.invoke(jira_app, ["update", "TEST-1"])

    assert result.exit_code == 0
    assert "successfully" in result.stdout.lower()
    mock_apply.assert_awaited_once_with(
        "TEST-1", transition_id="31", fields={"resolution": "Fixed the bug"}, comment="Closing this out",
    )


# ---------------------------------------------------------------------------
# Field-only flow: no transition chosen -> no comment prompt offered at all.
# ---------------------------------------------------------------------------

def test_jira_update_field_only_flow_skips_comment_prompt():
    from icx_engine.jira.cli_commands import jira_app
    transitions = [{"id": "11", "name": "To Do"}]
    requirements = _requirements(transitions=transitions, editable_fields={"summary": {"required": True}})

    # Only one typer.prompt call expected: transition selection (blank -> skip),
    # then the required field value. A third call would raise StopIteration.
    prompt_mock = Mock(side_effect=["", "New summary text"])
    # Only one typer.confirm call expected: the final "Proceed?" - if the
    # "Add a comment?" prompt were (incorrectly) offered, this would be
    # consumed there instead and the test would fail differently below.
    confirm_mock = Mock(side_effect=[True])

    with patch("icx_engine.jira.cli_commands.service.get_close_requirements",
               new=AsyncMock(return_value=requirements)), \
         patch("icx_engine.jira.cli_commands.service.apply_update",
               new=AsyncMock(return_value={"ok": True, "issue_key": "TEST-1"})) as mock_apply, \
         patch("typer.prompt", prompt_mock), \
         patch("typer.confirm", confirm_mock):
        runner = typer.testing.CliRunner()
        result = runner.invoke(jira_app, ["update", "TEST-1"])

    assert result.exit_code == 0
    mock_apply.assert_awaited_once_with(
        "TEST-1", transition_id=None, fields={"summary": "New summary text"}, comment=None,
    )
    # Exactly one confirm call happened (Proceed?) - side_effect of length 1
    # would have raised StopIteration if "Add a comment?" had also fired.
    assert confirm_mock.call_count == 1


# ---------------------------------------------------------------------------
# needs_fields retry-once path: first apply_update 400s with needs_fields,
# CLI prompts for those and retries exactly once, second call succeeds.
# ---------------------------------------------------------------------------

def test_jira_update_needs_fields_retry_once_succeeds():
    from icx_engine.jira.cli_commands import jira_app
    transitions = [{"id": "31", "name": "Done"}]
    requirements = _requirements(transitions=transitions, editable_fields={})

    prompt_mock = Mock(side_effect=["1", "Fixed on retry"])
    confirm_mock = Mock(side_effect=[False, True])  # Add a comment? -> no, Proceed? -> yes

    apply_mock = AsyncMock(side_effect=[
        {"ok": False, "needs_fields": {"resolution": "Resolution is required."}, "message": "bad"},
        {"ok": True, "issue_key": "TEST-1"},
    ])

    with patch("icx_engine.jira.cli_commands.service.get_close_requirements",
               new=AsyncMock(return_value=requirements)), \
         patch("icx_engine.jira.cli_commands.service.apply_update", new=apply_mock), \
         patch("typer.prompt", prompt_mock), \
         patch("typer.confirm", confirm_mock):
        runner = typer.testing.CliRunner()
        result = runner.invoke(jira_app, ["update", "TEST-1"])

    assert result.exit_code == 0
    assert "successfully" in result.stdout.lower()
    assert apply_mock.await_count == 2
    first_call, second_call = apply_mock.await_args_list
    assert first_call.kwargs["fields"] is None
    assert second_call.kwargs["fields"] == {"resolution": "Fixed on retry"}
    assert second_call.kwargs["transition_id"] == "31"


def test_jira_update_needs_fields_retry_once_fails_stops():
    from icx_engine.jira.cli_commands import jira_app
    transitions = [{"id": "31", "name": "Done"}]
    requirements = _requirements(transitions=transitions, editable_fields={})

    prompt_mock = Mock(side_effect=["1", "Still wrong"])
    confirm_mock = Mock(side_effect=[False, True])

    apply_mock = AsyncMock(side_effect=[
        {"ok": False, "needs_fields": {"resolution": "Resolution is required."}, "message": "bad"},
        {"ok": False, "needs_fields": {"resolution": "Still not valid."}, "message": "still bad"},
    ])

    with patch("icx_engine.jira.cli_commands.service.get_close_requirements",
               new=AsyncMock(return_value=requirements)), \
         patch("icx_engine.jira.cli_commands.service.apply_update", new=apply_mock), \
         patch("typer.prompt", prompt_mock), \
         patch("typer.confirm", confirm_mock):
        runner = typer.testing.CliRunner()
        result = runner.invoke(jira_app, ["update", "TEST-1"])

    assert result.exit_code != 0
    assert "failed" in result.stdout.lower()
    # Retried exactly once - never a third attempt even though the retry
    # also came back with needs_fields.
    assert apply_mock.await_count == 2


# ---------------------------------------------------------------------------
# Declined confirmation does nothing.
# ---------------------------------------------------------------------------

def test_jira_update_declined_confirmation_does_nothing():
    from icx_engine.jira.cli_commands import jira_app
    requirements = _requirements(transitions=[], editable_fields={})

    confirm_mock = Mock(side_effect=[False])  # Proceed? -> no

    with patch("icx_engine.jira.cli_commands.service.get_close_requirements",
               new=AsyncMock(return_value=requirements)), \
         patch("icx_engine.jira.cli_commands.service.apply_update",
               new=AsyncMock()) as mock_apply, \
         patch("typer.confirm", confirm_mock):
        runner = typer.testing.CliRunner()
        result = runner.invoke(jira_app, ["update", "TEST-1"])

    assert result.exit_code == 0
    assert "cancelled" in result.stdout.lower()
    mock_apply.assert_not_awaited()


# ---------------------------------------------------------------------------
# --debug/--traceback recognized + forced exception renders through
# render_icx_error (Task 5's from-day-one defensive pattern).
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


def test_jira_update_debug_and_traceback_flags_recognized():
    from icx_engine.jira.cli_commands import jira_app
    runner = typer.testing.CliRunner()
    result = runner.invoke(jira_app, ["update", "--help"])
    _assert_flags_recognized(result)


def test_jira_update_exception_renders_through_render_icx_error():
    from icx_engine.jira.cli_commands import jira_app

    with patch("icx_engine.jira.cli_commands.service.get_close_requirements",
               new=AsyncMock(side_effect=RuntimeError("forced update boom"))):
        runner = typer.testing.CliRunner()
        result = runner.invoke(jira_app, ["update", "TEST-1"])

    _assert_renders_via_render_icx_error(result, "forced update boom")


# ---------------------------------------------------------------------------
# icx jira create - happy path: project, issue-type selection (by number),
# summary, required field prompted from createmeta, confirmed, created.
# ---------------------------------------------------------------------------

def test_jira_create_happy_path_creates_and_prints_key():
    from icx_engine.jira.cli_commands import jira_app
    issue_types = [{"id": "10001", "name": "Bug"}, {"id": "10002", "name": "Task"}]
    createmeta_fields = {
        "summary": {"required": True},
        "priority": {"required": True},
    }

    prompt_mock = Mock(side_effect=["ABC", "1", "Something is broken", "High"])
    confirm_mock = Mock(side_effect=[True])  # Create this issue? -> yes

    with patch("icx_engine.jira.cli_commands.service.list_issue_types",
               new=AsyncMock(return_value=issue_types)), \
         patch("icx_engine.jira.cli_commands.service.get_createmeta_fields",
               new=AsyncMock(return_value=createmeta_fields)), \
         patch("icx_engine.jira.cli_commands.service.create_issue",
               new=AsyncMock(return_value={"ok": True, "issue_key": "ABC-42"})) as mock_create, \
         patch("typer.prompt", prompt_mock), \
         patch("typer.confirm", confirm_mock):
        runner = typer.testing.CliRunner()
        result = runner.invoke(jira_app, ["create"])

    assert result.exit_code == 0
    assert "ABC-42" in result.stdout
    mock_create.assert_awaited_once_with(
        "ABC", "Bug", "Something is broken", fields={"priority": "High"},
    )


def test_jira_create_selects_issue_type_by_name():
    from icx_engine.jira.cli_commands import jira_app
    issue_types = [{"id": "10001", "name": "Bug"}, {"id": "10002", "name": "Task"}]

    prompt_mock = Mock(side_effect=["ABC", "Task", "Do the thing"])
    confirm_mock = Mock(side_effect=[True])

    with patch("icx_engine.jira.cli_commands.service.list_issue_types",
               new=AsyncMock(return_value=issue_types)), \
         patch("icx_engine.jira.cli_commands.service.get_createmeta_fields",
               new=AsyncMock(return_value={})), \
         patch("icx_engine.jira.cli_commands.service.create_issue",
               new=AsyncMock(return_value={"ok": True, "issue_key": "ABC-7"})) as mock_create, \
         patch("typer.prompt", prompt_mock), \
         patch("typer.confirm", confirm_mock):
        runner = typer.testing.CliRunner()
        result = runner.invoke(jira_app, ["create"])

    assert result.exit_code == 0
    mock_create.assert_awaited_once_with("ABC", "Task", "Do the thing", fields=None)


def test_jira_create_invalid_issue_type_choice_exits_nonzero():
    from icx_engine.jira.cli_commands import jira_app
    issue_types = [{"id": "10001", "name": "Bug"}]

    prompt_mock = Mock(side_effect=["ABC", "NotARealType"])

    with patch("icx_engine.jira.cli_commands.service.list_issue_types",
               new=AsyncMock(return_value=issue_types)), \
         patch("icx_engine.jira.cli_commands.service.create_issue",
               new=AsyncMock()) as mock_create, \
         patch("typer.prompt", prompt_mock):
        runner = typer.testing.CliRunner()
        result = runner.invoke(jira_app, ["create"])

    assert result.exit_code != 0
    mock_create.assert_not_awaited()


def test_jira_create_no_issue_types_available_exits_nonzero():
    from icx_engine.jira.cli_commands import jira_app

    prompt_mock = Mock(side_effect=["ABC"])

    with patch("icx_engine.jira.cli_commands.service.list_issue_types",
               new=AsyncMock(return_value=[])), \
         patch("icx_engine.jira.cli_commands.service.create_issue",
               new=AsyncMock()) as mock_create, \
         patch("typer.prompt", prompt_mock):
        runner = typer.testing.CliRunner()
        result = runner.invoke(jira_app, ["create"])

    assert result.exit_code != 0
    mock_create.assert_not_awaited()


def test_jira_create_declined_confirmation_does_nothing():
    from icx_engine.jira.cli_commands import jira_app
    issue_types = [{"id": "10001", "name": "Bug"}]

    prompt_mock = Mock(side_effect=["ABC", "1", "Something is broken"])
    confirm_mock = Mock(side_effect=[False])  # Create this issue? -> no

    with patch("icx_engine.jira.cli_commands.service.list_issue_types",
               new=AsyncMock(return_value=issue_types)), \
         patch("icx_engine.jira.cli_commands.service.get_createmeta_fields",
               new=AsyncMock(return_value={})), \
         patch("icx_engine.jira.cli_commands.service.create_issue",
               new=AsyncMock()) as mock_create, \
         patch("typer.prompt", prompt_mock), \
         patch("typer.confirm", confirm_mock):
        runner = typer.testing.CliRunner()
        result = runner.invoke(jira_app, ["create"])

    assert result.exit_code == 0
    assert "cancelled" in result.stdout.lower()
    mock_create.assert_not_awaited()


def test_jira_create_debug_and_traceback_flags_recognized():
    from icx_engine.jira.cli_commands import jira_app
    runner = typer.testing.CliRunner()
    result = runner.invoke(jira_app, ["create", "--help"])
    _assert_flags_recognized(result)


def test_jira_create_exception_renders_through_render_icx_error():
    from icx_engine.jira.cli_commands import jira_app

    with patch("icx_engine.jira.cli_commands.service.list_issue_types",
               new=AsyncMock(side_effect=RuntimeError("forced create boom"))), \
         patch("typer.prompt", Mock(return_value="ABC")):
        runner = typer.testing.CliRunner()
        result = runner.invoke(jira_app, ["create"])

    _assert_renders_via_render_icx_error(result, "forced create boom")


# ---------------------------------------------------------------------------
# icx jira delete - explicit warning shown, confirmed, deletes.
# ---------------------------------------------------------------------------

def test_jira_delete_shows_warning_and_deletes_when_confirmed():
    from icx_engine.jira.cli_commands import jira_app

    confirm_mock = Mock(side_effect=[True])

    with patch("icx_engine.jira.cli_commands.service.delete_issue",
               new=AsyncMock(return_value={"ok": True, "issue_key": "TEST-1"})) as mock_delete, \
         patch("typer.confirm", confirm_mock):
        runner = typer.testing.CliRunner()
        result = runner.invoke(jira_app, ["delete", "TEST-1"])

    assert result.exit_code == 0
    out = result.stdout.lower()
    assert "permanent" in out
    assert "no undo" in out
    assert "no trash" in out
    assert "recycle bin" in out
    mock_delete.assert_awaited_once_with("TEST-1", delete_subtasks=False)


def test_jira_delete_with_delete_subtasks_flag():
    from icx_engine.jira.cli_commands import jira_app

    confirm_mock = Mock(side_effect=[True])

    with patch("icx_engine.jira.cli_commands.service.delete_issue",
               new=AsyncMock(return_value={"ok": True, "issue_key": "TEST-1"})) as mock_delete, \
         patch("typer.confirm", confirm_mock):
        runner = typer.testing.CliRunner()
        result = runner.invoke(jira_app, ["delete", "TEST-1", "--delete-subtasks"])

    assert result.exit_code == 0
    mock_delete.assert_awaited_once_with("TEST-1", delete_subtasks=True)


def test_jira_delete_declined_confirmation_does_nothing():
    from icx_engine.jira.cli_commands import jira_app

    confirm_mock = Mock(side_effect=[False])

    with patch("icx_engine.jira.cli_commands.service.delete_issue",
               new=AsyncMock()) as mock_delete, \
         patch("typer.confirm", confirm_mock):
        runner = typer.testing.CliRunner()
        result = runner.invoke(jira_app, ["delete", "TEST-1"])

    assert result.exit_code == 0
    assert "cancelled" in result.stdout.lower()
    mock_delete.assert_not_awaited()


def test_jira_delete_debug_and_traceback_flags_recognized():
    from icx_engine.jira.cli_commands import jira_app
    runner = typer.testing.CliRunner()
    result = runner.invoke(jira_app, ["delete", "--help"])
    _assert_flags_recognized(result)


def test_jira_delete_exception_renders_through_render_icx_error():
    from icx_engine.jira.cli_commands import jira_app

    with patch("icx_engine.jira.cli_commands.service.delete_issue",
               new=AsyncMock(side_effect=RuntimeError("forced delete boom"))), \
         patch("typer.confirm", Mock(return_value=True)):
        runner = typer.testing.CliRunner()
        result = runner.invoke(jira_app, ["delete", "TEST-1"])

    _assert_renders_via_render_icx_error(result, "forced delete boom")


# ---------------------------------------------------------------------------
# icx jira search - prints matching issue keys/summaries (lightweight, raw).
# ---------------------------------------------------------------------------

def test_jira_search_prints_matching_issues():
    from icx_engine.jira.cli_commands import jira_app
    search_result = {
        "jql": "project = ABC",
        "issues": [
            {"key": "ABC-1", "fields": {"summary": "First issue"}},
            {"key": "ABC-2", "fields": {"summary": "Second issue"}},
        ],
        "next_page_token": None, "is_last": True,
    }
    with patch("icx_engine.jira.cli_commands.service.search",
               new=AsyncMock(return_value=search_result)) as mock_search:
        runner = typer.testing.CliRunner()
        result = runner.invoke(jira_app, ["search", "project = ABC"])

    assert result.exit_code == 0
    assert "ABC-1" in result.stdout
    assert "First issue" in result.stdout
    assert "ABC-2" in result.stdout
    assert "Second issue" in result.stdout
    mock_search.assert_awaited_once_with("project = ABC", max_results=50)


def test_jira_search_no_results_prints_message():
    from icx_engine.jira.cli_commands import jira_app

    with patch("icx_engine.jira.cli_commands.service.search",
               new=AsyncMock(return_value={"issues": [], "is_last": True})):
        runner = typer.testing.CliRunner()
        result = runner.invoke(jira_app, ["search", "project = NONE"])

    assert result.exit_code == 0
    assert "no matching" in result.stdout.lower()


def test_jira_search_shows_next_page_token_when_not_last():
    from icx_engine.jira.cli_commands import jira_app
    search_result = {
        "issues": [{"key": "ABC-1", "fields": {"summary": "x"}}],
        "next_page_token": "tok-next", "is_last": False,
    }
    with patch("icx_engine.jira.cli_commands.service.search",
               new=AsyncMock(return_value=search_result)):
        runner = typer.testing.CliRunner()
        result = runner.invoke(jira_app, ["search", "project = ABC"])

    assert result.exit_code == 0
    assert "tok-next" in result.stdout


def test_jira_search_passes_max_results_option():
    from icx_engine.jira.cli_commands import jira_app

    with patch("icx_engine.jira.cli_commands.service.search",
               new=AsyncMock(return_value={"issues": []})) as mock_search:
        runner = typer.testing.CliRunner()
        runner.invoke(jira_app, ["search", "project = ABC", "--max-results", "5"])

    mock_search.assert_awaited_once_with("project = ABC", max_results=5)


def test_jira_search_debug_and_traceback_flags_recognized():
    from icx_engine.jira.cli_commands import jira_app
    runner = typer.testing.CliRunner()
    result = runner.invoke(jira_app, ["search", "--help"])
    _assert_flags_recognized(result)


def test_jira_search_exception_renders_through_render_icx_error():
    from icx_engine.jira.cli_commands import jira_app

    with patch("icx_engine.jira.cli_commands.service.search",
               new=AsyncMock(side_effect=RuntimeError("forced search boom"))):
        runner = typer.testing.CliRunner()
        result = runner.invoke(jira_app, ["search", "project = ABC"])

    _assert_renders_via_render_icx_error(result, "forced search boom")


# ---------------------------------------------------------------------------
# icx jira get - prints an issue's raw fields (lightweight, raw).
# ---------------------------------------------------------------------------

def test_jira_get_prints_raw_fields():
    from icx_engine.jira.cli_commands import jira_app
    get_result = {
        "issue_key": "TEST-1",
        "raw": {"key": "TEST-1", "fields": {"summary": "A bug", "status": {"name": "Open"}}},
    }
    with patch("icx_engine.jira.cli_commands.service.get_issue",
               new=AsyncMock(return_value=get_result)) as mock_get:
        runner = typer.testing.CliRunner()
        result = runner.invoke(jira_app, ["get", "TEST-1"])

    assert result.exit_code == 0
    assert "TEST-1" in result.stdout
    assert "summary" in result.stdout
    assert "A bug" in result.stdout
    mock_get.assert_awaited_once_with("TEST-1")


def test_jira_get_no_fields_prints_message():
    from icx_engine.jira.cli_commands import jira_app

    with patch("icx_engine.jira.cli_commands.service.get_issue",
               new=AsyncMock(return_value={"issue_key": "TEST-1", "raw": {"key": "TEST-1"}})):
        runner = typer.testing.CliRunner()
        result = runner.invoke(jira_app, ["get", "TEST-1"])

    assert result.exit_code == 0
    assert "no fields" in result.stdout.lower()


def test_jira_get_debug_and_traceback_flags_recognized():
    from icx_engine.jira.cli_commands import jira_app
    runner = typer.testing.CliRunner()
    result = runner.invoke(jira_app, ["get", "--help"])
    _assert_flags_recognized(result)


def test_jira_get_exception_renders_through_render_icx_error():
    from icx_engine.jira.cli_commands import jira_app

    with patch("icx_engine.jira.cli_commands.service.get_issue",
               new=AsyncMock(side_effect=RuntimeError("forced get boom"))):
        runner = typer.testing.CliRunner()
        result = runner.invoke(jira_app, ["get", "TEST-1"])

    _assert_renders_via_render_icx_error(result, "forced get boom")


# ---------------------------------------------------------------------------
# icx jira link types/create/delete
# ---------------------------------------------------------------------------

def test_jira_link_types_prints_available_types():
    from icx_engine.jira.cli_commands import jira_app
    link_types = [{"id": "10000", "name": "Blocks", "inward": "is blocked by", "outward": "blocks"}]
    with patch("icx_engine.jira.cli_commands.service.link_types",
               new=AsyncMock(return_value={"link_types": link_types})) as mock_link_types:
        runner = typer.testing.CliRunner()
        result = runner.invoke(jira_app, ["link", "types"])

    assert result.exit_code == 0
    assert "Blocks" in result.stdout
    assert "blocks" in result.stdout.lower()
    mock_link_types.assert_awaited_once_with()


def test_jira_link_types_no_types_prints_message():
    from icx_engine.jira.cli_commands import jira_app
    with patch("icx_engine.jira.cli_commands.service.link_types",
               new=AsyncMock(return_value={"link_types": []})):
        runner = typer.testing.CliRunner()
        result = runner.invoke(jira_app, ["link", "types"])

    assert result.exit_code == 0
    assert "no link types" in result.stdout.lower()


def test_jira_link_types_debug_and_traceback_flags_recognized():
    from icx_engine.jira.cli_commands import jira_app
    runner = typer.testing.CliRunner()
    result = runner.invoke(jira_app, ["link", "types", "--help"])
    _assert_flags_recognized(result)


def test_jira_link_types_exception_renders_through_render_icx_error():
    from icx_engine.jira.cli_commands import jira_app

    with patch("icx_engine.jira.cli_commands.service.link_types",
               new=AsyncMock(side_effect=RuntimeError("forced link types boom"))):
        runner = typer.testing.CliRunner()
        result = runner.invoke(jira_app, ["link", "types"])

    _assert_renders_via_render_icx_error(result, "forced link types boom")


def test_jira_link_create_creates_link():
    from icx_engine.jira.cli_commands import jira_app
    with patch("icx_engine.jira.cli_commands.service.create_link",
               new=AsyncMock(return_value={"ok": True})) as mock_create:
        runner = typer.testing.CliRunner()
        result = runner.invoke(jira_app, ["link", "create", "Blocks", "ABC-1", "ABC-2"])

    assert result.exit_code == 0
    assert "ABC-1" in result.stdout
    assert "ABC-2" in result.stdout
    mock_create.assert_awaited_once_with("Blocks", "ABC-1", "ABC-2")


def test_jira_link_create_debug_and_traceback_flags_recognized():
    from icx_engine.jira.cli_commands import jira_app
    runner = typer.testing.CliRunner()
    result = runner.invoke(jira_app, ["link", "create", "--help"])
    _assert_flags_recognized(result)


def test_jira_link_create_exception_renders_through_render_icx_error():
    from icx_engine.jira.cli_commands import jira_app

    with patch("icx_engine.jira.cli_commands.service.create_link",
               new=AsyncMock(side_effect=RuntimeError("forced link create boom"))):
        runner = typer.testing.CliRunner()
        result = runner.invoke(jira_app, ["link", "create", "Blocks", "ABC-1", "ABC-2"])

    _assert_renders_via_render_icx_error(result, "forced link create boom")


def test_jira_link_delete_shows_warning_and_deletes_when_confirmed():
    from icx_engine.jira.cli_commands import jira_app

    confirm_mock = Mock(side_effect=[True])

    with patch("icx_engine.jira.cli_commands.service.delete_link",
               new=AsyncMock(return_value={"ok": True})) as mock_delete, \
         patch("typer.confirm", confirm_mock):
        runner = typer.testing.CliRunner()
        result = runner.invoke(jira_app, ["link", "delete", "TEST-1", "10050"])

    assert result.exit_code == 0
    out = result.stdout.lower()
    assert "dependency" in out
    assert "recreated" in out
    assert "no undo" not in out
    assert "no trash" not in out
    mock_delete.assert_awaited_once_with("TEST-1", "10050")


def test_jira_link_delete_declined_confirmation_does_nothing():
    from icx_engine.jira.cli_commands import jira_app

    confirm_mock = Mock(side_effect=[False])

    with patch("icx_engine.jira.cli_commands.service.delete_link",
               new=AsyncMock()) as mock_delete, \
         patch("typer.confirm", confirm_mock):
        runner = typer.testing.CliRunner()
        result = runner.invoke(jira_app, ["link", "delete", "TEST-1", "10050"])

    assert result.exit_code == 0
    assert "cancelled" in result.stdout.lower()
    mock_delete.assert_not_awaited()


def test_jira_link_delete_debug_and_traceback_flags_recognized():
    from icx_engine.jira.cli_commands import jira_app
    runner = typer.testing.CliRunner()
    result = runner.invoke(jira_app, ["link", "delete", "--help"])
    _assert_flags_recognized(result)


def test_jira_link_delete_exception_renders_through_render_icx_error():
    from icx_engine.jira.cli_commands import jira_app

    with patch("icx_engine.jira.cli_commands.service.delete_link",
               new=AsyncMock(side_effect=RuntimeError("forced link delete boom"))), \
         patch("typer.confirm", Mock(return_value=True)):
        runner = typer.testing.CliRunner()
        result = runner.invoke(jira_app, ["link", "delete", "TEST-1", "10050"])

    _assert_renders_via_render_icx_error(result, "forced link delete boom")


# ---------------------------------------------------------------------------
# icx jira assign <KEY> <ACCOUNT_ID> / --unassign / --default
# ---------------------------------------------------------------------------

def test_jira_assign_with_account_id():
    from icx_engine.jira.cli_commands import jira_app
    with patch("icx_engine.jira.cli_commands.service.set_assignee",
               new=AsyncMock(return_value={"ok": True})) as mock_set:
        runner = typer.testing.CliRunner()
        result = runner.invoke(jira_app, ["assign", "TEST-1", "acc-1"])

    assert result.exit_code == 0
    assert "TEST-1" in result.stdout
    assert "acc-1" in result.stdout
    mock_set.assert_awaited_once_with("TEST-1", "acc-1")


def test_jira_assign_unassign_flag_sends_none():
    from icx_engine.jira.cli_commands import jira_app
    with patch("icx_engine.jira.cli_commands.service.set_assignee",
               new=AsyncMock(return_value={"ok": True})) as mock_set:
        runner = typer.testing.CliRunner()
        result = runner.invoke(jira_app, ["assign", "TEST-1", "--unassign"])

    assert result.exit_code == 0
    assert "unassigned" in result.stdout.lower()
    mock_set.assert_awaited_once_with("TEST-1", None)


def test_jira_assign_default_flag_sends_minus_one_sentinel():
    from icx_engine.jira.cli_commands import jira_app
    with patch("icx_engine.jira.cli_commands.service.set_assignee",
               new=AsyncMock(return_value={"ok": True})) as mock_set:
        runner = typer.testing.CliRunner()
        result = runner.invoke(jira_app, ["assign", "TEST-1", "--default"])

    assert result.exit_code == 0
    mock_set.assert_awaited_once_with("TEST-1", "-1")


def test_jira_assign_missing_account_id_and_no_flag_exits_nonzero():
    from icx_engine.jira.cli_commands import jira_app
    with patch("icx_engine.jira.cli_commands.service.set_assignee",
               new=AsyncMock()) as mock_set:
        runner = typer.testing.CliRunner()
        result = runner.invoke(jira_app, ["assign", "TEST-1"])

    assert result.exit_code != 0
    mock_set.assert_not_awaited()


def test_jira_assign_both_unassign_and_default_flags_exits_nonzero():
    from icx_engine.jira.cli_commands import jira_app
    with patch("icx_engine.jira.cli_commands.service.set_assignee",
               new=AsyncMock()) as mock_set:
        runner = typer.testing.CliRunner()
        result = runner.invoke(jira_app, ["assign", "TEST-1", "--unassign", "--default"])

    assert result.exit_code != 0
    mock_set.assert_not_awaited()


def test_jira_assign_debug_and_traceback_flags_recognized():
    from icx_engine.jira.cli_commands import jira_app
    runner = typer.testing.CliRunner()
    result = runner.invoke(jira_app, ["assign", "--help"])
    _assert_flags_recognized(result)


def test_jira_assign_exception_renders_through_render_icx_error():
    from icx_engine.jira.cli_commands import jira_app

    with patch("icx_engine.jira.cli_commands.service.set_assignee",
               new=AsyncMock(side_effect=RuntimeError("forced assign boom"))):
        runner = typer.testing.CliRunner()
        result = runner.invoke(jira_app, ["assign", "TEST-1", "acc-1"])

    _assert_renders_via_render_icx_error(result, "forced assign boom")


# ---------------------------------------------------------------------------
# icx jira attach add/remove
# ---------------------------------------------------------------------------

def test_jira_attach_add_reads_file_and_uploads(tmp_path):
    from icx_engine.jira.cli_commands import jira_app
    file_path = tmp_path / "report.txt"
    file_path.write_bytes(b"hello world")

    with patch("icx_engine.jira.cli_commands.service.upload_attachment",
               new=AsyncMock(return_value={
                   "ok": True, "issue_key": "TEST-1", "filename": "report.txt",
                   "attachments": [{"id": "10100"}],
               })) as mock_upload:
        runner = typer.testing.CliRunner()
        result = runner.invoke(jira_app, ["attach", "add", "TEST-1", str(file_path)])

    assert result.exit_code == 0
    assert "report.txt" in result.stdout
    assert "10100" in result.stdout
    mock_upload.assert_awaited_once()
    call_args = mock_upload.call_args
    assert call_args.args[0] == "TEST-1"
    assert call_args.args[1] == "report.txt"
    assert call_args.args[2] == b"hello world"


def test_jira_attach_add_infers_content_type_from_extension(tmp_path):
    from icx_engine.jira.cli_commands import jira_app
    file_path = tmp_path / "photo.png"
    file_path.write_bytes(b"\x89PNG\r\n")

    with patch("icx_engine.jira.cli_commands.service.upload_attachment",
               new=AsyncMock(return_value={"ok": True, "attachments": []})) as mock_upload:
        runner = typer.testing.CliRunner()
        result = runner.invoke(jira_app, ["attach", "add", "TEST-1", str(file_path)])

    assert result.exit_code == 0
    call_args = mock_upload.call_args
    assert call_args.kwargs["content_type"] == "image/png"


def test_jira_attach_add_omits_content_type_for_unknown_extension(tmp_path):
    from icx_engine.jira.cli_commands import jira_app
    file_path = tmp_path / "data.unknownext"
    file_path.write_bytes(b"\x00\x01")

    with patch("icx_engine.jira.cli_commands.service.upload_attachment",
               new=AsyncMock(return_value={"ok": True, "attachments": []})) as mock_upload:
        runner = typer.testing.CliRunner()
        result = runner.invoke(jira_app, ["attach", "add", "TEST-1", str(file_path)])

    assert result.exit_code == 0
    call_args = mock_upload.call_args
    assert call_args.kwargs["content_type"] is None


def test_jira_attach_add_missing_file_exits_nonzero():
    from icx_engine.jira.cli_commands import jira_app
    with patch("icx_engine.jira.cli_commands.service.upload_attachment",
               new=AsyncMock()) as mock_upload:
        runner = typer.testing.CliRunner()
        result = runner.invoke(jira_app, ["attach", "add", "TEST-1", "/does/not/exist.txt"])

    assert result.exit_code != 0
    mock_upload.assert_not_awaited()


def test_jira_attach_add_debug_and_traceback_flags_recognized():
    from icx_engine.jira.cli_commands import jira_app
    runner = typer.testing.CliRunner()
    result = runner.invoke(jira_app, ["attach", "add", "--help"])
    _assert_flags_recognized(result)


def test_jira_attach_add_exception_renders_through_render_icx_error(tmp_path):
    from icx_engine.jira.cli_commands import jira_app
    file_path = tmp_path / "report.txt"
    file_path.write_bytes(b"hello")

    with patch("icx_engine.jira.cli_commands.service.upload_attachment",
               new=AsyncMock(side_effect=RuntimeError("forced attach boom"))):
        runner = typer.testing.CliRunner()
        result = runner.invoke(jira_app, ["attach", "add", "TEST-1", str(file_path)])

    _assert_renders_via_render_icx_error(result, "forced attach boom")


def test_jira_attach_remove_shows_warning_and_deletes_when_confirmed():
    from icx_engine.jira.cli_commands import jira_app

    confirm_mock = Mock(side_effect=[True])

    with patch("icx_engine.jira.cli_commands.service.delete_attachment",
               new=AsyncMock(return_value={"ok": True})) as mock_delete, \
         patch("typer.confirm", confirm_mock):
        runner = typer.testing.CliRunner()
        result = runner.invoke(jira_app, ["attach", "remove", "TEST-1", "10100"])

    assert result.exit_code == 0
    out = result.stdout.lower()
    assert "permanent" in out
    assert "no undo" in out
    mock_delete.assert_awaited_once_with("TEST-1", "10100")


def test_jira_attach_remove_declined_confirmation_does_nothing():
    from icx_engine.jira.cli_commands import jira_app

    confirm_mock = Mock(side_effect=[False])

    with patch("icx_engine.jira.cli_commands.service.delete_attachment",
               new=AsyncMock()) as mock_delete, \
         patch("typer.confirm", confirm_mock):
        runner = typer.testing.CliRunner()
        result = runner.invoke(jira_app, ["attach", "remove", "TEST-1", "10100"])

    assert result.exit_code == 0
    assert "cancelled" in result.stdout.lower()
    mock_delete.assert_not_awaited()


def test_jira_attach_remove_debug_and_traceback_flags_recognized():
    from icx_engine.jira.cli_commands import jira_app
    runner = typer.testing.CliRunner()
    result = runner.invoke(jira_app, ["attach", "remove", "--help"])
    _assert_flags_recognized(result)


def test_jira_attach_remove_exception_renders_through_render_icx_error():
    from icx_engine.jira.cli_commands import jira_app

    with patch("icx_engine.jira.cli_commands.service.delete_attachment",
               new=AsyncMock(side_effect=RuntimeError("forced attach remove boom"))), \
         patch("typer.confirm", Mock(return_value=True)):
        runner = typer.testing.CliRunner()
        result = runner.invoke(jira_app, ["attach", "remove", "TEST-1", "10100"])

    _assert_renders_via_render_icx_error(result, "forced attach remove boom")


# ---------------------------------------------------------------------------
# icx jira whoami
# ---------------------------------------------------------------------------

def test_jira_whoami_prints_account_id_and_display_name():
    from icx_engine.jira.cli_commands import jira_app
    with patch("icx_engine.jira.cli_commands.service.get_current_user",
               new=AsyncMock(return_value={"accountId": "acc-self", "displayName": "Me"})) as mock_me:
        runner = typer.testing.CliRunner()
        result = runner.invoke(jira_app, ["whoami"])

    assert result.exit_code == 0
    assert "acc-self" in result.stdout
    assert "Me" in result.stdout
    mock_me.assert_awaited_once_with()


def test_jira_whoami_debug_and_traceback_flags_recognized():
    from icx_engine.jira.cli_commands import jira_app
    runner = typer.testing.CliRunner()
    result = runner.invoke(jira_app, ["whoami", "--help"])
    _assert_flags_recognized(result)


def test_jira_whoami_exception_renders_through_render_icx_error():
    from icx_engine.jira.cli_commands import jira_app
    with patch("icx_engine.jira.cli_commands.service.get_current_user",
               new=AsyncMock(side_effect=RuntimeError("forced whoami boom"))):
        runner = typer.testing.CliRunner()
        result = runner.invoke(jira_app, ["whoami"])

    _assert_renders_via_render_icx_error(result, "forced whoami boom")


# ---------------------------------------------------------------------------
# icx jira watch add/remove <KEY> [ACCOUNT_ID] - self-vs-other gating
# ---------------------------------------------------------------------------

def test_jira_watch_add_self_omitted_account_id_no_confirmation_needed():
    from icx_engine.jira.cli_commands import jira_app
    with patch("icx_engine.jira.cli_commands.service.get_current_user",
               new=AsyncMock(return_value={"accountId": "acc-self"})) as mock_me, \
         patch("icx_engine.jira.cli_commands.service.add_watcher",
               new=AsyncMock(return_value={"ok": True})) as mock_add, \
         patch("typer.confirm") as confirm_mock:
        runner = typer.testing.CliRunner()
        result = runner.invoke(jira_app, ["watch", "add", "TEST-1"])

    assert result.exit_code == 0
    assert "acc-self" in result.stdout
    mock_me.assert_awaited_once_with(issue_key="TEST-1")
    mock_add.assert_awaited_once_with("TEST-1", "acc-self")
    confirm_mock.assert_not_called()


def test_jira_watch_add_explicit_account_id_matching_self_no_confirmation_needed():
    from icx_engine.jira.cli_commands import jira_app
    with patch("icx_engine.jira.cli_commands.service.get_current_user",
               new=AsyncMock(return_value={"accountId": "acc-self"})), \
         patch("icx_engine.jira.cli_commands.service.add_watcher",
               new=AsyncMock(return_value={"ok": True})) as mock_add, \
         patch("typer.confirm") as confirm_mock:
        runner = typer.testing.CliRunner()
        result = runner.invoke(jira_app, ["watch", "add", "TEST-1", "acc-self"])

    assert result.exit_code == 0
    mock_add.assert_awaited_once_with("TEST-1", "acc-self")
    confirm_mock.assert_not_called()


def test_jira_watch_add_other_account_id_shows_warning_and_confirms():
    from icx_engine.jira.cli_commands import jira_app
    confirm_mock = Mock(side_effect=[True])
    with patch("icx_engine.jira.cli_commands.service.get_current_user",
               new=AsyncMock(return_value={"accountId": "acc-self"})), \
         patch("icx_engine.jira.cli_commands.service.add_watcher",
               new=AsyncMock(return_value={"ok": True})) as mock_add, \
         patch("typer.confirm", confirm_mock):
        runner = typer.testing.CliRunner()
        result = runner.invoke(jira_app, ["watch", "add", "TEST-1", "acc-other"])

    assert result.exit_code == 0
    out = result.stdout.lower()
    assert "different jira user" in out
    mock_add.assert_awaited_once_with("TEST-1", "acc-other")


def test_jira_watch_add_other_account_id_declined_does_nothing():
    from icx_engine.jira.cli_commands import jira_app
    confirm_mock = Mock(side_effect=[False])
    with patch("icx_engine.jira.cli_commands.service.get_current_user",
               new=AsyncMock(return_value={"accountId": "acc-self"})), \
         patch("icx_engine.jira.cli_commands.service.add_watcher",
               new=AsyncMock()) as mock_add, \
         patch("typer.confirm", confirm_mock):
        runner = typer.testing.CliRunner()
        result = runner.invoke(jira_app, ["watch", "add", "TEST-1", "acc-other"])

    assert result.exit_code == 0
    assert "cancelled" in result.stdout.lower()
    mock_add.assert_not_awaited()


def test_jira_watch_add_debug_and_traceback_flags_recognized():
    from icx_engine.jira.cli_commands import jira_app
    runner = typer.testing.CliRunner()
    result = runner.invoke(jira_app, ["watch", "add", "--help"])
    _assert_flags_recognized(result)


def test_jira_watch_add_exception_renders_through_render_icx_error():
    from icx_engine.jira.cli_commands import jira_app
    with patch("icx_engine.jira.cli_commands.service.get_current_user",
               new=AsyncMock(side_effect=RuntimeError("forced watch add boom"))):
        runner = typer.testing.CliRunner()
        result = runner.invoke(jira_app, ["watch", "add", "TEST-1"])

    _assert_renders_via_render_icx_error(result, "forced watch add boom")


def test_jira_watch_remove_self_omitted_account_id_no_confirmation_needed():
    from icx_engine.jira.cli_commands import jira_app
    with patch("icx_engine.jira.cli_commands.service.get_current_user",
               new=AsyncMock(return_value={"accountId": "acc-self"})), \
         patch("icx_engine.jira.cli_commands.service.remove_watcher",
               new=AsyncMock(return_value={"ok": True})) as mock_remove, \
         patch("typer.confirm") as confirm_mock:
        runner = typer.testing.CliRunner()
        result = runner.invoke(jira_app, ["watch", "remove", "TEST-1"])

    assert result.exit_code == 0
    mock_remove.assert_awaited_once_with("TEST-1", "acc-self")
    confirm_mock.assert_not_called()


def test_jira_watch_remove_other_account_id_shows_warning_and_confirms():
    from icx_engine.jira.cli_commands import jira_app
    confirm_mock = Mock(side_effect=[True])
    with patch("icx_engine.jira.cli_commands.service.get_current_user",
               new=AsyncMock(return_value={"accountId": "acc-self"})), \
         patch("icx_engine.jira.cli_commands.service.remove_watcher",
               new=AsyncMock(return_value={"ok": True})) as mock_remove, \
         patch("typer.confirm", confirm_mock):
        runner = typer.testing.CliRunner()
        result = runner.invoke(jira_app, ["watch", "remove", "TEST-1", "acc-other"])

    assert result.exit_code == 0
    mock_remove.assert_awaited_once_with("TEST-1", "acc-other")


def test_jira_watch_remove_other_account_id_declined_does_nothing():
    from icx_engine.jira.cli_commands import jira_app
    confirm_mock = Mock(side_effect=[False])
    with patch("icx_engine.jira.cli_commands.service.get_current_user",
               new=AsyncMock(return_value={"accountId": "acc-self"})), \
         patch("icx_engine.jira.cli_commands.service.remove_watcher",
               new=AsyncMock()) as mock_remove, \
         patch("typer.confirm", confirm_mock):
        runner = typer.testing.CliRunner()
        result = runner.invoke(jira_app, ["watch", "remove", "TEST-1", "acc-other"])

    assert result.exit_code == 0
    assert "cancelled" in result.stdout.lower()
    mock_remove.assert_not_awaited()


def test_jira_watch_remove_debug_and_traceback_flags_recognized():
    from icx_engine.jira.cli_commands import jira_app
    runner = typer.testing.CliRunner()
    result = runner.invoke(jira_app, ["watch", "remove", "--help"])
    _assert_flags_recognized(result)


def test_jira_watch_remove_exception_renders_through_render_icx_error():
    from icx_engine.jira.cli_commands import jira_app
    with patch("icx_engine.jira.cli_commands.service.get_current_user",
               new=AsyncMock(side_effect=RuntimeError("forced watch remove boom"))):
        runner = typer.testing.CliRunner()
        result = runner.invoke(jira_app, ["watch", "remove", "TEST-1"])

    _assert_renders_via_render_icx_error(result, "forced watch remove boom")


# ---------------------------------------------------------------------------
# icx jira worklog list/add/edit/delete <KEY> - self-vs-other gating
# ---------------------------------------------------------------------------

def test_jira_worklog_list_prints_entries():
    from icx_engine.jira.cli_commands import jira_app
    with patch("icx_engine.jira.cli_commands.service.list_worklogs",
               new=AsyncMock(return_value={"issue_key": "TEST-1", "worklogs": [
                   {"id": "500", "timeSpentSeconds": 3600, "started": "2026-07-28T10:00:00.000+0000",
                    "author": {"displayName": "Alice"}},
               ]})):
        runner = typer.testing.CliRunner()
        result = runner.invoke(jira_app, ["worklog", "list", "TEST-1"])

    assert result.exit_code == 0
    assert "Alice" in result.stdout
    assert "3600" in result.stdout


def test_jira_worklog_list_no_entries_prints_message():
    from icx_engine.jira.cli_commands import jira_app
    with patch("icx_engine.jira.cli_commands.service.list_worklogs",
               new=AsyncMock(return_value={"issue_key": "TEST-1", "worklogs": []})):
        runner = typer.testing.CliRunner()
        result = runner.invoke(jira_app, ["worklog", "list", "TEST-1"])

    assert result.exit_code == 0
    assert "no worklog" in result.stdout.lower()


def test_jira_worklog_list_debug_and_traceback_flags_recognized():
    from icx_engine.jira.cli_commands import jira_app
    runner = typer.testing.CliRunner()
    result = runner.invoke(jira_app, ["worklog", "list", "--help"])
    _assert_flags_recognized(result)


def test_jira_worklog_list_exception_renders_through_render_icx_error():
    from icx_engine.jira.cli_commands import jira_app
    with patch("icx_engine.jira.cli_commands.service.list_worklogs",
               new=AsyncMock(side_effect=RuntimeError("forced worklog list boom"))):
        runner = typer.testing.CliRunner()
        result = runner.invoke(jira_app, ["worklog", "list", "TEST-1"])

    _assert_renders_via_render_icx_error(result, "forced worklog list boom")


def test_jira_worklog_add_always_executes_immediately_no_confirm_at_all():
    """The real point: worklog creation never touches typer.confirm at all -
    Jira's worklog POST has no author-override, so there is no self-vs-other
    branch here in the first place."""
    from icx_engine.jira.cli_commands import jira_app
    with patch("icx_engine.jira.cli_commands.service.add_worklog",
               new=AsyncMock(return_value={"ok": True, "worklog": {"id": "500"}})) as mock_add, \
         patch("typer.confirm") as confirm_mock:
        runner = typer.testing.CliRunner()
        result = runner.invoke(jira_app, ["worklog", "add", "TEST-1", "3600", "2026-07-28T10:00:00"])

    assert result.exit_code == 0
    assert "500" in result.stdout
    mock_add.assert_awaited_once_with("TEST-1", 3600, "2026-07-28T10:00:00", comment=None)
    confirm_mock.assert_not_called()


def test_jira_worklog_add_passes_comment_option_through():
    from icx_engine.jira.cli_commands import jira_app
    with patch("icx_engine.jira.cli_commands.service.add_worklog",
               new=AsyncMock(return_value={"ok": True, "worklog": {"id": "500"}})) as mock_add:
        runner = typer.testing.CliRunner()
        result = runner.invoke(jira_app, [
            "worklog", "add", "TEST-1", "1800", "2026-07-28T10:00:00", "--comment", "Fixed it",
        ])

    assert result.exit_code == 0
    mock_add.assert_awaited_once_with("TEST-1", 1800, "2026-07-28T10:00:00", comment="Fixed it")


def test_jira_worklog_add_debug_and_traceback_flags_recognized():
    from icx_engine.jira.cli_commands import jira_app
    runner = typer.testing.CliRunner()
    result = runner.invoke(jira_app, ["worklog", "add", "--help"])
    _assert_flags_recognized(result)


def test_jira_worklog_add_exception_renders_through_render_icx_error():
    from icx_engine.jira.cli_commands import jira_app
    with patch("icx_engine.jira.cli_commands.service.add_worklog",
               new=AsyncMock(side_effect=RuntimeError("forced worklog add boom"))):
        runner = typer.testing.CliRunner()
        result = runner.invoke(jira_app, ["worklog", "add", "TEST-1", "3600", "2026-07-28T10:00:00"])

    _assert_renders_via_render_icx_error(result, "forced worklog add boom")


def test_jira_worklog_edit_no_fields_given_exits_nonzero():
    from icx_engine.jira.cli_commands import jira_app
    with patch("icx_engine.jira.cli_commands.service.edit_worklog",
               new=AsyncMock()) as mock_edit:
        runner = typer.testing.CliRunner()
        result = runner.invoke(jira_app, ["worklog", "edit", "TEST-1", "500"])

    assert result.exit_code != 0
    mock_edit.assert_not_awaited()


def test_jira_worklog_edit_self_worklog_no_confirmation_needed():
    from icx_engine.jira.cli_commands import jira_app
    with patch("icx_engine.jira.cli_commands.service.list_worklogs",
               new=AsyncMock(return_value={"issue_key": "TEST-1", "worklogs": [
                   {"id": "500", "author": {"accountId": "acc-self"}},
               ]})), \
         patch("icx_engine.jira.cli_commands.service.get_current_user",
               new=AsyncMock(return_value={"accountId": "acc-self"})), \
         patch("icx_engine.jira.cli_commands.service.edit_worklog",
               new=AsyncMock(return_value={"ok": True})) as mock_edit, \
         patch("typer.confirm") as confirm_mock:
        runner = typer.testing.CliRunner()
        result = runner.invoke(jira_app, [
            "worklog", "edit", "TEST-1", "500", "--time-spent-seconds", "7200",
        ])

    assert result.exit_code == 0
    mock_edit.assert_awaited_once_with(
        "TEST-1", "500", time_spent_seconds=7200, started=None, comment=None,
    )
    confirm_mock.assert_not_called()


def test_jira_worklog_edit_other_worklog_shows_warning_and_confirms():
    from icx_engine.jira.cli_commands import jira_app
    confirm_mock = Mock(side_effect=[True])
    with patch("icx_engine.jira.cli_commands.service.list_worklogs",
               new=AsyncMock(return_value={"issue_key": "TEST-1", "worklogs": [
                   {"id": "500", "author": {"accountId": "acc-other"}},
               ]})), \
         patch("icx_engine.jira.cli_commands.service.get_current_user",
               new=AsyncMock(return_value={"accountId": "acc-self"})), \
         patch("icx_engine.jira.cli_commands.service.edit_worklog",
               new=AsyncMock(return_value={"ok": True})) as mock_edit, \
         patch("typer.confirm", confirm_mock):
        runner = typer.testing.CliRunner()
        result = runner.invoke(jira_app, [
            "worklog", "edit", "TEST-1", "500", "--time-spent-seconds", "7200",
        ])

    assert result.exit_code == 0
    out = result.stdout.lower()
    assert "different jira user" in out
    mock_edit.assert_awaited_once_with(
        "TEST-1", "500", time_spent_seconds=7200, started=None, comment=None,
    )


def test_jira_worklog_edit_other_worklog_declined_does_nothing():
    from icx_engine.jira.cli_commands import jira_app
    confirm_mock = Mock(side_effect=[False])
    with patch("icx_engine.jira.cli_commands.service.list_worklogs",
               new=AsyncMock(return_value={"issue_key": "TEST-1", "worklogs": [
                   {"id": "500", "author": {"accountId": "acc-other"}},
               ]})), \
         patch("icx_engine.jira.cli_commands.service.get_current_user",
               new=AsyncMock(return_value={"accountId": "acc-self"})), \
         patch("icx_engine.jira.cli_commands.service.edit_worklog",
               new=AsyncMock()) as mock_edit, \
         patch("typer.confirm", confirm_mock):
        runner = typer.testing.CliRunner()
        result = runner.invoke(jira_app, [
            "worklog", "edit", "TEST-1", "500", "--time-spent-seconds", "7200",
        ])

    assert result.exit_code == 0
    assert "cancelled" in result.stdout.lower()
    mock_edit.assert_not_awaited()


def test_jira_worklog_edit_debug_and_traceback_flags_recognized():
    from icx_engine.jira.cli_commands import jira_app
    runner = typer.testing.CliRunner()
    result = runner.invoke(jira_app, ["worklog", "edit", "--help"])
    _assert_flags_recognized(result)


def test_jira_worklog_edit_exception_renders_through_render_icx_error():
    from icx_engine.jira.cli_commands import jira_app
    with patch("icx_engine.jira.cli_commands.service.list_worklogs",
               new=AsyncMock(side_effect=RuntimeError("forced worklog edit boom"))):
        runner = typer.testing.CliRunner()
        result = runner.invoke(jira_app, [
            "worklog", "edit", "TEST-1", "500", "--time-spent-seconds", "100",
        ])

    _assert_renders_via_render_icx_error(result, "forced worklog edit boom")


def test_jira_worklog_delete_self_worklog_no_confirmation_needed():
    from icx_engine.jira.cli_commands import jira_app
    with patch("icx_engine.jira.cli_commands.service.list_worklogs",
               new=AsyncMock(return_value={"issue_key": "TEST-1", "worklogs": [
                   {"id": "500", "author": {"accountId": "acc-self"}},
               ]})), \
         patch("icx_engine.jira.cli_commands.service.get_current_user",
               new=AsyncMock(return_value={"accountId": "acc-self"})), \
         patch("icx_engine.jira.cli_commands.service.delete_worklog",
               new=AsyncMock(return_value={"ok": True})) as mock_delete, \
         patch("typer.confirm") as confirm_mock:
        runner = typer.testing.CliRunner()
        result = runner.invoke(jira_app, ["worklog", "delete", "TEST-1", "500"])

    assert result.exit_code == 0
    mock_delete.assert_awaited_once_with("TEST-1", "500")
    confirm_mock.assert_not_called()


def test_jira_worklog_delete_other_worklog_shows_warning_and_confirms():
    from icx_engine.jira.cli_commands import jira_app
    confirm_mock = Mock(side_effect=[True])
    with patch("icx_engine.jira.cli_commands.service.list_worklogs",
               new=AsyncMock(return_value={"issue_key": "TEST-1", "worklogs": [
                   {"id": "500", "author": {"accountId": "acc-other"}},
               ]})), \
         patch("icx_engine.jira.cli_commands.service.get_current_user",
               new=AsyncMock(return_value={"accountId": "acc-self"})), \
         patch("icx_engine.jira.cli_commands.service.delete_worklog",
               new=AsyncMock(return_value={"ok": True})) as mock_delete, \
         patch("typer.confirm", confirm_mock):
        runner = typer.testing.CliRunner()
        result = runner.invoke(jira_app, ["worklog", "delete", "TEST-1", "500"])

    assert result.exit_code == 0
    out = result.stdout.lower()
    assert "different jira user" in out
    mock_delete.assert_awaited_once_with("TEST-1", "500")


def test_jira_worklog_delete_other_worklog_declined_does_nothing():
    from icx_engine.jira.cli_commands import jira_app
    confirm_mock = Mock(side_effect=[False])
    with patch("icx_engine.jira.cli_commands.service.list_worklogs",
               new=AsyncMock(return_value={"issue_key": "TEST-1", "worklogs": [
                   {"id": "500", "author": {"accountId": "acc-other"}},
               ]})), \
         patch("icx_engine.jira.cli_commands.service.get_current_user",
               new=AsyncMock(return_value={"accountId": "acc-self"})), \
         patch("icx_engine.jira.cli_commands.service.delete_worklog",
               new=AsyncMock()) as mock_delete, \
         patch("typer.confirm", confirm_mock):
        runner = typer.testing.CliRunner()
        result = runner.invoke(jira_app, ["worklog", "delete", "TEST-1", "500"])

    assert result.exit_code == 0
    assert "cancelled" in result.stdout.lower()
    mock_delete.assert_not_awaited()


def test_jira_worklog_delete_debug_and_traceback_flags_recognized():
    from icx_engine.jira.cli_commands import jira_app
    runner = typer.testing.CliRunner()
    result = runner.invoke(jira_app, ["worklog", "delete", "--help"])
    _assert_flags_recognized(result)


def test_jira_worklog_delete_exception_renders_through_render_icx_error():
    from icx_engine.jira.cli_commands import jira_app
    with patch("icx_engine.jira.cli_commands.service.list_worklogs",
               new=AsyncMock(side_effect=RuntimeError("forced worklog delete boom"))):
        runner = typer.testing.CliRunner()
        result = runner.invoke(jira_app, ["worklog", "delete", "TEST-1", "500"])

    _assert_renders_via_render_icx_error(result, "forced worklog delete boom")
