"""Tests for the runner-install manager (ICX brings its own test tooling; user-approved, reuse)."""
from pathlib import Path

import icx_engine.testing.runners.install as inst
from icx_engine.testing.runners.install import RUNNER_SPECS, ensure_runner, is_installed


def _fake_root(tmp_path, monkeypatch):
    monkeypatch.setattr(inst, "_install_root", lambda: tmp_path)


def test_specs_pinned_no_latest():
    for name, spec in RUNNER_SPECS.items():
        assert spec.version and spec.version != "latest"


def test_reuse_when_already_installed(tmp_path, monkeypatch):
    _fake_root(tmp_path, monkeypatch)
    spec = RUNNER_SPECS["schemathesis"]
    home = tmp_path / "schemathesis" / spec.version
    home.mkdir(parents=True)
    (home / "marker").write_text("x", encoding="utf-8")
    assert is_installed("schemathesis")
    called = {"n": 0}
    monkeypatch.setattr(inst, "_do_install", lambda *a, **k: called.__setitem__("n", called["n"] + 1) or True)
    path = ensure_runner("schemathesis", approve=lambda n: True)
    assert path == str(home)
    assert called["n"] == 0  # reused, no install


def test_missing_not_approved_returns_none(tmp_path, monkeypatch):
    _fake_root(tmp_path, monkeypatch)
    monkeypatch.delenv("ICX_AUTO_INSTALL_RUNNERS", raising=False)
    monkeypatch.setattr(inst, "_do_install", lambda *a, **k: True)
    assert ensure_runner("schemathesis") is None            # no approve, no env -> not installed
    assert ensure_runner("schemathesis", approve=lambda n: False) is None


def test_missing_approved_installs(tmp_path, monkeypatch):
    _fake_root(tmp_path, monkeypatch)
    def _fake_install(spec, dest):
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "installed").write_text("x", encoding="utf-8")
        return True
    monkeypatch.setattr(inst, "_do_install", _fake_install)
    path = ensure_runner("mutmut", approve=lambda n: True)
    assert path is not None and Path(path).exists()
    assert is_installed("mutmut")


def test_env_auto_install(tmp_path, monkeypatch):
    _fake_root(tmp_path, monkeypatch)
    monkeypatch.setenv("ICX_AUTO_INSTALL_RUNNERS", "1")
    monkeypatch.setattr(inst, "_do_install", lambda spec, dest: (dest.mkdir(parents=True, exist_ok=True), (dest / "m").write_text("x", encoding="utf-8"), True)[-1])
    assert ensure_runner("gotestsum") is not None


def test_install_failure_returns_none(tmp_path, monkeypatch):
    _fake_root(tmp_path, monkeypatch)
    monkeypatch.setattr(inst, "_do_install", lambda *a, **k: False)
    assert ensure_runner("stryker", approve=lambda n: True) is None


def test_unknown_runner_returns_none(tmp_path, monkeypatch):
    _fake_root(tmp_path, monkeypatch)
    assert ensure_runner("nope", approve=lambda n: True) is None


def test_binary_fail_closed_on_required_checksum(tmp_path, monkeypatch):
    _fake_root(tmp_path, monkeypatch)
    monkeypatch.setenv("ICX_REQUIRE_RUNNER_CHECKSUM", "1")
    monkeypatch.setattr(inst, "_do_install", lambda *a, **k: True)
    # hurl spec has no checksum -> required-checksum env forces None
    assert ensure_runner("hurl", approve=lambda n: True) is None
