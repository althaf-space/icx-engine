"""Data contracts for the SonarQube code-quality reader.

Mirrors the analyze flow's contract split: raw Sonar Web API JSON is normalized
into these typed models before returning to a caller (MCP agent or CLI). No LLM
is involved - the report is a faithful, structured projection of SonarQube data.

`SonarScope` is the request/filter model (developer scoping). `SonarReport` is
the assembled output (quality gate + measures + findings + duplications + test
gaps). All models are connector-agnostic and contain no vendor coupling.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

# Metric keys requested from /api/measures/component (project + per file).
# Every dashboard box is covered: reliability, security, maintainability,
# coverage, duplication, size, technical debt, and unit-test execution.
MEASURE_METRIC_KEYS: tuple[str, ...] = (
    "bugs", "vulnerabilities", "code_smells", "security_hotspots",
    "coverage", "line_coverage", "uncovered_lines",
    "duplicated_lines_density", "duplicated_lines", "duplicated_blocks", "duplicated_files",
    "ncloc", "sqale_index",
    "reliability_rating", "security_rating", "sqale_rating", "security_review_rating",
    "tests", "test_failures", "test_errors", "skipped_tests",
    "test_success_density", "test_execution_time",
    "new_bugs", "new_vulnerabilities", "new_code_smells",
    "new_coverage", "new_duplicated_lines_density",
)

_RATING_LETTERS = {"1": "A", "2": "B", "3": "C", "4": "D", "5": "E"}


def rating_letter(value: str | None) -> str | None:
    """Map a SonarQube numeric rating ('1.0'..'5.0') to a letter (A..E)."""
    if not value:
        return None
    return _RATING_LETTERS.get(value.split(".")[0])


class SonarFinding(BaseModel):
    """A single normalized issue or security hotspot."""

    key: str
    type: str                       # BUG | VULNERABILITY | CODE_SMELL | SECURITY_HOTSPOT
    severity: str                   # BLOCKER..INFO, or HIGH/MEDIUM/LOW for hotspots
    rule: str
    message: str
    file: str | None = None         # path relative to the project root
    line: int | None = None
    status: str = ""
    effort: str | None = None       # human remediation effort, e.g. "10min"
    effort_minutes: int | None = None
    author: str = ""
    assignee: str = ""
    tags: list[str] = Field(default_factory=list)
    new_code: bool = False          # introduced in the new-code period
    security_category: str = ""     # hotspots only, e.g. "sql-injection"
    creation_date: str = ""
    update_date: str = ""


class SonarMeasures(BaseModel):
    """Numeric measures for a project or a single file component."""

    component: str = ""
    bugs: int | None = None
    vulnerabilities: int | None = None
    code_smells: int | None = None
    security_hotspots: int | None = None
    coverage: float | None = None
    line_coverage: float | None = None
    uncovered_lines: int | None = None
    duplicated_lines_density: float | None = None
    duplicated_lines: int | None = None
    duplicated_blocks: int | None = None
    duplicated_files: int | None = None
    ncloc: int | None = None
    technical_debt_minutes: int | None = None   # from sqale_index
    technical_debt: str = ""                     # human, e.g. "3d 4h"
    reliability_rating: str | None = None        # A..E
    security_rating: str | None = None
    maintainability_rating: str | None = None    # from sqale_rating
    security_review_rating: str | None = None
    tests: int | None = None
    test_failures: int | None = None
    test_errors: int | None = None
    skipped_tests: int | None = None
    test_success_density: float | None = None
    test_execution_time_ms: int | None = None
    new_bugs: int | None = None
    new_vulnerabilities: int | None = None
    new_code_smells: int | None = None
    new_coverage: float | None = None
    new_duplicated_lines_density: float | None = None
    raw: dict[str, str] = Field(default_factory=dict)   # every raw metric:value


class SonarGateCondition(BaseModel):
    metric: str
    comparator: str = ""
    error_threshold: str = ""
    actual_value: str = ""
    status: str = ""                # OK | ERROR | WARN | NO_VALUE


class SonarQualityGate(BaseModel):
    status: str = "NONE"            # OK | ERROR | WARN | NONE
    conditions: list[SonarGateCondition] = Field(default_factory=list)


class SonarDupBlock(BaseModel):
    from_line: int
    size: int
    ref_file: str | None = None     # the file this block is duplicated with


class SonarDuplication(BaseModel):
    file: str
    blocks: list[SonarDupBlock] = Field(default_factory=list)


class SonarTestGap(BaseModel):
    file: str
    coverage: float | None = None
    has_tests: bool = False


class SonarScope(BaseModel):
    """Developer scoping. `files` is supplied by the user only - ICX never
    derives it. An empty `files` list means project-wide (bounded by `limit`)."""

    project: str
    branch: str | None = None
    files: list[str] = Field(default_factory=list)
    types: list[str] = Field(default_factory=list)
    severities: list[str] = Field(default_factory=list)
    statuses: list[str] = Field(default_factory=list)
    rules: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    author: str | None = None
    assignee: str | None = None
    new_code_only: bool = False
    limit: int = 1000


class SonarReport(BaseModel):
    """Assembled, structured projection of SonarQube for a scope."""

    project: str
    branch: str | None = None
    server_url: str = ""
    quality_gate: SonarQualityGate = Field(default_factory=SonarQualityGate)
    measures: SonarMeasures = Field(default_factory=SonarMeasures)
    file_measures: dict[str, SonarMeasures] = Field(default_factory=dict)
    findings: list[SonarFinding] = Field(default_factory=list)
    duplications: list[SonarDuplication] = Field(default_factory=list)
    test_gaps: list[SonarTestGap] = Field(default_factory=list)
    summary: dict = Field(default_factory=dict)
    total_findings: int = 0
    truncated: bool = False


class ComponentMeasure(BaseModel):
    """One row of a component_tree ranking - a file or directory's value for
    a single metric, used to answer 'top N files by metric X'."""

    key: str                        # full component key, e.g. "project:src/foo.py"
    path: str                       # path relative to the project root
    qualifier: str = ""             # FIL (file) | DIR (directory) | UTS (test file)
    metric: str = ""
    value: str | None = None
    language: str | None = None


class MetricHistoryPoint(BaseModel):
    date: str
    value: str | None = None


class AnalysisEvent(BaseModel):
    key: str = ""
    category: str = ""              # VERSION | QUALITY_GATE | OTHER | DEFINITION_CHANGE
    name: str = ""
    description: str = ""


class SonarAnalysis(BaseModel):
    key: str = ""
    date: str = ""
    project_version: str = ""
    events: list[AnalysisEvent] = Field(default_factory=list)


class SonarRule(BaseModel):
    """Full detail for a single rule - what a SonarFinding.rule key refers to."""

    key: str
    name: str = ""
    language: str = ""
    type: str = ""                  # BUG | VULNERABILITY | CODE_SMELL | SECURITY_HOTSPOT
    severity: str = ""
    status: str = ""                # READY | DEPRECATED | REMOVED | BETA
    html_description: str = ""      # full rationale + fix guidance, as authored by SonarSource
    remediation_function: str = ""
    tags: list[str] = Field(default_factory=list)
    repository: str = ""


class SonarHotspotDetail(BaseModel):
    """Full detail for one security hotspot - richer than the summary
    SonarFinding a hotspots/search list entry produces."""

    key: str
    rule_key: str = ""
    message: str = ""
    file: str | None = None
    line: int | None = None
    status: str = ""                 # TO_REVIEW | REVIEWED
    resolution: str | None = None    # FIXED | SAFE (only when status=REVIEWED)
    vulnerability_probability: str = ""
    security_category: str = ""
    author: str = ""
    creation_date: str = ""
    update_date: str = ""
    risk_description: str = ""
    vulnerability_description: str = ""
    fix_recommendations: str = ""


class SourceLine(BaseModel):
    """One line of source code with its per-line Sonar annotations - lets a
    caller see the exact flagged lines with coverage/duplication context
    without a separate file read."""

    line: int
    code: str = ""
    covered: bool | None = None        # None = not measured (e.g. non-executable line)
    line_hits: int | None = None
    duplicated: bool = False
    scm_author: str = ""
    scm_revision: str = ""
    scm_date: str = ""


class MetricInfo(BaseModel):
    """One entry from the metric catalog - what a metric key means."""

    key: str
    name: str = ""
    description: str = ""
    domain: str = ""                    # e.g. "Coverage", "Duplications"
    type: str = ""                      # e.g. "PERCENT", "INT", "RATING"
    direction: int = 0                  # 1 = higher is better, -1 = lower is better, 0 = neutral
    qualitative: bool = False


class QualityGateConditionDef(BaseModel):
    """One authored threshold in a quality gate's definition (distinct from
    SonarGateCondition, which is a per-analysis pass/fail RESULT - this is
    the gate's own configuration, independent of any specific analysis)."""

    metric: str
    comparator: str = ""
    error_threshold: str = ""


class QualityGateDefinition(BaseModel):
    """A quality gate's own identity and authored configuration."""

    id: str = ""
    name: str = ""
    is_default: bool = False
    conditions: list[QualityGateConditionDef] = Field(default_factory=list)


class IssueChangelogEntry(BaseModel):
    creation_date: str = ""
    user: str = ""
    changes: list[dict] = Field(default_factory=list)   # each: {"key": "...", "old_value": "...", "new_value": "..."}


class QualityProfile(BaseModel):
    key: str = ""
    name: str = ""
    language: str = ""
    is_default: bool = False
    active_rule_count: int = 0


class SystemHealth(BaseModel):
    health: str = ""                    # GREEN | YELLOW | RED
    causes: list[str] = Field(default_factory=list)
