from __future__ import annotations
import json
import pytest
from unittest.mock import AsyncMock, patch

from icx_engine.models.config import GitLabConnection

_CONN = GitLabConnection(name="gitlab.example.com", url="https://gitlab.example.com", token="glpat-x")


async def test_dispatch_gitlab_tool_returns_none_for_unknown_tool():
    from icx_engine.gitlab.mcp_tools import dispatch_gitlab_tool
    assert await dispatch_gitlab_tool("sonar_status", {}) is None


async def test_gitlab_list_merge_requests_happy_path():
    from icx_engine.gitlab.mcp_tools import dispatch_gitlab_tool
    mock_client = AsyncMock()
    mock_client.list_merge_requests.return_value = [{"iid": 1, "title": "Fix login"}]

    with patch("icx_engine.gitlab.mcp_tools.ConfigManager") as mock_cfg_cls:
        mock_cfg_cls.load.return_value.active_gitlab_connection.return_value = _CONN
        with patch("icx_engine.gitlab.mcp_tools.GitLabClient") as mock_client_cls:
            mock_client_cls.return_value.__aenter__.return_value = mock_client
            result = await dispatch_gitlab_tool("gitlab_list_merge_requests", {"project": "group/project"})

    payload = json.loads(result[0].text)
    assert payload["ok"] is True
    assert payload["merge_requests"] == [{"iid": 1, "title": "Fix login"}]
    mock_client.list_merge_requests.assert_awaited_once_with(
        "group/project", state="merged", target_branch=None, limit=20,
    )


async def test_gitlab_mr_changes_happy_path():
    from icx_engine.gitlab.mcp_tools import dispatch_gitlab_tool
    mock_client = AsyncMock()
    mock_client.get_merge_request_changes.return_value = {"iid": 5, "changes": []}

    with patch("icx_engine.gitlab.mcp_tools.ConfigManager") as mock_cfg_cls:
        mock_cfg_cls.load.return_value.active_gitlab_connection.return_value = _CONN
        with patch("icx_engine.gitlab.mcp_tools.GitLabClient") as mock_client_cls:
            mock_client_cls.return_value.__aenter__.return_value = mock_client
            result = await dispatch_gitlab_tool(
                "gitlab_mr_changes", {"project": "group/project", "mr_iid": 5},
            )

    payload = json.loads(result[0].text)
    assert payload["ok"] is True
    assert payload["iid"] == 5
    mock_client.get_merge_request_changes.assert_awaited_once_with("group/project", 5)


async def test_gitlab_list_commits_happy_path():
    from icx_engine.gitlab.mcp_tools import dispatch_gitlab_tool
    mock_client = AsyncMock()
    mock_client.list_commits.return_value = [{"id": "abc123", "title": "first commit"}]

    with patch("icx_engine.gitlab.mcp_tools.ConfigManager") as mock_cfg_cls:
        mock_cfg_cls.load.return_value.active_gitlab_connection.return_value = _CONN
        with patch("icx_engine.gitlab.mcp_tools.GitLabClient") as mock_client_cls:
            mock_client_cls.return_value.__aenter__.return_value = mock_client
            result = await dispatch_gitlab_tool(
                "gitlab_list_commits", {"project": "group/project", "ref": "main"},
            )

    payload = json.loads(result[0].text)
    assert payload["ok"] is True
    assert payload["commits"] == [{"id": "abc123", "title": "first commit"}]
    mock_client.list_commits.assert_awaited_once_with(
        "group/project", ref="main", path=None, since=None, limit=20,
    )


async def test_gitlab_compare_happy_path():
    from icx_engine.gitlab.mcp_tools import dispatch_gitlab_tool
    mock_client = AsyncMock()
    mock_client.compare.return_value = {"commits": [], "diffs": []}

    with patch("icx_engine.gitlab.mcp_tools.ConfigManager") as mock_cfg_cls:
        mock_cfg_cls.load.return_value.active_gitlab_connection.return_value = _CONN
        with patch("icx_engine.gitlab.mcp_tools.GitLabClient") as mock_client_cls:
            mock_client_cls.return_value.__aenter__.return_value = mock_client
            result = await dispatch_gitlab_tool(
                "gitlab_compare",
                {"project": "group/project", "from_ref": "main", "to_ref": "feature/x"},
            )

    payload = json.loads(result[0].text)
    assert payload["ok"] is True
    assert payload["diffs"] == []
    mock_client.compare.assert_awaited_once_with("group/project", "main", "feature/x")


