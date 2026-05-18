from typing import Literal
from pydantic import BaseModel, Field


class RawIssueData(BaseModel):
    """Internal model - raw source API data before LLM analysis. Never returned to callers."""

    issue_key: str
    issue_type: str
    summary: str
    description: str
    comments: list[str]
    attachments: list[str]          # filenames only
    priority: str
    status: str
    metadata: dict
    due_date: str | None = None
    attachment_content_urls: dict[str, str] = Field(default_factory=dict)  # filename → download URL
    attachment_texts: dict[str, str] = Field(default_factory=dict)          # filename → extracted text


class PastInsight(BaseModel):
    """A resolved past issue surfaced by the memory engine."""

    issue_key: str
    source_type: str
    summary: str
    resolution_note: str
    files_changed: list[str]
    similarity_score: float = Field(..., ge=0.0, le=1.0)
    saved_at: str
    work_item_type: str = ""
    pattern_used: str = ""


class IssueContext(BaseModel):
    """Returned by both CLI and MCP - LLM-analyzed, structured output."""

    problem_summary: str
    detailed_description: str
    reproduction_steps: list[str]       # empty list for non-bug types
    expected_behavior: str | None       # null for stories/tasks
    actual_behavior: str | None         # null for stories/tasks
    acceptance_criteria: list[str]      # populated for stories
    impact: str
    priority: str
    issue_type: str                     # always from source metadata, never LLM inference
    confidence_score: float = Field(..., ge=0.0, le=1.0)
    completeness_score: float = Field(..., ge=0.0, le=1.0)
    missing_information: list[str]
    images: dict[str, str] = Field(default_factory=dict)  # filename → Base64
    past_insights: list[PastInsight] = Field(default_factory=list)
    pending_images: list[str] = Field(default_factory=list)


class RawIssueResponse(BaseModel):
    """Returned by MCP when no LLM is configured - raw issue data with attachment content."""

    mode: Literal["raw"] = "raw"
    issue_key: str
    issue_type: str
    summary: str
    description: str
    comments: list[str]
    attachments: list[str]
    priority: str
    status: str
    metadata: dict
    due_date: str | None = None
    attachment_texts: dict[str, str] = Field(default_factory=dict)
    images: dict[str, str] = Field(default_factory=dict)  # filename → Base64
    note: str = (
        "No LLM analysis performed - no API key configured. "
        "Raw issue data, digested documents, and raw images are provided for your direct analysis."
    )
