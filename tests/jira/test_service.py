import pytest

from icx_engine.connectors.jira.client import JiraClient
from icx_engine.exceptions import JiraValidationError, NoConnectionError
from icx_engine.jira import service


class _FakeJiraClient:
    """Stand-in for JiraClient at the method level - service.py never sees the
    real HTTP transport once _make_client is monkeypatched."""

    def __init__(self, transitions=None, editmeta=None, transition_error=None, update_error=None):
        self._transitions = transitions if transitions is not None else []
        self._editmeta = editmeta if editmeta is not None else {}
        self._transition_error = transition_error
        self._update_error = update_error
        self.transition_calls: list[dict] = []
        self.update_calls: list[dict] = []

    async def get_transitions(self, issue_key):
        return self._transitions

    async def get_editmeta(self, issue_key):
        return self._editmeta

    async def transition_issue(self, issue_key, transition_id=None, fields=None, comment_adf=None):
        self.transition_calls.append({
            "issue_key": issue_key, "transition_id": transition_id,
            "fields": fields, "comment_adf": comment_adf,
        })
        if self._transition_error:
            raise self._transition_error

    async def update_fields(self, issue_key, fields):
        self.update_calls.append({"issue_key": issue_key, "fields": fields})
        if self._update_error:
            raise self._update_error


# -- get_close_requirements --------------------------------------------------

@pytest.mark.asyncio
async def test_get_close_requirements_merges_transitions_and_editmeta(monkeypatch, app_config):
    fake = _FakeJiraClient(
        transitions=[{"id": "11", "name": "To Do"}, {"id": "21", "name": "Done", "fields": {"resolution": {"required": True}}}],
        editmeta={"summary": {"required": True}, "resolution": {"required": False}},
    )
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: app_config))
    monkeypatch.setattr(service, "_make_client", lambda conn, allowed_hosts: fake)

    out = await service.get_close_requirements("TEST-1")

    assert out["issue_key"] == "TEST-1"
    assert out["transitions"] == fake._transitions
    assert out["editable_fields"] == fake._editmeta


# -- apply_update: transition-only / fields-only branching -------------------

@pytest.mark.asyncio
async def test_apply_update_transition_only_calls_transition_issue(monkeypatch, app_config):
    fake = _FakeJiraClient()
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: app_config))
    monkeypatch.setattr(service, "_make_client", lambda conn, allowed_hosts: fake)

    out = await service.apply_update("TEST-1", transition_id="31")

    assert out["ok"] is True
    assert fake.transition_calls == [{
        "issue_key": "TEST-1", "transition_id": "31", "fields": None, "comment_adf": None,
    }]
    assert fake.update_calls == []


@pytest.mark.asyncio
async def test_apply_update_fields_only_calls_update_fields(monkeypatch, app_config):
    fake = _FakeJiraClient()
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: app_config))
    monkeypatch.setattr(service, "_make_client", lambda conn, allowed_hosts: fake)

    out = await service.apply_update("TEST-1", fields={"summary": "New title"})

    assert out["ok"] is True
    assert fake.update_calls == [{"issue_key": "TEST-1", "fields": {"summary": "New title"}}]
    assert fake.transition_calls == []


@pytest.mark.asyncio
async def test_apply_update_transition_with_fields_and_comment_wraps_adf(monkeypatch, app_config):
    fake = _FakeJiraClient()
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: app_config))
    monkeypatch.setattr(service, "_make_client", lambda conn, allowed_hosts: fake)

    out = await service.apply_update(
        "TEST-1", transition_id="31", fields={"resolution": {"name": "Done"}}, comment="Closing this out",
    )

    assert out["ok"] is True
    call = fake.transition_calls[0]
    assert call["transition_id"] == "31"
    assert call["fields"] == {"resolution": {"name": "Done"}}
    assert call["comment_adf"] == {
        "type": "doc", "version": 1,
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Closing this out"}]}],
    }


# -- apply_update: the comment-with-no-transition gap ------------------------

@pytest.mark.asyncio
async def test_apply_update_comment_without_transition_raises_value_error(monkeypatch, app_config):
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: app_config))
    with pytest.raises(ValueError, match="transition_id"):
        await service.apply_update("TEST-1", comment="hello", fields={"summary": "x"})


@pytest.mark.asyncio
async def test_apply_update_comment_only_no_fields_no_transition_raises_value_error(monkeypatch, app_config):
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: app_config))
    with pytest.raises(ValueError, match="transition_id"):
        await service.apply_update("TEST-1", comment="hello")


@pytest.mark.asyncio
async def test_apply_update_nothing_given_raises_value_error(monkeypatch, app_config):
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: app_config))
    with pytest.raises(ValueError, match="[Nn]othing"):
        await service.apply_update("TEST-1")


# -- apply_update: 400-with-errors second-round shape ------------------------

@pytest.mark.asyncio
async def test_apply_update_400_on_transition_surfaces_needs_fields(monkeypatch, app_config):
    fake = _FakeJiraClient(
        transition_error=JiraValidationError("bad", errors={"resolution": "Resolution is required."})
    )
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: app_config))
    monkeypatch.setattr(service, "_make_client", lambda conn, allowed_hosts: fake)

    out = await service.apply_update("TEST-1", transition_id="31")

    assert out["ok"] is False
    assert out["needs_fields"] == {"resolution": "Resolution is required."}
    assert "message" in out


@pytest.mark.asyncio
async def test_apply_update_400_on_fields_only_surfaces_needs_fields(monkeypatch, app_config):
    fake = _FakeJiraClient(
        update_error=JiraValidationError("bad", errors={"duedate": "Date must be in the future."})
    )
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: app_config))
    monkeypatch.setattr(service, "_make_client", lambda conn, allowed_hosts: fake)

    out = await service.apply_update("TEST-1", fields={"duedate": "2020-01-01"})

    assert out["ok"] is False
    assert out["needs_fields"] == {"duedate": "Date must be in the future."}


# -- OAuth refresh must run before client construction -----------------------

@pytest.mark.asyncio
async def test_refresh_oauth_called_before_client_construction(monkeypatch, app_config):
    order: list[str] = []

    async def fake_refresh(conn, config):
        order.append("refresh")
        return conn

    def fake_make_client(conn, allowed_hosts):
        order.append("make_client")
        return _FakeJiraClient()

    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: app_config))
    monkeypatch.setattr(service, "refresh_oauth_if_needed", fake_refresh)
    monkeypatch.setattr(service, "_make_client", fake_make_client)

    await service.get_close_requirements("TEST-1")

    assert order == ["refresh", "make_client"]


@pytest.mark.asyncio
async def test_refresh_oauth_called_before_client_construction_on_apply_update(monkeypatch, app_config):
    order: list[str] = []

    async def fake_refresh(conn, config):
        order.append("refresh")
        return conn

    def fake_make_client(conn, allowed_hosts):
        order.append("make_client")
        return _FakeJiraClient()

    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: app_config))
    monkeypatch.setattr(service, "refresh_oauth_if_needed", fake_refresh)
    monkeypatch.setattr(service, "_make_client", fake_make_client)

    await service.apply_update("TEST-1", fields={"summary": "x"})

    assert order == ["refresh", "make_client"]


