from __future__ import annotations
import httpx
import pytest
import respx

from icx_engine.gitlab.client import GitLabClient, GitLabError, project_path_from_remote_url


def test_client_rejects_non_http_base_url():
    with pytest.raises(ValueError):
        GitLabClient(base_url="not-a-url", token="x")


def test_client_rejects_embedded_credentials_in_url():
    with pytest.raises(ValueError):
        GitLabClient(base_url="https://user:pass@gitlab.example.com", token="x")


@respx.mock
async def test_validate_returns_user_identity(gitlab_base_url):
    respx.get(f"{gitlab_base_url}/api/v4/user").mock(
        return_value=httpx.Response(200, json={"id": 42, "username": "althaf", "name": "Althaf"})
    )
    async with GitLabClient(gitlab_base_url, token="glpat-x") as client:
        result = await client.validate()
    assert result["valid"] is True
    assert result["user"]["username"] == "althaf"
    assert result["user"]["id"] == 42


@respx.mock
async def test_validate_sends_private_token_header_not_bearer(gitlab_base_url):
    route = respx.get(f"{gitlab_base_url}/api/v4/user").mock(
        return_value=httpx.Response(200, json={"id": 1, "username": "u", "name": "U"})
    )
    async with GitLabClient(gitlab_base_url, token="glpat-x") as client:
        await client.validate()
    sent_headers = route.calls[0].request.headers
    assert sent_headers.get("PRIVATE-TOKEN") == "glpat-x"
    assert "authorization" not in sent_headers


@respx.mock
async def test_validate_invalid_token_reports_invalid(gitlab_base_url):
    respx.get(f"{gitlab_base_url}/api/v4/user").mock(return_value=httpx.Response(401, json={"message": "401 Unauthorized"}))
    async with GitLabClient(gitlab_base_url, token="bad-token") as client:
        result = await client.validate()
    assert result["valid"] is False


@respx.mock
async def test_client_never_follows_a_cross_host_redirect(gitlab_base_url):
    # Security regression: the token must never be forwarded to a redirect
    # target on a different host. GitLabClient is constructed with
    # follow_redirects=False, so a 3xx response should come straight back to
    # the caller unfollowed - the "evil" host must never receive a request.
    evil_route = respx.get("https://evil.example.com/api/v4/user").mock(
        return_value=httpx.Response(200, json={"id": 1, "username": "attacker", "name": "A"})
    )
    real_route = respx.get(f"{gitlab_base_url}/api/v4/user").mock(
        return_value=httpx.Response(
            302, headers={"Location": "https://evil.example.com/api/v4/user"},
        )
    )
    async with GitLabClient(gitlab_base_url, token="glpat-x") as client:
        result = await client.validate()
    assert result["valid"] is False
    assert result["status_code"] == 302
    assert real_route.called
    assert not evil_route.called


@respx.mock
async def test_list_projects_returns_parsed_list(gitlab_base_url):
    respx.get(f"{gitlab_base_url}/api/v4/projects").mock(
        return_value=httpx.Response(200, json=[
            {"id": 1, "path_with_namespace": "group/project-a"},
            {"id": 2, "path_with_namespace": "group/project-b"},
        ])
    )
    async with GitLabClient(gitlab_base_url, token="glpat-x") as client:
        projects = await client.list_projects()
    assert len(projects) == 2
    assert projects[0]["path_with_namespace"] == "group/project-a"


@respx.mock
async def test_get_project_url_encodes_namespace_path(gitlab_base_url):
    route = respx.get(f"{gitlab_base_url}/api/v4/projects/group%2Fproject").mock(
        return_value=httpx.Response(200, json={"id": 7, "path_with_namespace": "group/project"})
    )
    async with GitLabClient(gitlab_base_url, token="glpat-x") as client:
        project = await client.get_project("group/project")
    assert project["id"] == 7
    assert route.called


