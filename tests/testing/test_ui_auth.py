"""Session capture/inline (Playwright) + storageState auth store."""
from __future__ import annotations

from pathlib import Path

import icx_engine.testing.ui_auth as uiauth
from icx_engine.testing import auth as _auth


# -- auth store: storageState path round-trips + sanitization -----------------------

def test_auth_stores_and_loads_storage_state(tmp_path):
    store = tmp_path / "a.json"
    _auth.save_session("proj", "host", "sess", store=store, storage_state="/x/state.json")
    rec = _auth.load_session("proj", "host", store=store)
    assert rec is not None and rec.storage_state == "/x/state.json"


def test_auth_safe_sanitizes():
    assert _auth._safe(r"a/b\c ok!") == "a_b_c_ok_"
    assert _auth._safe("") == "unknown"


def test_hostname_of_strips_port():
    assert _auth.hostname_of("http://localhost:3001/app") == "localhost"
    assert _auth.hostname_of("http://example.com/app") == "example.com"


# -- list_sessions_for_project: dev-server port-drift discovery ---------------------

def test_list_sessions_for_project_finds_multiple_hosts(tmp_path):
    store = tmp_path / "a.json"
    _auth.save_session("proj1", "localhost:3000", "s1", store=store, storage_state="/x/1.json")
    _auth.save_session("proj1", "localhost:3001", "s2", store=store, storage_state="/x/2.json")
    _auth.save_session("proj2", "localhost:3000", "other", store=store, storage_state="/x/3.json")
    hosts = {h for h, _ in _auth.list_sessions_for_project("proj1", store=store)}
    assert hosts == {"localhost:3000", "localhost:3001"}


def test_list_sessions_for_project_excludes_expired(tmp_path):
    store = tmp_path / "a.json"
    _auth.save_session("proj1", "localhost:3000", "s1", store=store, ttl_seconds=-10)  # already expired
    assert _auth.list_sessions_for_project("proj1", store=store) == []


def test_list_sessions_for_project_newest_first(tmp_path):
    import time
    store = tmp_path / "a.json"
    _auth.save_session("proj1", "localhost:3000", "s1", store=store)
    time.sleep(0.01)
    _auth.save_session("proj1", "localhost:3001", "s2", store=store)
    hosts = [h for h, _ in _auth.list_sessions_for_project("proj1", store=store)]
    assert hosts == ["localhost:3001", "localhost:3000"]


# -- capture: manual browser login, no creds in chat --------------------------------

async def test_capture_session_saves_on_success(monkeypatch, tmp_path):
    out = tmp_path / "s.json"
    monkeypatch.setattr(_auth, "session_state_path", lambda p, h: out)
    saved = {}
    monkeypatch.setattr(_auth, "save_session", lambda *a, **k: saved.update(a=a, k=k))

    async def _ok(mode, url, o, extra, timeout):
        Path(o).write_text("{}", encoding="utf-8")
        return True, ""
    monkeypatch.setattr(uiauth, "_run_harness", _ok)

    p, detail = await uiauth.capture_session("proj", "host", "http://x/login")
    assert p == str(out) and detail == ""
    assert saved["k"]["storage_state"] == str(out)


async def test_capture_session_none_on_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(_auth, "session_state_path", lambda p, h: tmp_path / "s.json")

    async def _fail(mode, url, o, extra, timeout):
        return False, "chromium not found"
    monkeypatch.setattr(uiauth, "_run_harness", _fail)
    p, detail = await uiauth.capture_session("p", "h", "http://x")
    assert p is None and "chromium not found" in detail


# -- inline: app credentials go to the process, never chat --------------------------

async def test_inline_passes_credentials_and_success_url(monkeypatch, tmp_path):
    monkeypatch.setattr(_auth, "session_state_path", lambda p, h: tmp_path / "s.json")
    monkeypatch.setattr(_auth, "save_session", lambda *a, **k: None)
    seen = {}

    async def _cap(mode, url, o, extra, timeout):
        seen["mode"] = mode
        seen["extra"] = extra
        Path(o).write_text("{}", encoding="utf-8")
        return True, ""
    monkeypatch.setattr(uiauth, "_run_harness", _cap)

    await uiauth.inline_session("p", "h", "http://x/login", "admin", "pw",
                                success_url="http://x/home")
    e = seen["extra"]
    assert seen["mode"] == "inline"
    assert "--user" in e and "admin" in e
    assert "--pass" in e and "pw" in e
    assert "--success-url" in e and "http://x/home" in e


# -- packaged harness asset ---------------------------------------------------------

def test_auth_harness_asset_exists_and_ascii():
    from icx_engine.testing.runners.install import auth_harness_path
    p = Path(auth_harness_path())
    assert p.exists() and p.name == "icx-auth.mjs"
    txt = p.read_text(encoding="utf-8")
    assert "storageState" in txt and "capture" in txt and "inline" in txt
    assert all(ord(c) < 128 for c in txt)


# -- harness env points Playwright at the ICX browser cache -------------------------

def test_harness_env_sets_browsers_path(monkeypatch, tmp_path):
    monkeypatch.setattr("icx_engine.testing.runners.install.installed_path",
                        lambda name: str(tmp_path / "pw"))
    node, env = uiauth._harness_env()
    assert "PLAYWRIGHT_BROWSERS_PATH" in env
    assert env["PLAYWRIGHT_BROWSERS_PATH"].endswith("browsers")
    assert "NODE_PATH" in env and env["NODE_PATH"].endswith("node_modules")
