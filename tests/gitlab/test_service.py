# tests/gitlab/test_service.py
from __future__ import annotations
import httpx
import pytest
import respx

from icx_engine.models.config import AppConfig, GitLabConnection
from icx_engine.gitlab.service import (
    add_connection,
    status,
    list_connections,
    remove_connection,
    set_active,
    create_and_merge_mr,
)


@respx.mock
async def test_add_connection_creates_and_activates_first_connection(monkeypatch):
    from icx_engine.config_manager import ConfigManager
    monkeypatch.setattr(ConfigManager, "save", staticmethod(lambda c: None))
    respx.get("https://gitlab.example.com/api/v4/user").mock(
        return_value=httpx.Response(200, json={"id": 1, "username": "althaf", "name": "Althaf"})
    )
    cfg = AppConfig()
    result = await add_connection("default", "https://gitlab.example.com", "glpat-x", cfg=cfg)
    assert result["name"] == "default"
    assert result["active"] is True                  # first connection auto-activates
    assert result["validation"]["valid"] is True
    assert result["validation"]["user"]["username"] == "althaf"
    assert cfg.active_gitlab == "default"
    assert cfg.gitlab_connections["default"].token == "glpat-x"


@respx.mock
async def test_add_second_connection_does_not_deactivate_first_unless_make_active(monkeypatch):
    from icx_engine.config_manager import ConfigManager
    monkeypatch.setattr(ConfigManager, "save", staticmethod(lambda c: None))
    respx.get("https://gitlab.example.com/api/v4/user").mock(
        return_value=httpx.Response(200, json={"id": 1, "username": "u1", "name": "U1"})
    )
    respx.get("https://gitlab.staging.com/api/v4/user").mock(
        return_value=httpx.Response(200, json={"id": 2, "username": "u2", "name": "U2"})
    )
    cfg = AppConfig()
    await add_connection("prod", "https://gitlab.example.com", "t1", cfg=cfg)
    out = await add_connection("staging", "https://gitlab.staging.com", "t2", cfg=cfg)
    assert out["active"] is False                    # second connection stays inactive by default
    assert cfg.active_gitlab == "prod"

    out2 = await add_connection("staging", "https://gitlab.staging.com", "t2", make_active=True, cfg=cfg)
    assert out2["active"] is True
    assert cfg.active_gitlab == "staging"


@respx.mock
async def test_add_connection_with_blank_token_keeps_existing_token(monkeypatch):
    # re-running `icx gitlab --add` to only change the URL/TLS setting must not
    # clear the stored token - a blank prompt answer becomes token=None here.
    from icx_engine.config_manager import ConfigManager
    monkeypatch.setattr(ConfigManager, "save", staticmethod(lambda c: None))
    respx.get("https://gitlab.example.com/api/v4/user").mock(
        return_value=httpx.Response(200, json={"id": 1, "username": "althaf", "name": "Althaf"})
    )
    cfg = AppConfig(
        gitlab_connections={
            "default": GitLabConnection(name="default", url="https://gitlab.example.com", token="glpat-original"),
        },
        active_gitlab="default",
    )
    out = await add_connection("default", "https://gitlab.example.com", None, cfg=cfg)
    assert out["validation"]["valid"] is True
    assert cfg.gitlab_connections["default"].token == "glpat-original"   # preserved, not wiped


@respx.mock
async def test_list_connections_reports_all_and_active():
    cfg = AppConfig(
        gitlab_connections={
            "prod": GitLabConnection(name="prod", url="https://gitlab.example.com", token="t1"),
            "staging": GitLabConnection(name="staging", url="https://gitlab.staging.com", token=None),
        },
        active_gitlab="prod",
    )
    out = list_connections(cfg=cfg)
    assert out["active"] == "prod"
    names = {c["name"] for c in out["connections"]}
    assert names == {"prod", "staging"}
    prod = next(c for c in out["connections"] if c["name"] == "prod")
    assert prod["active"] is True and prod["has_token"] is True
    staging = next(c for c in out["connections"] if c["name"] == "staging")
    assert staging["active"] is False and staging["has_token"] is False