async def test_gitlab_list_tags_happy_path():
    from icx_engine.gitlab.mcp_tools import dispatch_gitlab_tool
    mock_client = AsyncMock()
    mock_client.list_tags.return_value = [{"name": "v0.0.184-qa-20260727002"}]

    with patch("icx_engine.gitlab.mcp_tools.ConfigManager") as mock_cfg_cls:
        mock_cfg_cls.load.return_value.active_gitlab_connection.return_value = _CONN
        with patch("icx_engine.gitlab.mcp_tools.GitLabClient") as mock_client_cls:
            mock_client_cls.return_value.__aenter__.return_value = mock_client
            result = await dispatch_gitlab_tool("gitlab_list_tags", {"project": "group/project"})

    payload = json.loads(result[0].text)
    assert payload["ok"] is True
    assert payload["tags"] == [{"name": "v0.0.184-qa-20260727002"}]
    mock_client.list_tags.assert_awaited_once_with("group/project")


async def test_gitlab_list_branches_happy_path():
    from icx_engine.gitlab.mcp_tools import dispatch_gitlab_tool
    mock_client = AsyncMock()
    mock_client.list_branches.return_value = [{"name": "development", "default": True}]

    with patch("icx_engine.gitlab.mcp_tools.ConfigManager") as mock_cfg_cls:
        mock_cfg_cls.load.return_value.active_gitlab_connection.return_value = _CONN
        with patch("icx_engine.gitlab.mcp_tools.GitLabClient") as mock_client_cls:
            mock_client_cls.return_value.__aenter__.return_value = mock_client
            result = await dispatch_gitlab_tool(
                "gitlab_list_branches", {"project": "group/project", "search": "feature/x"},
            )

    payload = json.loads(result[0].text)
    assert payload["ok"] is True
    assert payload["branches"] == [{"name": "development", "default": True}]
    mock_client.list_branches.assert_awaited_once_with("group/project", search="feature/x")


async def test_gitlab_list_pipelines_happy_path():
    from icx_engine.gitlab.mcp_tools import dispatch_gitlab_tool
    mock_client = AsyncMock()
    mock_client.list_pipelines.return_value = [{"id": 1246493, "status": "failed"}]

    with patch("icx_engine.gitlab.mcp_tools.ConfigManager") as mock_cfg_cls:
        mock_cfg_cls.load.return_value.active_gitlab_connection.return_value = _CONN
        with patch("icx_engine.gitlab.mcp_tools.GitLabClient") as mock_client_cls:
            mock_client_cls.return_value.__aenter__.return_value = mock_client
            result = await dispatch_gitlab_tool(
                "gitlab_list_pipelines", {"project": "group/project", "ref": "main", "status": "failed"},
            )

    payload = json.loads(result[0].text)
    assert payload["ok"] is True
    assert payload["pipelines"] == [{"id": 1246493, "status": "failed"}]
    mock_client.list_pipelines.assert_awaited_once_with("group/project", ref="main", status="failed")


async def test_gitlab_pipeline_status_happy_path():
    from icx_engine.gitlab.mcp_tools import dispatch_gitlab_tool
    mock_client = AsyncMock()
    mock_client.get_pipeline.return_value = {"id": 1246493, "status": "failed", "jobs": []}

    with patch("icx_engine.gitlab.mcp_tools.ConfigManager") as mock_cfg_cls:
        mock_cfg_cls.load.return_value.active_gitlab_connection.return_value = _CONN
        with patch("icx_engine.gitlab.mcp_tools.GitLabClient") as mock_client_cls:
            mock_client_cls.return_value.__aenter__.return_value = mock_client
            result = await dispatch_gitlab_tool(
                "gitlab_pipeline_status", {"project": "group/project", "pipeline_id": 1246493},
            )

    payload = json.loads(result[0].text)
    assert payload["ok"] is True
    assert payload["pipeline"]["status"] == "failed"
    mock_client.get_pipeline.assert_awaited_once_with("group/project", 1246493)


