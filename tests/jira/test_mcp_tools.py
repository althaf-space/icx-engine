from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

from icx_engine.exceptions import NoConnectionError


async def test_dispatch_jira_tool_returns_none_for_unknown_tool():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    assert await dispatch_jira_tool("sonar_status", {}) is None


# -- jira_get_close_requirements ---------------------------------------------

async def test_jira_get_close_requirements_missing_issue_key_returns_named_error():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    result = await dispatch_jira_tool("jira_get_close_requirements", {})
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert "issue_key" in payload["error"]
    assert payload["error"] != "'issue_key'"


async def test_jira_get_close_requirements_returns_service_dict_on_success():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    service_result = {
        "issue_key": "TEST-1",
        "transitions": [{"id": "31", "name": "Done"}],
        "editable_fields": {"summary": {"required": True}},
    }
    with patch("icx_engine.jira.mcp_tools.service.get_close_requirements",
               new=AsyncMock(return_value=service_result)):
        result = await dispatch_jira_tool("jira_get_close_requirements", {"issue_key": "TEST-1"})
    payload = json.loads(result[0].text)
    assert payload["ok"] is True
    assert payload["issue_key"] == "TEST-1"
    assert payload["transitions"] == [{"id": "31", "name": "Done"}]
    assert payload["editable_fields"] == {"summary": {"required": True}}


async def test_jira_get_close_requirements_defaults_include_allowed_values_true():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    with patch("icx_engine.jira.mcp_tools.service.get_close_requirements",
               new=AsyncMock(return_value={"issue_key": "TEST-1", "transitions": [], "editable_fields": {}})) as mock_call:
        await dispatch_jira_tool("jira_get_close_requirements", {"issue_key": "TEST-1"})
    mock_call.assert_awaited_once_with("TEST-1", include_allowed_values=True, since_status=None)


async def test_jira_get_close_requirements_passes_include_allowed_values_false():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    with patch("icx_engine.jira.mcp_tools.service.get_close_requirements",
               new=AsyncMock(return_value={"issue_key": "TEST-1", "transitions": [], "editable_fields": {}})) as mock_call:
        await dispatch_jira_tool("jira_get_close_requirements", {
            "issue_key": "TEST-1", "include_allowed_values": False,
        })
    mock_call.assert_awaited_once_with("TEST-1", include_allowed_values=False, since_status=None)


async def test_jira_get_close_requirements_passes_since_status_through():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    with patch("icx_engine.jira.mcp_tools.service.get_close_requirements",
               new=AsyncMock(return_value={"issue_key": "TEST-1", "status": "Done", "unchanged": True})) as mock_call:
        result = await dispatch_jira_tool("jira_get_close_requirements", {
            "issue_key": "TEST-1", "since_status": "Done",
        })
    mock_call.assert_awaited_once_with("TEST-1", include_allowed_values=True, since_status="Done")
    payload = json.loads(result[0].text)
    assert payload["unchanged"] is True


async def test_jira_get_close_requirements_service_error_surfaces_as_ok_false():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    with patch("icx_engine.jira.mcp_tools.service.get_close_requirements",
               new=AsyncMock(side_effect=NoConnectionError("No Jira connection configured."))):
        result = await dispatch_jira_tool("jira_get_close_requirements", {"issue_key": "TEST-1"})
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert payload["error"] == "No Jira connection configured."


# -- jira_apply_update: confirmation gating ----------------------------------

async def test_jira_apply_update_missing_issue_key_returns_named_error():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    result = await dispatch_jira_tool("jira_apply_update", {"transition_id": "31"})
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert "issue_key" in payload["error"]
    assert payload["error"] != "'issue_key'"


async def test_jira_apply_update_returns_pending_confirmation_without_token():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    result = await dispatch_jira_tool("jira_apply_update", {
        "issue_key": "TEST-1", "transition_id": "31", "fields": {"summary": "x"},
        "comment": "Closing this out",
    })
    payload = json.loads(result[0].text)
    assert payload["status"] == "pending_confirmation"
    assert "token" in payload


async def test_jira_apply_update_pending_confirmation_echoes_given_arguments():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    result = await dispatch_jira_tool("jira_apply_update", {
        "issue_key": "TEST-1", "transition_id": "31", "fields": {"summary": "x"},
        "comment": "Closing this out",
    })
    payload = json.loads(result[0].text)
    assert payload["issue_key"] == "TEST-1"
    assert payload["transition_id"] == "31"
    assert payload["fields"] == {"summary": "x"}
    assert payload["comment"] == "Closing this out"


async def test_jira_apply_update_executes_with_valid_token():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    service_result = {
        "ok": True, "issue_key": "TEST-1", "transition_id": "31",
        "fields": {"summary": "x"}, "comment": "Closing this out",
    }
    first = await dispatch_jira_tool("jira_apply_update", {
        "issue_key": "TEST-1", "transition_id": "31", "fields": {"summary": "x"},
        "comment": "Closing this out",
    })
    token = json.loads(first[0].text)["token"]
    with patch("icx_engine.jira.mcp_tools.service.apply_update",
               new=AsyncMock(return_value=service_result)) as mock_apply:
        second = await dispatch_jira_tool("jira_apply_update", {
            "issue_key": "TEST-1", "transition_id": "31", "fields": {"summary": "x"},
            "comment": "Closing this out", "confirm_token": token,
        })
    payload = json.loads(second[0].text)
    assert payload["ok"] is True
    mock_apply.assert_awaited_once_with(
        "TEST-1", transition_id="31", fields={"summary": "x"}, comment="Closing this out",
    )


async def test_jira_apply_update_executes_from_first_call_payload_not_second_call_args():
    # Regression guard mirroring git_stage_and_commit's contract: the second
    # call's own arguments must never be trusted for the actual mutation -
    # only the payload stored under the token at issue time.
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    first = await dispatch_jira_tool("jira_apply_update", {
        "issue_key": "TEST-1", "transition_id": "31",
    })
    token = json.loads(first[0].text)["token"]
    with patch("icx_engine.jira.mcp_tools.service.apply_update",
               new=AsyncMock(return_value={"ok": True})) as mock_apply:
        await dispatch_jira_tool("jira_apply_update", {
            "issue_key": "SOMETHING-ELSE", "transition_id": "999", "confirm_token": token,
        })
    mock_apply.assert_awaited_once_with(
        "TEST-1", transition_id="31", fields=None, comment=None,
    )


async def test_jira_apply_update_invalid_token_returns_error():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    result = await dispatch_jira_tool("jira_apply_update", {
        "issue_key": "TEST-1", "confirm_token": "bogus",
    })
    payload = json.loads(result[0].text)
    assert payload["ok"] is False


async def test_jira_apply_update_reused_token_returns_error():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    first = await dispatch_jira_tool("jira_apply_update", {
        "issue_key": "TEST-1", "transition_id": "31",
    })
    token = json.loads(first[0].text)["token"]
    with patch("icx_engine.jira.mcp_tools.service.apply_update",
               new=AsyncMock(return_value={"ok": True})):
        await dispatch_jira_tool("jira_apply_update", {
            "issue_key": "TEST-1", "transition_id": "31", "confirm_token": token,
        })
        reused = await dispatch_jira_tool("jira_apply_update", {
            "issue_key": "TEST-1", "transition_id": "31", "confirm_token": token,
        })
    payload = json.loads(reused[0].text)
    assert payload["ok"] is False


async def test_jira_apply_update_needs_fields_response_passed_through():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    service_result = {
        "ok": False, "needs_fields": {"resolution": "Resolution is required."},
        "message": "bad",
    }
    first = await dispatch_jira_tool("jira_apply_update", {
        "issue_key": "TEST-1", "transition_id": "31",
    })
    token = json.loads(first[0].text)["token"]
    with patch("icx_engine.jira.mcp_tools.service.apply_update",
               new=AsyncMock(return_value=service_result)):
        second = await dispatch_jira_tool("jira_apply_update", {
            "issue_key": "TEST-1", "transition_id": "31", "confirm_token": token,
        })
    payload = json.loads(second[0].text)
    assert payload["ok"] is False
    assert payload["needs_fields"] == {"resolution": "Resolution is required."}


async def test_jira_apply_update_value_error_surfaces_as_ok_false():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    first = await dispatch_jira_tool("jira_apply_update", {
        "issue_key": "TEST-1", "transition_id": "31",
    })
    token = json.loads(first[0].text)["token"]
    with patch("icx_engine.jira.mcp_tools.service.apply_update",
               new=AsyncMock(side_effect=ValueError("Nothing to update - provide a transition_id and/or fields."))):
        second = await dispatch_jira_tool("jira_apply_update", {
            "issue_key": "TEST-1", "transition_id": "31", "confirm_token": token,
        })
    payload = json.loads(second[0].text)
    assert payload["ok"] is False
    assert payload["error"] == "Nothing to update - provide a transition_id and/or fields."


async def test_jira_apply_update_no_connection_error_surfaces_as_ok_false():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    first = await dispatch_jira_tool("jira_apply_update", {
        "issue_key": "TEST-1", "transition_id": "31",
    })
    token = json.loads(first[0].text)["token"]
    with patch("icx_engine.jira.mcp_tools.service.apply_update",
               new=AsyncMock(side_effect=NoConnectionError("No Jira connection configured."))):
        second = await dispatch_jira_tool("jira_apply_update", {
            "issue_key": "TEST-1", "transition_id": "31", "confirm_token": token,
        })
    payload = json.loads(second[0].text)
    assert payload["ok"] is False
    assert payload["error"] == "No Jira connection configured."


