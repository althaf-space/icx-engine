from __future__ import annotations
import json
from pathlib import Path
from unittest.mock import patch
import uuid
import pytest

from icx_engine.memory.schema import MemoryEntry


def _make_entry(issue_key: str) -> MemoryEntry:
    return MemoryEntry(
        id=str(uuid.uuid4()),
        issue_key=issue_key,
        project_key=issue_key.split("-")[0],
        source_type="jira",
        issue_type="Bug",
        summary=f"Issue {issue_key}",
        problem_description="desc",
        impact="high",
        resolution_note="fixed",
        files_changed=["src/auth.py"],
        resolution_confirmed=True,
        saved_at="2026-05-12T10:00:00Z",
        tags=["auth"],
    )


def test_export_creates_valid_json(tmp_path):
    from icx_engine.memory.export import export_to_json

    entries = [_make_entry("PROJ-1"), _make_entry("PROJ-2")]
    out_path = tmp_path / "export.json"
    export_to_json(entries, out_path)

    assert out_path.exists()
    data = json.loads(out_path.read_text(encoding="utf-8"))
    assert len(data["entries"]) == 2
    assert data["entries"][0]["issue_key"] == "PROJ-1"


def test_import_round_trip(tmp_path):
    from icx_engine.memory.export import export_to_json, import_from_json

    entries = [_make_entry("PROJ-1"), _make_entry("PROJ-2")]
    out_path = tmp_path / "export.json"
    export_to_json(entries, out_path)
    imported = import_from_json(out_path)

    assert len(imported) == 2
    assert imported[0].issue_key == "PROJ-1"
    assert imported[1].issue_key == "PROJ-2"
    assert imported[0].tags == ["auth"]


def test_import_missing_file_raises_memory_error(tmp_path):
    from icx_engine.memory.export import import_from_json
    from icx_engine.exceptions import MemoryError

    with pytest.raises(MemoryError, match="not found"):
        import_from_json(tmp_path / "nonexistent.json")
