"""Unit tests for JiraClient whitelist enforcement and JiraConnector URL rewriting."""
import json
import re
import httpx
import pytest
import respx

from icx_engine.connectors.jira.client import JiraClient
from icx_engine.connectors.jira.connector import JiraConnector
from icx_engine.connectors.jira.config import JiraConnection, TokenAuth, JiraOAuthAuth
from icx_engine.exceptions import IssueNotFound, JiraValidationError, SourceUnavailable


# -- JiraClient whitelist enforcement -----------------------------------------

async def test_whitelist_rejects_host_not_in_set():
    client = JiraClient(
        "https://api.atlassian.com/ex/jira/abc123/rest/api/3",
        "Bearer tok",
        {"api.atlassian.com"},
    )
    with pytest.raises(SourceUnavailable):
        await client.download_attachment("https://evil.example.com/file.png")


async def test_whitelist_rejects_http_scheme():
    client = JiraClient(
        "https://test.atlassian.net/rest/api/3",
        "Basic tok",
        {"test.atlassian.net"},
    )
    with pytest.raises(SourceUnavailable):
        await client.download_attachment("http://test.atlassian.net/file.png")


async def test_whitelist_rejects_domain_outside_set():
    client = JiraClient(
        "https://api.atlassian.com/ex/jira/abc123/rest/api/3",
        "Bearer tok",
        {"api.atlassian.com"},
    )
    with pytest.raises(SourceUnavailable):
        await client.download_attachment(
            "https://mysite.atlassian.net/rest/api/3/attachment/content/10001"
        )


@respx.mock
async def test_whitelist_allows_permitted_host():
    url = "https://api.atlassian.com/ex/jira/abc123/rest/api/3/attachment/content/1"
    respx.get(url).mock(return_value=httpx.Response(200, content=b"data"))
    client = JiraClient(
        "https://api.atlassian.com/ex/jira/abc123/rest/api/3",
        "Bearer tok",
        {"api.atlassian.com"},
    )
    result = await client.download_attachment(url)
    assert result == b"data"


# -- attachment_session connection pooling ------------------------------------

def _pool_client() -> JiraClient:
    return JiraClient(
        "https://api.atlassian.com/ex/jira/abc123/rest/api/3",
        "Bearer tok",
        {"api.atlassian.com"},
    )


async def test_attachment_session_sets_and_clears_shared_client():
    client = _pool_client()
    assert client._dl_client is None
    async with client.attachment_session():
        assert client._dl_client is not None      # one shared client during the batch
        shared = client._dl_client
        async with client.attachment_session():    # nested reuses, does not replace
            assert client._dl_client is shared
        assert client._dl_client is shared         # inner exit must not close the outer
    assert client._dl_client is None               # closed + reset after the batch


@respx.mock
async def test_attachment_session_downloads_reuse_one_client():
    base = "https://api.atlassian.com/ex/jira/abc123/rest/api/3/attachment/content"
    for i in (1, 2, 3):
        respx.get(f"{base}/{i}").mock(return_value=httpx.Response(200, content=f"f{i}".encode()))
    client = _pool_client()
    async with client.attachment_session():
        shared = client._dl_client
        results = [await client.download_attachment(f"{base}/{i}") for i in (1, 2, 3)]
        assert client._dl_client is shared         # same client across all 3 downloads
    assert results == [b"f1", b"f2", b"f3"]         # results identical to per-call path


@respx.mock
async def test_download_outside_session_still_works_per_call():
    url = "https://api.atlassian.com/ex/jira/abc123/rest/api/3/attachment/content/9"
    respx.get(url).mock(return_value=httpx.Response(200, content=b"solo"))
    client = _pool_client()
    result = await client.download_attachment(url)   # no session -> per-call client
    assert result == b"solo"
    assert client._dl_client is None


async def test_attachment_session_closes_client_on_exception():
    """An error raised mid-batch must still close + clear the shared client (no leak)."""
    client = _pool_client()
    with pytest.raises(RuntimeError):
        async with client.attachment_session():
            shared = client._dl_client
            assert shared is not None
            raise RuntimeError("boom mid-batch")
    assert client._dl_client is None         # reset even though the body raised
    assert shared.is_closed                  # underlying httpx client was aclosed


@respx.mock
async def test_redirect_to_allowed_host_is_followed():
    """Redirect from api.atlassian.com to api.media.atlassian.com (both in allowed_hosts) succeeds."""
    redirect_url = "https://api.media.atlassian.com/file/abc123"
    original_url = "https://api.atlassian.com/ex/jira/abc123/rest/api/3/attachment/content/1"
    respx.get(original_url).mock(
        return_value=httpx.Response(302, headers={"Location": redirect_url})
    )
    respx.get(redirect_url).mock(return_value=httpx.Response(200, content=b"filedata"))

    client = JiraClient(
        "https://api.atlassian.com/ex/jira/abc123/rest/api/3",
        "Bearer tok",
        {"api.atlassian.com", "api.media.atlassian.com"},
    )
    result = await client.download_attachment(original_url)
    assert result == b"filedata"


@respx.mock
async def test_relative_redirect_resolves_against_current_host():
    """A relative Location header resolves to the same host and is followed (finding S4)."""
    original_url = "https://api.atlassian.com/ex/jira/abc123/rest/api/3/attachment/content/1"
    resolved_url = "https://api.atlassian.com/rest/api/3/attachment/content/2"
    respx.get(original_url).mock(
        return_value=httpx.Response(302, headers={"Location": "/rest/api/3/attachment/content/2"})
    )
    respx.get(resolved_url).mock(return_value=httpx.Response(200, content=b"reldata"))

    client = JiraClient(
        "https://api.atlassian.com/ex/jira/abc123/rest/api/3",
        "Bearer tok",
        {"api.atlassian.com"},
    )
    result = await client.download_attachment(original_url)
    assert result == b"reldata"


@respx.mock
async def test_redirect_to_disallowed_host_raises():
    """Redirect to a host not in allowed_hosts is rejected before the request is made."""
    original_url = "https://api.atlassian.com/ex/jira/abc123/rest/api/3/attachment/content/1"
    respx.get(original_url).mock(
        return_value=httpx.Response(302, headers={"Location": "https://evil.example.com/steal"})
    )

    client = JiraClient(
        "https://api.atlassian.com/ex/jira/abc123/rest/api/3",
        "Bearer tok",
        {"api.atlassian.com"},
    )
    with pytest.raises(SourceUnavailable):
        await client.download_attachment(original_url)


@respx.mock
async def test_too_many_redirects_raises():
    """More than _MAX_REDIRECT_HOPS redirects raises ValueError."""
    # Chain: url0 -> url1 -> url2 -> url3 -> url4 (4 redirects, exceeds limit of 3)
    base = "https://api.atlassian.com/ex/jira/abc123/rest/api/3/attachment/content"
    for i in range(4):
        respx.get(f"{base}/{i}").mock(
            return_value=httpx.Response(302, headers={"Location": f"{base}/{i + 1}"})
        )
    respx.get(f"{base}/4").mock(return_value=httpx.Response(200, content=b"data"))

    client = JiraClient(
        "https://api.atlassian.com/ex/jira/abc123/rest/api/3",
        "Bearer tok",
        {"api.atlassian.com"},
    )
    with pytest.raises(SourceUnavailable):
        await client.download_attachment(f"{base}/0")