# -- jira_list_issue_types: discovery lookup for jira_create_issue -----------

async def test_jira_list_issue_types_missing_project_returns_named_error():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    result = await dispatch_jira_tool("jira_list_issue_types", {})
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert "project" in payload["error"]


async def test_jira_list_issue_types_ungated_executes_immediately():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    service_result = [{"id": "10001", "name": "Bug"}]
    with patch("icx_engine.jira.mcp_tools.service.list_issue_types",
               new=AsyncMock(return_value=service_result)) as mock_call:
        result = await dispatch_jira_tool("jira_list_issue_types", {"project": "ABC"})
    payload = json.loads(result[0].text)
    assert payload["ok"] is True
    assert payload["issue_types"] == [{"id": "10001", "name": "Bug"}]
    mock_call.assert_awaited_once_with("ABC", domain=None)


async def test_jira_list_issue_types_passes_domain_through():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    with patch("icx_engine.jira.mcp_tools.service.list_issue_types",
               new=AsyncMock(return_value=[])) as mock_call:
        await dispatch_jira_tool("jira_list_issue_types", {"project": "ABC", "domain": "test.atlassian.net"})
    mock_call.assert_awaited_once_with("ABC", domain="test.atlassian.net")


async def test_jira_list_issue_types_no_connection_error_surfaces_as_ok_false():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    with patch("icx_engine.jira.mcp_tools.service.list_issue_types",
               new=AsyncMock(side_effect=NoConnectionError("No Jira connection configured."))):
        result = await dispatch_jira_tool("jira_list_issue_types", {"project": "ABC"})
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert payload["error"] == "No Jira connection configured."


# -- jira_get_createmeta_fields: discovery lookup for jira_create_issue ------

async def test_jira_get_createmeta_fields_missing_project_returns_named_error():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    result = await dispatch_jira_tool("jira_get_createmeta_fields", {"issuetype_id": "10001"})
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert "project" in payload["error"]


async def test_jira_get_createmeta_fields_missing_issuetype_id_returns_named_error():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    result = await dispatch_jira_tool("jira_get_createmeta_fields", {"project": "ABC"})
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert "issuetype_id" in payload["error"]


async def test_jira_get_createmeta_fields_ungated_executes_immediately():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    service_result = {"customfield_10050": {"required": True, "schema": {"type": "option"}}}
    with patch("icx_engine.jira.mcp_tools.service.get_createmeta_fields",
               new=AsyncMock(return_value=service_result)) as mock_call:
        result = await dispatch_jira_tool("jira_get_createmeta_fields", {"project": "ABC", "issuetype_id": "10001"})
    payload = json.loads(result[0].text)
    assert payload["ok"] is True
    assert payload["fields"] == service_result
    mock_call.assert_awaited_once_with("ABC", "10001", domain=None)


async def test_jira_get_createmeta_fields_no_connection_error_surfaces_as_ok_false():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    with patch("icx_engine.jira.mcp_tools.service.get_createmeta_fields",
               new=AsyncMock(side_effect=NoConnectionError("No Jira connection configured."))):
        result = await dispatch_jira_tool("jira_get_createmeta_fields", {"project": "ABC", "issuetype_id": "10001"})
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert payload["error"] == "No Jira connection configured."


def test_jira_get_createmeta_fields_tool_description_warns_it_is_best_effort_and_names_fallback():
    """Locks in the CCBSS incident fix: createmeta can return completely empty on some
    Jira projects (a Jira Cloud gap, not a pagination bug) - the description must say so
    and point at jira_get_close_requirements as the reliable fallback, not let the agent
    silently guess a field key/value shape again."""
    from icx_engine.jira.mcp_tools import JIRA_TOOLS
    tool = next(t for t in JIRA_TOOLS if t.name == "jira_get_createmeta_fields")
    description = tool.description.lower()
    assert "best-effort" in description
    assert "empty" in description
    assert "jira_get_close_requirements" in description


# -- jira_create_issue: required-field validation ----------------------------

async def test_jira_create_issue_missing_project_returns_named_error():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    result = await dispatch_jira_tool("jira_create_issue", {"issuetype": "Bug", "summary": "x"})
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert "project" in payload["error"]
    assert payload["error"] != "'project'"


async def test_jira_create_issue_missing_issuetype_returns_named_error():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    result = await dispatch_jira_tool("jira_create_issue", {"project": "ABC", "summary": "x"})
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert "issuetype" in payload["error"]


async def test_jira_create_issue_missing_summary_returns_named_error():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    result = await dispatch_jira_tool("jira_create_issue", {"project": "ABC", "issuetype": "Bug"})
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert "summary" in payload["error"]


# -- jira_create_issue: confirmation gating -----------------------------------

async def test_jira_create_issue_returns_pending_confirmation_without_token():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    result = await dispatch_jira_tool("jira_create_issue", {
        "project": "ABC", "issuetype": "Bug", "summary": "Something is broken",
    })
    payload = json.loads(result[0].text)
    assert payload["status"] == "pending_confirmation"
    assert "token" in payload
    assert payload["project"] == "ABC"
    assert payload["issuetype"] == "Bug"
    assert payload["summary"] == "Something is broken"


async def test_jira_create_issue_executes_with_valid_token():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    service_result = {
        "ok": True, "issue_key": "ABC-42", "project": "ABC", "issuetype": "Bug",
        "summary": "Something is broken", "fields": {},
    }
    first = await dispatch_jira_tool("jira_create_issue", {
        "project": "ABC", "issuetype": "Bug", "summary": "Something is broken",
    })
    token = json.loads(first[0].text)["token"]
    with patch("icx_engine.jira.mcp_tools.service.create_issue",
               new=AsyncMock(return_value=service_result)) as mock_create:
        second = await dispatch_jira_tool("jira_create_issue", {
            "project": "ABC", "issuetype": "Bug", "summary": "Something is broken",
            "confirm_token": token,
        })
    payload = json.loads(second[0].text)
    assert payload["ok"] is True
    assert payload["issue_key"] == "ABC-42"
    mock_create.assert_awaited_once_with(
        "ABC", "Bug", "Something is broken", fields=None, domain=None,
    )


async def test_jira_create_issue_executes_from_first_call_payload_not_second_call_args():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    first = await dispatch_jira_tool("jira_create_issue", {
        "project": "ABC", "issuetype": "Bug", "summary": "Original summary",
    })
    token = json.loads(first[0].text)["token"]
    with patch("icx_engine.jira.mcp_tools.service.create_issue",
               new=AsyncMock(return_value={"ok": True, "issue_key": "ABC-1"})) as mock_create:
        await dispatch_jira_tool("jira_create_issue", {
            "project": "OTHER", "issuetype": "Task", "summary": "Different summary",
            "confirm_token": token,
        })
    mock_create.assert_awaited_once_with(
        "ABC", "Bug", "Original summary", fields=None, domain=None,
    )


async def test_jira_create_issue_invalid_token_returns_error():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    result = await dispatch_jira_tool("jira_create_issue", {
        "project": "ABC", "issuetype": "Bug", "summary": "x", "confirm_token": "bogus",
    })
    payload = json.loads(result[0].text)
    assert payload["ok"] is False


async def test_jira_create_issue_reused_token_returns_error():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    first = await dispatch_jira_tool("jira_create_issue", {
        "project": "ABC", "issuetype": "Bug", "summary": "x",
    })
    token = json.loads(first[0].text)["token"]
    with patch("icx_engine.jira.mcp_tools.service.create_issue",
               new=AsyncMock(return_value={"ok": True, "issue_key": "ABC-1"})):
        await dispatch_jira_tool("jira_create_issue", {
            "project": "ABC", "issuetype": "Bug", "summary": "x", "confirm_token": token,
        })
        reused = await dispatch_jira_tool("jira_create_issue", {
            "project": "ABC", "issuetype": "Bug", "summary": "x", "confirm_token": token,
        })
    payload = json.loads(reused[0].text)
    assert payload["ok"] is False


async def test_jira_create_issue_no_connection_error_surfaces_as_ok_false():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    first = await dispatch_jira_tool("jira_create_issue", {
        "project": "ABC", "issuetype": "Bug", "summary": "x",
    })
    token = json.loads(first[0].text)["token"]
    with patch("icx_engine.jira.mcp_tools.service.create_issue",
               new=AsyncMock(side_effect=NoConnectionError("No Jira connection configured."))):
        second = await dispatch_jira_tool("jira_create_issue", {
            "project": "ABC", "issuetype": "Bug", "summary": "x", "confirm_token": token,
        })
    payload = json.loads(second[0].text)
    assert payload["ok"] is False
    assert payload["error"] == "No Jira connection configured."


def test_jira_create_issue_tool_description_names_reliable_fallback_when_createmeta_empty():
    from icx_engine.jira.mcp_tools import JIRA_TOOLS
    tool = next(t for t in JIRA_TOOLS if t.name == "jira_create_issue")
    description = tool.description.lower()
    assert "unreliable" in description
    assert "jira_get_close_requirements" in description
    assert "never send a guessed field key" in description


# -- jira_delete_issue: required-field validation + explicit warning ---------

async def test_jira_delete_issue_missing_issue_key_returns_named_error():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    result = await dispatch_jira_tool("jira_delete_issue", {})
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert "issue_key" in payload["error"]
    assert payload["error"] != "'issue_key'"


def test_jira_delete_issue_tool_description_contains_explicit_warning():
    from icx_engine.jira.mcp_tools import JIRA_TOOLS
    tool = next(t for t in JIRA_TOOLS if t.name == "jira_delete_issue")
    description = tool.description.lower()
    assert "permanent" in description
    assert "no undo" in description
    assert "no trash" in description
    assert "recycle bin" in description


