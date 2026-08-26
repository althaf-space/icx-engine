"""MCP tool surface for SonarQube code-quality lookups. Owns its own Tool()
definitions and dispatch function - mcp_server.py's _list_tools()/_call_tool()
get a few additive lines only, no restructuring."""

from __future__ import annotations

import json

from mcp.types import TextContent, Tool

from icx_engine.skills.hints import attach_skill_hint
from icx_engine.mcp_server import (
    _ICX_FALLBACK, _sonar_opt_str, _sonar_require_project, _sonar_scope_args,
    _sonar_str_list, _SONAR_SCOPE_SCHEMA,
)

_SONAR_STATUS_TOOL = "sonar_status"
_SONAR_PROJECTS_TOOL = "sonar_projects"
_SONAR_BRANCHES_TOOL = "sonar_branches"
_SONAR_MEASURES_TOOL = "sonar_measures"
_SONAR_QUALITY_GATE_TOOL = "sonar_quality_gate"
_SONAR_FINDINGS_TOOL = "sonar_findings"
_SONAR_REPORT_TOOL = "sonar_report"
_SONAR_TOP_FILES_TOOL = "sonar_top_files"
_SONAR_HISTORY_TOOL = "sonar_history"
_SONAR_ANALYSES_TOOL = "sonar_analyses"
_SONAR_RULE_TOOL = "sonar_rule"
_SONAR_RULES_TOOL = "sonar_rules"
_SONAR_HOTSPOT_TOOL = "sonar_hotspot"
_SONAR_SOURCE_TOOL = "sonar_source"
_SONAR_METRICS_TOOL = "sonar_metrics"
_SONAR_QUALITY_GATE_DEFINITION_TOOL = "sonar_quality_gate_definition"
_SONAR_QUALITY_PROFILES_TOOL = "sonar_quality_profiles"
_SONAR_ISSUE_AUTHORS_TOOL = "sonar_issue_authors"
_SONAR_ISSUE_TAGS_TOOL = "sonar_issue_tags"
_SONAR_ISSUE_CHANGELOG_TOOL = "sonar_issue_changelog"
_SONAR_SYSTEM_HEALTH_TOOL = "sonar_system_health"
_SONAR_LANGUAGES_TOOL = "sonar_languages"

