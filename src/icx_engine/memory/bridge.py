"""Work item-to-code bridge.

Cross-references MemoryEntry.files_changed with the codebase graph.
No imports from connectors/. No new dependencies.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from icx_engine.memory.manager import MemoryManager
    from icx_engine.memory.schema import MemoryEntry


def _norm(path: str) -> str:
    """Forward-slash, lowercase - enables cross-platform substring matching."""
    return path.replace("\\", "/").lower()


def find_work_items_by_file(
    file_path: str,
    manager: "MemoryManager",
    project_key: str | None = None,
) -> list["MemoryEntry"]:
    """Return all entries whose files_changed list contains file_path as a substring.

    Matching is case-insensitive and path-separator-agnostic (Windows/Unix).
    Pass project_key to restrict search to one project.
    """
    needle = _norm(file_path)
    entries = manager.list_entries(project_key=project_key)
    return [e for e in entries if any(needle in _norm(f) for f in e.files_changed)]


def get_work_item_density(
    manager: "MemoryManager",
    project_key: str | None = None,
    top_n: int = 20,
) -> list[dict]:
    """Count work items per file across all saved entries.

    Returns [{file, count, work_items: [issue_keys]}] sorted by count descending.
    File paths are normalised (forward-slash, lowercase) in output.
    """
    entries = manager.list_entries(project_key=project_key)
    counts: dict[str, list[str]] = {}
    for entry in entries:
        for f in entry.files_changed:
            key = _norm(f)
            if key not in counts:
                counts[key] = []
            if entry.issue_key not in counts[key]:
                counts[key].append(entry.issue_key)

    result = [
        {"file": f, "count": len(keys), "work_items": keys}
        for f, keys in counts.items()
    ]
    result.sort(key=lambda x: -x["count"])
    return result[:top_n]


def find_work_items_by_function(
    fqn: str,
    project_path: str,
    manager: "MemoryManager",
) -> list["MemoryEntry"]:
    """Return work items touching files that contain the named function/class.

    Uses the codebase graph to map fqn -> top 5 relevant files, then delegates
    to find_work_items_by_file for each. Returns [] when no graph is available.
    """
    try:
        from pathlib import Path as _Path
        from icx_engine.graph import storage as _st
        from icx_engine.graph.query import GraphQuerier

        _project_path = _Path(project_path)
        _project_id = _st.derive_project_id(_project_path)
        graph_json = _st.graph_path(_project_id)
        if not graph_json.exists():
            return []

        q = GraphQuerier(graph_json)
        results = q.find_context(task=fqn)
        top_files = [r.file for r in results[:5] if r.file]
    except Exception:
        return []

    found: list["MemoryEntry"] = []
    seen: set[str] = set()
    for fp in top_files:
        for entry in find_work_items_by_file(fp, manager):
            if entry.issue_key not in seen:
                found.append(entry)
                seen.add(entry.issue_key)
    return found