# -- jira_delete_issue: confirmation gating -----------------------------------

async def test_jira_delete_issue_returns_pending_confirmation_without_token():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    result = await dispatch_jira_tool("jira_delete_issue", {"issue_key": "TEST-1"})
    payload = json.loads(result[0].text)
    assert payload["status"] == "pending_confirmation"
    assert "token" in payload
    assert payload["issue_key"] == "TEST-1"
    assert payload["delete_subtasks"] is False


async def test_jira_delete_issue_executes_with_valid_token():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    service_result = {"ok": True, "issue_key": "TEST-1", "deleted": True, "delete_subtasks": False}
    first = await dispatch_jira_tool("jira_delete_issue", {"issue_key": "TEST-1"})
    token = json.loads(first[0].text)["token"]
    with patch("icx_engine.jira.mcp_tools.service.delete_issue",
               new=AsyncMock(return_value=service_result)) as mock_delete:
        second = await dispatch_jira_tool("jira_delete_issue", {
            "issue_key": "TEST-1", "confirm_token": token,
        })
    payload = json.loads(second[0].text)
    assert payload["ok"] is True
    mock_delete.assert_awaited_once_with("TEST-1", delete_subtasks=False)


async def test_jira_delete_issue_passes_delete_subtasks_through_token_payload():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    first = await dispatch_jira_tool("jira_delete_issue", {
        "issue_key": "TEST-1", "delete_subtasks": True,
    })
    token = json.loads(first[0].text)["token"]
    with patch("icx_engine.jira.mcp_tools.service.delete_issue",
               new=AsyncMock(return_value={"ok": True})) as mock_delete:
        await dispatch_jira_tool("jira_delete_issue", {
            "issue_key": "TEST-1", "confirm_token": token,
        })
    mock_delete.assert_awaited_once_with("TEST-1", delete_subtasks=True)


async def test_jira_delete_issue_executes_from_first_call_payload_not_second_call_args():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    first = await dispatch_jira_tool("jira_delete_issue", {"issue_key": "TEST-1", "delete_subtasks": True})
    token = json.loads(first[0].text)["token"]
    with patch("icx_engine.jira.mcp_tools.service.delete_issue",
               new=AsyncMock(return_value={"ok": True})) as mock_delete:
        await dispatch_jira_tool("jira_delete_issue", {
            "issue_key": "SOMETHING-ELSE", "delete_subtasks": False, "confirm_token": token,
        })
    mock_delete.assert_awaited_once_with("TEST-1", delete_subtasks=True)


async def test_jira_delete_issue_invalid_token_returns_error():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    result = await dispatch_jira_tool("jira_delete_issue", {
        "issue_key": "TEST-1", "confirm_token": "bogus",
    })
    payload = json.loads(result[0].text)
    assert payload["ok"] is False


async def test_jira_delete_issue_reused_token_returns_error():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    first = await dispatch_jira_tool("jira_delete_issue", {"issue_key": "TEST-1"})
    token = json.loads(first[0].text)["token"]
    with patch("icx_engine.jira.mcp_tools.service.delete_issue",
               new=AsyncMock(return_value={"ok": True})):
        await dispatch_jira_tool("jira_delete_issue", {"issue_key": "TEST-1", "confirm_token": token})
        reused = await dispatch_jira_tool("jira_delete_issue", {"issue_key": "TEST-1", "confirm_token": token})
    payload = json.loads(reused[0].text)
    assert payload["ok"] is False


async def test_jira_delete_issue_no_connection_error_surfaces_as_ok_false():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    first = await dispatch_jira_tool("jira_delete_issue", {"issue_key": "TEST-1"})
    token = json.loads(first[0].text)["token"]
    with patch("icx_engine.jira.mcp_tools.service.delete_issue",
               new=AsyncMock(side_effect=NoConnectionError("No Jira connection configured."))):
        second = await dispatch_jira_tool("jira_delete_issue", {
            "issue_key": "TEST-1", "confirm_token": token,
        })
    payload = json.loads(second[0].text)
    assert payload["ok"] is False
    assert payload["error"] == "No Jira connection configured."


# -- jira_search: required-field validation + UNGATED execution -------------

async def test_jira_search_missing_jql_returns_named_error():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    result = await dispatch_jira_tool("jira_search", {})
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert "jql" in payload["error"]
    assert payload["error"] != "'jql'"


async def test_jira_search_ungated_executes_immediately_with_defaults():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    service_result = {
        "jql": "project = ABC", "issues": [{"key": "ABC-1"}],
        "next_page_token": None, "is_last": True,
    }
    with patch("icx_engine.jira.mcp_tools.service.search",
               new=AsyncMock(return_value=service_result)) as mock_search:
        result = await dispatch_jira_tool("jira_search", {"jql": "project = ABC"})
    payload = json.loads(result[0].text)
    assert payload["ok"] is True
    assert payload["issues"] == [{"key": "ABC-1"}]
    mock_search.assert_awaited_once_with(
        "project = ABC", fields=None, max_results=50, page_token=None, domain=None,
    )


async def test_jira_search_passes_optional_arguments_through():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    with patch("icx_engine.jira.mcp_tools.service.search",
               new=AsyncMock(return_value={"jql": "x", "issues": []})) as mock_search:
        await dispatch_jira_tool("jira_search", {
            "jql": "project = ABC", "fields": ["priority"], "max_results": 10,
            "page_token": "tok1", "domain": "test.atlassian.net",
        })
    mock_search.assert_awaited_once_with(
        "project = ABC", fields=["priority"], max_results=10,
        page_token="tok1", domain="test.atlassian.net",
    )


async def test_jira_search_no_connection_error_surfaces_as_ok_false():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    with patch("icx_engine.jira.mcp_tools.service.search",
               new=AsyncMock(side_effect=NoConnectionError("No Jira connection configured."))):
        result = await dispatch_jira_tool("jira_search", {"jql": "project = ABC"})
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert payload["error"] == "No Jira connection configured."


def test_jira_search_tool_description_distinguishes_from_analyze_tools():
    from icx_engine.jira.mcp_tools import JIRA_TOOLS
    tool = next(t for t in JIRA_TOOLS if t.name == "jira_search")
    description = tool.description.lower()
    assert "lightweight" in description
    assert "raw" in description
    assert "analyze_issue" in description


# -- jira_get_issue: required-field validation + UNGATED execution ----------

async def test_jira_get_issue_missing_issue_key_returns_named_error():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    result = await dispatch_jira_tool("jira_get_issue", {})
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert "issue_key" in payload["error"]
    assert payload["error"] != "'issue_key'"


async def test_jira_get_issue_ungated_executes_immediately():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    service_result = {"issue_key": "TEST-1", "raw": {"key": "TEST-1", "fields": {"summary": "x"}}}
    with patch("icx_engine.jira.mcp_tools.service.get_issue",
               new=AsyncMock(return_value=service_result)) as mock_get:
        result = await dispatch_jira_tool("jira_get_issue", {"issue_key": "TEST-1"})
    payload = json.loads(result[0].text)
    assert payload["ok"] is True
    assert payload["raw"]["key"] == "TEST-1"
    mock_get.assert_awaited_once_with("TEST-1", fields=None)


async def test_jira_get_issue_passes_fields_through():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    with patch("icx_engine.jira.mcp_tools.service.get_issue",
               new=AsyncMock(return_value={"issue_key": "TEST-1", "raw": {}})) as mock_get:
        await dispatch_jira_tool("jira_get_issue", {
            "issue_key": "TEST-1", "fields": ["summary", "status"],
        })
    mock_get.assert_awaited_once_with("TEST-1", fields=["summary", "status"])


async def test_jira_get_issue_no_connection_error_surfaces_as_ok_false():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    with patch("icx_engine.jira.mcp_tools.service.get_issue",
               new=AsyncMock(side_effect=NoConnectionError("No Jira connection configured."))):
        result = await dispatch_jira_tool("jira_get_issue", {"issue_key": "TEST-1"})
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert payload["error"] == "No Jira connection configured."


def test_jira_get_issue_tool_description_distinguishes_from_analyze_tools():
    from icx_engine.jira.mcp_tools import JIRA_TOOLS
    tool = next(t for t in JIRA_TOOLS if t.name == "jira_get_issue")
    description = tool.description.lower()
    assert "lightweight" in description
    assert "raw" in description
    assert "analyze_issue" in description


# -- jira_link_types: UNGATED, no required fields at all ---------------------

async def test_jira_link_types_ungated_executes_immediately_with_no_arguments():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    service_result = {"link_types": [{"id": "10000", "name": "Blocks"}]}
    with patch("icx_engine.jira.mcp_tools.service.link_types",
               new=AsyncMock(return_value=service_result)) as mock_link_types:
        result = await dispatch_jira_tool("jira_link_types", {})
    payload = json.loads(result[0].text)
    assert payload["ok"] is True
    assert payload["link_types"] == [{"id": "10000", "name": "Blocks"}]
    mock_link_types.assert_awaited_once_with(domain=None)


async def test_jira_link_types_passes_domain_through():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    with patch("icx_engine.jira.mcp_tools.service.link_types",
               new=AsyncMock(return_value={"link_types": []})) as mock_link_types:
        await dispatch_jira_tool("jira_link_types", {"domain": "test.atlassian.net"})
    mock_link_types.assert_awaited_once_with(domain="test.atlassian.net")


