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
