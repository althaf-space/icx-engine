import pytest

from icx_engine.exceptions import SourceUnavailable
from icx_engine.models.config import AppConfig, SonarConnection
from icx_engine.models.sonar import (
    ComponentMeasure,
    IssueChangelogEntry,
    MetricHistoryPoint,
    MetricInfo,
    QualityGateConditionDef,
    QualityGateDefinition,
    QualityProfile,
    SonarAnalysis,
    SonarFinding,
    SonarHotspotDetail,
    SonarMeasures,
    SonarQualityGate,
    SonarRule,
    SourceLine,
    SystemHealth,
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


class _MultiFileClient:
    """Fake client for exercising report()'s concurrent per-file measures fetch."""

    def __init__(self, fail_files=None):
        self._fail = set(fail_files or [])

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def quality_gate(self, project, branch=None):
        return SonarQualityGate(status="OK")

    async def measures(self, component, branch=None):
        if ":" not in component:
            return SonarMeasures(component=component, coverage=None, bugs=5)
        path = component.split(":", 1)[1]
        if path in self._fail:
            raise SourceUnavailable(f"measures failed for {path}")
        return SonarMeasures(component=component, coverage=80.0, bugs=0)

    async def issues(self, scope):
        return ([], 0, False)

    async def hotspots(self, scope):
        return []

    async def duplications(self, project, files, branch=None):
        return []


@pytest.mark.asyncio
async def test_report_file_measures_multiple_files_all_succeed(monkeypatch):
    cfg = _enabled_cfg()
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: cfg))
    monkeypatch.setattr(service, "_make_client", lambda c: _MultiFileClient())
    files = [f"src/File{i}.java" for i in range(5)]
    out = await service.report("my-project", files=files)
    assert set(out["file_measures"].keys()) == set(files)
    for path in files:
        assert out["file_measures"][path]["coverage"] == 80.0


@pytest.mark.asyncio
async def test_report_one_file_measures_failure_does_not_abort_others(monkeypatch):
    cfg = _enabled_cfg()
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: cfg))
    files = [f"src/File{i}.java" for i in range(4)]
    monkeypatch.setattr(service, "_make_client", lambda c: _MultiFileClient(fail_files={"src/File2.java"}))
    out = await service.report("my-project", files=files)
    assert "src/File2.java" not in out["file_measures"]
    assert set(out["file_measures"].keys()) == {f for f in files if f != "src/File2.java"}
    assert out["project"] == "my-project"  # report still assembled despite the per-file failure


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


class _ExtrasClient:
    """Fake client for the top_files/metric_history/analyses/rule/hotspot
    wrappers - returns whatever parsed models the test configures."""

    def __init__(self, tree=None, history=None, analyses_=None, rule_=None, hotspot_=None,
                 rules_=None, rules_total=None, source_lines=None, metrics_result=None,
                 metrics_total=None, qg_by_project=None, qg_by_name=None, profiles=None,
                 authors=None, tags=None, changelog=None, health=None, langs=None):
        self._tree = tree
        self._history = history
        self._analyses = analyses_
        self._rule = rule_
        self._hotspot = hotspot_
        self._rules = rules_ or []
        self._rules_total = rules_total if rules_total is not None else len(self._rules)
        self._source_lines = source_lines or []
        self._metrics_result = metrics_result or []
        self._metrics_total = metrics_total if metrics_total is not None else len(self._metrics_result)
        self._qg_by_project = qg_by_project
        self._qg_by_name = qg_by_name
        self._profiles = profiles or []
        self._authors = authors or []
        self._tags = tags or []
        self._changelog = changelog or []
        self._health = health
        self._langs = langs or []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def component_tree(self, component, metric_keys, branch=None, sort_metric=None,
                              ascending=False, qualifiers=None, page_size=100, max_pages=5):
        return self._tree

    async def search_history(self, component, metric_keys, branch=None, date_from=None, date_to=None):
        return self._history

    async def project_analyses(self, project, branch=None, date_from=None, date_to=None, page_size=20):
        return self._analyses

    async def rule_show(self, rule_key):
        return self._rule

    async def rules_search(self, language=None, tags=None, repositories=None, query=None, page_size=50):
        return self._rules, self._rules_total

    async def hotspot_show(self, hotspot_key):
        return self._hotspot

    async def sources_lines(self, component, branch=None, from_line=None, to_line=None):
        return self._source_lines

    async def metrics_search(self, page_size=100):
        return self._metrics_result, self._metrics_total

    async def qualitygates_get_by_project(self, project):
        return self._qg_by_project

    async def qualitygates_show(self, gate_id=None, name=None):
        return self._qg_by_name

    async def quality_profiles_search(self, language=None, project=None):
        return self._profiles

    async def issues_authors(self, project=None, query=None):
        return self._authors

    async def issues_tags(self, project=None, query=None):
        return self._tags

    async def issues_changelog(self, issue_key):
        return self._changelog

    async def system_health(self):
        return self._health

    async def languages_list(self, query=None):
        return self._langs