async def test_jira_link_types_no_connection_error_surfaces_as_ok_false():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    with patch("icx_engine.jira.mcp_tools.service.link_types",
               new=AsyncMock(side_effect=NoConnectionError("No Jira connection configured."))):
        result = await dispatch_jira_tool("jira_link_types", {})
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert payload["error"] == "No Jira connection configured."


# -- jira_link_create: required-field validation + UNGATED execution --------

async def test_jira_link_create_missing_link_type_name_returns_named_error():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    result = await dispatch_jira_tool("jira_link_create", {"inward_key": "ABC-1", "outward_key": "ABC-2"})
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert "link_type_name" in payload["error"]
    assert payload["error"] != "'link_type_name'"


async def test_jira_link_create_missing_inward_key_returns_named_error():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    result = await dispatch_jira_tool("jira_link_create", {"link_type_name": "Blocks", "outward_key": "ABC-2"})
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert "inward_key" in payload["error"]


async def test_jira_link_create_missing_outward_key_returns_named_error():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    result = await dispatch_jira_tool("jira_link_create", {"link_type_name": "Blocks", "inward_key": "ABC-1"})
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert "outward_key" in payload["error"]


async def test_jira_link_create_ungated_executes_immediately():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    service_result = {"ok": True, "link_type_name": "Blocks", "inward_key": "ABC-1", "outward_key": "ABC-2"}
    with patch("icx_engine.jira.mcp_tools.service.create_link",
               new=AsyncMock(return_value=service_result)) as mock_create:
        result = await dispatch_jira_tool("jira_link_create", {
            "link_type_name": "Blocks", "inward_key": "ABC-1", "outward_key": "ABC-2",
        })
    payload = json.loads(result[0].text)
    assert payload["ok"] is True
    mock_create.assert_awaited_once_with("Blocks", "ABC-1", "ABC-2")


async def test_jira_link_create_no_connection_error_surfaces_as_ok_false():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    with patch("icx_engine.jira.mcp_tools.service.create_link",
               new=AsyncMock(side_effect=NoConnectionError("No Jira connection configured."))):
        result = await dispatch_jira_tool("jira_link_create", {
            "link_type_name": "Blocks", "inward_key": "ABC-1", "outward_key": "ABC-2",
        })
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert payload["error"] == "No Jira connection configured."


# -- jira_link_delete: required-field validation + honest warning language --

async def test_jira_link_delete_missing_issue_key_returns_named_error():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    result = await dispatch_jira_tool("jira_link_delete", {"link_id": "10050"})
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert "issue_key" in payload["error"]
    assert payload["error"] != "'issue_key'"


async def test_jira_link_delete_missing_link_id_returns_named_error():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    result = await dispatch_jira_tool("jira_link_delete", {"issue_key": "TEST-1"})
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert "link_id" in payload["error"]


def test_jira_link_delete_tool_description_uses_honest_dependency_warning_not_false_permanence():
    from icx_engine.jira.mcp_tools import JIRA_TOOLS
    tool = next(t for t in JIRA_TOOLS if t.name == "jira_link_delete")
    description = tool.description.lower()
    assert "dependency" in description
    assert "recreate" in description
    assert "no trash" not in description
    assert "no recycle bin" not in description


async def test_jira_link_delete_returns_pending_confirmation_without_token():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    result = await dispatch_jira_tool("jira_link_delete", {"issue_key": "TEST-1", "link_id": "10050"})
    payload = json.loads(result[0].text)
    assert payload["status"] == "pending_confirmation"
    assert "token" in payload
    assert payload["issue_key"] == "TEST-1"
    assert payload["link_id"] == "10050"


async def test_jira_link_delete_executes_with_valid_token():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    service_result = {"ok": True, "issue_key": "TEST-1", "link_id": "10050", "deleted": True}
    first = await dispatch_jira_tool("jira_link_delete", {"issue_key": "TEST-1", "link_id": "10050"})
    token = json.loads(first[0].text)["token"]
    with patch("icx_engine.jira.mcp_tools.service.delete_link",
               new=AsyncMock(return_value=service_result)) as mock_delete:
        second = await dispatch_jira_tool("jira_link_delete", {
            "issue_key": "TEST-1", "link_id": "10050", "confirm_token": token,
        })
    payload = json.loads(second[0].text)
    assert payload["ok"] is True
    mock_delete.assert_awaited_once_with("TEST-1", "10050")


async def test_jira_link_delete_executes_from_first_call_payload_not_second_call_args():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    first = await dispatch_jira_tool("jira_link_delete", {"issue_key": "TEST-1", "link_id": "10050"})
    token = json.loads(first[0].text)["token"]
    with patch("icx_engine.jira.mcp_tools.service.delete_link",
               new=AsyncMock(return_value={"ok": True})) as mock_delete:
        await dispatch_jira_tool("jira_link_delete", {
            "issue_key": "OTHER-1", "link_id": "99999", "confirm_token": token,
        })
    mock_delete.assert_awaited_once_with("TEST-1", "10050")


async def test_jira_link_delete_invalid_token_returns_error():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    result = await dispatch_jira_tool("jira_link_delete", {
        "issue_key": "TEST-1", "link_id": "10050", "confirm_token": "bogus",
    })
    payload = json.loads(result[0].text)
    assert payload["ok"] is False


async def test_jira_link_delete_reused_token_returns_error():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    first = await dispatch_jira_tool("jira_link_delete", {"issue_key": "TEST-1", "link_id": "10050"})
    token = json.loads(first[0].text)["token"]
    with patch("icx_engine.jira.mcp_tools.service.delete_link",
               new=AsyncMock(return_value={"ok": True})):
        await dispatch_jira_tool("jira_link_delete", {
            "issue_key": "TEST-1", "link_id": "10050", "confirm_token": token,
        })
        reused = await dispatch_jira_tool("jira_link_delete", {
            "issue_key": "TEST-1", "link_id": "10050", "confirm_token": token,
        })
    payload = json.loads(reused[0].text)
    assert payload["ok"] is False


async def test_jira_link_delete_no_connection_error_surfaces_as_ok_false():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    first = await dispatch_jira_tool("jira_link_delete", {"issue_key": "TEST-1", "link_id": "10050"})
    token = json.loads(first[0].text)["token"]
    with patch("icx_engine.jira.mcp_tools.service.delete_link",
               new=AsyncMock(side_effect=NoConnectionError("No Jira connection configured."))):
        second = await dispatch_jira_tool("jira_link_delete", {
            "issue_key": "TEST-1", "link_id": "10050", "confirm_token": token,
        })
    payload = json.loads(second[0].text)
    assert payload["ok"] is False
    assert payload["error"] == "No Jira connection configured."


# -- jira_set_assignee: required-field validation + UNGATED execution -------

async def test_jira_set_assignee_missing_issue_key_returns_named_error():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    result = await dispatch_jira_tool("jira_set_assignee", {"account_id": "acc-1"})
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert "issue_key" in payload["error"]
    assert payload["error"] != "'issue_key'"


async def test_jira_set_assignee_ungated_executes_with_account_id():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    service_result = {"ok": True, "issue_key": "TEST-1", "account_id": "acc-1"}
    with patch("icx_engine.jira.mcp_tools.service.set_assignee",
               new=AsyncMock(return_value=service_result)) as mock_set:
        result = await dispatch_jira_tool("jira_set_assignee", {"issue_key": "TEST-1", "account_id": "acc-1"})
    payload = json.loads(result[0].text)
    assert payload["ok"] is True
    mock_set.assert_awaited_once_with("TEST-1", account_id="acc-1")


async def test_jira_set_assignee_omitted_account_id_unassigns():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    with patch("icx_engine.jira.mcp_tools.service.set_assignee",
               new=AsyncMock(return_value={"ok": True, "issue_key": "TEST-1", "account_id": None})) as mock_set:
        await dispatch_jira_tool("jira_set_assignee", {"issue_key": "TEST-1"})
    mock_set.assert_awaited_once_with("TEST-1", account_id=None)


async def test_jira_set_assignee_default_sentinel_passed_through():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    with patch("icx_engine.jira.mcp_tools.service.set_assignee",
               new=AsyncMock(return_value={"ok": True})) as mock_set:
        await dispatch_jira_tool("jira_set_assignee", {"issue_key": "TEST-1", "account_id": "-1"})
    mock_set.assert_awaited_once_with("TEST-1", account_id="-1")


async def test_jira_set_assignee_no_connection_error_surfaces_as_ok_false():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    with patch("icx_engine.jira.mcp_tools.service.set_assignee",
               new=AsyncMock(side_effect=NoConnectionError("No Jira connection configured."))):
        result = await dispatch_jira_tool("jira_set_assignee", {"issue_key": "TEST-1"})
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert payload["error"] == "No Jira connection configured."


# -- jira_search_assignable_users: discovery lookup for jira_set_assignee ----

async def test_jira_search_assignable_users_missing_issue_key_returns_named_error():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    result = await dispatch_jira_tool("jira_search_assignable_users", {})
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert "issue_key" in payload["error"]


async def test_jira_search_assignable_users_ungated_executes_immediately():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    service_result = [{"accountId": "acc-1", "displayName": "Jane Doe"}]
    with patch("icx_engine.jira.mcp_tools.service.search_assignable_users",
               new=AsyncMock(return_value=service_result)) as mock_call:
        result = await dispatch_jira_tool("jira_search_assignable_users", {"issue_key": "TEST-1"})
    payload = json.loads(result[0].text)
    assert payload["ok"] is True
    assert payload["users"] == service_result
    mock_call.assert_awaited_once_with("TEST-1", query="")