# -- connection resolution: ambiguity is surfaced, not swallowed -------------

@pytest.mark.asyncio
async def test_ambiguous_connection_raises_no_connection_error(monkeypatch, multi_connection_config):
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: multi_connection_config))
    with pytest.raises(NoConnectionError):
        await service.get_close_requirements("TEST-1")


# -- _make_client: base_url branches exactly like JiraConnector.fetch() -----

def test_make_client_token_builds_token_base_url(token_connection):
    client = service._make_client(token_connection, {token_connection.domain})
    assert isinstance(client, JiraClient)
    assert client._base_url == f"https://{token_connection.domain}/rest/api/3"


def test_make_client_oauth_builds_atlassian_proxy_base_url(oauth_connection):
    client = service._make_client(oauth_connection, {"api.atlassian.com", oauth_connection.domain})
    assert isinstance(client, JiraClient)
    assert client._base_url == f"https://api.atlassian.com/ex/jira/{oauth_connection.auth.cloud_id}/rest/api/3"


# -- Task 1: list_issue_types / get_createmeta_fields / create_issue / delete_issue --

class _FakeCreateDeleteClient:
    """Stand-in for JiraClient at the method level for create/delete/createmeta."""

    def __init__(self, issue_types=None, createmeta_fields=None, create_key="ABC-99", create_error=None, delete_error=None):
        self._issue_types = issue_types if issue_types is not None else []
        self._createmeta_fields = createmeta_fields if createmeta_fields is not None else {}
        self._create_key = create_key
        self._create_error = create_error
        self._delete_error = delete_error
        self.list_issuetypes_calls: list[str] = []
        self.get_createmeta_fields_calls: list[dict] = []
        self.create_issue_calls: list[dict] = []
        self.delete_issue_calls: list[dict] = []

    async def list_issuetypes(self, project):
        self.list_issuetypes_calls.append(project)
        return self._issue_types

    async def get_createmeta_fields(self, project, issuetype_id):
        self.get_createmeta_fields_calls.append({"project": project, "issuetype_id": issuetype_id})
        return self._createmeta_fields

    async def create_issue(self, project, issuetype, summary, extra_fields=None):
        self.create_issue_calls.append({
            "project": project, "issuetype": issuetype, "summary": summary, "extra_fields": extra_fields,
        })
        if self._create_error:
            raise self._create_error
        return self._create_key

    async def delete_issue(self, issue_key, delete_subtasks=False):
        self.delete_issue_calls.append({"issue_key": issue_key, "delete_subtasks": delete_subtasks})
        if self._delete_error:
            raise self._delete_error


@pytest.mark.asyncio
async def test_list_issue_types_passes_through(monkeypatch, app_config):
    fake = _FakeCreateDeleteClient(issue_types=[{"id": "10001", "name": "Bug"}])
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: app_config))
    monkeypatch.setattr(service, "_make_client", lambda conn, allowed_hosts: fake)

    result = await service.list_issue_types("ABC")

    assert result == [{"id": "10001", "name": "Bug"}]
    assert fake.list_issuetypes_calls == ["ABC"]


@pytest.mark.asyncio
async def test_get_createmeta_fields_passes_through(monkeypatch, app_config):
    fake = _FakeCreateDeleteClient(createmeta_fields={"summary": {"required": True}})
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: app_config))
    monkeypatch.setattr(service, "_make_client", lambda conn, allowed_hosts: fake)

    result = await service.get_createmeta_fields("ABC", "10001")

    assert result == {"summary": {"required": True}}
    assert fake.get_createmeta_fields_calls == [{"project": "ABC", "issuetype_id": "10001"}]


@pytest.mark.asyncio
async def test_create_issue_passes_through_and_returns_ok_dict(monkeypatch, app_config):
    fake = _FakeCreateDeleteClient(create_key="ABC-100")
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: app_config))
    monkeypatch.setattr(service, "_make_client", lambda conn, allowed_hosts: fake)

    out = await service.create_issue("ABC", "Bug", "Something broke", fields={"priority": {"name": "High"}})

    assert out == {
        "ok": True, "issue_key": "ABC-100", "project": "ABC", "issuetype": "Bug",
        "summary": "Something broke", "fields": {"priority": {"name": "High"}},
    }
    assert fake.create_issue_calls == [{
        "project": "ABC", "issuetype": "Bug", "summary": "Something broke",
        "extra_fields": {"priority": {"name": "High"}},
    }]


@pytest.mark.asyncio
async def test_create_issue_400_propagates_jira_validation_error(monkeypatch, app_config):
    fake = _FakeCreateDeleteClient(create_error=JiraValidationError("bad", errors={"summary": "required"}))
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: app_config))
    monkeypatch.setattr(service, "_make_client", lambda conn, allowed_hosts: fake)

    with pytest.raises(JiraValidationError):
        await service.create_issue("ABC", "Bug", "")


@pytest.mark.asyncio
async def test_create_issue_resolves_connection_by_domain_not_issue_key(monkeypatch, app_config):
    """create_issue has no issue_key - it must resolve via resolve_connection(domain=..., raw_input=None),
    not via the bare-issue-key narrowing path used by every other write in this module."""
    fake = _FakeCreateDeleteClient()
    calls: list[dict] = []

    def fake_resolve_connection(domain, config, raw_input=None):
        calls.append({"domain": domain, "raw_input": raw_input})
        return app_config.connections[0]

    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: app_config))
    monkeypatch.setattr(service, "resolve_connection", fake_resolve_connection)
    monkeypatch.setattr(service, "_make_client", lambda conn, allowed_hosts: fake)

    await service.create_issue("ABC", "Bug", "Something broke", domain="test.atlassian.net")

    assert calls == [{"domain": "test.atlassian.net", "raw_input": None}]


@pytest.mark.asyncio
async def test_create_issue_ambiguous_domain_raises_no_connection_error(monkeypatch, multi_connection_config):
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: multi_connection_config))
    with pytest.raises(NoConnectionError):
        await service.create_issue("ABC", "Bug", "Something broke")


@pytest.mark.asyncio
async def test_delete_issue_passes_through_via_resolve_client(monkeypatch, app_config):
    fake = _FakeCreateDeleteClient()
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: app_config))
    monkeypatch.setattr(service, "_make_client", lambda conn, allowed_hosts: fake)

    out = await service.delete_issue("TEST-1")

    assert out == {"ok": True, "issue_key": "TEST-1", "deleted": True, "delete_subtasks": False}
    assert fake.delete_issue_calls == [{"issue_key": "TEST-1", "delete_subtasks": False}]


@pytest.mark.asyncio
async def test_delete_issue_passes_delete_subtasks_flag(monkeypatch, app_config):
    fake = _FakeCreateDeleteClient()
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: app_config))
    monkeypatch.setattr(service, "_make_client", lambda conn, allowed_hosts: fake)

    await service.delete_issue("TEST-1", delete_subtasks=True)

    assert fake.delete_issue_calls == [{"issue_key": "TEST-1", "delete_subtasks": True}]