@respx.mock
async def test_get_project_raises_gitlab_error_on_404(gitlab_base_url):
    respx.get(f"{gitlab_base_url}/api/v4/projects/group%2Fmissing").mock(return_value=httpx.Response(404, json={"message": "404 Project Not Found"}))
    async with GitLabClient(gitlab_base_url, token="glpat-x") as client:
        with pytest.raises(GitLabError):
            await client.get_project("group/missing")


def test_project_path_from_remote_url_handles_ssh_form():
    assert project_path_from_remote_url("git@gitlab.example.com:group/subgroup/project.git") == "group/subgroup/project"


def test_project_path_from_remote_url_handles_https_form():
    assert project_path_from_remote_url("https://gitlab.example.com/group/project.git") == "group/project"


def test_project_path_from_remote_url_handles_https_form_no_dotgit_suffix():
    assert project_path_from_remote_url("https://gitlab.example.com/group/project") == "group/project"


def test_project_path_from_remote_url_returns_none_for_unrecognized_form():
    assert project_path_from_remote_url("not a url at all") is None


@respx.mock
async def test_create_merge_request_posts_expected_payload(gitlab_base_url):
    route = respx.post(f"{gitlab_base_url}/api/v4/projects/group%2Fproject/merge_requests").mock(
        return_value=httpx.Response(201, json={"iid": 5, "web_url": "https://gitlab.example.com/group/project/-/merge_requests/5"})
    )
    async with GitLabClient(gitlab_base_url, token="glpat-x") as client:
        result = await client.create_merge_request(
            "group/project", "feature/x-ABC-1", "development", "ABC-1 fix login", "desc",
            assignee_id=42, remove_source_branch=True,
        )
    assert result["iid"] == 5
    sent = route.calls[0].request
    import json as _json
    body = _json.loads(sent.content)
    assert body["source_branch"] == "feature/x-ABC-1"
    assert body["target_branch"] == "development"
    assert body["assignee_id"] == 42
    assert body["remove_source_branch"] is True


@respx.mock
async def test_get_merge_request_returns_status_fields(gitlab_base_url):
    respx.get(f"{gitlab_base_url}/api/v4/projects/group%2Fproject/merge_requests/5").mock(
        return_value=httpx.Response(200, json={"iid": 5, "state": "opened", "merge_status": "can_be_merged", "has_conflicts": False})
    )
    async with GitLabClient(gitlab_base_url, token="glpat-x") as client:
        mr = await client.get_merge_request("group/project", 5)
    assert mr["state"] == "opened"
    assert mr["merge_status"] == "can_be_merged"


@respx.mock
async def test_attempt_merge_success(gitlab_base_url):
    respx.put(f"{gitlab_base_url}/api/v4/projects/group%2Fproject/merge_requests/5/merge").mock(
        return_value=httpx.Response(200, json={"iid": 5, "state": "merged"})
    )
    async with GitLabClient(gitlab_base_url, token="glpat-x") as client:
        result = await client.attempt_merge("group/project", 5)
    assert result["merged"] is True
    assert result["state"] == "merged"


@respx.mock
async def test_attempt_merge_refused_returns_reason_not_raise(gitlab_base_url):
    respx.put(f"{gitlab_base_url}/api/v4/projects/group%2Fproject/merge_requests/5/merge").mock(
        return_value=httpx.Response(405, json={"message": "Branch cannot be merged"})
    )
    async with GitLabClient(gitlab_base_url, token="glpat-x") as client:
        result = await client.attempt_merge("group/project", 5)
    assert result["merged"] is False
    assert "cannot be merged" in result["reason"].lower()


@respx.mock
async def test_attempt_merge_5xx_raises_gitlab_error(gitlab_base_url):
    respx.put(f"{gitlab_base_url}/api/v4/projects/group%2Fproject/merge_requests/5/merge").mock(
        return_value=httpx.Response(503, text="Service Unavailable")
    )
    async with GitLabClient(gitlab_base_url, token="glpat-x") as client:
        with pytest.raises(GitLabError):
            await client.attempt_merge("group/project", 5)


