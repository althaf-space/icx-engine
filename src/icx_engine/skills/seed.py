"""Seeds ICX's curated default skills (defaults.py) into the user's skills store, and safely
reconciles later updates to those defaults without ever overwriting a skill the user has edited.
Mirrors the guarded, never-fatal reconciliation pattern config_manager.clean_stale_artifacts()
already uses for other user-side state."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from icx_engine.skills.defaults import DEFAULT_SKILLS
from icx_engine.skills.schema import SkillEntry
from icx_engine.skills.storage import SkillStorage

_STATE_FILE_NAME = ".defaults_state.json"


def _state_path(storage: SkillStorage) -> Path:
    return storage.root / _STATE_FILE_NAME


def _read_state(path: Path) -> dict[str, str]:
    """Maps skill name -> hash of the version ICX last shipped. Missing or corrupt file means
    unknown provenance for every name in it - guarded, never fatal."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            return {k: v for k, v in raw.items() if isinstance(k, str) and isinstance(v, str)}
    except (OSError, ValueError):
        pass
    return {}


def _write_state(path: Path, state: dict[str, str]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except OSError:
        pass


def seed_default_skills(storage: SkillStorage | None = None) -> dict[str, list[str]]:
    """Write or update ICX's curated default skills in `storage` (~/.icx/skills/ by default).

    A default is only overwritten when the hash of what's ACTUALLY on disk right now
    (existing.compute_hash(), recomputed from the live body text - never the possibly-stale
    icx_hash field alone) matches the hash of the version ICX last shipped for that name. Any
    mismatch - a real user edit, or a name ICX has no shipped-hash record for at all - is left
    untouched. Guarded per skill; one failing definition never blocks the rest.

    Returns {"seeded": [...], "updated": [...], "skipped_customized": [...]}."""
    storage = storage or SkillStorage()
    state_path = _state_path(storage)
    state = _read_state(state_path)
    result: dict[str, list[str]] = {"seeded": [], "updated": [], "skipped_customized": []}
    now = datetime.now(timezone.utc).isoformat()

    for definition in DEFAULT_SKILLS:
        name = definition["name"]
        try:
            entry = SkillEntry(
                name=name,
                description=definition["description"],
                tags=list(definition.get("tags", [])),
                scope_hint=definition.get("scope_hint", "generic"),
                title=definition.get("title", name),
                when_to_use=definition.get("when_to_use", ""),
                procedure=definition.get("procedure", ""),
                pitfalls=definition.get("pitfalls", ""),
                verification=definition.get("verification", ""),
                created_at=now,
                updated_at=now,
            )
            new_hash = entry.compute_hash()
            existing = storage.read(name)

            if existing is None:
                entry.icx_hash = new_hash
                storage.write(entry)
                result["seeded"].append(name)
            else:
                last_shipped = state.get(name)
                existing_actual_hash = existing.compute_hash()
                if last_shipped is not None and existing_actual_hash == last_shipped:
                    if new_hash != existing_actual_hash:
                        entry.icx_hash = new_hash
                        entry.created_at = existing.created_at or now
                        storage.write(entry)
                        result["updated"].append(name)
                else:
                    result["skipped_customized"].append(name)

            state[name] = new_hash
        except Exception:
            continue

    _write_state(state_path, state)
    return result