async def test_jira_search_assignable_users_passes_query_through():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    with patch("icx_engine.jira.mcp_tools.service.search_assignable_users",
               new=AsyncMock(return_value=[])) as mock_call:
        await dispatch_jira_tool("jira_search_assignable_users", {"issue_key": "TEST-1", "query": "jane"})
    mock_call.assert_awaited_once_with("TEST-1", query="jane")


async def test_jira_search_assignable_users_no_connection_error_surfaces_as_ok_false():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    with patch("icx_engine.jira.mcp_tools.service.search_assignable_users",
               new=AsyncMock(side_effect=NoConnectionError("No Jira connection configured."))):
        result = await dispatch_jira_tool("jira_search_assignable_users", {"issue_key": "TEST-1"})
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert payload["error"] == "No Jira connection configured."


# -- jira_attachment_upload: required-field validation + base64 round-trip --

async def test_jira_attachment_upload_missing_issue_key_returns_named_error():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    result = await dispatch_jira_tool("jira_attachment_upload", {
        "filename": "report.txt", "content_base64": "aGVsbG8=",
    })
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert "issue_key" in payload["error"]
    assert payload["error"] != "'issue_key'"


async def test_jira_attachment_upload_missing_filename_returns_named_error():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    result = await dispatch_jira_tool("jira_attachment_upload", {
        "issue_key": "TEST-1", "content_base64": "aGVsbG8=",
    })
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert "filename" in payload["error"]


async def test_jira_attachment_upload_missing_content_base64_returns_named_error():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    result = await dispatch_jira_tool("jira_attachment_upload", {
        "issue_key": "TEST-1", "filename": "report.txt",
    })
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert "content_base64" in payload["error"]


async def test_jira_attachment_upload_invalid_base64_returns_named_error():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    result = await dispatch_jira_tool("jira_attachment_upload", {
        "issue_key": "TEST-1", "filename": "report.txt", "content_base64": "not-valid-base64!!!",
    })
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert "base64" in payload["error"].lower()


async def test_jira_attachment_upload_ungated_decodes_base64_and_calls_service():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    import base64
    raw = b"hello world"
    encoded = base64.b64encode(raw).decode()
    service_result = {
        "ok": True, "issue_key": "TEST-1", "filename": "report.txt",
        "attachments": [{"id": "10100"}],
    }
    with patch("icx_engine.jira.mcp_tools.service.upload_attachment",
               new=AsyncMock(return_value=service_result)) as mock_upload:
        result = await dispatch_jira_tool("jira_attachment_upload", {
            "issue_key": "TEST-1", "filename": "report.txt", "content_base64": encoded,
        })
    payload = json.loads(result[0].text)
    assert payload["ok"] is True
    mock_upload.assert_awaited_once_with("TEST-1", "report.txt", raw, content_type=None)


async def test_jira_attachment_upload_passes_content_type_through():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    import base64
    encoded = base64.b64encode(b"\x89PNG").decode()
    with patch("icx_engine.jira.mcp_tools.service.upload_attachment",
               new=AsyncMock(return_value={"ok": True})) as mock_upload:
        await dispatch_jira_tool("jira_attachment_upload", {
            "issue_key": "TEST-1", "filename": "photo.png", "content_base64": encoded,
            "content_type": "image/png",
        })
    mock_upload.assert_awaited_once_with("TEST-1", "photo.png", b"\x89PNG", content_type="image/png")


async def test_jira_attachment_upload_no_connection_error_surfaces_as_ok_false():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    import base64
    encoded = base64.b64encode(b"x").decode()
    with patch("icx_engine.jira.mcp_tools.service.upload_attachment",
               new=AsyncMock(side_effect=NoConnectionError("No Jira connection configured."))):
        result = await dispatch_jira_tool("jira_attachment_upload", {
            "issue_key": "TEST-1", "filename": "x.txt", "content_base64": encoded,
        })
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert payload["error"] == "No Jira connection configured."


def test_jira_attachment_upload_tool_description_documents_base64_encoding():
    from icx_engine.jira.mcp_tools import JIRA_TOOLS
    tool = next(t for t in JIRA_TOOLS if t.name == "jira_attachment_upload")
    description = tool.description.lower()
    assert "base64" in description


def test_jira_attachment_upload_tool_description_documents_file_path():
    from icx_engine.jira.mcp_tools import JIRA_TOOLS
    tool = next(t for t in JIRA_TOOLS if t.name == "jira_attachment_upload")
    description = tool.description.lower()
    assert "file_path" in description


# -- jira_attachment_upload: file_path option (ICX reads the file directly) --

async def test_jira_attachment_upload_missing_both_file_path_and_content_base64_returns_named_error():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    result = await dispatch_jira_tool("jira_attachment_upload", {"issue_key": "TEST-1"})
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert "file_path" in payload["error"]
    assert "content_base64" in payload["error"]


async def test_jira_attachment_upload_both_file_path_and_content_base64_returns_named_error(tmp_path):
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    f = tmp_path / "report.txt"
    f.write_bytes(b"hello")
    result = await dispatch_jira_tool("jira_attachment_upload", {
        "issue_key": "TEST-1", "file_path": str(f), "content_base64": "aGVsbG8=",
    })
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert "exactly one" in payload["error"].lower()


async def test_jira_attachment_upload_nonexistent_file_path_returns_named_error():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    result = await dispatch_jira_tool("jira_attachment_upload", {
        "issue_key": "TEST-1", "file_path": "/definitely/does/not/exist.xlsx",
    })
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert "file_path" in payload["error"]


async def test_jira_attachment_upload_file_path_reads_real_bytes_and_derives_filename(tmp_path):
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    f = tmp_path / "report.xlsx"
    raw = b"\x50\x4b\x03\x04binary-excel-bytes"  # real bytes, not text
    f.write_bytes(raw)
    with patch("icx_engine.jira.mcp_tools.service.upload_attachment",
               new=AsyncMock(return_value={"ok": True})) as mock_upload:
        result = await dispatch_jira_tool("jira_attachment_upload", {
            "issue_key": "TEST-1", "file_path": str(f),
        })
    payload = json.loads(result[0].text)
    assert payload["ok"] is True
    # filename derived from the path itself, not passed explicitly
    mock_upload.assert_awaited_once_with("TEST-1", "report.xlsx", raw, content_type=None)


async def test_jira_attachment_upload_file_path_with_explicit_filename_override(tmp_path):
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    f = tmp_path / "on-disk-name.bin"
    f.write_bytes(b"data")
    with patch("icx_engine.jira.mcp_tools.service.upload_attachment",
               new=AsyncMock(return_value={"ok": True})) as mock_upload:
        await dispatch_jira_tool("jira_attachment_upload", {
            "issue_key": "TEST-1", "file_path": str(f), "filename": "renamed.bin",
        })
    mock_upload.assert_awaited_once_with("TEST-1", "renamed.bin", b"data", content_type=None)


# -- jira_attachment_delete: required-field validation + permanence warning -

async def test_jira_attachment_delete_missing_issue_key_returns_named_error():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    result = await dispatch_jira_tool("jira_attachment_delete", {"attachment_id": "10100"})
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert "issue_key" in payload["error"]
    assert payload["error"] != "'issue_key'"


async def test_jira_attachment_delete_missing_attachment_id_returns_named_error():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    result = await dispatch_jira_tool("jira_attachment_delete", {"issue_key": "TEST-1"})
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert "attachment_id" in payload["error"]


def test_jira_attachment_delete_tool_description_contains_explicit_warning():
    from icx_engine.jira.mcp_tools import JIRA_TOOLS
    tool = next(t for t in JIRA_TOOLS if t.name == "jira_attachment_delete")
    description = tool.description.lower()
    assert "permanent" in description
    assert "no undo" in description
    assert "no trash" in description
    assert "recycle bin" in description


async def test_jira_attachment_delete_returns_pending_confirmation_without_token():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    result = await dispatch_jira_tool("jira_attachment_delete", {
        "issue_key": "TEST-1", "attachment_id": "10100",
    })
    payload = json.loads(result[0].text)
    assert payload["status"] == "pending_confirmation"
    assert "token" in payload
    assert payload["issue_key"] == "TEST-1"
    assert payload["attachment_id"] == "10100"


async def test_jira_attachment_delete_executes_with_valid_token():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    service_result = {"ok": True, "issue_key": "TEST-1", "attachment_id": "10100", "deleted": True}
    first = await dispatch_jira_tool("jira_attachment_delete", {
        "issue_key": "TEST-1", "attachment_id": "10100",
    })
    token = json.loads(first[0].text)["token"]
    with patch("icx_engine.jira.mcp_tools.service.delete_attachment",
               new=AsyncMock(return_value=service_result)) as mock_delete:
        second = await dispatch_jira_tool("jira_attachment_delete", {
            "issue_key": "TEST-1", "attachment_id": "10100", "confirm_token": token,
        })
    payload = json.loads(second[0].text)
    assert payload["ok"] is True
    mock_delete.assert_awaited_once_with("TEST-1", "10100")


async def test_jira_attachment_delete_executes_from_first_call_payload_not_second_call_args():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    first = await dispatch_jira_tool("jira_attachment_delete", {
        "issue_key": "TEST-1", "attachment_id": "10100",
    })
    token = json.loads(first[0].text)["token"]
    with patch("icx_engine.jira.mcp_tools.service.delete_attachment",
               new=AsyncMock(return_value={"ok": True})) as mock_delete:
        await dispatch_jira_tool("jira_attachment_delete", {
            "issue_key": "OTHER-1", "attachment_id": "99999", "confirm_token": token,
        })
    mock_delete.assert_awaited_once_with("TEST-1", "10100")