@respx.mock
async def test_attempt_merge_malformed_200_body_raises_gitlab_error(gitlab_base_url):
    respx.put(f"{gitlab_base_url}/api/v4/projects/group%2Fproject/merge_requests/5/merge").mock(
        return_value=httpx.Response(200, text="not json")
    )
    async with GitLabClient(gitlab_base_url, token="glpat-x") as client:
        with pytest.raises(GitLabError):
            await client.attempt_merge("group/project", 5)


@respx.mock
async def test_find_merge_request_for_branch_returns_existing(gitlab_base_url):
    respx.get(f"{gitlab_base_url}/api/v4/projects/group%2Fproject/merge_requests").mock(
        return_value=httpx.Response(200, json=[{"iid": 9, "source_branch": "feature/x-ABC-1", "state": "opened"}])
    )
    async with GitLabClient(gitlab_base_url, token="glpat-x") as client:
        mr = await client.find_merge_request_for_branch("group/project", "feature/x-ABC-1")
    assert mr["iid"] == 9


@respx.mock
async def test_find_merge_request_for_branch_returns_none_when_absent(gitlab_base_url):
    respx.get(f"{gitlab_base_url}/api/v4/projects/group%2Fproject/merge_requests").mock(return_value=httpx.Response(200, json=[]))
    async with GitLabClient(gitlab_base_url, token="glpat-x") as client:
        mr = await client.find_merge_request_for_branch("group/project", "feature/x-ABC-1")
    assert mr is None


@respx.mock
async def test_list_tags_returns_parsed_list(gitlab_base_url):
    respx.get(f"{gitlab_base_url}/api/v4/projects/group%2Fproject/repository/tags").mock(
        return_value=httpx.Response(200, json=[
            {"name": "v0.0.184-qa-20260727002", "target": "abc123"},
            {"name": "v0.0.150-prod-20260701001", "target": "def456"},
        ])
    )
    async with GitLabClient(gitlab_base_url, token="glpat-x") as client:
        tags = await client.list_tags("group/project")
    assert len(tags) == 2
    assert tags[0]["name"] == "v0.0.184-qa-20260727002"


@respx.mock
async def test_list_tags_raises_gitlab_error_on_failure(gitlab_base_url):
    respx.get(f"{gitlab_base_url}/api/v4/projects/group%2Fproject/repository/tags").mock(
        return_value=httpx.Response(404, json={"message": "404 Project Not Found"})
    )
    async with GitLabClient(gitlab_base_url, token="glpat-x") as client:
        with pytest.raises(GitLabError):
            await client.list_tags("group/project")


@respx.mock
async def test_create_tag_posts_expected_payload(gitlab_base_url):
    route = respx.post(f"{gitlab_base_url}/api/v4/projects/group%2Fproject/repository/tags").mock(
        return_value=httpx.Response(201, json={"name": "v0.0.185-qa-20260727003", "target": "ghi789"})
    )
    async with GitLabClient(gitlab_base_url, token="glpat-x") as client:
        result = await client.create_tag("group/project", "v0.0.185-qa-20260727003", "main", "QA build")
    assert result["name"] == "v0.0.185-qa-20260727003"
    import json as _json
    body = _json.loads(route.calls[0].request.content)
    assert body["tag_name"] == "v0.0.185-qa-20260727003"
    assert body["ref"] == "main"
    assert body["message"] == "QA build"


@respx.mock
async def test_create_tag_raises_gitlab_error_on_failure(gitlab_base_url):
    respx.post(f"{gitlab_base_url}/api/v4/projects/group%2Fproject/repository/tags").mock(
        return_value=httpx.Response(400, json={"message": "Tag v0.0.185-qa-20260727003 already exists"})
    )
    async with GitLabClient(gitlab_base_url, token="glpat-x") as client:
        with pytest.raises(GitLabError):
            await client.create_tag("group/project", "v0.0.185-qa-20260727003", "main")


