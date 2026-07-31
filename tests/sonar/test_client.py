import asyncio
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
async def test_hotspots_honors_scope_limit():
    """Regression: hotspots() previously ignored scope.limit entirely and always
    drained up to 10000 results, unlike issues() which correctly caps. A project
    with more hotspots than the requested limit must stop early."""
    respx.get(f"{BASE}/api/hotspots/search").mock(
        return_value=httpx.Response(200, json={
            "paging": {"total": 500, "pageIndex": 1, "pageSize": 500},
            "hotspots": [{"key": f"HS{i}", "component": "myproj:a.py", "message": "m"} for i in range(500)],
        })
    )
    scope = SonarScope(project="myproj", limit=10)
    async with SonarClient(BASE, token="t") as client:
        hotspots = await client.hotspots(scope)
    assert len(hotspots) == 10


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
async def test_duplications_multiple_files_all_succeed():
    def _responder(request):
        path = dict(request.url.params)["key"].split(":", 1)[1]
        return httpx.Response(200, json={
            "duplications": [{"blocks": [{"from": 1, "size": 5, "_ref": "1"}]}],
            "files": {"1": {"key": f"my-project:{path}"}},
        })

    respx.get(f"{BASE}/api/duplications/show").mock(side_effect=_responder)
    async with SonarClient(BASE, "tok") as c:
        dups = await c.duplications("my-project", ["src/A.java", "src/B.java", "src/C.java"])
    assert {d.file for d in dups} == {"src/A.java", "src/B.java", "src/C.java"}


@respx.mock
async def test_duplications_one_file_failure_does_not_abort_others():
    def _responder(request):
        path = dict(request.url.params)["key"].split(":", 1)[1]
        if path == "src/missing.java":
            return httpx.Response(404, json={"errors": [{"msg": "not found"}]})
        return httpx.Response(200, json={
            "duplications": [{"blocks": [{"from": 1, "size": 5, "_ref": "1"}]}],
            "files": {"1": {"key": f"my-project:{path}"}},
        })

    respx.get(f"{BASE}/api/duplications/show").mock(side_effect=_responder)
    async with SonarClient(BASE, "tok") as c:
        dups = await c.duplications("my-project", ["src/A.java", "src/missing.java", "src/B.java"])
    files = {d.file for d in dups}
    assert files == {"src/A.java", "src/B.java"}


@respx.mock
async def test_duplications_fetches_concurrently():
    state = {"current": 0, "max": 0}

    async def _responder(request):
        state["current"] += 1
        state["max"] = max(state["max"], state["current"])
        await asyncio.sleep(0.05)
        state["current"] -= 1
        return httpx.Response(200, json={"duplications": [], "files": {}})

    respx.get(f"{BASE}/api/duplications/show").mock(side_effect=_responder)
    async with SonarClient(BASE, "tok") as c:
        await c.duplications("my-project", [f"src/F{i}.java" for i in range(5)])
    assert state["max"] > 1  # requests overlapped instead of running strictly one-at-a-time


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


@respx.mock
async def test_component_tree_returns_ranked_files():
    respx.get(f"{BASE}/api/measures/component_tree").mock(
        return_value=httpx.Response(200, json={
            "paging": {"total": 2, "pageIndex": 1, "pageSize": 100},
            "baseComponent": {"key": "myproj"},
            "components": [
                {"key": "myproj:src/a.py", "path": "src/a.py", "qualifier": "FIL", "language": "py",
                 "measures": [{"metric": "duplicated_lines_density", "value": "42.0"}]},
                {"key": "myproj:src/b.py", "path": "src/b.py", "qualifier": "FIL", "language": "py",
                 "measures": [{"metric": "duplicated_lines_density", "value": "10.0"}]},
            ],
        })
    )
    async with SonarClient(BASE, token="t") as client:
        rows, total = await client.component_tree("myproj", ["duplicated_lines_density"], sort_metric="duplicated_lines_density")
    assert total == 2
    assert len(rows) == 2
    assert rows[0].path == "src/a.py"
    assert rows[0].value == "42.0"
    assert rows[0].metric == "duplicated_lines_density"