async def test_jira_attachment_delete_invalid_token_returns_error():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    result = await dispatch_jira_tool("jira_attachment_delete", {
        "issue_key": "TEST-1", "attachment_id": "10100", "confirm_token": "bogus",
    })
    payload = json.loads(result[0].text)
    assert payload["ok"] is False


async def test_jira_attachment_delete_reused_token_returns_error():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    first = await dispatch_jira_tool("jira_attachment_delete", {
        "issue_key": "TEST-1", "attachment_id": "10100",
    })
    token = json.loads(first[0].text)["token"]
    with patch("icx_engine.jira.mcp_tools.service.delete_attachment",
               new=AsyncMock(return_value={"ok": True})):
        await dispatch_jira_tool("jira_attachment_delete", {
            "issue_key": "TEST-1", "attachment_id": "10100", "confirm_token": token,
        })
        reused = await dispatch_jira_tool("jira_attachment_delete", {
            "issue_key": "TEST-1", "attachment_id": "10100", "confirm_token": token,
        })
    payload = json.loads(reused[0].text)
    assert payload["ok"] is False


async def test_jira_attachment_delete_no_connection_error_surfaces_as_ok_false():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    first = await dispatch_jira_tool("jira_attachment_delete", {
        "issue_key": "TEST-1", "attachment_id": "10100",
    })
    token = json.loads(first[0].text)["token"]
    with patch("icx_engine.jira.mcp_tools.service.delete_attachment",
               new=AsyncMock(side_effect=NoConnectionError("No Jira connection configured."))):
        second = await dispatch_jira_tool("jira_attachment_delete", {
            "issue_key": "TEST-1", "attachment_id": "10100", "confirm_token": token,
        })
    payload = json.loads(second[0].text)
    assert payload["ok"] is False
    assert payload["error"] == "No Jira connection configured."


# -- Task 6: jira_get_current_user - UNGATED, no required args ---------------

async def test_jira_get_current_user_ungated_no_arguments_required():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    with patch("icx_engine.jira.mcp_tools.service.get_current_user",
               new=AsyncMock(return_value={"accountId": "acc-self", "displayName": "Me"})) as mock_get:
        result = await dispatch_jira_tool("jira_get_current_user", {})
    payload = json.loads(result[0].text)
    assert payload["ok"] is True
    assert payload["accountId"] == "acc-self"
    mock_get.assert_awaited_once_with(issue_key=None, domain=None)


async def test_jira_get_current_user_passes_issue_key_and_domain_through():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    with patch("icx_engine.jira.mcp_tools.service.get_current_user",
               new=AsyncMock(return_value={"accountId": "acc-self"})) as mock_get:
        await dispatch_jira_tool("jira_get_current_user", {
            "issue_key": "TEST-1", "domain": "test.atlassian.net",
        })
    mock_get.assert_awaited_once_with(issue_key="TEST-1", domain="test.atlassian.net")


async def test_jira_get_current_user_no_connection_error_surfaces_as_ok_false():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    with patch("icx_engine.jira.mcp_tools.service.get_current_user",
               new=AsyncMock(side_effect=NoConnectionError("No Jira connection configured."))):
        result = await dispatch_jira_tool("jira_get_current_user", {})
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert payload["error"] == "No Jira connection configured."


# -- Task 6: jira_list_watchers / jira_list_worklogs - required field + UNGATED

async def test_jira_list_watchers_missing_issue_key_returns_named_error():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    result = await dispatch_jira_tool("jira_list_watchers", {})
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert "issue_key" in payload["error"]
    assert payload["error"] != "'issue_key'"


async def test_jira_list_watchers_ungated_executes_immediately():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    service_result = {"issue_key": "TEST-1", "watchers": [{"accountId": "acc-1"}], "watch_count": 1}
    with patch("icx_engine.jira.mcp_tools.service.list_watchers",
               new=AsyncMock(return_value=service_result)) as mock_list:
        result = await dispatch_jira_tool("jira_list_watchers", {"issue_key": "TEST-1"})
    payload = json.loads(result[0].text)
    assert payload["ok"] is True
    assert payload["watch_count"] == 1
    mock_list.assert_awaited_once_with("TEST-1")


async def test_jira_list_watchers_no_connection_error_surfaces_as_ok_false():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    with patch("icx_engine.jira.mcp_tools.service.list_watchers",
               new=AsyncMock(side_effect=NoConnectionError("No Jira connection configured."))):
        result = await dispatch_jira_tool("jira_list_watchers", {"issue_key": "TEST-1"})
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert payload["error"] == "No Jira connection configured."


async def test_jira_list_worklogs_missing_issue_key_returns_named_error():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    result = await dispatch_jira_tool("jira_list_worklogs", {})
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert "issue_key" in payload["error"]
    assert payload["error"] != "'issue_key'"


async def test_jira_list_worklogs_ungated_executes_immediately():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    service_result = {"issue_key": "TEST-1", "worklogs": [{"id": "500"}]}
    with patch("icx_engine.jira.mcp_tools.service.list_worklogs",
               new=AsyncMock(return_value=service_result)) as mock_list:
        result = await dispatch_jira_tool("jira_list_worklogs", {"issue_key": "TEST-1"})
    payload = json.loads(result[0].text)
    assert payload["ok"] is True
    mock_list.assert_awaited_once_with("TEST-1")


async def test_jira_list_worklogs_no_connection_error_surfaces_as_ok_false():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    with patch("icx_engine.jira.mcp_tools.service.list_worklogs",
               new=AsyncMock(side_effect=NoConnectionError("No Jira connection configured."))):
        result = await dispatch_jira_tool("jira_list_worklogs", {"issue_key": "TEST-1"})
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert payload["error"] == "No Jira connection configured."


# -- Task 6: jira_set_watcher - required-field validation --------------------

async def test_jira_set_watcher_missing_issue_key_returns_named_error():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    result = await dispatch_jira_tool("jira_set_watcher", {"watching": True})
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert "issue_key" in payload["error"]
    assert payload["error"] != "'issue_key'"


async def test_jira_set_watcher_missing_watching_returns_named_error():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    result = await dispatch_jira_tool("jira_set_watcher", {"issue_key": "TEST-1"})
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert "watching" in payload["error"]


async def test_jira_set_watcher_non_bool_watching_returns_named_error():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    result = await dispatch_jira_tool("jira_set_watcher", {"issue_key": "TEST-1", "watching": "yes"})
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert "watching" in payload["error"]


async def test_jira_set_watcher_non_string_account_id_returns_named_error():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    result = await dispatch_jira_tool("jira_set_watcher", {
        "issue_key": "TEST-1", "watching": True, "account_id": 123,
    })
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert "account_id" in payload["error"]


# -- Task 6: jira_set_watcher - SELF branch: no token round-trip at all -----

async def test_jira_set_watcher_self_omitted_account_id_executes_immediately_add():
    """The real point of this task: targeting yourself never touches the
    confirm-token machinery at all - this is a genuinely distinct code path
    from the OTHER branch below, not just a documented distinction."""
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    with patch("icx_engine.jira.mcp_tools.service.get_current_user",
               new=AsyncMock(return_value={"accountId": "acc-self"})) as mock_me, \
         patch("icx_engine.jira.mcp_tools.service.add_watcher",
               new=AsyncMock(return_value={"ok": True, "issue_key": "TEST-1",
                                            "account_id": "acc-self", "watching": True})) as mock_add, \
         patch("icx_engine.jira.mcp_tools.service.remove_watcher", new=AsyncMock()) as mock_remove:
        result = await dispatch_jira_tool("jira_set_watcher", {"issue_key": "TEST-1", "watching": True})
    payload = json.loads(result[0].text)
    assert payload["ok"] is True
    assert "status" not in payload  # no pending_confirmation - executed immediately
    mock_me.assert_awaited_once_with(issue_key="TEST-1")
    mock_add.assert_awaited_once_with("TEST-1", "acc-self")
    mock_remove.assert_not_awaited()


async def test_jira_set_watcher_self_omitted_account_id_executes_immediately_remove():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    with patch("icx_engine.jira.mcp_tools.service.get_current_user",
               new=AsyncMock(return_value={"accountId": "acc-self"})), \
         patch("icx_engine.jira.mcp_tools.service.remove_watcher",
               new=AsyncMock(return_value={"ok": True})) as mock_remove, \
         patch("icx_engine.jira.mcp_tools.service.add_watcher", new=AsyncMock()) as mock_add:
        result = await dispatch_jira_tool("jira_set_watcher", {"issue_key": "TEST-1", "watching": False})
    payload = json.loads(result[0].text)
    assert payload["ok"] is True
    mock_remove.assert_awaited_once_with("TEST-1", "acc-self")
    mock_add.assert_not_awaited()


async def test_jira_set_watcher_explicit_account_id_matching_self_stays_ungated():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    with patch("icx_engine.jira.mcp_tools.service.get_current_user",
               new=AsyncMock(return_value={"accountId": "acc-self"})), \
         patch("icx_engine.jira.mcp_tools.service.add_watcher",
               new=AsyncMock(return_value={"ok": True})) as mock_add:
        result = await dispatch_jira_tool("jira_set_watcher", {
            "issue_key": "TEST-1", "watching": True, "account_id": "acc-self",
        })
    payload = json.loads(result[0].text)
    assert payload["ok"] is True
    assert "status" not in payload
    mock_add.assert_awaited_once_with("TEST-1", "acc-self")


# -- Task 6: jira_set_watcher - OTHER branch: full confirm-token round-trip -