@respx.mock
async def test_get_tag_returns_tag_detail(gitlab_base_url):
    respx.get(f"{gitlab_base_url}/api/v4/projects/group%2Fproject/repository/tags/zz-icx-verify-1").mock(
        return_value=httpx.Response(200, json={"name": "zz-icx-verify-1", "commit": {"id": "abc123"}})
    )
    async with GitLabClient(gitlab_base_url, token="glpat-x") as client:
        result = await client.get_tag("group/project", "zz-icx-verify-1")
    assert result["commit"]["id"] == "abc123"


@respx.mock
async def test_get_tag_raises_gitlab_error_on_404(gitlab_base_url):
    respx.get(f"{gitlab_base_url}/api/v4/projects/group%2Fproject/repository/tags/nope").mock(
        return_value=httpx.Response(404, json={"message": "404 Tag Not Found"})
    )
    async with GitLabClient(gitlab_base_url, token="glpat-x") as client:
        with pytest.raises(GitLabError):
            await client.get_tag("group/project", "nope")


@respx.mock
async def test_delete_tag_succeeds_on_204(gitlab_base_url):
    route = respx.delete(f"{gitlab_base_url}/api/v4/projects/group%2Fproject/repository/tags/zz-icx-verify-1").mock(
        return_value=httpx.Response(204)
    )
    async with GitLabClient(gitlab_base_url, token="glpat-x") as client:
        await client.delete_tag("group/project", "zz-icx-verify-1")
    assert route.calls.call_count == 1


@respx.mock
async def test_delete_tag_raises_gitlab_error_on_404(gitlab_base_url):
    respx.delete(f"{gitlab_base_url}/api/v4/projects/group%2Fproject/repository/tags/nope").mock(
        return_value=httpx.Response(404, json={"message": "404 Tag Not Found"})
    )
    async with GitLabClient(gitlab_base_url, token="glpat-x") as client:
        with pytest.raises(GitLabError):
            await client.delete_tag("group/project", "nope")


@respx.mock
async def test_list_merge_requests_returns_parsed_list(gitlab_base_url):
    respx.get(f"{gitlab_base_url}/api/v4/projects/group%2Fproject/merge_requests").mock(
        return_value=httpx.Response(200, json=[
            {"iid": 3, "title": "Fix login", "state": "merged", "merged_by": {"username": "althaf"}, "merged_at": "2026-07-20T10:00:00Z"},
            {"iid": 4, "title": "Add tests", "state": "merged", "merged_by": {"username": "nakhil"}, "merged_at": "2026-07-21T10:00:00Z"},
        ])
    )
    async with GitLabClient(gitlab_base_url, token="glpat-x") as client:
        mrs = await client.list_merge_requests("group/project")
    assert len(mrs) == 2
    assert mrs[0]["merged_by"]["username"] == "althaf"
    assert mrs[1]["merged_at"] == "2026-07-21T10:00:00Z"


@respx.mock
async def test_list_merge_requests_sends_expected_params(gitlab_base_url):
    route = respx.get(f"{gitlab_base_url}/api/v4/projects/group%2Fproject/merge_requests").mock(
        return_value=httpx.Response(200, json=[])
    )
    async with GitLabClient(gitlab_base_url, token="glpat-x") as client:
        await client.list_merge_requests("group/project", state="opened", target_branch="development", limit=5)
    sent = route.calls[0].request.url.params
    assert sent["state"] == "opened"
    assert sent["target_branch"] == "development"
    assert sent["per_page"] == "5"


@respx.mock
async def test_list_merge_requests_omits_target_branch_when_not_given(gitlab_base_url):
    route = respx.get(f"{gitlab_base_url}/api/v4/projects/group%2Fproject/merge_requests").mock(
        return_value=httpx.Response(200, json=[])
    )
    async with GitLabClient(gitlab_base_url, token="glpat-x") as client:
        await client.list_merge_requests("group/project")
    sent = route.calls[0].request.url.params
    assert "target_branch" not in sent


