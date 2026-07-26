"""Known-screen cache for the testing fast path - lets a re-run of the SAME screen skip the
expensive expand -> census -> compat pipeline when nothing relevant has changed. Mirrors auth.py's
storage pattern exactly (dataclass + single JSON file, 0o700/0o600). Pure I/O module; never raises -
callers treat any read/parse failure as a cache miss.

Freshness is deliberately conservative (see freshness()): a cached run is reusable ONLY when every
cached confirmed file's content is byte-identical to what was cached AND none of them went missing.
Detecting a genuinely NEW related file (one that didn't exist/wasn't discoverable last time) is the
caller's job (node_known_screen_check re-runs ICX's own deterministic discovery and compares against
`all_candidates` here) - this module only ever answers "did the files I was given change or disappear."
"""
from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class ScreenCacheEntry:
    project: str
    seed_hash: str
    cached_at: str
    test_type: str
    url: str | None
    all_candidates: list[str]             # full pre-exclusion candidate set at cache time
    confirmed_files: list[str]             # the user-approved subset actually analyzed
    file_hashes: dict[str, str]            # confirmed_files path -> sha256 of its content at cache time
    screen_model: dict | None
    census_coverage: float
    analyzer_id: str | None
    analyzer_family: str | None
    compat_resolution: dict[str, str] = field(default_factory=dict)


def store_path() -> Path:
    return Path.home() / ".icx" / "testing_screens.json"


def seed_key(seeds: list[str]) -> str:
    """Stable key from a seed file set - order-independent (sorted before hashing)."""
    joined = "\n".join(sorted(str(s) for s in (seeds or [])))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


def _key(project: str, seed_hash: str) -> str:
    return f"{project}::{seed_hash}"


def _resolve(store: Path | None) -> Path:
    return store if store is not None else store_path()


def _read(store: Path) -> dict:
    if not store.exists():
        return {}
    try:
        return json.loads(store.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write(store: Path, data: dict) -> None:
    store.parent.mkdir(parents=True, exist_ok=True,
                       **({"mode": 0o700} if sys.platform != "win32" else {}))
    store.write_text(json.dumps(data, indent=2), encoding="utf-8")
    if sys.platform != "win32":
        store.chmod(0o600)


def hash_file(path: str) -> str | None:
    """sha256 of a file's content; None if it cannot be read (missing, permission, not a file)."""
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def save_screen(
    project: str, seeds: list[str], *,
    test_type: str, url: str | None,
    all_candidates: list[str], confirmed_files: list[str],
    screen_model: dict | None, census_coverage: float,
    analyzer_id: str | None, analyzer_family: str | None,
    compat_resolution: dict[str, str] | None = None,
    store: Path | None = None,
) -> ScreenCacheEntry:
    path = _resolve(store)
    file_hashes = {p: h for p, h in ((p, hash_file(p)) for p in confirmed_files) if h is not None}
    entry = ScreenCacheEntry(
        project=project,
        seed_hash=seed_key(seeds),
        cached_at=datetime.now(timezone.utc).isoformat(),
        test_type=test_type,
        url=url,
        all_candidates=list(all_candidates or confirmed_files),
        confirmed_files=list(confirmed_files),
        file_hashes=file_hashes,
        screen_model=screen_model,
        census_coverage=float(census_coverage or 0.0),
        analyzer_id=analyzer_id,
        analyzer_family=analyzer_family,
        compat_resolution=dict(compat_resolution or {}),
    )
    data = _read(path)
    data[_key(project, entry.seed_hash)] = entry.__dict__
    _write(path, data)
    return entry


def load_screen(project: str, seeds: list[str], store: Path | None = None) -> ScreenCacheEntry | None:
    path = _resolve(store)
    data = _read(path)
    raw = data.get(_key(project, seed_key(seeds)))
    if not isinstance(raw, dict):
        return None
    try:
        return ScreenCacheEntry(
            project=raw["project"], seed_hash=raw["seed_hash"], cached_at=raw.get("cached_at", ""),
            test_type=raw.get("test_type", ""), url=raw.get("url"),
            all_candidates=list(raw.get("all_candidates") or []),
            confirmed_files=list(raw.get("confirmed_files") or []),
            file_hashes=dict(raw.get("file_hashes") or {}),
            screen_model=raw.get("screen_model"), census_coverage=float(raw.get("census_coverage") or 0.0),
            analyzer_id=raw.get("analyzer_id"), analyzer_family=raw.get("analyzer_family"),
            compat_resolution=dict(raw.get("compat_resolution") or {}),
        )
    except (KeyError, TypeError, ValueError):
        return None


def clear_screen(project: str, seeds: list[str], store: Path | None = None) -> None:
    path = _resolve(store)
    data = _read(path)
    if data.pop(_key(project, seed_key(seeds)), None) is not None:
        _write(path, data)


def freshness(entry: ScreenCacheEntry) -> tuple[bool, list[str]]:
    """(is_fresh, changed_paths). Fresh only when EVERY confirmed_files path still exists with an
    identical content hash - a changed or missing file is never silently tolerated."""
    changed = []
    for p, cached_hash in entry.file_hashes.items():
        if hash_file(p) != cached_hash:
            changed.append(p)
    # a confirmed file with no recorded hash (write-time read failure) can never be verified fresh
    unverifiable = [p for p in entry.confirmed_files if p not in entry.file_hashes]
    changed = sorted(set(changed) | set(unverifiable))
    return (not changed, changed)