@respx.mock
async def test_redirect_strips_auth_on_cross_host():
    """Auth header is not sent to a different host after redirect."""
    redirect_url = "https://api.media.atlassian.com/file/abc123"
    original_url = "https://api.atlassian.com/ex/jira/abc123/rest/api/3/attachment/content/1"
    respx.get(original_url).mock(
        return_value=httpx.Response(302, headers={"Location": redirect_url})
    )
    respx.get(redirect_url).mock(return_value=httpx.Response(200, content=b"filedata"))

    client = JiraClient(
        "https://api.atlassian.com/ex/jira/abc123/rest/api/3",
        "Bearer tok",
        {"api.atlassian.com", "api.media.atlassian.com"},
    )
    await client.download_attachment(original_url)

    # The CDN request must not carry the Authorization header
    cdn_request = respx.calls[-1].request
    assert "authorization" not in {k.lower() for k in cdn_request.headers}


def test_init_rejects_schemeless_base_url():
    with pytest.raises(ValueError, match="absolute HTTPS URL"):
        JiraClient("api.atlassian.com/ex/jira/abc/rest/api/3", "Bearer tok", {"api.atlassian.com"})


def test_init_rejects_http_base_url():
    with pytest.raises(ValueError, match="must use HTTPS"):
        JiraClient("http://test.atlassian.net/rest/api/3", "Basic tok", {"test.atlassian.net"})


# -- JiraConnector URL rewriting -----------------------------------------------

def _make_oauth_connector() -> JiraConnector:
    conn = JiraConnection(
        domain="mysite.atlassian.net",
        auth=JiraOAuthAuth(
            auth_type="oauth",
            cloud_id="abc-123",
            access_token="tok",
            refresh_token="rtok",
            expires_at=9999999999,
            client_id="cid",
            client_secret="csec",
        ),
    )
    return JiraConnector(conn)


def _make_token_connector() -> JiraConnector:
    conn = JiraConnection(
        domain="mysite.atlassian.net",
        auth=TokenAuth(auth_type="token", email="u@test.com", api_token="tok"),
    )
    return JiraConnector(conn)


def test_rewrite_token_returns_url_unchanged():
    connector = _make_token_connector()
    url = "https://mysite.atlassian.net/rest/api/3/attachment/content/10001"
    assert connector._rewrite_attachment_url(url) == url


def test_rewrite_oauth_atlassian_net_rewrites_to_proxy():
    connector = _make_oauth_connector()
    url = "https://mysite.atlassian.net/rest/api/3/attachment/content/10001"
    expected = "https://api.atlassian.com/ex/jira/abc-123/rest/api/3/attachment/content/10001"
    assert connector._rewrite_attachment_url(url) == expected


def test_rewrite_oauth_already_proxy_url_unchanged():
    connector = _make_oauth_connector()
    url = "https://api.atlassian.com/ex/jira/abc-123/rest/api/3/attachment/content/10001"
    assert connector._rewrite_attachment_url(url) == url


def test_rewrite_oauth_url_without_rest_api_rewrites_to_proxy():
    connector = _make_oauth_connector()
    url = "https://mysite.atlassian.net/files/attachment/10001"
    expected = "https://api.atlassian.com/ex/jira/abc-123/files/attachment/10001"
    assert connector._rewrite_attachment_url(url) == expected


def test_rewrite_token_rejects_wrong_host():
    connector = _make_token_connector()
    with pytest.raises(SourceUnavailable, match="does not match the configured domain"):
        connector._rewrite_attachment_url("https://evil.example.com/rest/api/3/attachment/content/1")


async def test_download_attachment_ssrf_redirect_raises_source_unavailable(token_connection):
    """Redirecting to a disallowed host must raise SourceUnavailable, not ValueError."""
    from icx_engine.connectors.jira.client import JiraClient
    from icx_engine.exceptions import SourceUnavailable
    import respx
    import httpx

    client = JiraClient(
        base_url=f"https://{token_connection.domain}/rest/api/3",
        auth_header="Basic dGVzdA==",
        allowed_hosts={token_connection.domain},
    )

    with respx.mock:
        respx.get(f"https://{token_connection.domain}/content/1").mock(
            return_value=httpx.Response(
                302, headers={"Location": "https://external-evil.com/file.bin"}
            )
        )
        with pytest.raises(SourceUnavailable) as exc_info:
            await client.download_attachment(f"https://{token_connection.domain}/content/1")

    msg = str(exc_info.value).lower()
    assert re.search(r"(?<![.\w])external-evil\.com(?![.\w])", msg) or "ssrf" in msg or "allowed" in msg


async def test_download_attachment_size_limit_raises_source_unavailable(token_connection):
    """Exceeding the size limit must raise SourceUnavailable, not ValueError."""
    from icx_engine.connectors.jira.client import JiraClient, _MAX_ATTACHMENT_BYTES
    from icx_engine.exceptions import SourceUnavailable
    import respx
    import httpx

    client = JiraClient(
        base_url=f"https://{token_connection.domain}/rest/api/3",
        auth_header="Basic dGVzdA==",
        allowed_hosts={token_connection.domain},
    )
    oversized = b"x" * (_MAX_ATTACHMENT_BYTES + 1)

    with respx.mock:
        respx.get(f"https://{token_connection.domain}/content/1").mock(
            return_value=httpx.Response(200, content=oversized)
        )
        with pytest.raises(SourceUnavailable):
            await client.download_attachment(f"https://{token_connection.domain}/content/1")


# -- Write transport: get_transitions / get_editmeta / transition_issue / update_fields --

def _write_client() -> JiraClient:
    return JiraClient(
        "https://test.atlassian.net/rest/api/3",
        "Basic dGVzdA==",
        {"test.atlassian.net"},
    )


@respx.mock
async def test_get_transitions_parses_response():
    url = "https://test.atlassian.net/rest/api/3/issue/ABC-1/transitions"
    respx.get(url).mock(return_value=httpx.Response(200, json={
        "transitions": [
            {"id": "11", "name": "To Do", "fields": {}},
            {"id": "21", "name": "Done", "fields": {"resolution": {"required": True}}},
        ],
    }))
    client = _write_client()
    result = await client.get_transitions("ABC-1")
    assert result == [
        {"id": "11", "name": "To Do", "fields": {}},
        {"id": "21", "name": "Done", "fields": {"resolution": {"required": True}}},
    ]
    assert respx.calls.last.request.url.params["expand"] == "transitions.fields"


