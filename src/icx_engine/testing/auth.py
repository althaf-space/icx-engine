from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

SESSION_TTL_SECONDS = 3600


@dataclass
class AuthRecord:
    session_id: str
    captured_at: str
    expires_at: str


def host_of(url: str) -> str:
    return urlparse(url).netloc


def store_path() -> Path:
    return Path.home() / ".icx" / "testing_auth.json"


def _key(project: str, host: str) -> str:
    return f"{project}::{host}"


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


def save_session(project: str, host: str, session_id: str,
                 store: Path | None = None, ttl_seconds: int = SESSION_TTL_SECONDS) -> AuthRecord:
    path = _resolve(store)
    now = datetime.now(timezone.utc)
    rec = AuthRecord(
        session_id=session_id,
        captured_at=now.isoformat(),
        expires_at=(now + timedelta(seconds=ttl_seconds)).isoformat(),
    )
    data = _read(path)
    data[_key(project, host)] = {
        "session_id": rec.session_id,
        "captured_at": rec.captured_at,
        "expires_at": rec.expires_at,
    }
    _write(path, data)
    return rec


def load_session(project: str, host: str, store: Path | None = None) -> AuthRecord | None:
    path = _resolve(store)
    data = _read(path)
    entry = data.get(_key(project, host))
    if not entry:
        return None
    try:
        expires = datetime.fromisoformat(entry["expires_at"])
    except (KeyError, ValueError):
        return None
    if datetime.now(timezone.utc) >= expires:
        return None
    return AuthRecord(entry["session_id"], entry.get("captured_at", ""), entry["expires_at"])


def clear_session(project: str, host: str, store: Path | None = None) -> None:
    path = _resolve(store)
    data = _read(path)
    if data.pop(_key(project, host), None) is not None:
        _write(path, data)
