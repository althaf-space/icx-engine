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


async def test_capture_session_restricts_file_perms(monkeypatch, tmp_path):
    import sys
    out = tmp_path / "s.json"
    monkeypatch.setattr(_auth, "session_state_path", lambda p, h: out)
    monkeypatch.setattr(_auth, "save_session", lambda *a, **k: None)

    async def _ok(mode, url, o, extra, timeout):
        Path(o).write_text("{}", encoding="utf-8")
        Path(f"{o}.session").write_text("{}", encoding="utf-8")
        return True, ""
    monkeypatch.setattr(uiauth, "_run_harness", _ok)

    await uiauth.capture_session("proj", "host", "http://x/login")
    if sys.platform != "win32":
        import stat
        assert stat.S_IMODE(out.stat().st_mode) == 0o600
        assert stat.S_IMODE((tmp_path / "s.json.session").stat().st_mode) == 0o600


async def test_restrict_session_files_never_raises_when_companion_missing(tmp_path):
    # Companion .session file absent (e.g. snapSession() failed harness-side) must not break capture.
    out = tmp_path / "no-companion.json"
    out.write_text("{}", encoding="utf-8")
    uiauth._restrict_session_files(str(out))


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

    async def _cap(mode, url, o, extra, timeout, extra_env=None):
        seen["mode"] = mode
        seen["extra"] = extra
        seen["extra_env"] = extra_env
        Path(o).write_text("{}", encoding="utf-8")
        return True, ""
    monkeypatch.setattr(uiauth, "_run_harness", _cap)

    await uiauth.inline_session("p", "h", "http://x/login", "admin", "pw",
                                success_url="http://x/home")
    e = seen["extra"]
    assert seen["mode"] == "inline"
    assert "--user" not in e and "admin" not in e
    assert "--pass" not in e and "pw" not in e
    assert seen["extra_env"] == {"ICX_AUTH_USER": "admin", "ICX_AUTH_PASS": "pw"}
    assert "--success-url" in e and "http://x/home" in e


async def test_inline_session_password_never_in_argv_but_in_env(monkeypatch, tmp_path):
    """Password must travel to the harness subprocess via env, never via argv - argv is
    visible to other local users through process listing for the process's lifetime."""
    monkeypatch.setattr(_auth, "session_state_path", lambda p, h: tmp_path / "s.json")
    monkeypatch.setattr(_auth, "save_session", lambda *a, **k: None)
    monkeypatch.setattr(uiauth, "_harness_env", lambda: ("node", {"PATH": "/x"}))
    monkeypatch.setattr("icx_engine.testing.runners.install.auth_harness_path",
                        lambda: str(tmp_path / "icx-auth.mjs"))
    monkeypatch.setattr("icx_engine.testing.runners.install.runtime_harness_path",
                        lambda name, path: path)
    monkeypatch.setattr("icx_engine._proc.win_argv", lambda cmd: cmd)
    captured = {}

    class _FakeProc:
        pid = 999
        returncode = 0
        async def communicate(self):
            return b"", b""

    async def _fake_exec(*args, **kwargs):
        captured["cmd"] = list(args)
        captured["env"] = kwargs.get("env")
        return _FakeProc()
    monkeypatch.setattr(uiauth.asyncio, "create_subprocess_exec", _fake_exec)
    monkeypatch.setattr(uiauth.Path, "exists", lambda self: True)

    secret_password = "s3cr3t-pw"
    await uiauth.inline_session("p", "h", "http://x/login", "admin", secret_password)

    cmd = captured["cmd"]
    env = captured["env"]
    assert secret_password not in cmd
    assert not any(secret_password in str(part) for part in cmd)
    assert env is not None and env.get("ICX_AUTH_PASS") == secret_password
    assert env.get("ICX_AUTH_USER") == "admin"


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