@respx.mock
async def test_get_editmeta_parses_response():
    url = "https://test.atlassian.net/rest/api/3/issue/ABC-1/editmeta"
    respx.get(url).mock(return_value=httpx.Response(200, json={
        "fields": {
            "summary": {"required": True, "schema": {"type": "string"}},
            "resolution": {"required": False, "allowedValues": [{"name": "Done"}]},
        },
    }))
    client = _write_client()
    result = await client.get_editmeta("ABC-1")
    assert result == {
        "summary": {"required": True, "schema": {"type": "string"}},
        "resolution": {"required": False, "allowedValues": [{"name": "Done"}]},
    }


@respx.mock
async def test_transition_issue_with_only_transition_id_posts_transition_only():
    url = "https://test.atlassian.net/rest/api/3/issue/ABC-1/transitions"
    route = respx.post(url).mock(return_value=httpx.Response(204))
    client = _write_client()
    await client.transition_issue("ABC-1", transition_id="31")
    assert route.called
    body = json.loads(route.calls.last.request.content)
    assert body == {"transition": {"id": "31"}}


@respx.mock
async def test_transition_issue_with_transition_id_fields_and_comment_posts_all_three():
    url = "https://test.atlassian.net/rest/api/3/issue/ABC-1/transitions"
    route = respx.post(url).mock(return_value=httpx.Response(204))
    client = _write_client()
    comment_adf = {"type": "doc", "version": 1, "content": []}
    await client.transition_issue(
        "ABC-1",
        transition_id="31",
        fields={"resolution": {"name": "Done"}},
        comment_adf=comment_adf,
    )
    body = json.loads(route.calls.last.request.content)
    assert body == {
        "transition": {"id": "31"},
        "fields": {"resolution": {"name": "Done"}},
        "update": {"comment": [{"add": {"body": comment_adf}}]},
    }


@respx.mock
async def test_transition_issue_without_transition_id_puts_fields_only():
    put_url = "https://test.atlassian.net/rest/api/3/issue/ABC-1"
    put_route = respx.put(put_url).mock(return_value=httpx.Response(204))
    client = _write_client()
    await client.transition_issue("ABC-1", fields={"summary": "New title"})
    assert put_route.called
    body = json.loads(put_route.calls.last.request.content)
    assert body == {"fields": {"summary": "New title"}}
    transitions_calls = [
        call for call in respx.calls
        if call.request.url.path.endswith("/transitions")
    ]
    assert transitions_calls == []


@respx.mock
async def test_update_fields_puts_fields_only():
    url = "https://test.atlassian.net/rest/api/3/issue/ABC-1"
    route = respx.put(url).mock(return_value=httpx.Response(204))
    client = _write_client()
    await client.update_fields("ABC-1", {"summary": "x"})
    assert route.called
    body = json.loads(route.calls.last.request.content)
    assert body == {"fields": {"summary": "x"}}


@respx.mock
async def test_transition_issue_400_raises_jira_validation_error_with_errors():
    url = "https://test.atlassian.net/rest/api/3/issue/ABC-1/transitions"
    respx.post(url).mock(return_value=httpx.Response(400, json={
        "errorMessages": [],
        "errors": {"resolution": "Resolution is required."},
    }))
    client = _write_client()
    with pytest.raises(JiraValidationError) as exc_info:
        await client.transition_issue("ABC-1", transition_id="31")
    assert exc_info.value.errors == {"resolution": "Resolution is required."}


@respx.mock
async def test_update_fields_400_raises_jira_validation_error_with_errors():
    url = "https://test.atlassian.net/rest/api/3/issue/ABC-1"
    respx.put(url).mock(return_value=httpx.Response(400, json={
        "errorMessages": ["Field 'duedate' is invalid"],
        "errors": {"duedate": "Date must be in the future."},
    }))
    client = _write_client()
    with pytest.raises(JiraValidationError) as exc_info:
        await client.update_fields("ABC-1", {"duedate": "2020-01-01"})
    assert exc_info.value.errors == {"duedate": "Date must be in the future."}


@respx.mock
async def test_transition_issue_404_raises_issue_not_found():
    url = "https://test.atlassian.net/rest/api/3/issue/ABC-1/transitions"
    respx.post(url).mock(return_value=httpx.Response(404))
    client = _write_client()
    with pytest.raises(IssueNotFound):
        await client.transition_issue("ABC-1", transition_id="31")


@respx.mock
async def test_get_transitions_404_raises_issue_not_found():
    url = "https://test.atlassian.net/rest/api/3/issue/ABC-1/transitions"
    respx.get(url).mock(return_value=httpx.Response(404))
    client = _write_client()
    with pytest.raises(IssueNotFound):
        await client.get_transitions("ABC-1")


# -- Task 1: list_issuetypes / get_createmeta_fields / create_issue / delete_issue --

@respx.mock
async def test_list_issuetypes_parses_values():
    url = "https://test.atlassian.net/rest/api/3/issue/createmeta/ABC/issuetypes"
    respx.get(url).mock(return_value=httpx.Response(200, json={
        "values": [{"id": "10001", "name": "Bug"}, {"id": "10002", "name": "Task"}],
    }))
    client = _write_client()
    result = await client.list_issuetypes("ABC")
    assert result == [{"id": "10001", "name": "Bug"}, {"id": "10002", "name": "Task"}]


@respx.mock
async def test_get_createmeta_fields_rekeys_values_by_field_id():
    url = "https://test.atlassian.net/rest/api/3/issue/createmeta/ABC/issuetypes/10001"
    respx.get(url).mock(return_value=httpx.Response(200, json={
        "values": [
            {"fieldId": "summary", "required": True, "schema": {"type": "string"}},
            {"fieldId": "priority", "required": False},
        ],
    }))
    client = _write_client()
    result = await client.get_createmeta_fields("ABC", "10001")
    assert result == {
        "summary": {"fieldId": "summary", "required": True, "schema": {"type": "string"}},
        "priority": {"fieldId": "priority", "required": False},
    }


@respx.mock
async def test_create_issue_posts_minimal_body_and_returns_key():
    url = "https://test.atlassian.net/rest/api/3/issue"
    route = respx.post(url).mock(return_value=httpx.Response(201, json={
        "id": "10005", "key": "ABC-42", "self": "https://test.atlassian.net/rest/api/3/issue/10005",
    }))
    client = _write_client()
    key = await client.create_issue("ABC", "Bug", "Something is broken")
    assert key == "ABC-42"
    body = json.loads(route.calls.last.request.content)
    assert body == {
        "fields": {
            "project": {"key": "ABC"},
            "issuetype": {"name": "Bug"},
            "summary": "Something is broken",
        }
    }


@respx.mock
async def test_create_issue_merges_extra_fields():
    url = "https://test.atlassian.net/rest/api/3/issue"
    route = respx.post(url).mock(return_value=httpx.Response(201, json={"key": "ABC-43"}))
    client = _write_client()
    key = await client.create_issue(
        "ABC", "Task", "Do the thing", extra_fields={"priority": {"name": "High"}},
    )
    assert key == "ABC-43"
    body = json.loads(route.calls.last.request.content)
    assert body == {
        "fields": {
            "project": {"key": "ABC"},
            "issuetype": {"name": "Task"},
            "summary": "Do the thing",
            "priority": {"name": "High"},
        }
    }


