import pytest

from icx_engine.models.config import AppConfig, SonarConnection
from icx_engine.models.sonar import (
    SonarFinding,
    SonarMeasures,
    SonarQualityGate,
)
from icx_engine.sonar import service
from icx_engine.sonar.client import SonarClient


class _FakeClient:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def validate(self):
        return {"valid": True, "server_status": "UP", "version": "10.4"}

    async def quality_gate(self, project, branch=None):
        return SonarQualityGate(status="OK")

    async def measures(self, component, branch=None):
        coverage = 0.0 if component.endswith("refund.java") else 80.0
        return SonarMeasures(component=component, coverage=coverage, bugs=1)

    async def issues(self, scope):
        return ([SonarFinding(key="1", type="BUG", severity="MAJOR", rule="r",
                              message="m", file="src/refund.java")], 1, False)

    async def hotspots(self, scope):
        return [SonarFinding(key="h", type="SECURITY_HOTSPOT", severity="HIGH",
                             rule="r", message="review")]

    async def duplications(self, project, files, branch=None):
        return []


def _enabled_cfg():
    cfg = AppConfig()
    cfg.sonar_connections = {
        "default": SonarConnection(name="default", url="http://sonar.test:9000", token="tok"),
    }
    cfg.active_sonar = "default"
    return cfg


def test_service_does_not_reference_magik():
    # The direct reader must not route through the Magik testing client.
    assert not hasattr(service, "MagikClient")
    src = service.__file__
    with open(src, "r", encoding="utf-8") as fh:
        text = fh.read()
    assert "MagikClient" not in text
    assert "testing.client" not in text


def test_make_client_builds_sonar_client():
    cfg = _enabled_cfg()
    client = service._make_client(cfg)
    assert isinstance(client, SonarClient)


@pytest.mark.asyncio
async def test_report_gated_when_disabled(monkeypatch):
    cfg = AppConfig()  # disabled
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: cfg))
    with pytest.raises(service.SonarDisabled):
        await service.report("my-project")


@pytest.mark.asyncio
async def test_not_configured_raises(monkeypatch):
    # active connection exists but its token is missing (e.g. keyring lost)
    cfg = AppConfig()
    cfg.sonar_connections = {"default": SonarConnection(name="default", url="http://s:9000", token=None)}
    cfg.active_sonar = "default"
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: cfg))
    with pytest.raises(service.SonarNotConfigured):
        await service.report("my-project")


def test_sonar_enabled_derives_from_active_connection():
    assert service.sonar_enabled(_enabled_cfg()) is True
    assert service.sonar_enabled(AppConfig()) is False


def test_legacy_config_migrates_to_active_connection():
    # a pre-connections config with the legacy enabled flag resolves as active
    cfg = AppConfig()
    cfg.sonar_enabled = True
    cfg.sonar_url = "http://legacy:9000"
    cfg.sonar_token = "tok"
    conn = cfg.active_sonar_connection()
    assert conn is not None and conn.url == "http://legacy:9000"
    # a legacy config that was DISABLED stays off
    off = AppConfig()
    off.sonar_url = "http://legacy:9000"
    off.sonar_token = "tok"
    assert off.active_sonar_connection() is None


def test_legacy_config_promoted_to_named_connection():
    # constructed WITH legacy fields (as on load) -> promoted to a real, visible,
    # removable connection so it shows in `icx status` and `icx sonar --list`
    cfg = AppConfig(sonar_url="http://legacy:9000", sonar_token="tok", sonar_enabled=True)
    assert "default" in cfg.sonar_connections
    assert cfg.active_sonar == "default"
    assert cfg.sonar_connections["default"].url == "http://legacy:9000"
    assert cfg.sonar_connections["default"].token == "tok"
    assert cfg.sonar_url is None  # legacy fields cleared after migration

    # a DISABLED legacy config: connection visible/removable but not made active
    off = AppConfig(sonar_url="http://legacy:9000", sonar_token="tok", sonar_enabled=False)
    assert "default" in off.sonar_connections
    assert off.active_sonar is None
    assert off.active_sonar_connection() is None