@respx.mock
async def test_list_merge_requests_numeric_project_id_skips_encoding(gitlab_base_url):
    route = respx.get(f"{gitlab_base_url}/api/v4/projects/42/merge_requests").mock(
        return_value=httpx.Response(200, json=[])
    )
    async with GitLabClient(gitlab_base_url, token="glpat-x") as client:
        mrs = await client.list_merge_requests("42")
    assert mrs == []
    assert route.called


@respx.mock
async def test_list_merge_requests_raises_gitlab_error_on_failure(gitlab_base_url):
    respx.get(f"{gitlab_base_url}/api/v4/projects/group%2Fproject/merge_requests").mock(
        return_value=httpx.Response(500, text="Internal Server Error")
    )
    async with GitLabClient(gitlab_base_url, token="glpat-x") as client:
        with pytest.raises(GitLabError):
            await client.list_merge_requests("group/project")


@respx.mock
async def test_get_merge_request_changes_returns_file_diffs(gitlab_base_url):
    respx.get(f"{gitlab_base_url}/api/v4/projects/group%2Fproject/merge_requests/5/changes").mock(
        return_value=httpx.Response(200, json={
            "iid": 5,
            "changes": [
                {"old_path": "src/a.py", "new_path": "src/a.py", "diff": "@@ -1,2 +1,3 @@\n+new line"},
            ],
        })
    )
    async with GitLabClient(gitlab_base_url, token="glpat-x") as client:
        result = await client.get_merge_request_changes("group/project", 5)
    assert result["iid"] == 5
    assert result["changes"][0]["new_path"] == "src/a.py"


@respx.mock
async def test_get_merge_request_changes_numeric_project_id_skips_encoding(gitlab_base_url):
    route = respx.get(f"{gitlab_base_url}/api/v4/projects/42/merge_requests/5/changes").mock(
        return_value=httpx.Response(200, json={"iid": 5, "changes": []})
    )
    async with GitLabClient(gitlab_base_url, token="glpat-x") as client:
        result = await client.get_merge_request_changes("42", 5)
    assert result["iid"] == 5
    assert route.called


@respx.mock
async def test_get_merge_request_changes_raises_gitlab_error_on_failure(gitlab_base_url):
    respx.get(f"{gitlab_base_url}/api/v4/projects/group%2Fproject/merge_requests/5/changes").mock(
        return_value=httpx.Response(404, json={"message": "404 Not found"})
    )
    async with GitLabClient(gitlab_base_url, token="glpat-x") as client:
        with pytest.raises(GitLabError):
            await client.get_merge_request_changes("group/project", 5)


@respx.mock
async def test_list_commits_returns_parsed_list(gitlab_base_url):
    respx.get(f"{gitlab_base_url}/api/v4/projects/group%2Fproject/repository/commits").mock(
        return_value=httpx.Response(200, json=[
            {"id": "abc123", "title": "fix login", "author_name": "althaf"},
            {"id": "def456", "title": "add tests", "author_name": "nakhil"},
        ])
    )
    async with GitLabClient(gitlab_base_url, token="glpat-x") as client:
        commits = await client.list_commits("group/project")
    assert len(commits) == 2
    assert commits[0]["id"] == "abc123"


@respx.mock
async def test_list_commits_sends_expected_params_when_provided(gitlab_base_url):
    route = respx.get(f"{gitlab_base_url}/api/v4/projects/group%2Fproject/repository/commits").mock(
        return_value=httpx.Response(200, json=[])
    )
    async with GitLabClient(gitlab_base_url, token="glpat-x") as client:
        await client.list_commits("group/project", ref="development", path="src/a.py", since="2026-07-01T00:00:00Z", limit=10)
    sent = route.calls[0].request.url.params
    assert sent["ref_name"] == "development"
    assert sent["path"] == "src/a.py"
    assert sent["since"] == "2026-07-01T00:00:00Z"
    assert sent["per_page"] == "10"