@respx.mock
async def test_create_issue_400_raises_jira_validation_error_with_errors():
    url = "https://test.atlassian.net/rest/api/3/issue"
    respx.post(url).mock(return_value=httpx.Response(400, json={
        "errorMessages": [],
        "errors": {"summary": "Summary is required."},
    }))
    client = _write_client()
    with pytest.raises(JiraValidationError) as exc_info:
        await client.create_issue("ABC", "Bug", "")
    assert exc_info.value.errors == {"summary": "Summary is required."}


@respx.mock
async def test_delete_issue_sends_no_query_param_by_default():
    url = "https://test.atlassian.net/rest/api/3/issue/ABC-1"
    route = respx.delete(url).mock(return_value=httpx.Response(204))
    client = _write_client()
    await client.delete_issue("ABC-1")
    assert route.called
    assert "deleteSubtasks" not in route.calls.last.request.url.params


@respx.mock
async def test_delete_issue_sends_delete_subtasks_query_param_when_true():
    url = "https://test.atlassian.net/rest/api/3/issue/ABC-1"
    route = respx.delete(url).mock(return_value=httpx.Response(204))
    client = _write_client()
    await client.delete_issue("ABC-1", delete_subtasks=True)
    assert route.called
    assert route.calls.last.request.url.params["deleteSubtasks"] == "true"


@respx.mock
async def test_delete_issue_400_with_subtasks_present_raises_jira_validation_error():
    url = "https://test.atlassian.net/rest/api/3/issue/ABC-1"
    respx.delete(url).mock(return_value=httpx.Response(400, json={
        "errorMessages": ["The issue has subtasks - set deleteSubtasks to delete them too."],
        "errors": {},
    }))
    client = _write_client()
    with pytest.raises(JiraValidationError) as exc_info:
        await client.delete_issue("ABC-1")
    assert "subtasks" in str(exc_info.value).lower()


@respx.mock
async def test_delete_issue_404_raises_issue_not_found():
    url = "https://test.atlassian.net/rest/api/3/issue/ABC-1"
    respx.delete(url).mock(return_value=httpx.Response(404))
    client = _write_client()
    with pytest.raises(IssueNotFound):
        await client.delete_issue("ABC-1")


# -- Task 2: list_comments / add_comment / edit_comment / delete_comment ----

@respx.mock
async def test_list_comments_parses_response():
    url = "https://test.atlassian.net/rest/api/3/issue/ABC-1/comment"
    respx.get(url).mock(return_value=httpx.Response(200, json={
        "comments": [
            {"id": "10001", "body": {"type": "doc"}, "author": {"displayName": "Alice"}},
        ],
    }))
    client = _write_client()
    result = await client.list_comments("ABC-1")
    assert result == [{"id": "10001", "body": {"type": "doc"}, "author": {"displayName": "Alice"}}]


@respx.mock
async def test_list_comments_no_comments_key_returns_empty_list():
    url = "https://test.atlassian.net/rest/api/3/issue/ABC-1/comment"
    respx.get(url).mock(return_value=httpx.Response(200, json={}))
    client = _write_client()
    result = await client.list_comments("ABC-1")
    assert result == []


@respx.mock
async def test_list_comments_404_raises_issue_not_found():
    url = "https://test.atlassian.net/rest/api/3/issue/ABC-1/comment"
    respx.get(url).mock(return_value=httpx.Response(404))
    client = _write_client()
    with pytest.raises(IssueNotFound):
        await client.list_comments("ABC-1")


@respx.mock
async def test_add_comment_posts_body_and_returns_created_comment():
    url = "https://test.atlassian.net/rest/api/3/issue/ABC-1/comment"
    route = respx.post(url).mock(return_value=httpx.Response(201, json={
        "id": "10002", "body": {"type": "doc", "version": 1, "content": []},
    }))
    client = _write_client()
    body_adf = {
        "type": "doc", "version": 1,
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": "hi"}]}],
    }
    result = await client.add_comment("ABC-1", body_adf)
    assert result == {"id": "10002", "body": {"type": "doc", "version": 1, "content": []}}
    body = json.loads(route.calls.last.request.content)
    assert body == {"body": body_adf}


@respx.mock
async def test_add_comment_400_raises_jira_validation_error_with_errors():
    url = "https://test.atlassian.net/rest/api/3/issue/ABC-1/comment"
    respx.post(url).mock(return_value=httpx.Response(400, json={
        "errorMessages": [],
        "errors": {"body": "Comment body is required."},
    }))
    client = _write_client()
    with pytest.raises(JiraValidationError) as exc_info:
        await client.add_comment("ABC-1", {"type": "doc"})
    assert exc_info.value.errors == {"body": "Comment body is required."}


@respx.mock
async def test_add_comment_404_raises_issue_not_found():
    url = "https://test.atlassian.net/rest/api/3/issue/ABC-1/comment"
    respx.post(url).mock(return_value=httpx.Response(404))
    client = _write_client()
    with pytest.raises(IssueNotFound):
        await client.add_comment("ABC-1", {"type": "doc"})


@respx.mock
async def test_edit_comment_puts_body_and_returns_updated_comment():
    url = "https://test.atlassian.net/rest/api/3/issue/ABC-1/comment/10002"
    route = respx.put(url).mock(return_value=httpx.Response(200, json={
        "id": "10002", "body": {"type": "doc", "version": 1, "content": []},
    }))
    client = _write_client()
    body_adf = {
        "type": "doc", "version": 1,
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": "edited"}]}],
    }
    result = await client.edit_comment("ABC-1", "10002", body_adf)
    assert result == {"id": "10002", "body": {"type": "doc", "version": 1, "content": []}}
    body = json.loads(route.calls.last.request.content)
    assert body == {"body": body_adf}


@respx.mock
async def test_edit_comment_400_raises_jira_validation_error_with_errors():
    url = "https://test.atlassian.net/rest/api/3/issue/ABC-1/comment/10002"
    respx.put(url).mock(return_value=httpx.Response(400, json={
        "errorMessages": [],
        "errors": {"body": "Comment body is required."},
    }))
    client = _write_client()
    with pytest.raises(JiraValidationError) as exc_info:
        await client.edit_comment("ABC-1", "10002", {"type": "doc"})
    assert exc_info.value.errors == {"body": "Comment body is required."}


@respx.mock
async def test_edit_comment_404_raises_issue_not_found():
    url = "https://test.atlassian.net/rest/api/3/issue/ABC-1/comment/10002"
    respx.put(url).mock(return_value=httpx.Response(404))
    client = _write_client()
    with pytest.raises(IssueNotFound):
        await client.edit_comment("ABC-1", "10002", {"type": "doc"})


