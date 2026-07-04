import base64

import httpx
import pytest
import respx

from icx_engine.exceptions import AuthError, SourceUnavailable
from icx_engine.models.sonar import SonarScope
from icx_engine.sonar.client import SonarClient, _effort_to_minutes

BASE = "http://sonar.test:9000"


def test_effort_parsing():
    assert _effort_to_minutes("10min") == 10
    assert _effort_to_minutes("1h30min") == 90
    assert _effort_to_minutes("2d") == 2 * 8 * 60
    assert _effort_to_minutes(None) is None
    assert _effort_to_minutes("") is None


def test_bad_base_url_rejected():
    with pytest.raises(ValueError):
        SonarClient("ftp://host/x", "t")
    with pytest.raises(ValueError):
        SonarClient("http://user:pass@host:9000", "t")


@respx.mock
async def test_validate_ok():
    respx.get(f"{BASE}/api/authentication/validate").mock(
        return_value=httpx.Response(200, json={"valid": True}))
    respx.get(f"{BASE}/api/system/status").mock(
        return_value=httpx.Response(200, json={"status": "UP", "version": "10.4"}))
    async with SonarClient(BASE, "tok") as c:
        out = await c.validate()
    assert out["valid"] is True
    assert out["version"] == "10.4"


@respx.mock
async def test_basic_auth_header_is_token_colon():
    captured = {}

    def responder(request):
        captured["auth"] = request.headers.get("Authorization", "")
        return httpx.Response(200, json={"valid": True})

    respx.get(f"{BASE}/api/authentication/validate").mock(side_effect=responder)
    respx.get(f"{BASE}/api/system/status").mock(return_value=httpx.Response(200, json={}))
    async with SonarClient(BASE, "mytoken") as c:
        await c.validate()
    assert captured["auth"] == "Basic " + base64.b64encode(b"mytoken:").decode()


@respx.mock
async def test_401_raises_auth_error():
    respx.get(f"{BASE}/api/qualitygates/project_status").mock(
        return_value=httpx.Response(401, json={}))
    async with SonarClient(BASE, "tok") as c:
        with pytest.raises(AuthError):
            await c.quality_gate("my-project")


@respx.mock
async def test_issues_normalized_and_truncated():
    respx.get(f"{BASE}/api/issues/search").mock(return_value=httpx.Response(200, json={
        "total": 3,
        "issues": [
            {"key": "AY1", "rule": "java:S100", "severity": "MAJOR",
             "component": "my-project:src/app/Handler.java", "line": 42,
             "status": "OPEN", "message": "Bad", "effort": "1h30min",
             "author": "dev@example.com", "tags": ["cwe"], "type": "BUG",
             "creationDate": "2026-01-01", "updateDate": "2026-01-02"},
            {"key": "AY2", "rule": "java:S200", "severity": "MINOR",
             "component": "my-project:src/app/Util.java", "line": 5,
             "status": "OPEN", "message": "Smell", "type": "CODE_SMELL"},
        ],
    }))
    scope = SonarScope(project="my-project")
    async with SonarClient(BASE, "tok") as c:
        findings, total, truncated = await c.issues(scope)
    assert total == 3
    assert truncated is True            # 3 reported, 2 returned
    assert findings[0].file == "src/app/Handler.java"
    assert findings[0].effort_minutes == 90
    assert findings[0].tags == ["cwe"]


@respx.mock
async def test_issues_capped_by_limit():
    respx.get(f"{BASE}/api/issues/search").mock(return_value=httpx.Response(200, json={
        "total": 2,
        "issues": [
            {"key": "A", "type": "BUG", "severity": "MAJOR", "rule": "r", "message": "m",
             "component": "my-project:a.py", "status": "OPEN"},
            {"key": "B", "type": "BUG", "severity": "MAJOR", "rule": "r", "message": "m",
             "component": "my-project:b.py", "status": "OPEN"},
        ],
    }))
    scope = SonarScope(project="my-project", limit=1)
    async with SonarClient(BASE, "tok") as c:
        findings, total, truncated = await c.issues(scope)
    assert len(findings) == 1
    assert truncated is True