@pytest.mark.asyncio
async def test_delete_issue_ambiguous_connection_raises_no_connection_error(monkeypatch, multi_connection_config):
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: multi_connection_config))
    with pytest.raises(NoConnectionError):
        await service.delete_issue("TEST-1")


# -- Task 2: list_comments / add_comment / edit_comment / delete_comment ----

class _FakeCommentClient:
    """Stand-in for JiraClient at the method level for comment CRUD."""

    def __init__(self, comments=None, add_result=None, edit_result=None,
                 add_error=None, edit_error=None, delete_error=None):
        self._comments = comments if comments is not None else []
        self._add_result = add_result if add_result is not None else {"id": "10002"}
        self._edit_result = edit_result if edit_result is not None else {"id": "10002"}
        self._add_error = add_error
        self._edit_error = edit_error
        self._delete_error = delete_error
        self.list_comments_calls: list[str] = []
        self.add_comment_calls: list[dict] = []
        self.edit_comment_calls: list[dict] = []
        self.delete_comment_calls: list[dict] = []

    async def list_comments(self, issue_key):
        self.list_comments_calls.append(issue_key)
        return self._comments

    async def add_comment(self, issue_key, body_adf):
        self.add_comment_calls.append({"issue_key": issue_key, "body_adf": body_adf})
        if self._add_error:
            raise self._add_error
        return self._add_result

    async def edit_comment(self, issue_key, comment_id, body_adf):
        self.edit_comment_calls.append({
            "issue_key": issue_key, "comment_id": comment_id, "body_adf": body_adf,
        })
        if self._edit_error:
            raise self._edit_error
        return self._edit_result

    async def delete_comment(self, issue_key, comment_id):
        self.delete_comment_calls.append({"issue_key": issue_key, "comment_id": comment_id})
        if self._delete_error:
            raise self._delete_error


@pytest.mark.asyncio
async def test_list_comments_passes_through(monkeypatch, app_config):
    fake = _FakeCommentClient(comments=[{"id": "10001", "body": {}}])
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: app_config))
    monkeypatch.setattr(service, "_make_client", lambda conn, allowed_hosts: fake)

    out = await service.list_comments("TEST-1")

    assert out == {"issue_key": "TEST-1", "comments": [{"id": "10001", "body": {}}]}
    assert fake.list_comments_calls == ["TEST-1"]


@pytest.mark.asyncio
async def test_add_comment_wraps_text_as_adf(monkeypatch, app_config):
    fake = _FakeCommentClient(add_result={"id": "10002", "body": {"type": "doc"}})
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: app_config))
    monkeypatch.setattr(service, "_make_client", lambda conn, allowed_hosts: fake)

    out = await service.add_comment("TEST-1", "Closing this out")

    assert out == {"ok": True, "issue_key": "TEST-1", "comment": {"id": "10002", "body": {"type": "doc"}}}
    assert fake.add_comment_calls == [{
        "issue_key": "TEST-1",
        "body_adf": {
            "type": "doc", "version": 1,
            "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Closing this out"}]}],
        },
    }]


@pytest.mark.asyncio
async def test_add_comment_400_propagates_jira_validation_error(monkeypatch, app_config):
    fake = _FakeCommentClient(add_error=JiraValidationError("bad", errors={"body": "required"}))
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: app_config))
    monkeypatch.setattr(service, "_make_client", lambda conn, allowed_hosts: fake)

    with pytest.raises(JiraValidationError):
        await service.add_comment("TEST-1", "x")


@pytest.mark.asyncio
async def test_edit_comment_wraps_text_as_adf(monkeypatch, app_config):
    fake = _FakeCommentClient(edit_result={"id": "10002", "body": {"type": "doc"}})
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: app_config))
    monkeypatch.setattr(service, "_make_client", lambda conn, allowed_hosts: fake)

    out = await service.edit_comment("TEST-1", "10002", "Updated text")

    assert out == {
        "ok": True, "issue_key": "TEST-1", "comment_id": "10002",
        "comment": {"id": "10002", "body": {"type": "doc"}},
    }
    assert fake.edit_comment_calls == [{
        "issue_key": "TEST-1", "comment_id": "10002",
        "body_adf": {
            "type": "doc", "version": 1,
            "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Updated text"}]}],
        },
    }]


@pytest.mark.asyncio
async def test_edit_comment_400_propagates_jira_validation_error(monkeypatch, app_config):
    fake = _FakeCommentClient(edit_error=JiraValidationError("bad", errors={"body": "required"}))
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: app_config))
    monkeypatch.setattr(service, "_make_client", lambda conn, allowed_hosts: fake)

    with pytest.raises(JiraValidationError):
        await service.edit_comment("TEST-1", "10002", "x")


@pytest.mark.asyncio
async def test_delete_comment_passes_through(monkeypatch, app_config):
    fake = _FakeCommentClient()
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: app_config))
    monkeypatch.setattr(service, "_make_client", lambda conn, allowed_hosts: fake)

    out = await service.delete_comment("TEST-1", "10002")

    assert out == {"ok": True, "issue_key": "TEST-1", "comment_id": "10002", "deleted": True}
    assert fake.delete_comment_calls == [{"issue_key": "TEST-1", "comment_id": "10002"}]


@pytest.mark.asyncio
async def test_list_comments_ambiguous_connection_raises_no_connection_error(monkeypatch, multi_connection_config):
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: multi_connection_config))
    with pytest.raises(NoConnectionError):
        await service.list_comments("TEST-1")


@pytest.mark.asyncio
async def test_add_comment_ambiguous_connection_raises_no_connection_error(monkeypatch, multi_connection_config):
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: multi_connection_config))
    with pytest.raises(NoConnectionError):
        await service.add_comment("TEST-1", "x")


@pytest.mark.asyncio
async def test_edit_comment_ambiguous_connection_raises_no_connection_error(monkeypatch, multi_connection_config):
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: multi_connection_config))
    with pytest.raises(NoConnectionError):
        await service.edit_comment("TEST-1", "10002", "x")


@pytest.mark.asyncio
async def test_delete_comment_ambiguous_connection_raises_no_connection_error(monkeypatch, multi_connection_config):
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: multi_connection_config))
    with pytest.raises(NoConnectionError):
        await service.delete_comment("TEST-1", "10002")


# -- Task 3: search / get_issue ----------------------------------------------

class _FakeSearchClient:
    """Stand-in for JiraClient at the method level for search/get_issue_raw."""

    def __init__(self, search_result=None, issue_raw=None):
        self._search_result = search_result if search_result is not None else {
            "issues": [], "next_page_token": None, "is_last": True,
        }
        self._issue_raw = issue_raw if issue_raw is not None else {}
        self.search_calls: list[dict] = []
        self.get_issue_raw_calls: list[dict] = []

    async def search_issues(self, jql, fields=None, max_results=50, page_token=None):
        self.search_calls.append({
            "jql": jql, "fields": fields, "max_results": max_results, "page_token": page_token,
        })
        return self._search_result

    async def get_issue_raw(self, issue_key, fields=None):
        self.get_issue_raw_calls.append({"issue_key": issue_key, "fields": fields})
        return self._issue_raw