def test_set_active_switches_connection(monkeypatch):
    from icx_engine.config_manager import ConfigManager
    monkeypatch.setattr(ConfigManager, "save", staticmethod(lambda c: None))
    cfg = AppConfig(
        gitlab_connections={
            "prod": GitLabConnection(name="prod", url="https://gitlab.example.com", token="t1"),
            "staging": GitLabConnection(name="staging", url="https://gitlab.staging.com", token="t2"),
        },
        active_gitlab="prod",
    )
    out = set_active("staging", cfg=cfg)
    assert out["active"] == "staging"
    assert cfg.active_gitlab == "staging"


def test_set_active_raises_for_unknown_name(monkeypatch):
    from icx_engine.config_manager import ConfigManager
    monkeypatch.setattr(ConfigManager, "save", staticmethod(lambda c: None))
    cfg = AppConfig()
    with pytest.raises(KeyError):
        set_active("does-not-exist", cfg=cfg)


def test_remove_connection_promotes_remaining_active(monkeypatch):
    from icx_engine.config_manager import ConfigManager
    deleted = []
    monkeypatch.setattr(ConfigManager, "save", staticmethod(lambda c: None))
    monkeypatch.setattr(ConfigManager, "delete_gitlab_connection_secret",
                        staticmethod(lambda n: deleted.append(n)))
    cfg = AppConfig(
        gitlab_connections={
            "prod": GitLabConnection(name="prod", url="https://gitlab.example.com", token="t1"),
            "staging": GitLabConnection(name="staging", url="https://gitlab.staging.com", token="t2"),
        },
        active_gitlab="prod",
    )
    out = remove_connection("prod", cfg=cfg)
    assert out["removed"] == "prod"
    assert "prod" not in cfg.gitlab_connections
    assert cfg.active_gitlab == "staging"             # active fell back to remaining
    assert deleted == ["prod"]                        # keyring token cleared on remove


def test_remove_connection_clears_active_when_last_one_removed(monkeypatch):
    from icx_engine.config_manager import ConfigManager
    deleted = []
    monkeypatch.setattr(ConfigManager, "save", staticmethod(lambda c: None))
    monkeypatch.setattr(ConfigManager, "delete_gitlab_connection_secret",
                        staticmethod(lambda n: deleted.append(n)))
    cfg = AppConfig(
        gitlab_connections={
            "gitlab.example.com": GitLabConnection(name="gitlab.example.com", url="https://gitlab.example.com", token="glpat-x"),
        },
        active_gitlab="gitlab.example.com",
    )
    out = remove_connection("gitlab.example.com", cfg=cfg)
    assert out["removed"] == "gitlab.example.com"
    assert out["active"] is None
    assert cfg.active_gitlab is None
    assert deleted == ["gitlab.example.com"]           # keyring token cleared on remove
    assert "gitlab.example.com" not in cfg.gitlab_connections


def test_remove_connection_raises_for_unknown_name(monkeypatch):
    from icx_engine.config_manager import ConfigManager
    monkeypatch.setattr(ConfigManager, "save", staticmethod(lambda c: None))
    cfg = AppConfig()
    with pytest.raises(KeyError):
        remove_connection("does-not-exist", cfg=cfg)


@respx.mock
async def test_status_reports_disconnected_when_nothing_configured():
    cfg = AppConfig()
    out = await status(cfg=cfg)
    assert out["configured"] is False
    assert out["active"] is None


@respx.mock
async def test_status_validates_the_active_connection_live():
    respx.get("https://gitlab.example.com/api/v4/user").mock(
        return_value=httpx.Response(200, json={"id": 1, "username": "u", "name": "U"})
    )
    cfg = AppConfig(
        gitlab_connections={"gitlab.example.com": GitLabConnection(name="gitlab.example.com", url="https://gitlab.example.com", token="glpat-x")},
        active_gitlab="gitlab.example.com",
    )
    out = await status(cfg=cfg)
    assert out["configured"] is True
    assert out["connection"]["valid"] is True


def _conn() -> GitLabConnection:
    return GitLabConnection(name="gitlab.example.com", url="https://gitlab.example.com", token="glpat-x")


@respx.mock
async def test_create_and_merge_mr_creates_new_and_merges_clean():
    respx.get("https://gitlab.example.com/api/v4/projects/group%2Fproject/merge_requests").mock(return_value=httpx.Response(200, json=[]))
    respx.post("https://gitlab.example.com/api/v4/projects/group%2Fproject/merge_requests").mock(
        return_value=httpx.Response(201, json={"iid": 5, "web_url": "https://gitlab.example.com/group/project/-/merge_requests/5"})
    )
    respx.put("https://gitlab.example.com/api/v4/projects/group%2Fproject/merge_requests/5/merge").mock(
        return_value=httpx.Response(200, json={"iid": 5, "state": "merged"})
    )
    out = await create_and_merge_mr(
        _conn(), "group/project", "feature/x-ABC-1", "development", "ABC-1 fix login", "desc", assignee_id=42,
    )
    assert out["mr_iid"] == 5
    assert out["created"] is True
    assert out["merged"] is True


