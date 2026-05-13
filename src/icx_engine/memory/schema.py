from __future__ import annotations
from dataclasses import dataclass
from pydantic import BaseModel, Field


class MemoryEntry(BaseModel):
    """User-facing memory record. Never contains secrets or raw API data."""

    id: str
    issue_key: str
    project_key: str
    source_type: str
    issue_type: str
    summary: str
    problem_description: str
    impact: str = ""
    resolution_note: str
    files_changed: list[str]
    resolution_confirmed: bool
    saved_at: str
    tags: list[str] = Field(default_factory=list)


@dataclass
class MemoryQueryInput:
    """Connector-agnostic query contract. Built from RawIssueData by engine.run()."""

    issue_key: str
    project_key: str
    source_type: str
    summary: str
    description: str
    issue_type: str