@pytest.mark.asyncio
async def test_search_clamps_max_results_to_100(monkeypatch, app_config):
    """ICX-side hard cap: requesting far more than 100 must still send 100 to the client."""
    fake = _FakeSearchClient()
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: app_config))
    monkeypatch.setattr(service, "_make_client", lambda conn, allowed_hosts: fake)

    await service.search("project = ABC", max_results=99999)

    assert fake.search_calls[0]["max_results"] == 100


@pytest.mark.asyncio
async def test_search_floors_max_results_to_1(monkeypatch, app_config):
    fake = _FakeSearchClient()
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: app_config))
    monkeypatch.setattr(service, "_make_client", lambda conn, allowed_hosts: fake)

    await service.search("project = ABC", max_results=0)

    assert fake.search_calls[0]["max_results"] == 1


@pytest.mark.asyncio
async def test_search_max_results_within_range_passed_through_unmodified(monkeypatch, app_config):
    fake = _FakeSearchClient()
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: app_config))
    monkeypatch.setattr(service, "_make_client", lambda conn, allowed_hosts: fake)

    await service.search("project = ABC", max_results=25)

    assert fake.search_calls[0]["max_results"] == 25


@pytest.mark.asyncio
async def test_search_defaults_fields_to_small_explicit_list_when_omitted(monkeypatch, app_config):
    """ICX-side hard cap: an omitted fields list must default to a small
    explicit set, never Jira's unbounded default field set."""
    fake = _FakeSearchClient()
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: app_config))
    monkeypatch.setattr(service, "_make_client", lambda conn, allowed_hosts: fake)

    await service.search("project = ABC")

    assert fake.search_calls[0]["fields"] == ["summary", "status", "issuetype"]


@pytest.mark.asyncio
async def test_search_empty_fields_list_also_defaults(monkeypatch, app_config):
    fake = _FakeSearchClient()
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: app_config))
    monkeypatch.setattr(service, "_make_client", lambda conn, allowed_hosts: fake)

    await service.search("project = ABC", fields=[])

    assert fake.search_calls[0]["fields"] == ["summary", "status", "issuetype"]


@pytest.mark.asyncio
async def test_search_passes_through_explicit_fields_unmodified(monkeypatch, app_config):
    fake = _FakeSearchClient()
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: app_config))
    monkeypatch.setattr(service, "_make_client", lambda conn, allowed_hosts: fake)

    await service.search("project = ABC", fields=["priority"])

    assert fake.search_calls[0]["fields"] == ["priority"]


@pytest.mark.asyncio
async def test_search_passes_page_token_through(monkeypatch, app_config):
    fake = _FakeSearchClient()
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: app_config))
    monkeypatch.setattr(service, "_make_client", lambda conn, allowed_hosts: fake)

    await service.search("project = ABC", page_token="tok1")

    assert fake.search_calls[0]["page_token"] == "tok1"


@pytest.mark.asyncio
async def test_search_returns_dict_with_issues_and_pagination(monkeypatch, app_config):
    fake = _FakeSearchClient(search_result={
        "issues": [{"key": "ABC-1"}], "next_page_token": "next", "is_last": False,
    })
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: app_config))
    monkeypatch.setattr(service, "_make_client", lambda conn, allowed_hosts: fake)

    out = await service.search("project = ABC")

    assert out == {
        "jql": "project = ABC", "issues": [{"key": "ABC-1"}],
        "next_page_token": "next", "is_last": False,
    }


@pytest.mark.asyncio
async def test_search_resolves_connection_by_domain_not_issue_key(monkeypatch, app_config):
    """search has no issue_key - it must resolve via resolve_connection(domain=..., raw_input=None),
    matching create_issue's precedent exactly."""
    fake = _FakeSearchClient()
    calls: list[dict] = []

    def fake_resolve_connection(domain, config, raw_input=None):
        calls.append({"domain": domain, "raw_input": raw_input})
        return app_config.connections[0]

    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: app_config))
    monkeypatch.setattr(service, "resolve_connection", fake_resolve_connection)
    monkeypatch.setattr(service, "_make_client", lambda conn, allowed_hosts: fake)

    await service.search("project = ABC", domain="test.atlassian.net")

    assert calls == [{"domain": "test.atlassian.net", "raw_input": None}]


@pytest.mark.asyncio
async def test_search_ambiguous_domain_raises_no_connection_error(monkeypatch, multi_connection_config):
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: multi_connection_config))
    with pytest.raises(NoConnectionError):
        await service.search("project = ABC")


@pytest.mark.asyncio
async def test_get_issue_passes_through_via_resolve_client(monkeypatch, app_config):
    fake = _FakeSearchClient(issue_raw={"key": "TEST-1", "fields": {"summary": "x"}})
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: app_config))
    monkeypatch.setattr(service, "_make_client", lambda conn, allowed_hosts: fake)

    out = await service.get_issue("TEST-1", fields=["summary"])

    assert out == {"issue_key": "TEST-1", "raw": {"key": "TEST-1", "fields": {"summary": "x"}}}
    assert fake.get_issue_raw_calls == [{"issue_key": "TEST-1", "fields": ["summary"]}]


@pytest.mark.asyncio
async def test_get_issue_no_fields_passes_none_through(monkeypatch, app_config):
    """get_issue is not subject to search's hard caps - it's a single-issue
    fetch, not a bulk query, so no default-fields substitution applies here."""
    fake = _FakeSearchClient(issue_raw={"key": "TEST-1"})
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: app_config))
    monkeypatch.setattr(service, "_make_client", lambda conn, allowed_hosts: fake)

    await service.get_issue("TEST-1")

    assert fake.get_issue_raw_calls == [{"issue_key": "TEST-1", "fields": None}]


@pytest.mark.asyncio
async def test_get_issue_ambiguous_connection_raises_no_connection_error(monkeypatch, multi_connection_config):
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: multi_connection_config))
    with pytest.raises(NoConnectionError):
        await service.get_issue("TEST-1")


# -- Task 4: link_types / create_link / delete_link / set_assignee ----------