@respx.mock
async def test_create_and_merge_mr_reuses_existing_open_mr():
    respx.get("https://gitlab.example.com/api/v4/projects/group%2Fproject/merge_requests").mock(
        return_value=httpx.Response(200, json=[{"iid": 9, "source_branch": "feature/x-ABC-1", "state": "opened"}])
    )
    respx.put("https://gitlab.example.com/api/v4/projects/group%2Fproject/merge_requests/9/merge").mock(
        return_value=httpx.Response(200, json={"iid": 9, "state": "merged"})
    )
    out = await create_and_merge_mr(
        _conn(), "group/project", "feature/x-ABC-1", "development", "ABC-1 fix login", "desc", assignee_id=42,
    )
    assert out["mr_iid"] == 9
    assert out["created"] is False
    assert out["merged"] is True


@respx.mock
async def test_create_and_merge_mr_reports_refusal_reason():
    respx.get("https://gitlab.example.com/api/v4/projects/group%2Fproject/merge_requests").mock(return_value=httpx.Response(200, json=[]))
    respx.post("https://gitlab.example.com/api/v4/projects/group%2Fproject/merge_requests").mock(
        return_value=httpx.Response(201, json={"iid": 5, "web_url": "https://gitlab.example.com/group/project/-/merge_requests/5"})
    )
    respx.put("https://gitlab.example.com/api/v4/projects/group%2Fproject/merge_requests/5/merge").mock(
        return_value=httpx.Response(405, json={"message": "This merge request needs approval"})
    )
    out = await create_and_merge_mr(
        _conn(), "group/project", "feature/x-ABC-1", "development", "ABC-1 fix login", "desc", assignee_id=42,
    )
    assert out["mr_iid"] == 5
    assert out["merged"] is False
    assert "approval" in out["refusal_reason"].lower()


from icx_engine.gitlab.service import parse_tag_name, group_tags_by_environment, propose_next_tag


def test_parse_tag_name_matches_expected_format():
    parsed = parse_tag_name("v0.0.184-qa-20260727002")
    assert parsed is not None
    assert parsed.major == 0
    assert parsed.minor == 0
    assert parsed.patch == 184
    assert parsed.environment == "qa"
    assert parsed.date == "20260727"
    assert parsed.seq == 2


def test_parse_tag_name_returns_none_for_unrelated_tag():
    assert parse_tag_name("release-2026") is None
    assert parse_tag_name("v1.0.0") is None


def test_group_tags_by_environment_separates_and_sorts_newest_first():
    tags = [
        {"name": "v0.0.150-prod-20260701001"},
        {"name": "v0.0.184-qa-20260727002"},
        {"name": "v0.0.183-qa-20260726001"},
        {"name": "not-a-version-tag"},
    ]
    grouped = group_tags_by_environment(tags)
    assert set(grouped.keys()) == {"prod", "qa"}
    assert [t.name for t in grouped["qa"]] == ["v0.0.184-qa-20260727002", "v0.0.183-qa-20260726001"]
    assert len(grouped["prod"]) == 1


def test_propose_next_tag_increments_patch_and_seq_same_day():
    latest = parse_tag_name("v0.0.184-qa-20260727002")
    proposed = propose_next_tag("qa", latest, today="20260727")
    assert proposed == "v0.0.185-qa-20260727003"


def test_propose_next_tag_resets_seq_on_new_day():
    latest = parse_tag_name("v0.0.184-qa-20260727002")
    proposed = propose_next_tag("qa", latest, today="20260728")
    assert proposed == "v0.0.185-qa-20260728001"


def test_propose_next_tag_seeds_fresh_environment_with_no_prior_tags():
    proposed = propose_next_tag("staging", None, today="20260727")
    assert proposed == "v0.0.1-staging-20260727001"


def test_propose_next_tag_rejects_mismatched_environment():
    latest = parse_tag_name("v0.0.184-qa-20260727002")
    with pytest.raises(ValueError):
        propose_next_tag("prod", latest, today="20260727")