@respx.mock
async def test_delete_comment_sends_delete_request():
    url = "https://test.atlassian.net/rest/api/3/issue/ABC-1/comment/10002"
    route = respx.delete(url).mock(return_value=httpx.Response(204))
    client = _write_client()
    await client.delete_comment("ABC-1", "10002")
    assert route.called


@respx.mock
async def test_delete_comment_400_raises_jira_validation_error():
    url = "https://test.atlassian.net/rest/api/3/issue/ABC-1/comment/10002"
    respx.delete(url).mock(return_value=httpx.Response(400, json={
        "errorMessages": ["Comment does not exist or you do not have permission to delete it."],
        "errors": {},
    }))
    client = _write_client()
    with pytest.raises(JiraValidationError):
        await client.delete_comment("ABC-1", "10002")


@respx.mock
async def test_delete_comment_404_raises_issue_not_found():
    url = "https://test.atlassian.net/rest/api/3/issue/ABC-1/comment/10002"
    respx.delete(url).mock(return_value=httpx.Response(404))
    client = _write_client()
    with pytest.raises(IssueNotFound):
        await client.delete_comment("ABC-1", "10002")


# -- Task 3: search_issues / get_issue_raw -----------------------------------

@respx.mock
async def test_search_issues_posts_body_with_fields_and_max_results():
    url = "https://test.atlassian.net/rest/api/3/search/jql"
    route = respx.post(url).mock(return_value=httpx.Response(200, json={
        "issues": [{"key": "ABC-1"}], "nextPageToken": None, "isLast": True,
    }))
    client = _write_client()
    result = await client.search_issues("project = ABC", fields=["summary"], max_results=10)
    body = json.loads(route.calls.last.request.content)
    assert body == {"jql": "project = ABC", "fields": ["summary"], "maxResults": 10}
    assert result == {"issues": [{"key": "ABC-1"}], "next_page_token": None, "is_last": True}


@respx.mock
async def test_search_issues_omits_next_page_token_key_when_none():
    url = "https://test.atlassian.net/rest/api/3/search/jql"
    route = respx.post(url).mock(return_value=httpx.Response(200, json={"issues": []}))
    client = _write_client()
    await client.search_issues("project = ABC")
    body = json.loads(route.calls.last.request.content)
    assert "nextPageToken" not in body


@respx.mock
async def test_search_issues_includes_next_page_token_when_given():
    url = "https://test.atlassian.net/rest/api/3/search/jql"
    route = respx.post(url).mock(return_value=httpx.Response(200, json={
        "issues": [], "nextPageToken": "tok2", "isLast": False,
    }))
    client = _write_client()
    result = await client.search_issues("project = ABC", page_token="tok1")
    body = json.loads(route.calls.last.request.content)
    assert body["nextPageToken"] == "tok1"
    assert result["next_page_token"] == "tok2"
    assert result["is_last"] is False


@respx.mock
async def test_search_issues_defaults_passed_through_unmodified():
    """The client itself does not clamp max_results or default fields - that
    ICX-side cost discipline lives in jira/service.py's search(), not here."""
    url = "https://test.atlassian.net/rest/api/3/search/jql"
    route = respx.post(url).mock(return_value=httpx.Response(200, json={"issues": []}))
    client = _write_client()
    await client.search_issues("project = ABC")
    body = json.loads(route.calls.last.request.content)
    assert body == {"jql": "project = ABC", "fields": None, "maxResults": 50}


@respx.mock
async def test_search_issues_400_raises_source_unavailable():
    url = "https://test.atlassian.net/rest/api/3/search/jql"
    respx.post(url).mock(return_value=httpx.Response(400, json={"errorMessages": ["Invalid JQL"]}))
    client = _write_client()
    with pytest.raises(SourceUnavailable):
        await client.search_issues("bad jql (")


@respx.mock
async def test_search_issues_no_issues_key_returns_empty_list():
    url = "https://test.atlassian.net/rest/api/3/search/jql"
    respx.post(url).mock(return_value=httpx.Response(200, json={}))
    client = _write_client()
    result = await client.search_issues("project = ABC")
    assert result == {"issues": [], "next_page_token": None, "is_last": True}


@respx.mock
async def test_get_issue_raw_gets_with_fields_query_param():
    url = "https://test.atlassian.net/rest/api/3/issue/ABC-1"
    route = respx.get(url).mock(return_value=httpx.Response(200, json={
        "key": "ABC-1", "fields": {"summary": "x"},
    }))
    client = _write_client()
    result = await client.get_issue_raw("ABC-1", fields=["summary", "status"])
    assert result == {"key": "ABC-1", "fields": {"summary": "x"}}
    assert route.calls.last.request.url.params["fields"] == "summary,status"


@respx.mock
async def test_get_issue_raw_without_fields_omits_query_param():
    url = "https://test.atlassian.net/rest/api/3/issue/ABC-1"
    route = respx.get(url).mock(return_value=httpx.Response(200, json={"key": "ABC-1"}))
    client = _write_client()
    await client.get_issue_raw("ABC-1")
    assert "fields" not in route.calls.last.request.url.params


@respx.mock
async def test_get_issue_raw_404_raises_issue_not_found():
    url = "https://test.atlassian.net/rest/api/3/issue/ABC-1"
    respx.get(url).mock(return_value=httpx.Response(404))
    client = _write_client()
    with pytest.raises(IssueNotFound):
        await client.get_issue_raw("ABC-1")


# -- Task 4: list_link_types / create_link / delete_link / set_assignee -----

@respx.mock
async def test_list_link_types_parses_response():
    url = "https://test.atlassian.net/rest/api/3/issueLinkType"
    respx.get(url).mock(return_value=httpx.Response(200, json={
        "issueLinkTypes": [
            {"id": "10000", "name": "Blocks", "inward": "is blocked by", "outward": "blocks"},
        ],
    }))
    client = _write_client()
    result = await client.list_link_types()
    assert result == [{"id": "10000", "name": "Blocks", "inward": "is blocked by", "outward": "blocks"}]


@respx.mock
async def test_list_link_types_no_key_returns_empty_list():
    url = "https://test.atlassian.net/rest/api/3/issueLinkType"
    respx.get(url).mock(return_value=httpx.Response(200, json={}))
    client = _write_client()
    result = await client.list_link_types()
    assert result == []


@respx.mock
async def test_list_link_types_404_raises_issue_not_found():
    url = "https://test.atlassian.net/rest/api/3/issueLinkType"
    respx.get(url).mock(return_value=httpx.Response(404))
    client = _write_client()
    with pytest.raises(IssueNotFound):
        await client.list_link_types()


@respx.mock
async def test_create_link_posts_correct_body():
    url = "https://test.atlassian.net/rest/api/3/issueLink"
    route = respx.post(url).mock(return_value=httpx.Response(201))
    client = _write_client()
    await client.create_link("Blocks", "ABC-1", "ABC-2")
    assert route.called
    body = json.loads(route.calls.last.request.content)
    assert body == {
        "type": {"name": "Blocks"},
        "inwardIssue": {"key": "ABC-1"},
        "outwardIssue": {"key": "ABC-2"},
    }