class _FakeLinkClient:
    """Stand-in for JiraClient at the method level for link/assignee methods."""

    def __init__(self, link_types=None, create_link_error=None,
                 delete_link_error=None, set_assignee_error=None):
        self._link_types = link_types if link_types is not None else []
        self._create_link_error = create_link_error
        self._delete_link_error = delete_link_error
        self._set_assignee_error = set_assignee_error
        self.list_link_types_calls = 0
        self.create_link_calls: list[dict] = []
        self.delete_link_calls: list[str] = []
        self.set_assignee_calls: list[dict] = []

    async def list_link_types(self):
        self.list_link_types_calls += 1
        return self._link_types

    async def create_link(self, link_type_name, inward_key, outward_key):
        self.create_link_calls.append({
            "link_type_name": link_type_name, "inward_key": inward_key, "outward_key": outward_key,
        })
        if self._create_link_error:
            raise self._create_link_error

    async def delete_link(self, link_id):
        self.delete_link_calls.append(link_id)
        if self._delete_link_error:
            raise self._delete_link_error

    async def set_assignee(self, issue_key, account_id):
        self.set_assignee_calls.append({"issue_key": issue_key, "account_id": account_id})
        if self._set_assignee_error:
            raise self._set_assignee_error


@pytest.mark.asyncio
async def test_link_types_passes_through(monkeypatch, app_config):
    fake = _FakeLinkClient(link_types=[{"id": "10000", "name": "Blocks"}])
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: app_config))
    monkeypatch.setattr(service, "_make_client", lambda conn, allowed_hosts: fake)

    out = await service.link_types()

    assert out == {"link_types": [{"id": "10000", "name": "Blocks"}]}
    assert fake.list_link_types_calls == 1


@pytest.mark.asyncio
async def test_link_types_resolves_connection_by_domain_not_issue_key(monkeypatch, app_config):
    """link_types is a global lookup with no issue_key at all - it must resolve via
    resolve_connection(domain=..., raw_input=None), matching create_issue/search's precedent."""
    fake = _FakeLinkClient()
    calls: list[dict] = []

    def fake_resolve_connection(domain, config, raw_input=None):
        calls.append({"domain": domain, "raw_input": raw_input})
        return app_config.connections[0]

    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: app_config))
    monkeypatch.setattr(service, "resolve_connection", fake_resolve_connection)
    monkeypatch.setattr(service, "_make_client", lambda conn, allowed_hosts: fake)

    await service.link_types(domain="test.atlassian.net")

    assert calls == [{"domain": "test.atlassian.net", "raw_input": None}]


@pytest.mark.asyncio
async def test_link_types_ambiguous_domain_raises_no_connection_error(monkeypatch, multi_connection_config):
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: multi_connection_config))
    with pytest.raises(NoConnectionError):
        await service.link_types()


@pytest.mark.asyncio
async def test_create_link_passes_through_and_returns_ok_dict(monkeypatch, app_config):
    fake = _FakeLinkClient()
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: app_config))
    monkeypatch.setattr(service, "_make_client", lambda conn, allowed_hosts: fake)

    out = await service.create_link("Blocks", "ABC-1", "ABC-2")

    assert out == {"ok": True, "link_type_name": "Blocks", "inward_key": "ABC-1", "outward_key": "ABC-2"}
    assert fake.create_link_calls == [{
        "link_type_name": "Blocks", "inward_key": "ABC-1", "outward_key": "ABC-2",
    }]


@pytest.mark.asyncio
async def test_create_link_resolves_connection_by_inward_key(monkeypatch, app_config):
    """create_link's connection-resolution decision: resolve by inward_key, not
    outward_key - an arbitrary-but-documented choice, since Jira does not support
    cross-instance links so both keys resolve to the same connection in practice."""
    fake = _FakeLinkClient()
    calls: list[str] = []

    def fake_resolve_connection(domain, config, raw_input=None):
        calls.append(raw_input)
        return app_config.connections[0]

    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: app_config))
    monkeypatch.setattr(service, "resolve_connection", fake_resolve_connection)
    monkeypatch.setattr(service, "_make_client", lambda conn, allowed_hosts: fake)

    await service.create_link("Blocks", "INWARD-1", "OUTWARD-2")

    assert calls == ["INWARD-1"]


@pytest.mark.asyncio
async def test_create_link_400_propagates_jira_validation_error(monkeypatch, app_config):
    fake = _FakeLinkClient(create_link_error=JiraValidationError("bad", errors={"type": "invalid"}))
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: app_config))
    monkeypatch.setattr(service, "_make_client", lambda conn, allowed_hosts: fake)

    with pytest.raises(JiraValidationError):
        await service.create_link("NotReal", "ABC-1", "ABC-2")


@pytest.mark.asyncio
async def test_create_link_ambiguous_connection_raises_no_connection_error(monkeypatch, multi_connection_config):
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: multi_connection_config))
    with pytest.raises(NoConnectionError):
        await service.create_link("Blocks", "ABC-1", "ABC-2")


@pytest.mark.asyncio
async def test_delete_link_passes_through_and_returns_ok_dict(monkeypatch, app_config):
    fake = _FakeLinkClient()
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: app_config))
    monkeypatch.setattr(service, "_make_client", lambda conn, allowed_hosts: fake)

    out = await service.delete_link("TEST-1", "10050")

    assert out == {"ok": True, "issue_key": "TEST-1", "link_id": "10050", "deleted": True}
    assert fake.delete_link_calls == ["10050"]


@pytest.mark.asyncio
async def test_delete_link_resolves_connection_by_given_issue_key(monkeypatch, app_config):
    """delete_link's connection-resolution decision: the caller-supplied issue_key
    resolves the connection via the normal bare-key path (_resolve_client), even
    though Jira's DELETE .../issueLink/{id} call itself never uses it."""
    fake = _FakeLinkClient()
    calls: list[str] = []

    def fake_resolve_connection(domain, config, raw_input=None):
        calls.append(raw_input)
        return app_config.connections[0]

    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: app_config))
    monkeypatch.setattr(service, "resolve_connection", fake_resolve_connection)
    monkeypatch.setattr(service, "_make_client", lambda conn, allowed_hosts: fake)

    await service.delete_link("TEST-1", "10050")

    assert calls == ["TEST-1"]


@pytest.mark.asyncio
async def test_delete_link_ambiguous_connection_raises_no_connection_error(monkeypatch, multi_connection_config):
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: multi_connection_config))
    with pytest.raises(NoConnectionError):
        await service.delete_link("TEST-1", "10050")


@pytest.mark.asyncio
async def test_set_assignee_passes_through_and_returns_ok_dict(monkeypatch, app_config):
    fake = _FakeLinkClient()
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: app_config))
    monkeypatch.setattr(service, "_make_client", lambda conn, allowed_hosts: fake)

    out = await service.set_assignee("TEST-1", "acc-1")

    assert out == {"ok": True, "issue_key": "TEST-1", "account_id": "acc-1"}
    assert fake.set_assignee_calls == [{"issue_key": "TEST-1", "account_id": "acc-1"}]


@pytest.mark.asyncio
async def test_set_assignee_none_unassigns(monkeypatch, app_config):
    fake = _FakeLinkClient()
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: app_config))
    monkeypatch.setattr(service, "_make_client", lambda conn, allowed_hosts: fake)

    out = await service.set_assignee("TEST-1")

    assert out == {"ok": True, "issue_key": "TEST-1", "account_id": None}
    assert fake.set_assignee_calls == [{"issue_key": "TEST-1", "account_id": None}]