@pytest.mark.asyncio
async def test_add_list_active_remove_connections(monkeypatch):
    cfg = AppConfig()
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: cfg))
    monkeypatch.setattr(service.ConfigManager, "save", staticmethod(lambda c: None))
    monkeypatch.setattr(service.ConfigManager, "delete_sonar_connection_secret", staticmethod(lambda n: None))
    monkeypatch.setattr(service, "_make_client", lambda c: _FakeClient())

    out = await service.add_connection("prod", "http://prod:9000/dashboard?id=x", "t1", cfg=cfg)
    assert out["name"] == "prod" and out["active"] is True
    assert out["url"] == "http://prod:9000"          # dashboard URL normalized to base
    assert cfg.sonar_connections["prod"].token == "t1"

    await service.add_connection("staging", "http://staging:9000", "t2", cfg=cfg)
    listed = service.list_connections(cfg=cfg)
    assert {c["name"] for c in listed["connections"]} == {"prod", "staging"}
    assert listed["active"] == "prod"                 # first stays active

    service.set_active("staging", cfg=cfg)
    assert cfg.active_sonar == "staging"

    service.remove_connection("staging", cfg=cfg)
    assert "staging" not in cfg.sonar_connections
    assert cfg.active_sonar == "prod"                 # active fell back to remaining


@pytest.mark.asyncio
async def test_report_assembles_and_flags_test_gap(monkeypatch):
    cfg = _enabled_cfg()
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: cfg))
    monkeypatch.setattr(service, "_make_client", lambda c: _FakeClient())
    out = await service.report("my-project", branch="main", files=["src/refund.java"])
    assert out["quality_gate"]["status"] == "OK"
    assert out["measures"]["bugs"] == 1
    assert "src/refund.java" in out["file_measures"]
    # coverage 0.0 file -> flagged as a test gap
    assert out["test_gaps"][0]["file"] == "src/refund.java"
    assert out["test_gaps"][0]["has_tests"] is False
    # findings merge issues + hotspots
    assert out["summary"]["total"] == 2
    assert out["summary"]["by_type"]["SECURITY_HOTSPOT"] == 1


@pytest.mark.asyncio
async def test_findings_merges_issues_and_hotspots(monkeypatch):
    cfg = _enabled_cfg()
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: cfg))
    monkeypatch.setattr(service, "_make_client", lambda c: _FakeClient())
    out = await service.findings("my-project")
    assert out["summary"]["total"] == 2
    assert out["total_findings"] == 2


class _DiscoveryClient:
    def __init__(self, total):
        self._total = total

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def projects(self, query=None, limit=50):
        items = [{"key": f"p{i}", "name": f"P{i}"} for i in range(min(limit, self._total))]
        return items, self._total

    async def branches(self, project, query=None):
        names = ["main"] + [f"feature/{i}" for i in range(max(self._total - 1, 0))]
        if query:
            names = [n for n in names if query in n]
        return [{"name": n, "is_main": n == "main", "type": "BRANCH",
                 "quality_gate": "OK", "analysis_date": ""} for n in names]


@pytest.mark.asyncio
async def test_projects_withheld_when_too_many(monkeypatch):
    cfg = _enabled_cfg()
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: cfg))
    monkeypatch.setattr(service, "_make_client", lambda c: _DiscoveryClient(500))
    out = await service.projects()
    assert out["total"] == 500
    assert out["projects"] == []          # withheld - too many, no query
    assert out["truncated"] is True
    assert "paste" in out["instructions"].lower()
    assert "MANDATORY" in out["instructions"]


@pytest.mark.asyncio
async def test_projects_small_server_lists_all(monkeypatch):
    cfg = _enabled_cfg()
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: cfg))
    monkeypatch.setattr(service, "_make_client", lambda c: _DiscoveryClient(5))
    out = await service.projects()
    assert out["returned"] == 5
    assert out["truncated"] is False


@pytest.mark.asyncio
async def test_projects_query_shows_results(monkeypatch):
    cfg = _enabled_cfg()
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: cfg))
    monkeypatch.setattr(service, "_make_client", lambda c: _DiscoveryClient(500))
    out = await service.projects(query="p")
    assert out["projects"]                 # query given -> results shown even if many exist
    assert out["query"] == "p"


@pytest.mark.asyncio
async def test_branches_withheld_when_too_many(monkeypatch):
    cfg = _enabled_cfg()
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: cfg))
    monkeypatch.setattr(service, "_make_client", lambda c: _DiscoveryClient(100))
    out = await service.branches("my-project")
    assert out["total"] == 100
    assert out["branches"] == []
    assert out["truncated"] is True
    assert "branch" in out["instructions"].lower()


@pytest.mark.asyncio
async def test_branches_query_filters(monkeypatch):
    cfg = _enabled_cfg()
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: cfg))
    monkeypatch.setattr(service, "_make_client", lambda c: _DiscoveryClient(3))
    out = await service.branches("my-project", query="feature")
    assert out["branches"]
    assert all("feature" in b["name"] for b in out["branches"])


@pytest.mark.asyncio
async def test_status_when_disabled(monkeypatch):
    cfg = AppConfig()
    cfg.sonar_url = "http://sonar.test:9000"
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: cfg))
    out = await service.status()
    assert out["enabled"] is False
    assert out["connection"] is None
    assert out["configured"] is False