async def test_jira_set_watcher_other_account_id_returns_pending_confirmation():
    """Genuinely distinct from the self branch: no service.add_watcher/
    remove_watcher call happens at all on this first call."""
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    with patch("icx_engine.jira.mcp_tools.service.get_current_user",
               new=AsyncMock(return_value={"accountId": "acc-self"})), \
         patch("icx_engine.jira.mcp_tools.service.add_watcher", new=AsyncMock()) as mock_add:
        result = await dispatch_jira_tool("jira_set_watcher", {
            "issue_key": "TEST-1", "watching": True, "account_id": "acc-other",
        })
    payload = json.loads(result[0].text)
    assert payload["status"] == "pending_confirmation"
    assert "token" in payload
    assert payload["issue_key"] == "TEST-1"
    assert payload["account_id"] == "acc-other"
    assert payload["watching"] is True
    mock_add.assert_not_awaited()


async def test_jira_set_watcher_other_executes_add_with_valid_token():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    with patch("icx_engine.jira.mcp_tools.service.get_current_user",
               new=AsyncMock(return_value={"accountId": "acc-self"})):
        first = await dispatch_jira_tool("jira_set_watcher", {
            "issue_key": "TEST-1", "watching": True, "account_id": "acc-other",
        })
    token = json.loads(first[0].text)["token"]
    with patch("icx_engine.jira.mcp_tools.service.add_watcher",
               new=AsyncMock(return_value={"ok": True, "issue_key": "TEST-1",
                                            "account_id": "acc-other", "watching": True})) as mock_add:
        second = await dispatch_jira_tool("jira_set_watcher", {
            "issue_key": "TEST-1", "watching": True, "account_id": "acc-other", "confirm_token": token,
        })
    payload = json.loads(second[0].text)
    assert payload["ok"] is True
    mock_add.assert_awaited_once_with("TEST-1", "acc-other")


async def test_jira_set_watcher_other_executes_remove_with_valid_token():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    with patch("icx_engine.jira.mcp_tools.service.get_current_user",
               new=AsyncMock(return_value={"accountId": "acc-self"})):
        first = await dispatch_jira_tool("jira_set_watcher", {
            "issue_key": "TEST-1", "watching": False, "account_id": "acc-other",
        })
    token = json.loads(first[0].text)["token"]
    with patch("icx_engine.jira.mcp_tools.service.remove_watcher",
               new=AsyncMock(return_value={"ok": True})) as mock_remove:
        second = await dispatch_jira_tool("jira_set_watcher", {
            "issue_key": "TEST-1", "watching": False, "account_id": "acc-other", "confirm_token": token,
        })
    payload = json.loads(second[0].text)
    assert payload["ok"] is True
    mock_remove.assert_awaited_once_with("TEST-1", "acc-other")


async def test_jira_set_watcher_other_executes_from_first_call_payload_not_second_call_args():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    with patch("icx_engine.jira.mcp_tools.service.get_current_user",
               new=AsyncMock(return_value={"accountId": "acc-self"})):
        first = await dispatch_jira_tool("jira_set_watcher", {
            "issue_key": "TEST-1", "watching": True, "account_id": "acc-other",
        })
    token = json.loads(first[0].text)["token"]
    with patch("icx_engine.jira.mcp_tools.service.add_watcher",
               new=AsyncMock(return_value={"ok": True})) as mock_add:
        await dispatch_jira_tool("jira_set_watcher", {
            "issue_key": "OTHER-9", "watching": False, "account_id": "someone-else",
            "confirm_token": token,
        })
    mock_add.assert_awaited_once_with("TEST-1", "acc-other")


async def test_jira_set_watcher_invalid_token_returns_error():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    result = await dispatch_jira_tool("jira_set_watcher", {
        "issue_key": "TEST-1", "watching": True, "confirm_token": "bogus",
    })
    payload = json.loads(result[0].text)
    assert payload["ok"] is False


async def test_jira_set_watcher_reused_token_returns_error():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    with patch("icx_engine.jira.mcp_tools.service.get_current_user",
               new=AsyncMock(return_value={"accountId": "acc-self"})):
        first = await dispatch_jira_tool("jira_set_watcher", {
            "issue_key": "TEST-1", "watching": True, "account_id": "acc-other",
        })
    token = json.loads(first[0].text)["token"]
    with patch("icx_engine.jira.mcp_tools.service.add_watcher",
               new=AsyncMock(return_value={"ok": True})):
        await dispatch_jira_tool("jira_set_watcher", {
            "issue_key": "TEST-1", "watching": True, "account_id": "acc-other", "confirm_token": token,
        })
        reused = await dispatch_jira_tool("jira_set_watcher", {
            "issue_key": "TEST-1", "watching": True, "account_id": "acc-other", "confirm_token": token,
        })
    payload = json.loads(reused[0].text)
    assert payload["ok"] is False


async def test_jira_set_watcher_no_connection_error_surfaces_as_ok_false():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    with patch("icx_engine.jira.mcp_tools.service.get_current_user",
               new=AsyncMock(side_effect=NoConnectionError("No Jira connection configured."))):
        result = await dispatch_jira_tool("jira_set_watcher", {"issue_key": "TEST-1", "watching": True})
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert payload["error"] == "No Jira connection configured."


# -- Task 6: jira_worklog_add - always UNGATED (no author-override in Jira) -

async def test_jira_worklog_add_missing_issue_key_returns_named_error():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    result = await dispatch_jira_tool("jira_worklog_add", {"time_spent_seconds": 3600, "started": "2026-07-28T10:00:00"})
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert "issue_key" in payload["error"]
    assert payload["error"] != "'issue_key'"


async def test_jira_worklog_add_missing_time_spent_seconds_returns_named_error():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    result = await dispatch_jira_tool("jira_worklog_add", {"issue_key": "TEST-1", "started": "2026-07-28T10:00:00"})
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert "time_spent_seconds" in payload["error"]


async def test_jira_worklog_add_missing_started_returns_named_error():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    result = await dispatch_jira_tool("jira_worklog_add", {"issue_key": "TEST-1", "time_spent_seconds": 3600})
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert "started" in payload["error"]


async def test_jira_worklog_add_ungated_executes_immediately_no_confirm_token_ever():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    service_result = {"ok": True, "issue_key": "TEST-1", "worklog": {"id": "500"}}
    with patch("icx_engine.jira.mcp_tools.service.add_worklog",
               new=AsyncMock(return_value=service_result)) as mock_add:
        result = await dispatch_jira_tool("jira_worklog_add", {
            "issue_key": "TEST-1", "time_spent_seconds": 3600, "started": "2026-07-28T10:00:00",
        })
    payload = json.loads(result[0].text)
    assert payload["ok"] is True
    assert "status" not in payload
    mock_add.assert_awaited_once_with("TEST-1", 3600, "2026-07-28T10:00:00", comment=None)


async def test_jira_worklog_add_passes_comment_through():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    with patch("icx_engine.jira.mcp_tools.service.add_worklog",
               new=AsyncMock(return_value={"ok": True})) as mock_add:
        await dispatch_jira_tool("jira_worklog_add", {
            "issue_key": "TEST-1", "time_spent_seconds": 1800,
            "started": "2026-07-28T10:00:00", "comment": "Investigated the bug",
        })
    mock_add.assert_awaited_once_with("TEST-1", 1800, "2026-07-28T10:00:00", comment="Investigated the bug")


async def test_jira_worklog_add_no_connection_error_surfaces_as_ok_false():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    with patch("icx_engine.jira.mcp_tools.service.add_worklog",
               new=AsyncMock(side_effect=NoConnectionError("No Jira connection configured."))):
        result = await dispatch_jira_tool("jira_worklog_add", {
            "issue_key": "TEST-1", "time_spent_seconds": 3600, "started": "2026-07-28T10:00:00",
        })
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert payload["error"] == "No Jira connection configured."


def test_jira_worklog_add_tool_description_explains_no_author_override():
    from icx_engine.jira.mcp_tools import JIRA_TOOLS
    tool = next(t for t in JIRA_TOOLS if t.name == "jira_worklog_add")
    description = tool.description.lower()
    assert "ungated" in description
    assert "author" in description


# -- Task 6: jira_worklog_edit - required fields + self-vs-other gating -----

async def test_jira_worklog_edit_missing_issue_key_returns_named_error():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    result = await dispatch_jira_tool("jira_worklog_edit", {"worklog_id": "500", "time_spent_seconds": 100})
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert "issue_key" in payload["error"]
    assert payload["error"] != "'issue_key'"


async def test_jira_worklog_edit_missing_worklog_id_returns_named_error():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    result = await dispatch_jira_tool("jira_worklog_edit", {"issue_key": "TEST-1", "time_spent_seconds": 100})
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert "worklog_id" in payload["error"]


async def test_jira_worklog_edit_no_fields_given_returns_named_error():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    result = await dispatch_jira_tool("jira_worklog_edit", {"issue_key": "TEST-1", "worklog_id": "500"})
    payload = json.loads(result[0].text)
    assert payload["ok"] is False


