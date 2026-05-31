from __future__ import annotations
import uuid
from unittest.mock import patch, MagicMock

from icx_engine.memory.schema import MemoryEntry


def _make_entry(**kwargs) -> MemoryEntry:
    defaults = dict(
        id=str(uuid.uuid4()),
        issue_key="PROJ-1",
        project_key="PROJ",
        source_type="jira",
        issue_type="Bug",
        summary="Auth fails",
        problem_description="JWT expired",
        impact="",
        resolution_note="Updated TTL",
        files_changed=["src/auth/token.py"],
        resolution_confirmed=True,
        saved_at="2026-05-12T10:00:00Z",
        tags=[],
        work_item_type="bug",
        pattern_used="",
    )
    defaults.update(kwargs)
    return MemoryEntry(**defaults)


def _mgr_with(entries: list[MemoryEntry]):
    mgr = MagicMock()
    mgr.list_entries.return_value = entries
    return mgr


# ── find_work_items_by_file ───────────────────────────────────────────────────

def test_find_by_file_returns_matching_entry():
    from icx_engine.memory.bridge import find_work_items_by_file
    entry = _make_entry(files_changed=["src/auth/token.py"])
    mgr = _mgr_with([entry])
    result = find_work_items_by_file("auth/token.py", mgr)
    assert len(result) == 1
    assert result[0].issue_key == "PROJ-1"


def test_find_by_file_excludes_non_matching():
    from icx_engine.memory.bridge import find_work_items_by_file
    entry = _make_entry(files_changed=["src/payments/invoice.py"])
    mgr = _mgr_with([entry])
    result = find_work_items_by_file("auth/token.py", mgr)
    assert result == []


def test_find_by_file_case_insensitive():
    from icx_engine.memory.bridge import find_work_items_by_file
    entry = _make_entry(files_changed=["src/Auth/Token.py"])
    mgr = _mgr_with([entry])
    result = find_work_items_by_file("auth/token.py", mgr)
    assert len(result) == 1


def test_find_by_file_windows_separator_matches_unix():
    from icx_engine.memory.bridge import find_work_items_by_file
    entry = _make_entry(files_changed=["src\\auth\\token.py"])
    mgr = _mgr_with([entry])
    result = find_work_items_by_file("auth/token.py", mgr)
    assert len(result) == 1


def test_find_by_file_unix_separator_matches_windows_query():
    from icx_engine.memory.bridge import find_work_items_by_file
    entry = _make_entry(files_changed=["src/auth/token.py"])
    mgr = _mgr_with([entry])
    result = find_work_items_by_file("src\\auth\\token.py", mgr)
    assert len(result) == 1


def test_find_by_file_passes_project_key_to_list_entries():
    from icx_engine.memory.bridge import find_work_items_by_file
    mgr = _mgr_with([])
    find_work_items_by_file("any/file.py", mgr, project_key="PROJ")
    mgr.list_entries.assert_called_once_with(project_key="PROJ")


def test_find_by_file_multiple_files_changed():
    from icx_engine.memory.bridge import find_work_items_by_file
    entry = _make_entry(files_changed=["src/auth/token.py", "src/auth/session.py"])
    mgr = _mgr_with([entry])
    assert len(find_work_items_by_file("session.py", mgr)) == 1
    assert len(find_work_items_by_file("token.py", mgr)) == 1


# ── get_work_item_density ───────────────────────────────────────────────────────────

def test_bug_density_counts_correctly():
    from icx_engine.memory.bridge import get_work_item_density
    e1 = _make_entry(issue_key="PROJ-1", files_changed=["src/auth/token.py"])
    e2 = _make_entry(issue_key="PROJ-2", id=str(uuid.uuid4()), files_changed=["src/auth/token.py", "src/auth/session.py"])
    mgr = _mgr_with([e1, e2])
    rows = get_work_item_density(mgr)
    token_row = next(r for r in rows if "token.py" in r["file"])
    assert token_row["count"] == 2
    assert set(token_row["work_items"]) == {"PROJ-1", "PROJ-2"}


def test_bug_density_sorted_desc():
    from icx_engine.memory.bridge import get_work_item_density
    entries = [
        _make_entry(issue_key=f"PROJ-{i}", id=str(uuid.uuid4()), files_changed=["src/hot.py"])
        for i in range(3)
    ] + [_make_entry(issue_key="PROJ-99", id=str(uuid.uuid4()), files_changed=["src/cold.py"])]
    mgr = _mgr_with(entries)
    rows = get_work_item_density(mgr)
    assert rows[0]["count"] >= rows[-1]["count"]
    assert rows[0]["file"].endswith("hot.py")


def test_bug_density_top_n_capped():
    from icx_engine.memory.bridge import get_work_item_density
    entries = [
        _make_entry(issue_key=f"PROJ-{i}", id=str(uuid.uuid4()), files_changed=[f"src/file{i}.py"])
        for i in range(10)
    ]
    mgr = _mgr_with(entries)
    rows = get_work_item_density(mgr, top_n=3)
    assert len(rows) <= 3


def test_bug_density_deduplicates_same_issue_key():
    from icx_engine.memory.bridge import get_work_item_density
    entry = _make_entry(issue_key="PROJ-1", files_changed=["src/auth/token.py"])
    mgr = _mgr_with([entry, entry])  # same entry twice
    rows = get_work_item_density(mgr)
    token_row = next(r for r in rows if "token.py" in r["file"])
    assert token_row["work_items"].count("PROJ-1") == 1


# ── find_work_items_by_function ───────────────────────────────────────────────

def test_find_by_function_no_graph_returns_empty():
    from icx_engine.memory.bridge import find_work_items_by_function
    mgr = _mgr_with([])
    result = find_work_items_by_function("auth_service.validate_token", "/nonexistent/project", mgr)
    assert result == []


def test_find_by_function_graph_exception_returns_empty():
    from icx_engine.memory.bridge import find_work_items_by_function
    mgr = _mgr_with([])
    with patch("icx_engine.graph.storage.derive_project_id", side_effect=RuntimeError("boom")):
        result = find_work_items_by_function("fn", "/some/path", mgr)
    assert result == []