@respx.mock
async def test_list_commits_omits_optional_params_when_not_given(gitlab_base_url):
    route = respx.get(f"{gitlab_base_url}/api/v4/projects/group%2Fproject/repository/commits").mock(
        return_value=httpx.Response(200, json=[])
    )
    async with GitLabClient(gitlab_base_url, token="glpat-x") as client:
        await client.list_commits("group/project")
    sent = route.calls[0].request.url.params
    assert "ref_name" not in sent
    assert "path" not in sent
    assert "since" not in sent
    assert sent["per_page"] == "20"


@respx.mock
async def test_list_commits_numeric_project_id_skips_encoding(gitlab_base_url):
    route = respx.get(f"{gitlab_base_url}/api/v4/projects/42/repository/commits").mock(
        return_value=httpx.Response(200, json=[])
    )
    async with GitLabClient(gitlab_base_url, token="glpat-x") as client:
        commits = await client.list_commits("42")
    assert commits == []
    assert route.called


@respx.mock
async def test_list_commits_raises_gitlab_error_on_failure(gitlab_base_url):
    respx.get(f"{gitlab_base_url}/api/v4/projects/group%2Fproject/repository/commits").mock(
        return_value=httpx.Response(500, text="Internal Server Error")
    )
    async with GitLabClient(gitlab_base_url, token="glpat-x") as client:
        with pytest.raises(GitLabError):
            await client.list_commits("group/project")


@respx.mock
async def test_compare_returns_commits_and_diffs(gitlab_base_url):
    respx.get(f"{gitlab_base_url}/api/v4/projects/group%2Fproject/repository/compare").mock(
        return_value=httpx.Response(200, json={
            "commits": [{"id": "abc123", "title": "fix login"}],
            "diffs": [{"old_path": "src/a.py", "new_path": "src/a.py", "diff": "@@ -1,2 +1,3 @@\n+new line"}],
        })
    )
    async with GitLabClient(gitlab_base_url, token="glpat-x") as client:
        result = await client.compare("group/project", "development", "feature/x-ABC-1")
    assert result["commits"][0]["id"] == "abc123"
    assert result["diffs"][0]["new_path"] == "src/a.py"


@respx.mock
async def test_compare_sends_expected_params(gitlab_base_url):
    route = respx.get(f"{gitlab_base_url}/api/v4/projects/group%2Fproject/repository/compare").mock(
        return_value=httpx.Response(200, json={"commits": [], "diffs": []})
    )
    async with GitLabClient(gitlab_base_url, token="glpat-x") as client:
        await client.compare("group/project", "development", "feature/x-ABC-1")
    sent = route.calls[0].request.url.params
    assert sent["from"] == "development"
    assert sent["to"] == "feature/x-ABC-1"


@respx.mock
async def test_compare_numeric_project_id_skips_encoding(gitlab_base_url):
    route = respx.get(f"{gitlab_base_url}/api/v4/projects/42/repository/compare").mock(
        return_value=httpx.Response(200, json={"commits": [], "diffs": []})
    )
    async with GitLabClient(gitlab_base_url, token="glpat-x") as client:
        result = await client.compare("42", "development", "feature/x-ABC-1")
    assert result == {"commits": [], "diffs": []}
    assert route.called


@respx.mock
async def test_compare_raises_gitlab_error_on_failure(gitlab_base_url):
    respx.get(f"{gitlab_base_url}/api/v4/projects/group%2Fproject/repository/compare").mock(
        return_value=httpx.Response(404, json={"message": "404 Not found"})
    )
    async with GitLabClient(gitlab_base_url, token="glpat-x") as client:
        with pytest.raises(GitLabError):
            await client.compare("group/project", "development", "feature/x-ABC-1")


# -- list_branches ------------------------------------------------------------