@pytest.mark.asyncio
async def test_set_assignee_default_sentinel_passed_through(monkeypatch, app_config):
    fake = _FakeLinkClient()
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: app_config))
    monkeypatch.setattr(service, "_make_client", lambda conn, allowed_hosts: fake)

    await service.set_assignee("TEST-1", "-1")

    assert fake.set_assignee_calls == [{"issue_key": "TEST-1", "account_id": "-1"}]


@pytest.mark.asyncio
async def test_set_assignee_400_propagates_jira_validation_error(monkeypatch, app_config):
    fake = _FakeLinkClient(set_assignee_error=JiraValidationError("bad", errors={"accountId": "invalid"}))
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: app_config))
    monkeypatch.setattr(service, "_make_client", lambda conn, allowed_hosts: fake)

    with pytest.raises(JiraValidationError):
        await service.set_assignee("TEST-1", "bogus")


@pytest.mark.asyncio
async def test_set_assignee_ambiguous_connection_raises_no_connection_error(monkeypatch, multi_connection_config):
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: multi_connection_config))
    with pytest.raises(NoConnectionError):
        await service.set_assignee("TEST-1", "acc-1")


# -- Task 5: upload_attachment / delete_attachment ---------------------------

class _FakeAttachmentClient:
    """Stand-in for JiraClient at the method level for attachment upload/delete."""

    def __init__(self, upload_result=None, upload_error=None, delete_error=None):
        self._upload_result = upload_result if upload_result is not None else [{"id": "10100"}]
        self._upload_error = upload_error
        self._delete_error = delete_error
        self.upload_attachment_calls: list[dict] = []
        self.delete_attachment_calls: list[str] = []

    async def upload_attachment(self, issue_key, filename, content_bytes, content_type=None):
        self.upload_attachment_calls.append({
            "issue_key": issue_key, "filename": filename,
            "content_bytes": content_bytes, "content_type": content_type,
        })
        if self._upload_error:
            raise self._upload_error
        return self._upload_result

    async def delete_attachment(self, attachment_id):
        self.delete_attachment_calls.append(attachment_id)
        if self._delete_error:
            raise self._delete_error


@pytest.mark.asyncio
async def test_upload_attachment_passes_through_and_returns_ok_dict(monkeypatch, app_config):
    fake = _FakeAttachmentClient(upload_result=[{"id": "10100", "filename": "report.txt"}])
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: app_config))
    monkeypatch.setattr(service, "_make_client", lambda conn, allowed_hosts: fake)

    out = await service.upload_attachment("TEST-1", "report.txt", b"hello world")

    assert out == {
        "ok": True, "issue_key": "TEST-1", "filename": "report.txt",
        "attachments": [{"id": "10100", "filename": "report.txt"}],
    }
    assert fake.upload_attachment_calls == [{
        "issue_key": "TEST-1", "filename": "report.txt",
        "content_bytes": b"hello world", "content_type": None,
    }]


@pytest.mark.asyncio
async def test_upload_attachment_passes_content_type_through(monkeypatch, app_config):
    fake = _FakeAttachmentClient()
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: app_config))
    monkeypatch.setattr(service, "_make_client", lambda conn, allowed_hosts: fake)

    await service.upload_attachment("TEST-1", "photo.png", b"\x89PNG", content_type="image/png")

    assert fake.upload_attachment_calls[0]["content_type"] == "image/png"


@pytest.mark.asyncio
async def test_upload_attachment_400_propagates_jira_validation_error(monkeypatch, app_config):
    fake = _FakeAttachmentClient(upload_error=JiraValidationError("bad", errors={"file": "too large"}))
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: app_config))
    monkeypatch.setattr(service, "_make_client", lambda conn, allowed_hosts: fake)

    with pytest.raises(JiraValidationError):
        await service.upload_attachment("TEST-1", "huge.bin", b"x")


@pytest.mark.asyncio
async def test_upload_attachment_ambiguous_connection_raises_no_connection_error(monkeypatch, multi_connection_config):
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: multi_connection_config))
    with pytest.raises(NoConnectionError):
        await service.upload_attachment("TEST-1", "report.txt", b"hello")


@pytest.mark.asyncio
async def test_delete_attachment_passes_through_and_returns_ok_dict(monkeypatch, app_config):
    fake = _FakeAttachmentClient()
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: app_config))
    monkeypatch.setattr(service, "_make_client", lambda conn, allowed_hosts: fake)

    out = await service.delete_attachment("TEST-1", "10100")

    assert out == {"ok": True, "issue_key": "TEST-1", "attachment_id": "10100", "deleted": True}
    assert fake.delete_attachment_calls == ["10100"]


@pytest.mark.asyncio
async def test_delete_attachment_resolves_connection_by_given_issue_key(monkeypatch, app_config):
    """delete_attachment's connection-resolution decision: mirrors delete_link's
    precedent exactly - issue_key resolves the connection via the normal
    bare-key path (_resolve_client), even though Jira's DELETE
    .../attachment/{id} call itself never uses it."""
    fake = _FakeAttachmentClient()
    calls: list[str] = []

    def fake_resolve_connection(domain, config, raw_input=None):
        calls.append(raw_input)
        return app_config.connections[0]

    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: app_config))
    monkeypatch.setattr(service, "resolve_connection", fake_resolve_connection)
    monkeypatch.setattr(service, "_make_client", lambda conn, allowed_hosts: fake)

    await service.delete_attachment("TEST-1", "10100")

    assert calls == ["TEST-1"]


@pytest.mark.asyncio
async def test_delete_attachment_400_propagates_jira_validation_error(monkeypatch, app_config):
    fake = _FakeAttachmentClient(delete_error=JiraValidationError("bad", errors={}))
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: app_config))
    monkeypatch.setattr(service, "_make_client", lambda conn, allowed_hosts: fake)

    with pytest.raises(JiraValidationError):
        await service.delete_attachment("TEST-1", "10100")


@pytest.mark.asyncio
async def test_delete_attachment_ambiguous_connection_raises_no_connection_error(monkeypatch, multi_connection_config):
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: multi_connection_config))
    with pytest.raises(NoConnectionError):
        await service.delete_attachment("TEST-1", "10100")


# -- Task 6: get_current_user / watchers / worklog ---------------------------

import datetime as _dt


