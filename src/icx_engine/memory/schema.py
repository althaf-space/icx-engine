from __future__ import annotations
from dataclasses import dataclass, field
from pydantic import BaseModel, Field


def _sq(value: str) -> str:
    """Escape a string for use in a LanceDB SQL filter (single-quote escape)."""
    return value.replace("'", "''")


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
    work_item_type: str = "bug"
    pattern_used: str = ""
    confirmation_count: int = 0
    memory_confidence: float = 0.0


@dataclass
class MemoryQueryInput:
    """Connector-agnostic query contract. Built from RawIssueData by engine.run()."""

    issue_key: str
    project_key: str
    source_type: str
    summary: str
    description: str
    issue_type: str
    tags: list[str] = field(default_factory=list)