@pytest.mark.asyncio
async def test_top_files_ranks_and_limits(monkeypatch):
    cfg = _enabled_cfg()
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: cfg))
    rows = [
        ComponentMeasure(key="myproj:a.py", path="a.py", qualifier="FIL",
                         metric="duplicated_lines_density", value="50.0"),
        ComponentMeasure(key="myproj:b.py", path="b.py", qualifier="FIL",
                         metric="duplicated_lines_density", value="10.0"),
    ]
    monkeypatch.setattr(service, "_make_client", lambda c: _ExtrasClient(tree=(rows, 3)))
    out = await service.top_files("myproj", "duplicated_lines_density", limit=2)
    assert out["project"] == "myproj"
    assert out["metric"] == "duplicated_lines_density"
    assert out["total"] == 3
    assert len(out["files"]) == 2
    assert out["files"][0]["path"] == "a.py"


@pytest.mark.asyncio
async def test_metric_history_returns_series(monkeypatch):
    cfg = _enabled_cfg()
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: cfg))
    history = {"coverage": [MetricHistoryPoint(date="2026-07-01", value="80.0")]}
    monkeypatch.setattr(service, "_make_client", lambda c: _ExtrasClient(history=history))
    out = await service.metric_history("myproj", ["coverage"])
    assert "coverage" in out["history"]
    assert out["history"]["coverage"][0]["value"] == "80.0"


@pytest.mark.asyncio
async def test_analyses_returns_list(monkeypatch):
    cfg = _enabled_cfg()
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: cfg))
    analyses_ = [SonarAnalysis(key="A1", date="2026-07-27", project_version="1.0", events=[])]
    monkeypatch.setattr(service, "_make_client", lambda c: _ExtrasClient(analyses_=analyses_))
    out = await service.analyses("myproj")
    assert len(out["analyses"]) == 1
    assert out["analyses"][0]["project_version"] == "1.0"


@pytest.mark.asyncio
async def test_rule_returns_detail(monkeypatch):
    cfg = _enabled_cfg()
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: cfg))
    rule_ = SonarRule(key="python:S1481", type="CODE_SMELL")
    monkeypatch.setattr(service, "_make_client", lambda c: _ExtrasClient(rule_=rule_))
    out = await service.rule("python:S1481")
    assert out["key"] == "python:S1481"


@pytest.mark.asyncio
async def test_rules_returns_list(monkeypatch):
    cfg = _enabled_cfg()
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: cfg))
    rules_ = [SonarRule(key="python:S1481", type="CODE_SMELL"), SonarRule(key="python:S1172", type="CODE_SMELL")]
    monkeypatch.setattr(service, "_make_client", lambda c: _ExtrasClient(rules_=rules_, rules_total=2))
    out = await service.rules(language="py")
    assert out["total"] == 2
    assert out["returned"] == 2
    assert out["rules"][0]["key"] == "python:S1481"


@pytest.mark.asyncio
async def test_hotspot_returns_detail(monkeypatch):
    cfg = _enabled_cfg()
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: cfg))
    hotspot_ = SonarHotspotDetail(key="HS1", file="a.py")
    monkeypatch.setattr(service, "_make_client", lambda c: _ExtrasClient(hotspot_=hotspot_))
    out = await service.hotspot("HS1")
    assert out["key"] == "HS1"


@pytest.mark.asyncio
async def test_source_lines_returns_annotated_lines(monkeypatch):
    cfg = _enabled_cfg()
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: cfg))
    lines = [
        SourceLine(line=1, code="import os", covered=True, line_hits=3),
        SourceLine(line=2, code="", covered=None),
    ]
    monkeypatch.setattr(service, "_make_client", lambda c: _ExtrasClient(source_lines=lines))
    out = await service.source_lines("myproj", "src/a.py")
    assert out["project"] == "myproj"
    assert out["path"] == "src/a.py"
    assert len(out["lines"]) == 2
    assert out["lines"][0]["code"] == "import os"
    assert out["lines"][0]["covered"] is True


