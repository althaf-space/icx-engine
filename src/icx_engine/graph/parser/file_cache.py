"""
Per-file SHA-256 hash registry for incremental graph builds.
Stored at ~/.icx/graphs/<project_id>/file_hashes.json
Format: {"src/auth/token.py": "sha256hex", ...}
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


def load_hashes(cache_path: Path) -> dict[str, str]:
    try:
        return json.loads(cache_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_hashes(cache_path: Path, hashes: dict[str, str]) -> None:
    tmp = cache_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(hashes, separators=(",", ":")), encoding="utf-8")
    tmp.replace(cache_path)


def hash_file(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def compute_changed_files(
    project_path: str,
    all_files: list[str],
    stored_hashes: dict[str, str],
) -> tuple[list[str], list[str], dict[str, str]]:
    """
    Returns:
        changed_files: new or modified (hash differs from stored)
        deleted_files: in stored_hashes but not in all_files
        new_hashes: full updated hash dict for all current files
    """
    root = Path(project_path)
    current_hashes: dict[str, str] = {}
    changed_files: list[str] = []

    for rel_path in all_files:
        h = hash_file(root / rel_path)
        current_hashes[rel_path] = h
        if stored_hashes.get(rel_path) != h:
            changed_files.append(rel_path)

    deleted_files = list(set(stored_hashes.keys()) - set(current_hashes.keys()))
    return changed_files, deleted_files, current_hashes