@respx.mock
async def test_create_link_400_raises_jira_validation_error_with_errors():
    url = "https://test.atlassian.net/rest/api/3/issueLink"
    respx.post(url).mock(return_value=httpx.Response(400, json={
        "errorMessages": [],
        "errors": {"type": "Link type does not exist."},
    }))
    client = _write_client()
    with pytest.raises(JiraValidationError) as exc_info:
        await client.create_link("NotReal", "ABC-1", "ABC-2")
    assert exc_info.value.errors == {"type": "Link type does not exist."}


@respx.mock
async def test_create_link_404_raises_issue_not_found():
    url = "https://test.atlassian.net/rest/api/3/issueLink"
    respx.post(url).mock(return_value=httpx.Response(404))
    client = _write_client()
    with pytest.raises(IssueNotFound):
        await client.create_link("Blocks", "ABC-1", "ABC-2")


@respx.mock
async def test_delete_link_sends_delete_to_global_endpoint():
    url = "https://test.atlassian.net/rest/api/3/issueLink/10050"
    route = respx.delete(url).mock(return_value=httpx.Response(204))
    client = _write_client()
    await client.delete_link("10050")
    assert route.called


@respx.mock
async def test_delete_link_400_raises_jira_validation_error():
    url = "https://test.atlassian.net/rest/api/3/issueLink/10050"
    respx.delete(url).mock(return_value=httpx.Response(400, json={
        "errorMessages": ["Link does not exist or you do not have permission to delete it."],
        "errors": {},
    }))
    client = _write_client()
    with pytest.raises(JiraValidationError):
        await client.delete_link("10050")


@respx.mock
async def test_delete_link_404_raises_issue_not_found():
    url = "https://test.atlassian.net/rest/api/3/issueLink/10050"
    respx.delete(url).mock(return_value=httpx.Response(404))
    client = _write_client()
    with pytest.raises(IssueNotFound):
        await client.delete_link("10050")


@respx.mock
async def test_set_assignee_with_account_id_sends_that_id():
    url = "https://test.atlassian.net/rest/api/3/issue/ABC-1/assignee"
    route = respx.put(url).mock(return_value=httpx.Response(204))
    client = _write_client()
    await client.set_assignee("ABC-1", "5b10a2844c20165700ede21g")
    assert route.called
    body = json.loads(route.calls.last.request.content)
    assert body == {"accountId": "5b10a2844c20165700ede21g"}


@respx.mock
async def test_set_assignee_default_sentinel_sends_literal_string_minus_one():
    url = "https://test.atlassian.net/rest/api/3/issue/ABC-1/assignee"
    route = respx.put(url).mock(return_value=httpx.Response(204))
    client = _write_client()
    await client.set_assignee("ABC-1", "-1")
    body = json.loads(route.calls.last.request.content)
    assert body == {"accountId": "-1"}
    assert isinstance(body["accountId"], str)


@respx.mock
async def test_set_assignee_none_sends_json_null():
    url = "https://test.atlassian.net/rest/api/3/issue/ABC-1/assignee"
    route = respx.put(url).mock(return_value=httpx.Response(204))
    client = _write_client()
    await client.set_assignee("ABC-1", None)
    raw_body = route.calls.last.request.content
    body = json.loads(raw_body)
    assert body == {"accountId": None}
    assert b"null" in raw_body


@respx.mock
async def test_set_assignee_400_raises_jira_validation_error():
    url = "https://test.atlassian.net/rest/api/3/issue/ABC-1/assignee"
    respx.put(url).mock(return_value=httpx.Response(400, json={
        "errorMessages": ["Account id does not exist."],
        "errors": {},
    }))
    client = _write_client()
    with pytest.raises(JiraValidationError):
        await client.set_assignee("ABC-1", "bogus-account")


@respx.mock
async def test_set_assignee_404_raises_issue_not_found():
    url = "https://test.atlassian.net/rest/api/3/issue/ABC-1/assignee"
    respx.put(url).mock(return_value=httpx.Response(404))
    client = _write_client()
    with pytest.raises(IssueNotFound):
        await client.set_assignee("ABC-1", "acc-1")


# -- Task 5: upload_attachment / delete_attachment ---------------------------

@respx.mock
async def test_upload_attachment_sends_x_atlassian_token_header():
    """The real gotcha this task exists for: Jira's attachment upload endpoint
    silently 403s without this exact header. Must be asserted on the actual
    captured request, not just that the call succeeds against a mock that
    ignores headers entirely."""
    url = "https://test.atlassian.net/rest/api/3/issue/ABC-1/attachments"
    route = respx.post(url).mock(return_value=httpx.Response(200, json=[
        {"id": "10100", "filename": "report.txt"},
    ]))
    client = _write_client()
    await client.upload_attachment("ABC-1", "report.txt", b"hello world")
    assert route.called
    sent_headers = route.calls.last.request.headers
    assert sent_headers["X-Atlassian-Token"] == "no-check"


@respx.mock
async def test_upload_attachment_returns_metadata_array():
    url = "https://test.atlassian.net/rest/api/3/issue/ABC-1/attachments"
    respx.post(url).mock(return_value=httpx.Response(200, json=[
        {"id": "10100", "filename": "report.txt", "size": 11},
    ]))
    client = _write_client()
    result = await client.upload_attachment("ABC-1", "report.txt", b"hello world")
    assert result == [{"id": "10100", "filename": "report.txt", "size": 11}]


@respx.mock
async def test_upload_attachment_uses_multipart_field_name_file_and_filename():
    url = "https://test.atlassian.net/rest/api/3/issue/ABC-1/attachments"
    route = respx.post(url).mock(return_value=httpx.Response(200, json=[]))
    client = _write_client()
    await client.upload_attachment("ABC-1", "report.txt", b"hello world")
    sent = route.calls.last.request
    body = sent.content.decode("utf-8", errors="replace")
    assert 'name="file"' in body
    assert 'filename="report.txt"' in body
    assert "hello world" in body
    assert sent.headers["Content-Type"].startswith("multipart/form-data")


@respx.mock
async def test_upload_attachment_uses_given_content_type():
    url = "https://test.atlassian.net/rest/api/3/issue/ABC-1/attachments"
    route = respx.post(url).mock(return_value=httpx.Response(200, json=[]))
    client = _write_client()
    await client.upload_attachment("ABC-1", "photo.png", b"\x89PNG", content_type="image/png")
    body = route.calls.last.request.content.decode("utf-8", errors="replace")
    assert "Content-Type: image/png" in body


@respx.mock
async def test_upload_attachment_defaults_content_type_when_omitted():
    url = "https://test.atlassian.net/rest/api/3/issue/ABC-1/attachments"
    route = respx.post(url).mock(return_value=httpx.Response(200, json=[]))
    client = _write_client()
    await client.upload_attachment("ABC-1", "data.bin", b"\x00\x01")
    body = route.calls.last.request.content.decode("utf-8", errors="replace")
    assert "Content-Type: application/octet-stream" in body