@pytest.mark.asyncio
async def test_metrics_returns_catalog(monkeypatch):
    cfg = _enabled_cfg()
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: cfg))
    metrics_ = [
        MetricInfo(key="coverage", name="Coverage", domain="Coverage", type="PERCENT"),
        MetricInfo(key="bugs", name="Bugs", domain="Reliability", type="INT"),
    ]
    monkeypatch.setattr(service, "_make_client", lambda c: _ExtrasClient(metrics_result=metrics_, metrics_total=2))
    out = await service.metrics()
    assert out["total"] == 2
    assert len(out["metrics"]) == 2
    assert out["metrics"][0]["key"] == "coverage"


@pytest.mark.asyncio
async def test_quality_gate_definition_by_project(monkeypatch):
    cfg = _enabled_cfg()
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: cfg))
    gate_ = QualityGateDefinition(id="1", name="Sonar way", is_default=True,
                                  conditions=[QualityGateConditionDef(metric="coverage", comparator="LT", error_threshold="80")])
    monkeypatch.setattr(service, "_make_client", lambda c: _ExtrasClient(qg_by_project=gate_))
    out = await service.quality_gate_definition(project="myproj")
    assert out["name"] == "Sonar way"
    assert out["conditions"][0]["metric"] == "coverage"


@pytest.mark.asyncio
async def test_quality_gate_definition_by_name(monkeypatch):
    cfg = _enabled_cfg()
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: cfg))
    gate_ = QualityGateDefinition(id="2", name="Custom Gate", is_default=False)
    monkeypatch.setattr(service, "_make_client", lambda c: _ExtrasClient(qg_by_name=gate_))
    out = await service.quality_gate_definition(gate_name="Custom Gate")
    assert out["name"] == "Custom Gate"
    assert out["name"] != "Sonar way"


@pytest.mark.asyncio
async def test_quality_gate_definition_requires_project_or_name():
    with pytest.raises(ValueError):
        await service.quality_gate_definition()


@pytest.mark.asyncio
async def test_quality_profiles_returns_list(monkeypatch):
    cfg = _enabled_cfg()
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: cfg))
    profiles_ = [
        QualityProfile(key="py-default", name="Sonar way", language="py", is_default=True, active_rule_count=120),
    ]
    monkeypatch.setattr(service, "_make_client", lambda c: _ExtrasClient(profiles=profiles_))
    out = await service.quality_profiles(language="py")
    assert len(out["profiles"]) == 1
    assert out["profiles"][0]["language"] == "py"


@pytest.mark.asyncio
async def test_issue_authors_returns_list(monkeypatch):
    cfg = _enabled_cfg()
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: cfg))
    monkeypatch.setattr(service, "_make_client", lambda c: _ExtrasClient(authors=["alice", "bob"]))
    out = await service.issue_authors(project="myproj")
    assert out["authors"] == ["alice", "bob"]


@pytest.mark.asyncio
async def test_issue_tags_returns_list(monkeypatch):
    cfg = _enabled_cfg()
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: cfg))
    monkeypatch.setattr(service, "_make_client", lambda c: _ExtrasClient(tags=["security", "bug"]))
    out = await service.issue_tags(project="myproj")
    assert out["tags"] == ["security", "bug"]


@pytest.mark.asyncio
async def test_issue_changelog_returns_entries(monkeypatch):
    cfg = _enabled_cfg()
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: cfg))
    changelog_ = [
        IssueChangelogEntry(creation_date="2026-07-01", user="alice",
                            changes=[{"key": "resolution", "old_value": "", "new_value": "FIXED"}]),
    ]
    monkeypatch.setattr(service, "_make_client", lambda c: _ExtrasClient(changelog=changelog_))
    out = await service.issue_changelog("ISSUE-1")
    assert out["issue"] == "ISSUE-1"
    assert len(out["changelog"]) == 1
    assert out["changelog"][0]["user"] == "alice"


@pytest.mark.asyncio
async def test_system_health_returns_status(monkeypatch):
    cfg = _enabled_cfg()
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: cfg))
    health_ = SystemHealth(health="GREEN", causes=[])
    monkeypatch.setattr(service, "_make_client", lambda c: _ExtrasClient(health=health_))
    out = await service.system_health()
    assert out["health"] == "GREEN"


@pytest.mark.asyncio
async def test_languages_returns_list(monkeypatch):
    cfg = _enabled_cfg()
    monkeypatch.setattr(service.ConfigManager, "load", staticmethod(lambda: cfg))
    langs_ = [{"key": "py", "name": "Python"}, {"key": "java", "name": "Java"}]
    monkeypatch.setattr(service, "_make_client", lambda c: _ExtrasClient(langs=langs_))
    out = await service.languages()
    assert len(out["languages"]) == 2
    assert out["languages"][0]["key"] == "py"
