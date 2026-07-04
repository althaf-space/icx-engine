from pathlib import Path
from datetime import datetime, timezone, timedelta
from icx_engine.testing import auth


def test_host_of():
    assert auth.host_of("http://host-x:3000/app/#/users") == "host-x:3000"
    assert auth.host_of("https://a.example/path") == "a.example"


def test_save_and_load_roundtrip(tmp_path: Path):
    store = tmp_path / "testing_auth.json"
    rec = auth.save_session("proj-a", "host-x", "sess-1", store=store, ttl_seconds=3600)
    assert rec.session_id == "sess-1"
    got = auth.load_session("proj-a", "host-x", store=store)
    assert got is not None and got.session_id == "sess-1"


def test_two_projects_do_not_collide(tmp_path: Path):
    store = tmp_path / "testing_auth.json"
    auth.save_session("proj-a", "host-x", "sess-a", store=store)
    auth.save_session("proj-b", "host-x", "sess-b", store=store)
    assert auth.load_session("proj-a", "host-x", store=store).session_id == "sess-a"
    assert auth.load_session("proj-b", "host-x", store=store).session_id == "sess-b"


def test_expired_session_returns_none(tmp_path: Path):
    store = tmp_path / "testing_auth.json"
    auth.save_session("proj-a", "host-x", "old", store=store, ttl_seconds=3600)
    import json
    data = json.loads(store.read_text(encoding="utf-8"))
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    data["proj-a::host-x"]["expires_at"] = past
    store.write_text(json.dumps(data), encoding="utf-8")
    assert auth.load_session("proj-a", "host-x", store=store) is None


def test_clear_session(tmp_path: Path):
    store = tmp_path / "testing_auth.json"
    auth.save_session("proj-a", "host-x", "sess", store=store)
    auth.clear_session("proj-a", "host-x", store=store)
    assert auth.load_session("proj-a", "host-x", store=store) is None