SONAR_TOOLS: list[Tool] = [
    Tool(name=_SONAR_STATUS_TOOL,
         description="USE WHEN the user asks about code quality and you must first confirm Sonar is reachable: shows Sonar configuration and live connection health. ALWAYS call this before other sonar_* tools if a connection error is possible. Works regardless of sonar_enabled.",
         inputSchema={"type": "object", "properties": {}, "required": []},
            annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}),
    Tool(name=_SONAR_PROJECTS_TOOL,
         description=("Discover SonarQube projects the token can access. FOLLOW the mandatory protocol in the "
                      "response `instructions` field: first ask the user whether ICX should fetch projects or "
                      "they will paste the key; when `truncated` is true the list is withheld (too many) - relay "
                      "the count and ask the user to paste the exact key or supply a `query` search term. Never "
                      "invent a project key. Requires sonar_enabled."),
         inputSchema={"type": "object",
                      "properties": {"query": {"type": "string"}},
                      "required": []},
            annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True}),
    Tool(name=_SONAR_BRANCHES_TOOL,
         description=("Discover analyzed branches for a project. FOLLOW the response `instructions`: ask the user "
                      "whether ICX should fetch branches or they will paste the branch name; when `truncated` is "
                      "true, ask them to paste the name or supply a `query`. Never invent a branch name. Requires sonar_enabled."),
         inputSchema={"type": "object",
                      "properties": {"project": {"type": "string"}, "query": {"type": "string"}},
                      "required": ["project"]},
            annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}),
    Tool(name=_SONAR_MEASURES_TOOL,
         description="USE WHEN you need the headline code-quality numbers for a project: MUST fetch project measures (bugs, vulnerabilities, code smells, security hotspots, coverage, duplication, technical debt, ratings, tests) for a project/branch here rather than guessing them. Requires sonar_enabled.",
         inputSchema={"type": "object",
                      "properties": {"project": {"type": "string"}, "branch": {"type": "string"}},
                      "required": ["project"]},
            annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}),
    Tool(name=_SONAR_QUALITY_GATE_TOOL,
         description="USE WHEN deciding if code is releasable or why a build's quality gate failed: MUST fetch the quality gate status and failing conditions for a project/branch here - never assert pass/fail without it. Requires sonar_enabled.",
         inputSchema={"type": "object",
                      "properties": {"project": {"type": "string"}, "branch": {"type": "string"}},
                      "required": ["project"]},
            annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}),
    Tool(name=_SONAR_FINDINGS_TOOL,
         description=("USE WHEN the user wants the specific Sonar issues on their code: MUST fetch scoped findings "
                      "(bugs, vulnerabilities, code smells, security hotspots) for a project/branch here - do not "
                      "invent findings. Scope to the files the developer is working on by passing `files` (a "
                      "user-supplied list of paths); omit `files` for the whole project. Filter with "
                      "types/severities/statuses/author/assignee/new_code_only. Requires sonar_enabled."),
         inputSchema=_SONAR_SCOPE_SCHEMA,
            annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}),
    Tool(name=_SONAR_REPORT_TOOL,
         description=("USE WHEN you need the complete code-quality picture in one call (prefer this over calling the "
                      "individual sonar_* tools separately): MUST assemble a full structured report for a "
                      "project/branch - quality gate, project measures, per-file measures, findings (issues + "
                      "security hotspots), duplication blocks, and test-coverage gaps. Pass `files` (user-supplied "
                      "paths) to scope everything to the developer's working set; omit for the whole project. "
                      "Requires sonar_enabled."),
         inputSchema=_SONAR_SCOPE_SCHEMA,
            annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}),
    Tool(name=_SONAR_TOP_FILES_TOOL,
         description=("USE WHEN the user wants to know WHICH files are worst for a metric (most duplicated, "
                      "least covered, most bugs, etc.) rather than already knowing the file: MUST call this to "
                      "rank/list files by a single metric - never guess which files are worst. Requires sonar_enabled."),
         inputSchema={"type": "object",
                      "properties": {
                          "project": {"type": "string"}, "branch": {"type": "string"},
                          "metric": {"type": "string"}, "limit": {"type": "integer"},
                          "ascending": {"type": "boolean"},
                      },
                      "required": ["project", "metric"]},
            annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}),
    Tool(name=_SONAR_HISTORY_TOOL,
         description=("USE WHEN the user asks whether a metric is improving or degrading over time: MUST fetch "
                      "the chronological history for one or more metrics here rather than comparing a single "
                      "snapshot. Requires sonar_enabled."),
         inputSchema={"type": "object",
                      "properties": {
                          "project": {"type": "string"}, "branch": {"type": "string"},
                          "metrics": {"type": "array", "items": {"type": "string"}},
                          "date_from": {"type": "string"}, "date_to": {"type": "string"},
                      },
                      "required": ["project", "metrics"]},
            annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}),
    Tool(name=_SONAR_ANALYSES_TOOL,
         description=("USE WHEN the user asks when scans ran or what versions/quality-gate events happened: "
                      "MUST fetch the project's analysis history here. Requires sonar_enabled."),
         inputSchema={"type": "object",
                      "properties": {
                          "project": {"type": "string"}, "branch": {"type": "string"},
                          "date_from": {"type": "string"}, "date_to": {"type": "string"},
                      },
                      "required": ["project"]},
            annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}),
    Tool(name=_SONAR_RULE_TOOL,
         description=("USE WHEN a finding's `rule` key needs explaining (why it was flagged, how to fix it): "
                      "MUST fetch the rule's full description here rather than guessing what a rule key means. "
                      "Requires sonar_enabled."),
         inputSchema={"type": "object",
                      "properties": {"rule_key": {"type": "string"}},
                      "required": ["rule_key"]},
            annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}),
    Tool(name=_SONAR_RULES_TOOL,
         description=("USE WHEN the user wants to browse or search rules by language/tag/repository "
                      "rather than looking up one specific rule key: MUST fetch the rule list here. "
                      "Requires sonar_enabled."),
         inputSchema={"type": "object",
                      "properties": {
                          "language": {"type": "string"},
                          "tags": {"type": "array", "items": {"type": "string"}},
                          "repositories": {"type": "array", "items": {"type": "string"}},
                          "query": {"type": "string"},
                          "page_size": {"type": "integer"},
                      },
                      "required": []},
            annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}),
    Tool(name=_SONAR_HOTSPOT_TOOL,
         description=("USE WHEN a specific security hotspot needs full risk/fix detail beyond what "
                      "sonar_findings' summary shows: MUST fetch the hotspot's full detail here. "
                      "Requires sonar_enabled."),
         inputSchema={"type": "object",
                      "properties": {"hotspot_key": {"type": "string"}},
                      "required": ["hotspot_key"]},
            annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}),
    Tool(name=_SONAR_SOURCE_TOOL,
         description=("USE WHEN you need to see the exact flagged source lines with coverage/duplication "
                      "context (not just the finding's message): MUST fetch annotated source lines here "
                      "rather than reading the file separately with no Sonar context. Requires sonar_enabled."),
         inputSchema={"type": "object",
                      "properties": {
                          "project": {"type": "string"}, "path": {"type": "string"},
                          "branch": {"type": "string"}, "from_line": {"type": "integer"}, "to_line": {"type": "integer"},
                      },
                      "required": ["project", "path"]},
            annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}),
    Tool(name=_SONAR_METRICS_TOOL,
         description=("USE WHEN the user asks what a metric key means, or which metrics exist: MUST fetch "
                      "the metric catalog here rather than guessing. Requires sonar_enabled."),
         inputSchema={"type": "object", "properties": {"page_size": {"type": "integer"}}, "required": []},
            annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}),
    Tool(name=_SONAR_QUALITY_GATE_DEFINITION_TOOL,
         description=("USE WHEN the user asks what quality gate is assigned to a project or what its "
                      "configured thresholds are: MUST fetch the gate's full authored definition here - "
                      "sonar_quality_gate only reports pass/fail for the LAST analysis, not the gate's own "
                      "configuration. Pass either project (to resolve the assigned gate) or gate_name "
                      "(to look up a specific gate by name). Requires sonar_enabled."),
         inputSchema={"type": "object",
                      "properties": {"project": {"type": "string"}, "gate_name": {"type": "string"}},
                      "required": []},
            annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}),
    Tool(name=_SONAR_QUALITY_PROFILES_TOOL,
         description=("USE WHEN the user asks which quality profile is applied to a project or language, "
                      "or how many rules it enables: MUST fetch profiles here. Requires sonar_enabled."),
         inputSchema={"type": "object",
                      "properties": {"language": {"type": "string"}, "project": {"type": "string"}},
                      "required": []},
            annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}),
    Tool(name=_SONAR_ISSUE_AUTHORS_TOOL,
         description=("USE WHEN the user asks who has open issues or wants to filter/scope by author: "
                      "MUST fetch the list of issue authors here. Requires sonar_enabled."),
         inputSchema={"type": "object",
                      "properties": {"project": {"type": "string"}, "query": {"type": "string"}},
                      "required": []},
            annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}),
    Tool(name=_SONAR_ISSUE_TAGS_TOOL,
         description=("USE WHEN the user asks what issue tags exist or wants to filter/scope by tag: "
                      "MUST fetch the list of issue tags here. Requires sonar_enabled."),
         inputSchema={"type": "object",
                      "properties": {"project": {"type": "string"}, "query": {"type": "string"}},
                      "required": []},
            annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}),
    Tool(name=_SONAR_ISSUE_CHANGELOG_TOOL,
         description=("USE WHEN the user asks about an issue's history (when assigned/resolved and by "
                      "whom): MUST fetch the issue's changelog here. Requires sonar_enabled."),
         inputSchema={"type": "object", "properties": {"issue_key": {"type": "string"}}, "required": ["issue_key"]},
            annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}),
    Tool(name=_SONAR_SYSTEM_HEALTH_TOOL,
         description=("USE WHEN the user asks if the Sonar server itself is healthy (not just reachable): "
                      "MUST fetch system health here - sonar_status only confirms the server responds, "
                      "not whether it's degraded. Requires sonar_enabled."),
         inputSchema={"type": "object", "properties": {}, "required": []},
            annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True}),
    Tool(name=_SONAR_LANGUAGES_TOOL,
         description=("USE WHEN the user asks what languages this Sonar server analyzes: MUST fetch the "
                      "language list here. Requires sonar_enabled."),
         inputSchema={"type": "object", "properties": {"query": {"type": "string"}}, "required": []},
            annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True}),
]