class _FakeWatcherWorklogClient:
    """Stand-in for JiraClient at the method level for watcher/worklog/whoami."""

    def __init__(self, me=None, watchers=None, worklogs=None,
                 add_worklog_result=None, edit_worklog_result=None,
                 add_watcher_error=None, remove_watcher_error=None,
                 add_worklog_error=None, edit_worklog_error=None, delete_worklog_error=None):
        self._me = me if me is not None else {"accountId": "acc-self", "displayName": "Me"}
        self._watchers = watchers if watchers is not None else {"watchers": [], "watchCount": 0}
        self._worklogs = worklogs if worklogs is not None else {"worklogs": []}
        self._add_worklog_result = add_worklog_result if add_worklog_result is not None else {"id": "500"}
        self._edit_worklog_result = edit_worklog_result if edit_worklog_result is not None else {"id": "500"}
        self._add_watcher_error = add_watcher_error
        self._remove_watcher_error = remove_watcher_error
        self._add_worklog_error = add_worklog_error
        self._edit_worklog_error = edit_worklog_error
        self._delete_worklog_error = delete_worklog_error
        self.get_current_user_calls = 0
        self.list_watchers_calls: list[str] = []
        self.add_watcher_calls: list[dict] = []
        self.remove_watcher_calls: list[dict] = []
        self.list_worklogs_calls: list[str] = []
        self.add_worklog_calls: list[dict] = []
        self.edit_worklog_calls: list[dict] = []
        self.delete_worklog_calls: list[dict] = []

    async def get_current_user(self):
        self.get_current_user_calls += 1
        return self._me

    async def list_watchers(self, issue_key):
        self.list_watchers_calls.append(issue_key)
        return self._watchers

    async def add_watcher(self, issue_key, account_id):
        self.add_watcher_calls.append({"issue_key": issue_key, "account_id": account_id})
        if self._add_watcher_error:
            raise self._add_watcher_error

    async def remove_watcher(self, issue_key, account_id):
        self.remove_watcher_calls.append({"issue_key": issue_key, "account_id": account_id})
        if self._remove_watcher_error:
            raise self._remove_watcher_error

    async def list_worklogs(self, issue_key):
        self.list_worklogs_calls.append(issue_key)
        return self._worklogs

    async def add_worklog(self, issue_key, time_spent_seconds, started, comment_adf=None):
        self.add_worklog_calls.append({
            "issue_key": issue_key, "time_spent_seconds": time_spent_seconds,
            "started": started, "comment_adf": comment_adf,
        })
        if self._add_worklog_error:
            raise self._add_worklog_error
        return self._add_worklog_result

    async def edit_worklog(self, issue_key, worklog_id, time_spent_seconds=None, started=None, comment_adf=None):
        self.edit_worklog_calls.append({
            "issue_key": issue_key, "worklog_id": worklog_id,
            "time_spent_seconds": time_spent_seconds, "started": started, "comment_adf": comment_adf,
        })
        if self._edit_worklog_error:
            raise self._edit_worklog_error
        return self._edit_worklog_result

    async def delete_worklog(self, issue_key, worklog_id):
        self.delete_worklog_calls.append({"issue_key": issue_key, "worklog_id": worklog_id})
        if self._delete_worklog_error:
            raise self._delete_worklog_error


# -- _format_started_for_jira --------------------------------------------------

def test_format_started_datetime_naive_assumed_utc():
    dt = _dt.datetime(2026, 7, 28, 10, 0, 0)
    assert service._format_started_for_jira(dt) == "2026-07-28T10:00:00.000+0000"


def test_format_started_datetime_with_tzinfo_preserves_offset():
    tz = _dt.timezone(_dt.timedelta(hours=5, minutes=30))
    dt = _dt.datetime(2026, 7, 28, 10, 0, 0, tzinfo=tz)
    assert service._format_started_for_jira(dt) == "2026-07-28T10:00:00.000+0530"


def test_format_started_iso_string_z_suffix_converted_to_numeric_offset():
    result = service._format_started_for_jira("2026-07-28T10:00:00Z")
    assert result == "2026-07-28T10:00:00.000+0000"
    assert not result.endswith("Z")


def test_format_started_iso_string_naive_assumed_utc():
    result = service._format_started_for_jira("2026-07-28T10:00:00")
    assert result == "2026-07-28T10:00:00.000+0000"


def test_format_started_iso_string_with_offset_preserved():
    result = service._format_started_for_jira("2026-07-28T10:00:00+05:30")
    assert result == "2026-07-28T10:00:00.000+0530"


# -- get_current_user: connection resolution ---------------------------------

@pytest.mark.asyncio
async def test_get_current_user_resolves_by_issue_key_when_given(monkeypatch, app_config):
    fake = _FakeWatcherWorklogClient(me={"accountId": "acc-x"})
    calls: list[dict] = []

    def fake_resolve_connection(domain, config, raw_input=None):
        calls.append({"domain": domain, "raw_input": raw_input})
        return app_config.connections[0]

    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: app_config))
    monkeypatch.setattr(service, "resolve_connection", fake_resolve_connection)
    monkeypatch.setattr(service, "_make_client", lambda conn, allowed_hosts: fake)

    out = await service.get_current_user(issue_key="TEST-1")

    assert out == {"accountId": "acc-x"}
    # resolves the SAME connection a watcher/worklog mutation on TEST-1 would use
    assert calls == [{"domain": None, "raw_input": "TEST-1"}]


@pytest.mark.asyncio
async def test_get_current_user_resolves_by_domain_when_no_issue_key(monkeypatch, app_config):
    fake = _FakeWatcherWorklogClient()
    calls: list[dict] = []

    def fake_resolve_connection(domain, config, raw_input=None):
        calls.append({"domain": domain, "raw_input": raw_input})
        return app_config.connections[0]

    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: app_config))
    monkeypatch.setattr(service, "resolve_connection", fake_resolve_connection)
    monkeypatch.setattr(service, "_make_client", lambda conn, allowed_hosts: fake)

    await service.get_current_user(domain="test.atlassian.net")

    assert calls == [{"domain": "test.atlassian.net", "raw_input": None}]


@pytest.mark.asyncio
async def test_get_current_user_ambiguous_domain_raises_no_connection_error(monkeypatch, multi_connection_config):
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: multi_connection_config))
    with pytest.raises(NoConnectionError):
        await service.get_current_user()


@pytest.mark.asyncio
async def test_get_current_user_ambiguous_issue_key_raises_no_connection_error(monkeypatch, multi_connection_config):
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: multi_connection_config))
    with pytest.raises(NoConnectionError):
        await service.get_current_user(issue_key="TEST-1")


# -- list_watchers / add_watcher / remove_watcher ----------------------------

@pytest.mark.asyncio
async def test_list_watchers_passes_through(monkeypatch, app_config):
    fake = _FakeWatcherWorklogClient(watchers={"watchers": [{"accountId": "acc-1"}], "watchCount": 1})
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: app_config))
    monkeypatch.setattr(service, "_make_client", lambda conn, allowed_hosts: fake)

    out = await service.list_watchers("TEST-1")

    assert out == {"issue_key": "TEST-1", "watchers": [{"accountId": "acc-1"}], "watch_count": 1}
    assert fake.list_watchers_calls == ["TEST-1"]


@pytest.mark.asyncio
async def test_add_watcher_passes_through_and_returns_ok_dict(monkeypatch, app_config):
    fake = _FakeWatcherWorklogClient()
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: app_config))
    monkeypatch.setattr(service, "_make_client", lambda conn, allowed_hosts: fake)

    out = await service.add_watcher("TEST-1", "acc-1")

    assert out == {"ok": True, "issue_key": "TEST-1", "account_id": "acc-1", "watching": True}
    assert fake.add_watcher_calls == [{"issue_key": "TEST-1", "account_id": "acc-1"}]