@respx.mock
async def test_hotspots_normalized_and_file_filtered():
    respx.get(f"{BASE}/api/hotspots/search").mock(return_value=httpx.Response(200, json={
        "paging": {"total": 2},
        "hotspots": [
            {"key": "H1", "component": "my-project:src/app/Login.java",
             "vulnerabilityProbability": "HIGH", "status": "TO_REVIEW",
             "message": "Review auth", "ruleKey": "java:S2076",
             "securityCategory": "auth", "line": 10},
            {"key": "H2", "component": "my-project:src/other/X.java",
             "vulnerabilityProbability": "LOW", "status": "TO_REVIEW",
             "message": "x", "ruleKey": "java:S1", "line": 1},
        ],
    }))
    scope = SonarScope(project="my-project", files=["src/app/Login.java"])
    async with SonarClient(BASE, "tok") as c:
        hotspots = await c.hotspots(scope)
    assert len(hotspots) == 1
    assert hotspots[0].type == "SECURITY_HOTSPOT"
    assert hotspots[0].severity == "HIGH"
    assert hotspots[0].file == "src/app/Login.java"


@respx.mock
async def test_measures_parsed():
    respx.get(f"{BASE}/api/measures/component").mock(return_value=httpx.Response(200, json={
        "component": {"key": "my-project", "measures": [
            {"metric": "bugs", "value": "12"},
            {"metric": "coverage", "value": "84.5"},
            {"metric": "sqale_index", "value": "480"},
            {"metric": "reliability_rating", "value": "1.0"},
            {"metric": "duplicated_lines_density", "value": "3.2"},
            {"metric": "tests", "value": "0"},
        ]},
    }))
    async with SonarClient(BASE, "tok") as c:
        m = await c.measures("my-project")
    assert m.bugs == 12
    assert m.coverage == 84.5
    assert m.technical_debt_minutes == 480
    assert m.technical_debt == "1d"
    assert m.reliability_rating == "A"
    assert m.duplicated_lines_density == 3.2
    assert m.tests == 0
    assert m.raw["bugs"] == "12"


@respx.mock
async def test_quality_gate_parsed():
    respx.get(f"{BASE}/api/qualitygates/project_status").mock(return_value=httpx.Response(200, json={
        "projectStatus": {"status": "ERROR", "conditions": [
            {"metricKey": "new_coverage", "comparator": "LT",
             "errorThreshold": "80", "actualValue": "75.0", "status": "ERROR"},
        ]},
    }))
    async with SonarClient(BASE, "tok") as c:
        gate = await c.quality_gate("my-project")
    assert gate.status == "ERROR"
    assert gate.conditions[0].metric == "new_coverage"
    assert gate.conditions[0].actual_value == "75.0"


@respx.mock
async def test_duplications_parsed():
    respx.get(f"{BASE}/api/duplications/show").mock(return_value=httpx.Response(200, json={
        "duplications": [{"blocks": [
            {"from": 10, "size": 5, "_ref": "1"},
            {"from": 40, "size": 5, "_ref": "2"},
        ]}],
        "files": {
            "1": {"key": "my-project:src/A.java"},
            "2": {"key": "my-project:src/B.java"},
        },
    }))
    async with SonarClient(BASE, "tok") as c:
        dups = await c.duplications("my-project", ["src/A.java"])
    assert len(dups) == 1
    assert dups[0].file == "src/A.java"
    assert dups[0].blocks[0].ref_file == "src/A.java"
    assert dups[0].blocks[1].ref_file == "src/B.java"