@respx.mock
async def test_component_tree_sends_sort_and_qualifier_params():
    route = respx.get(f"{BASE}/api/measures/component_tree").mock(
        return_value=httpx.Response(200, json={"paging": {"total": 0}, "components": []})
    )
    async with SonarClient(BASE, token="t") as client:
        await client.component_tree(
            "myproj", ["coverage"], branch="main", sort_metric="coverage",
            ascending=True, qualifiers=["FIL"],
        )
    sent = dict(route.calls[0].request.url.params)
    assert sent["s"] == "metric"
    assert sent["metricSort"] == "coverage"
    assert sent["asc"] == "true"
    assert sent["qualifiers"] == "FIL"
    assert sent["branch"] == "main"
    assert sent["metricKeys"] == "coverage"


@respx.mock
async def test_component_tree_paginates_up_to_max_pages():
    call_count = {"n": 0}

    def _responder(request):
        call_count["n"] += 1
        page = int(dict(request.url.params).get("p", "1"))
        comps = [{"key": f"myproj:f{page}.py", "path": f"f{page}.py", "qualifier": "FIL",
                   "measures": [{"metric": "ncloc", "value": "10"}]}]
        return httpx.Response(200, json={"paging": {"total": 250, "pageIndex": page, "pageSize": 100}, "components": comps})

    respx.get(f"{BASE}/api/measures/component_tree").mock(side_effect=_responder)
    async with SonarClient(BASE, token="t") as client:
        rows, total = await client.component_tree("myproj", ["ncloc"], page_size=100, max_pages=3)
    assert total == 250
    assert call_count["n"] == 3  # stops at max_pages even though total implies more
    assert len(rows) == 3


@respx.mock
async def test_search_history_returns_per_metric_series():
    respx.get(f"{BASE}/api/measures/search_history").mock(
        return_value=httpx.Response(200, json={
            "paging": {"total": 2},
            "measures": [
                {"metric": "coverage", "history": [
                    {"date": "2026-07-01T00:00:00+0000", "value": "80.0"},
                    {"date": "2026-07-15T00:00:00+0000", "value": "82.5"},
                ]},
            ],
        })
    )
    async with SonarClient(BASE, token="t") as client:
        history = await client.search_history("myproj", ["coverage"])
    assert "coverage" in history
    assert len(history["coverage"]) == 2
    assert history["coverage"][1].value == "82.5"


@respx.mock
async def test_project_analyses_returns_events():
    respx.get(f"{BASE}/api/project_analyses/search").mock(
        return_value=httpx.Response(200, json={
            "paging": {"total": 1},
            "analyses": [
                {"key": "AX1", "date": "2026-07-27T10:00:00+0000", "projectVersion": "1.2.3",
                 "events": [{"key": "E1", "category": "VERSION", "name": "1.2.3"}]},
            ],
        })
    )
    async with SonarClient(BASE, token="t") as client:
        analyses = await client.project_analyses("myproj")
    assert len(analyses) == 1
    assert analyses[0].project_version == "1.2.3"
    assert analyses[0].events[0].category == "VERSION"


@respx.mock
async def test_project_analyses_sends_date_range_params():
    route = respx.get(f"{BASE}/api/project_analyses/search").mock(
        return_value=httpx.Response(200, json={"paging": {"total": 0}, "analyses": []})
    )
    async with SonarClient(BASE, token="t") as client:
        await client.project_analyses("myproj", date_from="2026-01-01", date_to="2026-07-01")
    sent = dict(route.calls[0].request.url.params)
    assert sent["from"] == "2026-01-01"
    assert sent["to"] == "2026-07-01"


@respx.mock
async def test_rule_show_returns_full_detail():
    respx.get(f"{BASE}/api/rules/show").mock(
        return_value=httpx.Response(200, json={
            "rule": {
                "key": "python:S1481", "name": "Unused local variables should be removed",
                "lang": "py", "type": "CODE_SMELL", "severity": "MINOR", "status": "READY",
                "htmlDesc": "<p>Remove the unused variable.</p>",
                "remFnBaseEffort": "5min", "tags": ["unused"], "repo": "python",
            }
        })
    )
    async with SonarClient(BASE, token="t") as client:
        rule = await client.rule_show("python:S1481")
    assert rule.key == "python:S1481"
    assert rule.type == "CODE_SMELL"
    assert "Remove the unused variable" in rule.html_description
    assert rule.tags == ["unused"]


@respx.mock
async def test_rules_search_returns_list_and_total():
    respx.get(f"{BASE}/api/rules/search").mock(
        return_value=httpx.Response(200, json={
            "total": 2,
            "rules": [
                {"key": "python:S1481", "name": "Unused local variables", "lang": "py", "type": "CODE_SMELL"},
                {"key": "python:S1854", "name": "Dead stores", "lang": "py", "type": "CODE_SMELL"},
            ],
        })
    )
    async with SonarClient(BASE, token="t") as client:
        rules, total = await client.rules_search(language="py")
    assert total == 2
    assert len(rules) == 2
    assert rules[0].key == "python:S1481"