@respx.mock
async def test_upload_attachment_400_raises_jira_validation_error():
    url = "https://test.atlassian.net/rest/api/3/issue/ABC-1/attachments"
    respx.post(url).mock(return_value=httpx.Response(400, json={
        "errorMessages": ["Attachment exceeds the maximum configured attachment size."],
        "errors": {},
    }))
    client = _write_client()
    with pytest.raises(JiraValidationError):
        await client.upload_attachment("ABC-1", "huge.bin", b"x" * 10)


@respx.mock
async def test_upload_attachment_404_raises_issue_not_found():
    url = "https://test.atlassian.net/rest/api/3/issue/ABC-1/attachments"
    respx.post(url).mock(return_value=httpx.Response(404))
    client = _write_client()
    with pytest.raises(IssueNotFound):
        await client.upload_attachment("ABC-1", "report.txt", b"hello")


@respx.mock
async def test_delete_attachment_sends_delete_to_global_endpoint():
    url = "https://test.atlassian.net/rest/api/3/attachment/10100"
    route = respx.delete(url).mock(return_value=httpx.Response(204))
    client = _write_client()
    await client.delete_attachment("10100")
    assert route.called


@respx.mock
async def test_delete_attachment_400_raises_jira_validation_error():
    url = "https://test.atlassian.net/rest/api/3/attachment/10100"
    respx.delete(url).mock(return_value=httpx.Response(400, json={
        "errorMessages": ["Attachment does not exist or you do not have permission to delete it."],
        "errors": {},
    }))
    client = _write_client()
    with pytest.raises(JiraValidationError):
        await client.delete_attachment("10100")


@respx.mock
async def test_delete_attachment_404_raises_issue_not_found():
    url = "https://test.atlassian.net/rest/api/3/attachment/10100"
    respx.delete(url).mock(return_value=httpx.Response(404))
    client = _write_client()
    with pytest.raises(IssueNotFound):
        await client.delete_attachment("10100")


# -- Task 6: get_current_user / watchers / worklog ---------------------------

@respx.mock
async def test_get_current_user_parses_response():
    url = "https://test.atlassian.net/rest/api/3/myself"
    respx.get(url).mock(return_value=httpx.Response(200, json={
        "accountId": "5b10a2844c20165700ede21g", "displayName": "Alice",
    }))
    client = _write_client()
    result = await client.get_current_user()
    assert result == {"accountId": "5b10a2844c20165700ede21g", "displayName": "Alice"}


@respx.mock
async def test_get_current_user_401_raises():
    from icx_engine.exceptions import AuthError
    url = "https://test.atlassian.net/rest/api/3/myself"
    respx.get(url).mock(return_value=httpx.Response(401))
    client = _write_client()
    with pytest.raises(AuthError):
        await client.get_current_user()


@respx.mock
async def test_list_watchers_parses_response():
    url = "https://test.atlassian.net/rest/api/3/issue/ABC-1/watchers"
    respx.get(url).mock(return_value=httpx.Response(200, json={
        "watchers": [{"accountId": "acc-1", "displayName": "Alice"}], "watchCount": 1,
    }))
    client = _write_client()
    result = await client.list_watchers("ABC-1")
    assert result == {"watchers": [{"accountId": "acc-1", "displayName": "Alice"}], "watchCount": 1}


@respx.mock
async def test_list_watchers_404_raises_issue_not_found():
    url = "https://test.atlassian.net/rest/api/3/issue/ABC-1/watchers"
    respx.get(url).mock(return_value=httpx.Response(404))
    client = _write_client()
    with pytest.raises(IssueNotFound):
        await client.list_watchers("ABC-1")


@respx.mock
async def test_add_watcher_posts_bare_string_body_not_object():
    """The real gotcha this task exists for: the POST body is a bare JSON
    string (the accountId), NOT {"accountId": ...} like every other write in
    this codebase. Must assert the actual request body type, not just that
    the call succeeds against a mock that accepts anything."""
    url = "https://test.atlassian.net/rest/api/3/issue/ABC-1/watchers"
    route = respx.post(url).mock(return_value=httpx.Response(204))
    client = _write_client()
    await client.add_watcher("ABC-1", "5b10a2844c20165700ede21g")
    assert route.called
    raw_body = route.calls.last.request.content
    parsed = json.loads(raw_body)
    assert parsed == "5b10a2844c20165700ede21g"
    assert isinstance(parsed, str)
    # A bare JSON string is quoted, not braced - the clearest possible proof
    # this is not an object body.
    assert raw_body.strip().startswith(b'"')
    assert not raw_body.strip().startswith(b"{")


@respx.mock
async def test_add_watcher_400_raises_jira_validation_error():
    url = "https://test.atlassian.net/rest/api/3/issue/ABC-1/watchers"
    respx.post(url).mock(return_value=httpx.Response(400, json={
        "errorMessages": ["User does not exist."], "errors": {},
    }))
    client = _write_client()
    with pytest.raises(JiraValidationError):
        await client.add_watcher("ABC-1", "bogus-account")


@respx.mock
async def test_add_watcher_404_raises_issue_not_found():
    url = "https://test.atlassian.net/rest/api/3/issue/ABC-1/watchers"
    respx.post(url).mock(return_value=httpx.Response(404))
    client = _write_client()
    with pytest.raises(IssueNotFound):
        await client.add_watcher("ABC-1", "acc-1")


@respx.mock
async def test_remove_watcher_sends_account_id_as_query_param_not_body():
    url = "https://test.atlassian.net/rest/api/3/issue/ABC-1/watchers"
    route = respx.delete(url).mock(return_value=httpx.Response(204))
    client = _write_client()
    await client.remove_watcher("ABC-1", "acc-1")
    assert route.called
    assert route.calls.last.request.url.params["accountId"] == "acc-1"
    assert route.calls.last.request.content == b""


@respx.mock
async def test_remove_watcher_400_raises_jira_validation_error():
    url = "https://test.atlassian.net/rest/api/3/issue/ABC-1/watchers"
    respx.delete(url).mock(return_value=httpx.Response(400, json={
        "errorMessages": ["User is not a watcher."], "errors": {},
    }))
    client = _write_client()
    with pytest.raises(JiraValidationError):
        await client.remove_watcher("ABC-1", "acc-1")


@respx.mock
async def test_remove_watcher_404_raises_issue_not_found():
    url = "https://test.atlassian.net/rest/api/3/issue/ABC-1/watchers"
    respx.delete(url).mock(return_value=httpx.Response(404))
    client = _write_client()
    with pytest.raises(IssueNotFound):
        await client.remove_watcher("ABC-1", "acc-1")


@respx.mock
async def test_list_worklogs_parses_response():
    url = "https://test.atlassian.net/rest/api/3/issue/ABC-1/worklog"
    respx.get(url).mock(return_value=httpx.Response(200, json={
        "worklogs": [{"id": "100", "timeSpentSeconds": 3600, "author": {"accountId": "acc-1"}}],
    }))
    client = _write_client()
    result = await client.list_worklogs("ABC-1")
    assert result == {"worklogs": [{"id": "100", "timeSpentSeconds": 3600, "author": {"accountId": "acc-1"}}]}