@respx.mock
async def test_list_branches_returns_parsed_list(gitlab_base_url):
    respx.get(f"{gitlab_base_url}/api/v4/projects/group%2Fproject/repository/branches").mock(
        return_value=httpx.Response(200, json=[
            {"name": "development", "protected": True, "default": True, "commit": {"committed_date": "2026-08-03T05:19:48.000+00:00"}},
            {"name": "feature/x-ABC-1", "protected": False, "default": False, "commit": {"committed_date": "2026-08-01T00:00:00.000+00:00"}},
        ])
    )
    async with GitLabClient(gitlab_base_url, token="glpat-x") as client:
        branches = await client.list_branches("group/project")
    assert len(branches) == 2
    assert branches[0]["name"] == "development"
    assert branches[0]["default"] is True


@respx.mock
async def test_list_branches_sends_search_param_when_given(gitlab_base_url):
    route = respx.get(f"{gitlab_base_url}/api/v4/projects/group%2Fproject/repository/branches").mock(
        return_value=httpx.Response(200, json=[])
    )
    async with GitLabClient(gitlab_base_url, token="glpat-x") as client:
        await client.list_branches("group/project", search="feature/x")
    assert route.calls[0].request.url.params["search"] == "feature/x"


@respx.mock
async def test_list_branches_raises_gitlab_error_on_failure(gitlab_base_url):
    respx.get(f"{gitlab_base_url}/api/v4/projects/group%2Fproject/repository/branches").mock(
        return_value=httpx.Response(404, json={"message": "404 Project Not Found"})
    )
    async with GitLabClient(gitlab_base_url, token="glpat-x") as client:
        with pytest.raises(GitLabError):
            await client.list_branches("group/project")


# -- list_pipelines -------------------------------------------------------------

@respx.mock
async def test_list_pipelines_returns_parsed_list(gitlab_base_url):
    respx.get(f"{gitlab_base_url}/api/v4/projects/group%2Fproject/pipelines").mock(
        return_value=httpx.Response(200, json=[
            {"id": 1246493, "iid": 89, "status": "failed", "ref": "refs/merge-requests/85/head"},
        ])
    )
    async with GitLabClient(gitlab_base_url, token="glpat-x") as client:
        pipelines = await client.list_pipelines("group/project")
    assert len(pipelines) == 1
    assert pipelines[0]["status"] == "failed"


@respx.mock
async def test_list_pipelines_sends_ref_and_status_params(gitlab_base_url):
    route = respx.get(f"{gitlab_base_url}/api/v4/projects/group%2Fproject/pipelines").mock(
        return_value=httpx.Response(200, json=[])
    )
    async with GitLabClient(gitlab_base_url, token="glpat-x") as client:
        await client.list_pipelines("group/project", ref="main", status="success")
    params = route.calls[0].request.url.params
    assert params["ref"] == "main"
    assert params["status"] == "success"


@respx.mock
async def test_list_pipelines_raises_gitlab_error_on_failure(gitlab_base_url):
    respx.get(f"{gitlab_base_url}/api/v4/projects/group%2Fproject/pipelines").mock(
        return_value=httpx.Response(404, json={"message": "404 Project Not Found"})
    )
    async with GitLabClient(gitlab_base_url, token="glpat-x") as client:
        with pytest.raises(GitLabError):
            await client.list_pipelines("group/project")


# -- get_pipeline (pipeline detail + jobs) --------------------------------------

@respx.mock
async def test_get_pipeline_merges_jobs_into_pipeline_dict(gitlab_base_url):
    respx.get(f"{gitlab_base_url}/api/v4/projects/group%2Fproject/pipelines/1246493").mock(
        return_value=httpx.Response(200, json={"id": 1246493, "status": "failed"})
    )
    respx.get(f"{gitlab_base_url}/api/v4/projects/group%2Fproject/pipelines/1246493/jobs").mock(
        return_value=httpx.Response(200, json=[
            {"id": 2177771, "name": "buildapp", "status": "failed", "stage": "build"},
        ])
    )
    async with GitLabClient(gitlab_base_url, token="glpat-x") as client:
        pipeline = await client.get_pipeline("group/project", 1246493)
    assert pipeline["status"] == "failed"
    assert pipeline["jobs"][0]["name"] == "buildapp"