@pytest.mark.asyncio
async def test_remove_watcher_passes_through_and_returns_ok_dict(monkeypatch, app_config):
    fake = _FakeWatcherWorklogClient()
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: app_config))
    monkeypatch.setattr(service, "_make_client", lambda conn, allowed_hosts: fake)

    out = await service.remove_watcher("TEST-1", "acc-1")

    assert out == {"ok": True, "issue_key": "TEST-1", "account_id": "acc-1", "watching": False}
    assert fake.remove_watcher_calls == [{"issue_key": "TEST-1", "account_id": "acc-1"}]


@pytest.mark.asyncio
async def test_add_watcher_400_propagates_jira_validation_error(monkeypatch, app_config):
    fake = _FakeWatcherWorklogClient(add_watcher_error=JiraValidationError("bad", errors={}))
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: app_config))
    monkeypatch.setattr(service, "_make_client", lambda conn, allowed_hosts: fake)

    with pytest.raises(JiraValidationError):
        await service.add_watcher("TEST-1", "bogus")


@pytest.mark.asyncio
async def test_list_watchers_ambiguous_connection_raises_no_connection_error(monkeypatch, multi_connection_config):
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: multi_connection_config))
    with pytest.raises(NoConnectionError):
        await service.list_watchers("TEST-1")


# -- list_worklogs / add_worklog / edit_worklog / delete_worklog ------------

@pytest.mark.asyncio
async def test_list_worklogs_passes_through(monkeypatch, app_config):
    fake = _FakeWatcherWorklogClient(worklogs={"worklogs": [{"id": "500", "author": {"accountId": "acc-1"}}]})
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: app_config))
    monkeypatch.setattr(service, "_make_client", lambda conn, allowed_hosts: fake)

    out = await service.list_worklogs("TEST-1")

    assert out == {"issue_key": "TEST-1", "worklogs": [{"id": "500", "author": {"accountId": "acc-1"}}]}


@pytest.mark.asyncio
async def test_add_worklog_wraps_comment_and_formats_started(monkeypatch, app_config):
    fake = _FakeWatcherWorklogClient(add_worklog_result={"id": "500"})
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: app_config))
    monkeypatch.setattr(service, "_make_client", lambda conn, allowed_hosts: fake)

    out = await service.add_worklog(
        "TEST-1", 3600, "2026-07-28T10:00:00Z", comment="Worked on it",
    )

    assert out == {"ok": True, "issue_key": "TEST-1", "worklog": {"id": "500"}}
    call = fake.add_worklog_calls[0]
    assert call["time_spent_seconds"] == 3600
    assert call["started"] == "2026-07-28T10:00:00.000+0000"
    assert call["comment_adf"] == {
        "type": "doc", "version": 1,
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Worked on it"}]}],
    }


@pytest.mark.asyncio
async def test_add_worklog_no_comment_passes_none(monkeypatch, app_config):
    fake = _FakeWatcherWorklogClient()
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: app_config))
    monkeypatch.setattr(service, "_make_client", lambda conn, allowed_hosts: fake)

    await service.add_worklog("TEST-1", 1800, "2026-07-28T10:00:00Z")

    assert fake.add_worklog_calls[0]["comment_adf"] is None


@pytest.mark.asyncio
async def test_add_worklog_400_propagates_jira_validation_error(monkeypatch, app_config):
    fake = _FakeWatcherWorklogClient(add_worklog_error=JiraValidationError("bad", errors={}))
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: app_config))
    monkeypatch.setattr(service, "_make_client", lambda conn, allowed_hosts: fake)

    with pytest.raises(JiraValidationError):
        await service.add_worklog("TEST-1", 0, "2026-07-28T10:00:00Z")


@pytest.mark.asyncio
async def test_edit_worklog_passes_only_given_fields(monkeypatch, app_config):
    fake = _FakeWatcherWorklogClient(edit_worklog_result={"id": "500", "timeSpentSeconds": 7200})
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: app_config))
    monkeypatch.setattr(service, "_make_client", lambda conn, allowed_hosts: fake)

    out = await service.edit_worklog("TEST-1", "500", time_spent_seconds=7200)

    assert out == {"ok": True, "issue_key": "TEST-1", "worklog_id": "500", "worklog": {"id": "500", "timeSpentSeconds": 7200}}
    call = fake.edit_worklog_calls[0]
    assert call["time_spent_seconds"] == 7200
    assert call["started"] is None
    assert call["comment_adf"] is None


@pytest.mark.asyncio
async def test_edit_worklog_formats_started_when_given(monkeypatch, app_config):
    fake = _FakeWatcherWorklogClient()
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: app_config))
    monkeypatch.setattr(service, "_make_client", lambda conn, allowed_hosts: fake)

    await service.edit_worklog("TEST-1", "500", started="2026-07-28T12:00:00Z")

    assert fake.edit_worklog_calls[0]["started"] == "2026-07-28T12:00:00.000+0000"


@pytest.mark.asyncio
async def test_edit_worklog_400_propagates_jira_validation_error(monkeypatch, app_config):
    fake = _FakeWatcherWorklogClient(edit_worklog_error=JiraValidationError("bad", errors={}))
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: app_config))
    monkeypatch.setattr(service, "_make_client", lambda conn, allowed_hosts: fake)

    with pytest.raises(JiraValidationError):
        await service.edit_worklog("TEST-1", "500", time_spent_seconds=-1)


@pytest.mark.asyncio
async def test_delete_worklog_passes_through_and_returns_ok_dict(monkeypatch, app_config):
    fake = _FakeWatcherWorklogClient()
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: app_config))
    monkeypatch.setattr(service, "_make_client", lambda conn, allowed_hosts: fake)

    out = await service.delete_worklog("TEST-1", "500")

    assert out == {"ok": True, "issue_key": "TEST-1", "worklog_id": "500", "deleted": True}
    assert fake.delete_worklog_calls == [{"issue_key": "TEST-1", "worklog_id": "500"}]


@pytest.mark.asyncio
async def test_delete_worklog_400_propagates_jira_validation_error(monkeypatch, app_config):
    fake = _FakeWatcherWorklogClient(delete_worklog_error=JiraValidationError("bad", errors={}))
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: app_config))
    monkeypatch.setattr(service, "_make_client", lambda conn, allowed_hosts: fake)

    with pytest.raises(JiraValidationError):
        await service.delete_worklog("TEST-1", "500")


@pytest.mark.asyncio
async def test_list_worklogs_ambiguous_connection_raises_no_connection_error(monkeypatch, multi_connection_config):
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: multi_connection_config))
    with pytest.raises(NoConnectionError):
        await service.list_worklogs("TEST-1")


@pytest.mark.asyncio
async def test_add_worklog_ambiguous_connection_raises_no_connection_error(monkeypatch, multi_connection_config):
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: multi_connection_config))
    with pytest.raises(NoConnectionError):
        await service.add_worklog("TEST-1", 3600, "2026-07-28T10:00:00Z")