@respx.mock
async def test_list_worklogs_404_raises_issue_not_found():
    url = "https://test.atlassian.net/rest/api/3/issue/ABC-1/worklog"
    respx.get(url).mock(return_value=httpx.Response(404))
    client = _write_client()
    with pytest.raises(IssueNotFound):
        await client.list_worklogs("ABC-1")


@respx.mock
async def test_add_worklog_posts_correct_body_with_numeric_offset_no_trailing_z():
    """The other real gotcha this task exists for: `started` must carry a
    numeric timezone offset with NO trailing 'Z'. Assert the exact string
    format on the actual request body."""
    url = "https://test.atlassian.net/rest/api/3/issue/ABC-1/worklog"
    route = respx.post(url).mock(return_value=httpx.Response(201, json={
        "id": "100", "timeSpentSeconds": 5400, "started": "2026-07-28T10:00:00.000+0000",
    }))
    client = _write_client()
    result = await client.add_worklog("ABC-1", 5400, "2026-07-28T10:00:00.000+0000")
    body = json.loads(route.calls.last.request.content)
    assert body == {"timeSpentSeconds": 5400, "started": "2026-07-28T10:00:00.000+0000"}
    assert not body["started"].endswith("Z")
    assert result == {"id": "100", "timeSpentSeconds": 5400, "started": "2026-07-28T10:00:00.000+0000"}


@respx.mock
async def test_add_worklog_includes_comment_when_given():
    url = "https://test.atlassian.net/rest/api/3/issue/ABC-1/worklog"
    route = respx.post(url).mock(return_value=httpx.Response(201, json={"id": "100"}))
    client = _write_client()
    comment_adf = {"type": "doc", "version": 1, "content": []}
    await client.add_worklog("ABC-1", 3600, "2026-07-28T10:00:00.000+0000", comment_adf=comment_adf)
    body = json.loads(route.calls.last.request.content)
    assert body["comment"] == comment_adf


@respx.mock
async def test_add_worklog_omits_comment_key_when_not_given():
    url = "https://test.atlassian.net/rest/api/3/issue/ABC-1/worklog"
    route = respx.post(url).mock(return_value=httpx.Response(201, json={"id": "100"}))
    client = _write_client()
    await client.add_worklog("ABC-1", 3600, "2026-07-28T10:00:00.000+0000")
    body = json.loads(route.calls.last.request.content)
    assert "comment" not in body


@respx.mock
async def test_add_worklog_400_raises_jira_validation_error():
    url = "https://test.atlassian.net/rest/api/3/issue/ABC-1/worklog"
    respx.post(url).mock(return_value=httpx.Response(400, json={
        "errorMessages": [], "errors": {"timeSpentSeconds": "Time spent is required."},
    }))
    client = _write_client()
    with pytest.raises(JiraValidationError) as exc_info:
        await client.add_worklog("ABC-1", 0, "2026-07-28T10:00:00.000+0000")
    assert exc_info.value.errors == {"timeSpentSeconds": "Time spent is required."}


@respx.mock
async def test_add_worklog_404_raises_issue_not_found():
    url = "https://test.atlassian.net/rest/api/3/issue/ABC-1/worklog"
    respx.post(url).mock(return_value=httpx.Response(404))
    client = _write_client()
    with pytest.raises(IssueNotFound):
        await client.add_worklog("ABC-1", 3600, "2026-07-28T10:00:00.000+0000")


@respx.mock
async def test_edit_worklog_puts_only_given_fields():
    url = "https://test.atlassian.net/rest/api/3/issue/ABC-1/worklog/100"
    route = respx.put(url).mock(return_value=httpx.Response(200, json={"id": "100", "timeSpentSeconds": 7200}))
    client = _write_client()
    result = await client.edit_worklog("ABC-1", "100", time_spent_seconds=7200)
    body = json.loads(route.calls.last.request.content)
    assert body == {"timeSpentSeconds": 7200}
    assert result == {"id": "100", "timeSpentSeconds": 7200}


@respx.mock
async def test_edit_worklog_puts_all_fields_when_all_given():
    url = "https://test.atlassian.net/rest/api/3/issue/ABC-1/worklog/100"
    route = respx.put(url).mock(return_value=httpx.Response(200, json={"id": "100"}))
    client = _write_client()
    comment_adf = {"type": "doc", "version": 1, "content": []}
    await client.edit_worklog(
        "ABC-1", "100", time_spent_seconds=1800,
        started="2026-07-28T11:00:00.000+0000", comment_adf=comment_adf,
    )
    body = json.loads(route.calls.last.request.content)
    assert body == {
        "timeSpentSeconds": 1800, "started": "2026-07-28T11:00:00.000+0000", "comment": comment_adf,
    }


@respx.mock
async def test_edit_worklog_400_raises_jira_validation_error():
    url = "https://test.atlassian.net/rest/api/3/issue/ABC-1/worklog/100"
    respx.put(url).mock(return_value=httpx.Response(400, json={
        "errorMessages": [], "errors": {"timeSpentSeconds": "Invalid."},
    }))
    client = _write_client()
    with pytest.raises(JiraValidationError):
        await client.edit_worklog("ABC-1", "100", time_spent_seconds=-1)


@respx.mock
async def test_edit_worklog_404_raises_issue_not_found():
    url = "https://test.atlassian.net/rest/api/3/issue/ABC-1/worklog/100"
    respx.put(url).mock(return_value=httpx.Response(404))
    client = _write_client()
    with pytest.raises(IssueNotFound):
        await client.edit_worklog("ABC-1", "100", time_spent_seconds=100)


@respx.mock
async def test_delete_worklog_sends_delete_request():
    url = "https://test.atlassian.net/rest/api/3/issue/ABC-1/worklog/100"
    route = respx.delete(url).mock(return_value=httpx.Response(204))
    client = _write_client()
    await client.delete_worklog("ABC-1", "100")
    assert route.called


@respx.mock
async def test_delete_worklog_400_raises_jira_validation_error():
    url = "https://test.atlassian.net/rest/api/3/issue/ABC-1/worklog/100"
    respx.delete(url).mock(return_value=httpx.Response(400, json={
        "errorMessages": ["Worklog does not exist or you do not have permission to delete it."],
        "errors": {},
    }))
    client = _write_client()
    with pytest.raises(JiraValidationError):
        await client.delete_worklog("ABC-1", "100")


@respx.mock
async def test_delete_worklog_404_raises_issue_not_found():
    url = "https://test.atlassian.net/rest/api/3/issue/ABC-1/worklog/100"
    respx.delete(url).mock(return_value=httpx.Response(404))
    client = _write_client()
    with pytest.raises(IssueNotFound):
        await client.delete_worklog("ABC-1", "100")