@respx.mock
async def test_hotspot_show_returns_full_detail():
    respx.get(f"{BASE}/api/hotspots/show").mock(
        return_value=httpx.Response(200, json={
            "key": "HS1", "message": "Make sure this is safe.",
            "component": {"key": "myproj:src/a.py"}, "line": 42, "status": "TO_REVIEW",
            "author": "dev@example.com", "creationDate": "2026-07-01",
            "rule": {
                "key": "python:S4830",
                "vulnerabilityProbability": "HIGH", "securityCategory": "ssrf",
                "riskDescription": "<p>Risk here</p>",
                "vulnerabilityDescription": "<p>Vuln here</p>",
                "fixRecommendations": "<p>Fix here</p>",
            },
        })
    )
    async with SonarClient(BASE, token="t") as client:
        detail = await client.hotspot_show("HS1")
    assert detail.key == "HS1"
    assert detail.file == "src/a.py"
    assert detail.line == 42
    assert "Risk here" in detail.risk_description
    assert "Fix here" in detail.fix_recommendations
    assert detail.rule_key == "python:S4830"
    assert detail.vulnerability_probability == "HIGH"
    assert detail.security_category == "ssrf"


@respx.mock
async def test_sources_lines_returns_annotated_lines():
    respx.get(f"{BASE}/api/sources/lines").mock(
        return_value=httpx.Response(200, json={
            "sources": [
                {"line": 1, "code": "def foo():", "scmAuthor": "dev@x.com", "scmDate": "2026-07-01",
                 "lineHits": 5, "duplicated": False},
                {"line": 2, "code": "    pass", "scmAuthor": "dev@x.com", "duplicated": True},
            ]
        })
    )
    async with SonarClient(BASE, token="t") as client:
        lines = await client.sources_lines("myproj:src/a.py")
    assert len(lines) == 2
    assert lines[0].code == "def foo():"
    assert lines[0].covered is True
    assert lines[0].line_hits == 5
    assert lines[1].duplicated is True


@respx.mock
async def test_sources_lines_sends_from_to_params():
    route = respx.get(f"{BASE}/api/sources/lines").mock(
        return_value=httpx.Response(200, json={"sources": []})
    )
    async with SonarClient(BASE, token="t") as client:
        await client.sources_lines("myproj:src/a.py", branch="main", from_line=10, to_line=20)
    sent = dict(route.calls[0].request.url.params)
    assert sent["from"] == "10"
    assert sent["to"] == "20"
    assert sent["branch"] == "main"


@respx.mock
async def test_sources_lines_uncovered_line_has_none_covered_status():
    respx.get(f"{BASE}/api/sources/lines").mock(
        return_value=httpx.Response(200, json={"sources": [{"line": 1, "code": "# comment"}]})
    )
    async with SonarClient(BASE, token="t") as client:
        lines = await client.sources_lines("myproj:src/a.py")
    assert lines[0].covered is None  # lineHits absent - not a measured/executable line


@respx.mock
async def test_sources_raw_returns_plain_text():
    respx.get(f"{BASE}/api/sources/raw").mock(
        return_value=httpx.Response(200, text="def foo():\n    pass\n")
    )
    async with SonarClient(BASE, token="t") as client:
        text = await client.sources_raw("myproj:src/a.py")
    assert text == "def foo():\n    pass\n"


@respx.mock
async def test_metrics_search_returns_catalog():
    respx.get(f"{BASE}/api/metrics/search").mock(
        return_value=httpx.Response(200, json={
            "total": 1,
            "metrics": [{"key": "coverage", "name": "Coverage", "description": "Test coverage",
                         "domain": "Coverage", "type": "PERCENT", "direction": 1, "qualitative": True}],
        })
    )
    async with SonarClient(BASE, token="t") as client:
        metrics, total = await client.metrics_search()
    assert total == 1
    assert metrics[0].key == "coverage"
    assert metrics[0].direction == 1


@respx.mock
async def test_qualitygates_list_returns_gates():
    respx.get(f"{BASE}/api/qualitygates/list").mock(
        return_value=httpx.Response(200, json={
            "qualitygates": [{"id": "1", "name": "Sonar way", "isDefault": True}],
        })
    )
    async with SonarClient(BASE, token="t") as client:
        gates = await client.qualitygates_list()
    assert len(gates) == 1
    assert gates[0].name == "Sonar way"
    assert gates[0].is_default is True