@respx.mock
async def test_get_pipeline_raises_when_pipeline_fetch_fails(gitlab_base_url):
    respx.get(f"{gitlab_base_url}/api/v4/projects/group%2Fproject/pipelines/999").mock(
        return_value=httpx.Response(404, json={"message": "404 Not found"})
    )
    async with GitLabClient(gitlab_base_url, token="glpat-x") as client:
        with pytest.raises(GitLabError):
            await client.get_pipeline("group/project", 999)


@respx.mock
async def test_get_pipeline_raises_when_jobs_fetch_fails(gitlab_base_url):
    respx.get(f"{gitlab_base_url}/api/v4/projects/group%2Fproject/pipelines/1246493").mock(
        return_value=httpx.Response(200, json={"id": 1246493, "status": "failed"})
    )
    respx.get(f"{gitlab_base_url}/api/v4/projects/group%2Fproject/pipelines/1246493/jobs").mock(
        return_value=httpx.Response(500, json={"message": "server error"})
    )
    async with GitLabClient(gitlab_base_url, token="glpat-x") as client:
        with pytest.raises(GitLabError):
            await client.get_pipeline("group/project", 1246493)


# -- get_job_trace --------------------------------------------------------------

@respx.mock
async def test_get_job_trace_returns_raw_text(gitlab_base_url):
    respx.get(f"{gitlab_base_url}/api/v4/projects/group%2Fproject/jobs/2177771/trace").mock(
        return_value=httpx.Response(200, text="ERROR: Job failed: exit code 1")
    )
    async with GitLabClient(gitlab_base_url, token="glpat-x") as client:
        trace = await client.get_job_trace("group/project", 2177771)
    assert "exit code 1" in trace


@respx.mock
async def test_get_job_trace_raises_gitlab_error_on_failure(gitlab_base_url):
    respx.get(f"{gitlab_base_url}/api/v4/projects/group%2Fproject/jobs/999/trace").mock(
        return_value=httpx.Response(404, json={"message": "404 Not found"})
    )
    async with GitLabClient(gitlab_base_url, token="glpat-x") as client:
        with pytest.raises(GitLabError):
            await client.get_job_trace("group/project", 999)


# -- get_repository_file --------------------------------------------------------

@respx.mock
async def test_get_repository_file_returns_raw_text(gitlab_base_url):
    respx.get(f"{gitlab_base_url}/api/v4/projects/group%2Fproject/repository/files/.gitlab-ci.yml/raw").mock(
        return_value=httpx.Response(200, text="stages:\n  - build\n")
    )
    async with GitLabClient(gitlab_base_url, token="glpat-x") as client:
        text = await client.get_repository_file("group/project", ".gitlab-ci.yml", "development")
    assert "stages" in text


@respx.mock
async def test_get_repository_file_sends_ref_param(gitlab_base_url):
    route = respx.get(f"{gitlab_base_url}/api/v4/projects/group%2Fproject/repository/files/.gitlab-ci.yml/raw").mock(
        return_value=httpx.Response(200, text="")
    )
    async with GitLabClient(gitlab_base_url, token="glpat-x") as client:
        await client.get_repository_file("group/project", ".gitlab-ci.yml", "development")
    assert route.calls[0].request.url.params["ref"] == "development"


@respx.mock
async def test_get_repository_file_404_raises_gitlab_error(gitlab_base_url):
    respx.get(f"{gitlab_base_url}/api/v4/projects/group%2Fproject/repository/files/.gitlab-ci.yml/raw").mock(
        return_value=httpx.Response(404, text="")
    )
    async with GitLabClient(gitlab_base_url, token="glpat-x") as client:
        with pytest.raises(GitLabError):
            await client.get_repository_file("group/project", ".gitlab-ci.yml", "development")