async def test_gitlab_pipeline_status_missing_pipeline_id_returns_named_error():
    from icx_engine.gitlab.mcp_tools import dispatch_gitlab_tool
    result = await dispatch_gitlab_tool("gitlab_pipeline_status", {"project": "group/project"})
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert "pipeline_id" in payload["error"]


async def test_gitlab_job_log_happy_path():
    from icx_engine.gitlab.mcp_tools import dispatch_gitlab_tool
    mock_client = AsyncMock()
    mock_client.get_job_trace.return_value = "ERROR: Job failed: exit code 1"

    with patch("icx_engine.gitlab.mcp_tools.ConfigManager") as mock_cfg_cls:
        mock_cfg_cls.load.return_value.active_gitlab_connection.return_value = _CONN
        with patch("icx_engine.gitlab.mcp_tools.GitLabClient") as mock_client_cls:
            mock_client_cls.return_value.__aenter__.return_value = mock_client
            result = await dispatch_gitlab_tool(
                "gitlab_job_log", {"project": "group/project", "job_id": 2177771},
            )

    payload = json.loads(result[0].text)
    assert payload["ok"] is True
    assert "exit code 1" in payload["log"]
    mock_client.get_job_trace.assert_awaited_once_with("group/project", 2177771)


async def test_gitlab_job_log_missing_job_id_returns_named_error():
    from icx_engine.gitlab.mcp_tools import dispatch_gitlab_tool
    result = await dispatch_gitlab_tool("gitlab_job_log", {"project": "group/project"})
    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert "job_id" in payload["error"]


def test_resolve_project_uses_project_directly():
    from icx_engine.gitlab.mcp_tools import _resolve_project
    assert _resolve_project({"project": "group/project"}) == "group/project"


def test_resolve_project_derives_from_repo_path_with_resolvable_remote():
    from icx_engine.gitlab.mcp_tools import _resolve_project
    with patch("icx_engine.gitlab.mcp_tools.remote_url", return_value="https://gitlab.example.com/group/project.git"):
        assert _resolve_project({"repo_path": "/fake/repo"}) == "group/project"


def test_resolve_project_returns_none_when_neither_given():
    from icx_engine.gitlab.mcp_tools import _resolve_project
    assert _resolve_project({}) is None


def test_resolve_project_returns_none_when_remote_unparseable():
    from icx_engine.gitlab.mcp_tools import _resolve_project
    with patch("icx_engine.gitlab.mcp_tools.remote_url", return_value="not-a-recognized-remote"):
        assert _resolve_project({"repo_path": "/fake/repo"}) is None


@pytest.mark.parametrize("tool_name,extra_args", [
    ("gitlab_list_merge_requests", {}),
    ("gitlab_mr_changes", {"mr_iid": 5}),
    ("gitlab_list_commits", {}),
    ("gitlab_compare", {"from_ref": "main", "to_ref": "feature/x"}),
    ("gitlab_list_tags", {}),
    ("gitlab_list_branches", {}),
    ("gitlab_list_pipelines", {}),
    ("gitlab_pipeline_status", {"pipeline_id": 1}),
    ("gitlab_job_log", {"job_id": 1}),
])
async def test_no_active_gitlab_connection_returns_err_for_every_tool(tool_name, extra_args):
    from icx_engine.gitlab.mcp_tools import dispatch_gitlab_tool
    with patch("icx_engine.gitlab.mcp_tools.ConfigManager") as mock_cfg_cls:
        mock_cfg_cls.load.return_value.active_gitlab_connection.return_value = None
        result = await dispatch_gitlab_tool(tool_name, {"project": "group/project", **extra_args})

    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert payload["error"] == "No active GitLab connection. Run `icx gitlab --add` first."