@respx.mock
async def test_qualitygates_show_returns_conditions():
    respx.get(f"{BASE}/api/qualitygates/show").mock(
        return_value=httpx.Response(200, json={
            "id": "1", "name": "Sonar way", "isDefault": True,
            "conditions": [{"metric": "new_coverage", "op": "LT", "error": "80"}],
        })
    )
    async with SonarClient(BASE, token="t") as client:
        gate = await client.qualitygates_show(name="Sonar way")
    assert gate.name == "Sonar way"
    assert len(gate.conditions) == 1
    assert gate.conditions[0].metric == "new_coverage"
    assert gate.conditions[0].error_threshold == "80"


@respx.mock
async def test_qualitygates_get_by_project_resolves_then_shows():
    respx.get(f"{BASE}/api/qualitygates/get_by_project").mock(
        return_value=httpx.Response(200, json={"qualityGate": {"id": "1", "name": "Sonar way"}})
    )
    respx.get(f"{BASE}/api/qualitygates/show").mock(
        return_value=httpx.Response(200, json={"id": "1", "name": "Sonar way", "conditions": []})
    )
    async with SonarClient(BASE, token="t") as client:
        gate = await client.qualitygates_get_by_project("myproj")
    assert gate.name == "Sonar way"


@respx.mock
async def test_issues_authors_returns_list():
    respx.get(f"{BASE}/api/issues/authors").mock(
        return_value=httpx.Response(200, json={"authors": ["dev1@x.com", "dev2@x.com"]})
    )
    async with SonarClient(BASE, token="t") as client:
        authors = await client.issues_authors(project="myproj")
    assert authors == ["dev1@x.com", "dev2@x.com"]


@respx.mock
async def test_issues_tags_returns_list():
    respx.get(f"{BASE}/api/issues/tags").mock(
        return_value=httpx.Response(200, json={"tags": ["security", "convention"]})
    )
    async with SonarClient(BASE, token="t") as client:
        tags = await client.issues_tags(project="myproj")
    assert tags == ["security", "convention"]


@respx.mock
async def test_issues_changelog_returns_entries():
    respx.get(f"{BASE}/api/issues/changelog").mock(
        return_value=httpx.Response(200, json={
            "changelog": [{"creationDate": "2026-07-01", "user": "dev@x.com",
                           "diffs": [{"key": "status", "oldValue": "OPEN", "newValue": "RESOLVED"}]}],
        })
    )
    async with SonarClient(BASE, token="t") as client:
        entries = await client.issues_changelog("ISSUE-1")
    assert len(entries) == 1
    assert entries[0].user == "dev@x.com"
    assert entries[0].changes[0]["key"] == "status"


@respx.mock
async def test_quality_profiles_search_returns_profiles():
    respx.get(f"{BASE}/api/qualityprofiles/search").mock(
        return_value=httpx.Response(200, json={
            "profiles": [{"key": "py-default", "name": "Sonar way", "language": "py",
                          "isDefault": True, "activeRuleCount": 250}],
        })
    )
    async with SonarClient(BASE, token="t") as client:
        profiles = await client.quality_profiles_search(language="py")
    assert len(profiles) == 1
    assert profiles[0].active_rule_count == 250


@respx.mock
async def test_system_health_returns_status_and_causes():
    respx.get(f"{BASE}/api/system/health").mock(
        return_value=httpx.Response(200, json={"health": "YELLOW", "causes": [{"message": "Low disk space"}]})
    )
    async with SonarClient(BASE, token="t") as client:
        health = await client.system_health()
    assert health.health == "YELLOW"
    assert health.causes == ["Low disk space"]


@respx.mock
async def test_languages_list_returns_languages():
    respx.get(f"{BASE}/api/languages/list").mock(
        return_value=httpx.Response(200, json={"languages": [{"key": "py", "name": "Python"}]})
    )
    async with SonarClient(BASE, token="t") as client:
        languages = await client.languages_list()
    assert languages == [{"key": "py", "name": "Python"}]


@respx.mock
async def test_languages_list_sends_query_param():
    route = respx.get(f"{BASE}/api/languages/list").mock(
        return_value=httpx.Response(200, json={"languages": []})
    )
    async with SonarClient(BASE, token="t") as client:
        await client.languages_list(query="py")
    sent = dict(route.calls[0].request.url.params)
    assert sent["q"] == "py"