async def dispatch_sonar_tool(name: str, arguments: dict) -> list[TextContent] | None:
    args = arguments or {}

    if name == _SONAR_STATUS_TOOL:
        from icx_engine.sonar import service as _sonar_svc
        try:
            data = await _sonar_svc.status()
            resp = attach_skill_hint({"ok": True, "data": data}, "sonar-quality-review",
                                      rank_prompt="sonar code quality findings", archetype="quality")
            return [TextContent(type="text", text=json.dumps(resp))]
        except Exception as exc:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": str(exc)}))]

    if name == _SONAR_PROJECTS_TOOL:
        from icx_engine.sonar import service as _sonar_svc
        try:
            data = await _sonar_svc.projects(query=_sonar_opt_str(args, "query"))
            return [TextContent(type="text", text=json.dumps({"ok": True, "data": data}))]
        except _sonar_svc.SonarDisabled as exc:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": str(exc),
                "fallback": _ICX_FALLBACK("SonarQube", "icx sonar --add")}))]
        except Exception as exc:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": str(exc)}))]

    if name == _SONAR_BRANCHES_TOOL:
        from icx_engine.sonar import service as _sonar_svc
        project = _sonar_require_project(args)
        if project is None:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": "project is required and must be a non-empty string."}))]
        try:
            data = await _sonar_svc.branches(project, query=_sonar_opt_str(args, "query"))
            return [TextContent(type="text", text=json.dumps({"ok": True, "data": data}))]
        except _sonar_svc.SonarDisabled as exc:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": str(exc),
                "fallback": _ICX_FALLBACK("SonarQube", "icx sonar --add")}))]
        except Exception as exc:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": str(exc)}))]

    if name in (_SONAR_MEASURES_TOOL, _SONAR_QUALITY_GATE_TOOL):
        from icx_engine.sonar import service as _sonar_svc
        project = _sonar_require_project(args)
        if project is None:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": "project is required and must be a non-empty string."}))]
        branch = _sonar_opt_str(args, "branch")
        try:
            if name == _SONAR_MEASURES_TOOL:
                data = await _sonar_svc.measures(project, branch)
            else:
                data = await _sonar_svc.quality_gate(project, branch)
            return [TextContent(type="text", text=json.dumps({"ok": True, "data": data}))]
        except _sonar_svc.SonarDisabled as exc:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": str(exc),
                "fallback": _ICX_FALLBACK("SonarQube", "icx sonar --add")}))]
        except Exception as exc:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": str(exc)}))]

    if name in (_SONAR_FINDINGS_TOOL, _SONAR_REPORT_TOOL):
        from icx_engine.sonar import service as _sonar_svc
        scope = _sonar_scope_args(args)
        if scope is None:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": "project is required and must be a non-empty string."}))]
        try:
            fn = _sonar_svc.findings if name == _SONAR_FINDINGS_TOOL else _sonar_svc.report
            data = await fn(**scope)
            return [TextContent(type="text", text=json.dumps({"ok": True, "data": data}))]
        except _sonar_svc.SonarDisabled as exc:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": str(exc),
                "fallback": _ICX_FALLBACK("SonarQube", "icx sonar --add")}))]
        except Exception as exc:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": str(exc)}))]

    if name == _SONAR_TOP_FILES_TOOL:
        from icx_engine.sonar import service as _sonar_svc
        project = _sonar_require_project(args)
        metric = _sonar_opt_str(args, "metric")
        if project is None or metric is None:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": "project and metric are required."}))]
        a = args or {}
        limit = a.get("limit")
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            limit = 20
        try:
            data = await _sonar_svc.top_files(
                project, metric, branch=_sonar_opt_str(args, "branch"),
                limit=limit, ascending=bool(a.get("ascending")),
            )
            return [TextContent(type="text", text=json.dumps({"ok": True, "data": data}))]
        except _sonar_svc.SonarDisabled as exc:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": str(exc),
                "fallback": _ICX_FALLBACK("SonarQube", "icx sonar --add")}))]
        except Exception as exc:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": str(exc)}))]

    if name == _SONAR_HISTORY_TOOL:
        from icx_engine.sonar import service as _sonar_svc
        project = _sonar_require_project(args)
        metrics = _sonar_str_list(args, "metrics")
        if project is None or not metrics:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": "project and metrics (non-empty) are required."}))]
        try:
            data = await _sonar_svc.metric_history(
                project, metrics, branch=_sonar_opt_str(args, "branch"),
                date_from=_sonar_opt_str(args, "date_from"), date_to=_sonar_opt_str(args, "date_to"),
            )
            return [TextContent(type="text", text=json.dumps({"ok": True, "data": data}))]
        except _sonar_svc.SonarDisabled as exc:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": str(exc),
                "fallback": _ICX_FALLBACK("SonarQube", "icx sonar --add")}))]
        except Exception as exc:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": str(exc)}))]

    if name == _SONAR_ANALYSES_TOOL:
        from icx_engine.sonar import service as _sonar_svc
        project = _sonar_require_project(args)
        if project is None:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": "project is required."}))]
        try:
            data = await _sonar_svc.analyses(
                project, branch=_sonar_opt_str(args, "branch"),
                date_from=_sonar_opt_str(args, "date_from"), date_to=_sonar_opt_str(args, "date_to"),
            )
            return [TextContent(type="text", text=json.dumps({"ok": True, "data": data}))]
        except _sonar_svc.SonarDisabled as exc:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": str(exc),
                "fallback": _ICX_FALLBACK("SonarQube", "icx sonar --add")}))]
        except Exception as exc:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": str(exc)}))]

    if name == _SONAR_RULE_TOOL:
        from icx_engine.sonar import service as _sonar_svc
        rule_key = _sonar_opt_str(args, "rule_key")
        if rule_key is None:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": "rule_key is required."}))]
        try:
            data = await _sonar_svc.rule(rule_key)
            return [TextContent(type="text", text=json.dumps({"ok": True, "data": data}))]
        except _sonar_svc.SonarDisabled as exc:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": str(exc),
                "fallback": _ICX_FALLBACK("SonarQube", "icx sonar --add")}))]
        except Exception as exc:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": str(exc)}))]

    if name == _SONAR_RULES_TOOL:
        from icx_engine.sonar import service as _sonar_svc
        a = args or {}
        page_size = a.get("page_size")
        if not isinstance(page_size, int) or isinstance(page_size, bool) or page_size <= 0:
            page_size = 50
        try:
            data = await _sonar_svc.rules(
                language=_sonar_opt_str(args, "language"),
                tags=_sonar_str_list(args, "tags"),
                repositories=_sonar_str_list(args, "repositories"),
                query=_sonar_opt_str(args, "query"),
                page_size=page_size,
            )
            return [TextContent(type="text", text=json.dumps({"ok": True, "data": data}))]
        except _sonar_svc.SonarDisabled as exc:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": str(exc),
                "fallback": _ICX_FALLBACK("SonarQube", "icx sonar --add")}))]
        except Exception as exc:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": str(exc)}))]

    if name == _SONAR_HOTSPOT_TOOL:
        from icx_engine.sonar import service as _sonar_svc
        hotspot_key = _sonar_opt_str(args, "hotspot_key")
        if hotspot_key is None:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": "hotspot_key is required."}))]
        try:
            data = await _sonar_svc.hotspot(hotspot_key)
            return [TextContent(type="text", text=json.dumps({"ok": True, "data": data}))]
        except _sonar_svc.SonarDisabled as exc:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": str(exc),
                "fallback": _ICX_FALLBACK("SonarQube", "icx sonar --add")}))]
        except Exception as exc:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": str(exc)}))]

    if name == _SONAR_SOURCE_TOOL:
        from icx_engine.sonar import service as _sonar_svc
        project = _sonar_require_project(args)
        path = _sonar_opt_str(args, "path")
        if project is None or path is None:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": "project and path are required."}))]
        a = args or {}
        from_line = a.get("from_line") if isinstance(a.get("from_line"), int) and not isinstance(a.get("from_line"), bool) else None
        to_line = a.get("to_line") if isinstance(a.get("to_line"), int) and not isinstance(a.get("to_line"), bool) else None
        try:
            data = await _sonar_svc.source_lines(project, path, branch=_sonar_opt_str(args, "branch"), from_line=from_line, to_line=to_line)
            return [TextContent(type="text", text=json.dumps({"ok": True, "data": data}))]
        except _sonar_svc.SonarDisabled as exc:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": str(exc),
                "fallback": _ICX_FALLBACK("SonarQube", "icx sonar --add")}))]
        except Exception as exc:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": str(exc)}))]

    if name == _SONAR_METRICS_TOOL:
        from icx_engine.sonar import service as _sonar_svc
        a = args or {}
        page_size = a.get("page_size")
        if not isinstance(page_size, int) or isinstance(page_size, bool) or page_size <= 0:
            page_size = 100
        try:
            data = await _sonar_svc.metrics(page_size=page_size)
            return [TextContent(type="text", text=json.dumps({"ok": True, "data": data}))]
        except _sonar_svc.SonarDisabled as exc:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": str(exc),
                "fallback": _ICX_FALLBACK("SonarQube", "icx sonar --add")}))]
        except Exception as exc:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": str(exc)}))]

    if name == _SONAR_QUALITY_GATE_DEFINITION_TOOL:
        from icx_engine.sonar import service as _sonar_svc
        try:
            data = await _sonar_svc.quality_gate_definition(
                project=_sonar_opt_str(args, "project"), gate_name=_sonar_opt_str(args, "gate_name"),
            )
            return [TextContent(type="text", text=json.dumps({"ok": True, "data": data}))]
        except ValueError as exc:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": str(exc)}))]
        except _sonar_svc.SonarDisabled as exc:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": str(exc),
                "fallback": _ICX_FALLBACK("SonarQube", "icx sonar --add")}))]
        except Exception as exc:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": str(exc)}))]

    if name == _SONAR_QUALITY_PROFILES_TOOL:
        from icx_engine.sonar import service as _sonar_svc
        try:
            data = await _sonar_svc.quality_profiles(
                language=_sonar_opt_str(args, "language"), project=_sonar_opt_str(args, "project"),
            )
            return [TextContent(type="text", text=json.dumps({"ok": True, "data": data}))]
        except _sonar_svc.SonarDisabled as exc:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": str(exc),
                "fallback": _ICX_FALLBACK("SonarQube", "icx sonar --add")}))]
        except Exception as exc:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": str(exc)}))]

    if name == _SONAR_ISSUE_AUTHORS_TOOL:
        from icx_engine.sonar import service as _sonar_svc
        try:
            data = await _sonar_svc.issue_authors(project=_sonar_opt_str(args, "project"), query=_sonar_opt_str(args, "query"))
            return [TextContent(type="text", text=json.dumps({"ok": True, "data": data}))]
        except _sonar_svc.SonarDisabled as exc:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": str(exc),
                "fallback": _ICX_FALLBACK("SonarQube", "icx sonar --add")}))]
        except Exception as exc:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": str(exc)}))]

    if name == _SONAR_ISSUE_TAGS_TOOL:
        from icx_engine.sonar import service as _sonar_svc
        try:
            data = await _sonar_svc.issue_tags(project=_sonar_opt_str(args, "project"), query=_sonar_opt_str(args, "query"))
            return [TextContent(type="text", text=json.dumps({"ok": True, "data": data}))]
        except _sonar_svc.SonarDisabled as exc:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": str(exc),
                "fallback": _ICX_FALLBACK("SonarQube", "icx sonar --add")}))]
        except Exception as exc:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": str(exc)}))]

    if name == _SONAR_ISSUE_CHANGELOG_TOOL:
        from icx_engine.sonar import service as _sonar_svc
        issue_key = _sonar_opt_str(args, "issue_key")
        if issue_key is None:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": "issue_key is required."}))]
        try:
            data = await _sonar_svc.issue_changelog(issue_key)
            return [TextContent(type="text", text=json.dumps({"ok": True, "data": data}))]
        except _sonar_svc.SonarDisabled as exc:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": str(exc),
                "fallback": _ICX_FALLBACK("SonarQube", "icx sonar --add")}))]
        except Exception as exc:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": str(exc)}))]

    if name == _SONAR_SYSTEM_HEALTH_TOOL:
        from icx_engine.sonar import service as _sonar_svc
        try:
            data = await _sonar_svc.system_health()
            return [TextContent(type="text", text=json.dumps({"ok": True, "data": data}))]
        except _sonar_svc.SonarDisabled as exc:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": str(exc),
                "fallback": _ICX_FALLBACK("SonarQube", "icx sonar --add")}))]
        except Exception as exc:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": str(exc)}))]

    if name == _SONAR_LANGUAGES_TOOL:
        from icx_engine.sonar import service as _sonar_svc
        try:
            data = await _sonar_svc.languages(query=_sonar_opt_str(args, "query"))
            return [TextContent(type="text", text=json.dumps({"ok": True, "data": data}))]
        except _sonar_svc.SonarDisabled as exc:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": str(exc),
                "fallback": _ICX_FALLBACK("SonarQube", "icx sonar --add")}))]
        except Exception as exc:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": str(exc)}))]

    return None