@respx.mock
async def test_issues_hotspot_only_skips_issue_search():
    # types = hotspot only -> no /api/issues/search request is made at all
    # (if one were made, respx would raise for an unmocked route).
    scope = SonarScope(project="my-project", types=["SECURITY_HOTSPOT"])
    async with SonarClient(BASE, "tok") as c:
        findings, total, truncated = await c.issues(scope)
    assert findings == []
    assert total == 0
    assert truncated is False


@respx.mock
async def test_issues_filters_out_hotspot_type():
    route = respx.get(f"{BASE}/api/issues/search").mock(
        return_value=httpx.Response(200, json={"total": 0, "issues": []}))
    scope = SonarScope(project="my-project", types=["BUG", "SECURITY_HOTSPOT"])
    async with SonarClient(BASE, "tok") as c:
        await c.issues(scope)
    sent = str(route.calls.last.request.url)
    assert "types=BUG" in sent
    assert "SECURITY_HOTSPOT" not in sent


@respx.mock
async def test_duplications_404_does_not_crash():
    respx.get(f"{BASE}/api/duplications/show").mock(
        return_value=httpx.Response(404, json={"errors": [{"msg": "not found"}]}))
    async with SonarClient(BASE, "tok") as c:
        dups = await c.duplications("my-project", ["src/missing.java"])
    assert dups == []


@respx.mock
async def test_404_surfaces_sonar_error_text():
    respx.get(f"{BASE}/api/measures/component").mock(return_value=httpx.Response(
        404, json={"errors": [{"msg": "Component key 'x' not found"}]}))
    async with SonarClient(BASE, "tok") as c:
        with pytest.raises(SourceUnavailable) as ei:
            await c.measures("x")
    assert "not found" in str(ei.value).lower()


@respx.mock
async def test_400_branch_error_surfaced():
    respx.get(f"{BASE}/api/issues/search").mock(return_value=httpx.Response(
        400, json={"errors": [{"msg": "Branch 'x' does not exist"}]}))
    scope = SonarScope(project="my-project", branch="x")
    async with SonarClient(BASE, "tok") as c:
        with pytest.raises(SourceUnavailable) as ei:
            await c.issues(scope)
    assert "branch" in str(ei.value).lower()


@respx.mock
async def test_projects_and_branches():
    respx.get(f"{BASE}/api/components/search").mock(return_value=httpx.Response(200, json={
        "paging": {"total": 1},
        "components": [{"key": "my-project", "name": "My Project", "qualifier": "TRK"}],
    }))
    respx.get(f"{BASE}/api/project_branches/list").mock(return_value=httpx.Response(200, json={
        "branches": [{"name": "main", "isMain": True, "type": "BRANCH",
                      "status": {"qualityGateStatus": "OK"}, "analysisDate": "2026-01-01"}],
    }))
    async with SonarClient(BASE, "tok") as c:
        items, total = await c.projects()
        branches = await c.branches("my-project")
    assert items == [{"key": "my-project", "name": "My Project"}]
    assert total == 1
    assert branches[0]["name"] == "main"
    assert branches[0]["is_main"] is True


@respx.mock
async def test_projects_query_passed_and_branches_filtered():
    route = respx.get(f"{BASE}/api/components/search").mock(return_value=httpx.Response(200, json={
        "paging": {"total": 2},
        "components": [{"key": "svc-a", "name": "A"}, {"key": "svc-b", "name": "B"}],
    }))
    respx.get(f"{BASE}/api/project_branches/list").mock(return_value=httpx.Response(200, json={
        "branches": [
            {"name": "main", "isMain": True, "type": "BRANCH"},
            {"name": "feature/x", "isMain": False, "type": "BRANCH"},
        ],
    }))
    async with SonarClient(BASE, "tok") as c:
        items, total = await c.projects(query="svc")
        branches = await c.branches("my-project", query="feature")
    assert "q=svc" in str(route.calls.last.request.url)
    assert total == 2
    assert [b["name"] for b in branches] == ["feature/x"]