async def test_jira_worklog_edit_self_worklog_executes_immediately():
    """Genuinely distinct code path from the other-branch test below: no
    token, service.edit_worklog is called directly on the first invocation."""
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    with patch("icx_engine.jira.mcp_tools.service.list_worklogs",
               new=AsyncMock(return_value={"issue_key": "TEST-1", "worklogs": [
                   {"id": "500", "author": {"accountId": "acc-self"}},
               ]})) as mock_list, \
         patch("icx_engine.jira.mcp_tools.service.get_current_user",
               new=AsyncMock(return_value={"accountId": "acc-self"})) as mock_me, \
         patch("icx_engine.jira.mcp_tools.service.edit_worklog",
               new=AsyncMock(return_value={"ok": True, "issue_key": "TEST-1", "worklog_id": "500"})) as mock_edit:
        result = await dispatch_jira_tool("jira_worklog_edit", {
            "issue_key": "TEST-1", "worklog_id": "500", "time_spent_seconds": 7200,
        })
    payload = json.loads(result[0].text)
    assert payload["ok"] is True
    assert "status" not in payload
    mock_list.assert_awaited_once_with("TEST-1")
    mock_me.assert_awaited_once_with(issue_key="TEST-1")
    mock_edit.assert_awaited_once_with(
        "TEST-1", "500", time_spent_seconds=7200, started=None, comment=None,
    )


async def test_jira_worklog_edit_other_worklog_returns_pending_confirmation():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    with patch("icx_engine.jira.mcp_tools.service.list_worklogs",
               new=AsyncMock(return_value={"issue_key": "TEST-1", "worklogs": [
                   {"id": "500", "author": {"accountId": "acc-other"}},
               ]})), \
         patch("icx_engine.jira.mcp_tools.service.get_current_user",
               new=AsyncMock(return_value={"accountId": "acc-self"})), \
         patch("icx_engine.jira.mcp_tools.service.edit_worklog", new=AsyncMock()) as mock_edit:
        result = await dispatch_jira_tool("jira_worklog_edit", {
            "issue_key": "TEST-1", "worklog_id": "500", "time_spent_seconds": 7200,
        })
    payload = json.loads(result[0].text)
    assert payload["status"] == "pending_confirmation"
    assert "token" in payload
    assert payload["issue_key"] == "TEST-1"
    assert payload["worklog_id"] == "500"
    assert payload["time_spent_seconds"] == 7200
    mock_edit.assert_not_awaited()


async def test_jira_worklog_edit_other_worklog_executes_with_valid_token():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    with patch("icx_engine.jira.mcp_tools.service.list_worklogs",
               new=AsyncMock(return_value={"issue_key": "TEST-1", "worklogs": [
                   {"id": "500", "author": {"accountId": "acc-other"}},
               ]})), \
         patch("icx_engine.jira.mcp_tools.service.get_current_user",
               new=AsyncMock(return_value={"accountId": "acc-self"})):
        first = await dispatch_jira_tool("jira_worklog_edit", {
            "issue_key": "TEST-1", "worklog_id": "500", "time_spent_seconds": 7200,
        })
    token = json.loads(first[0].text)["token"]
    with patch("icx_engine.jira.mcp_tools.service.edit_worklog",
               new=AsyncMock(return_value={"ok": True})) as mock_edit:
        second = await dispatch_jira_tool("jira_worklog_edit", {
            "issue_key": "TEST-1", "worklog_id": "500", "time_spent_seconds": 7200, "confirm_token": token,
        })
    payload = json.loads(second[0].text)
    assert payload["ok"] is True
    mock_edit.assert_awaited_once_with(
        "TEST-1", "500", time_spent_seconds=7200, started=None, comment=None,
    )


async def test_jira_worklog_edit_unknown_worklog_id_treated_as_other_gated():
    """Fail-safe default: a worklog_id that list_worklogs doesn't recognize
    must gate, not silently execute as if it were the caller's own."""
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    with patch("icx_engine.jira.mcp_tools.service.list_worklogs",
               new=AsyncMock(return_value={"issue_key": "TEST-1", "worklogs": []})), \
         patch("icx_engine.jira.mcp_tools.service.get_current_user",
               new=AsyncMock(return_value={"accountId": "acc-self"})), \
         patch("icx_engine.jira.mcp_tools.service.edit_worklog", new=AsyncMock()) as mock_edit:
        result = await dispatch_jira_tool("jira_worklog_edit", {
            "issue_key": "TEST-1", "worklog_id": "does-not-exist", "time_spent_seconds": 100,
        })
    payload = json.loads(result[0].text)
    assert payload["status"] == "pending_confirmation"
    mock_edit.assert_not_awaited()


async def test_jira_worklog_edit_no_connection_error_surfaces_as_ok_false():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    with patch("icx_engine.jira.mcp_tools.service.list_worklogs",
               new=AsyncMock(side_effect=NoConnectionError("No Jira connection configured."))):
        result = await dispatch_jira_tool("jira_worklog_edit", {
            "issue_key": "TEST-1", "worklog_id": "500", "time_spent_seconds": 100,
        })
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert payload["error"] == "No Jira connection configured."


# -- Task 6: jira_worklog_delete - required fields + self-vs-other gating ---

async def test_jira_worklog_delete_missing_issue_key_returns_named_error():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    result = await dispatch_jira_tool("jira_worklog_delete", {"worklog_id": "500"})
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert "issue_key" in payload["error"]
    assert payload["error"] != "'issue_key'"


async def test_jira_worklog_delete_missing_worklog_id_returns_named_error():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    result = await dispatch_jira_tool("jira_worklog_delete", {"issue_key": "TEST-1"})
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert "worklog_id" in payload["error"]


async def test_jira_worklog_delete_self_worklog_executes_immediately():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    with patch("icx_engine.jira.mcp_tools.service.list_worklogs",
               new=AsyncMock(return_value={"issue_key": "TEST-1", "worklogs": [
                   {"id": "500", "author": {"accountId": "acc-self"}},
               ]})), \
         patch("icx_engine.jira.mcp_tools.service.get_current_user",
               new=AsyncMock(return_value={"accountId": "acc-self"})), \
         patch("icx_engine.jira.mcp_tools.service.delete_worklog",
               new=AsyncMock(return_value={"ok": True, "issue_key": "TEST-1", "worklog_id": "500", "deleted": True})) as mock_delete:
        result = await dispatch_jira_tool("jira_worklog_delete", {"issue_key": "TEST-1", "worklog_id": "500"})
    payload = json.loads(result[0].text)
    assert payload["ok"] is True
    assert "status" not in payload
    mock_delete.assert_awaited_once_with("TEST-1", "500")


async def test_jira_worklog_delete_other_worklog_returns_pending_confirmation():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    with patch("icx_engine.jira.mcp_tools.service.list_worklogs",
               new=AsyncMock(return_value={"issue_key": "TEST-1", "worklogs": [
                   {"id": "500", "author": {"accountId": "acc-other"}},
               ]})), \
         patch("icx_engine.jira.mcp_tools.service.get_current_user",
               new=AsyncMock(return_value={"accountId": "acc-self"})), \
         patch("icx_engine.jira.mcp_tools.service.delete_worklog", new=AsyncMock()) as mock_delete:
        result = await dispatch_jira_tool("jira_worklog_delete", {"issue_key": "TEST-1", "worklog_id": "500"})
    payload = json.loads(result[0].text)
    assert payload["status"] == "pending_confirmation"
    assert "token" in payload
    assert payload["issue_key"] == "TEST-1"
    assert payload["worklog_id"] == "500"
    mock_delete.assert_not_awaited()


async def test_jira_worklog_delete_other_worklog_executes_with_valid_token():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    with patch("icx_engine.jira.mcp_tools.service.list_worklogs",
               new=AsyncMock(return_value={"issue_key": "TEST-1", "worklogs": [
                   {"id": "500", "author": {"accountId": "acc-other"}},
               ]})), \
         patch("icx_engine.jira.mcp_tools.service.get_current_user",
               new=AsyncMock(return_value={"accountId": "acc-self"})):
        first = await dispatch_jira_tool("jira_worklog_delete", {"issue_key": "TEST-1", "worklog_id": "500"})
    token = json.loads(first[0].text)["token"]
    with patch("icx_engine.jira.mcp_tools.service.delete_worklog",
               new=AsyncMock(return_value={"ok": True})) as mock_delete:
        second = await dispatch_jira_tool("jira_worklog_delete", {
            "issue_key": "TEST-1", "worklog_id": "500", "confirm_token": token,
        })
    payload = json.loads(second[0].text)
    assert payload["ok"] is True
    mock_delete.assert_awaited_once_with("TEST-1", "500")


async def test_jira_worklog_delete_reused_token_returns_error():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    with patch("icx_engine.jira.mcp_tools.service.list_worklogs",
               new=AsyncMock(return_value={"issue_key": "TEST-1", "worklogs": [
                   {"id": "500", "author": {"accountId": "acc-other"}},
               ]})), \
         patch("icx_engine.jira.mcp_tools.service.get_current_user",
               new=AsyncMock(return_value={"accountId": "acc-self"})):
        first = await dispatch_jira_tool("jira_worklog_delete", {"issue_key": "TEST-1", "worklog_id": "500"})
    token = json.loads(first[0].text)["token"]
    with patch("icx_engine.jira.mcp_tools.service.delete_worklog",
               new=AsyncMock(return_value={"ok": True})):
        await dispatch_jira_tool("jira_worklog_delete", {
            "issue_key": "TEST-1", "worklog_id": "500", "confirm_token": token,
        })
        reused = await dispatch_jira_tool("jira_worklog_delete", {
            "issue_key": "TEST-1", "worklog_id": "500", "confirm_token": token,
        })
    payload = json.loads(reused[0].text)
    assert payload["ok"] is False


async def test_jira_worklog_delete_no_connection_error_surfaces_as_ok_false():
    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    with patch("icx_engine.jira.mcp_tools.service.list_worklogs",
               new=AsyncMock(side_effect=NoConnectionError("No Jira connection configured."))):
        result = await dispatch_jira_tool("jira_worklog_delete", {"issue_key": "TEST-1", "worklog_id": "500"})
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert payload["error"] == "No Jira connection configured."
