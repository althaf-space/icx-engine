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
    def _fake_install(spec, dest, node_dir=None):
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
    monkeypatch.setattr(inst, "_do_install", lambda spec, dest, node_dir=None: (dest.mkdir(parents=True, exist_ok=True), (dest / "m").write_text("x", encoding="utf-8"), True)[-1])
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


def test_go_install_points_gobin_at_dest(tmp_path, monkeypatch):
    # `go install` writes to GOBIN; _do_install must set GOBIN=dest so the pinned home is non-empty
    # afterward (otherwise is_installed stays False and we reinstall on every run).
    monkeypatch.setattr("icx_engine._proc.win_argv", lambda c: c)   # test the raw argv, not OS wrapping
    captured = {}
    def _fake_run(cmd, capture_output=True, text=True, timeout=600, env=None):
        captured["cmd"] = cmd
        captured["env"] = env
        class _R: returncode = 0
        return _R()
    monkeypatch.setattr(inst.subprocess, "run", _fake_run)
    dest = tmp_path / "gotestsum" / RUNNER_SPECS["gotestsum"].version
    ok = inst._do_install(RUNNER_SPECS["gotestsum"], dest)
    assert ok is True
    assert captured["cmd"][:2] == ["go", "install"]
    assert captured["env"]["GOBIN"] == str(dest)


# -- UI bundle: playwright + chromium, all under ~/.icx -----------------

def test_ui_bundle_installs_playwright_and_chromium(tmp_path, monkeypatch):
    monkeypatch.setattr("icx_engine._proc.win_argv", lambda c: c)   # test raw argv, not OS wrapping
    calls = []

    def _run(cmd, **k):
        calls.append(cmd)
        class _R:
            returncode = 0
        return _R()

    monkeypatch.setattr(inst.subprocess, "run", _run)
    dest = tmp_path / "playwright" / RUNNER_SPECS["playwright"].version
    ok = inst._do_install(RUNNER_SPECS["playwright"], dest)
    assert ok is True
    assert calls[0][:2] == ["npm", "install"]
    assert any(c == f"playwright@{RUNNER_SPECS['playwright'].version}" for c in calls[0])
    # @playwright/test is the actual test-runner package (playwright test CLI, --reporter=junit) -
    # without it the agent cannot run the command the gate tells it to run.
    assert any(c == f"@playwright/test@{RUNNER_SPECS['playwright'].version}" for c in calls[0])
    assert calls[1][-2:] == ["install", "chromium"]              # browser download
    assert (dest / "browsers").exists()                          # cached under the pinned home


def test_ui_bundle_fails_if_npm_fails(tmp_path, monkeypatch):
    def _run(cmd, **k):
        class _R:
            returncode = 1
        return _R()
    monkeypatch.setattr(inst.subprocess, "run", _run)
    dest = tmp_path / "playwright" / RUNNER_SPECS["playwright"].version
    assert inst._do_install(RUNNER_SPECS["playwright"], dest) is False


# -- binary install (hurl) cross-OS ------------------------------------------------

def test_hurl_asset_url_linux(monkeypatch):
    import platform
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(platform, "machine", lambda: "x86_64")
    url, kind, binname = inst._hurl_asset("5.0.1")
    assert url.endswith("hurl-5.0.1-x86_64-unknown-linux-gnu.tar.gz")
    assert kind == "tar" and binname == "hurl"


def test_hurl_asset_url_windows_arm(monkeypatch):
    import platform
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    monkeypatch.setattr(platform, "machine", lambda: "ARM64")
    url, kind, binname = inst._hurl_asset("5.0.1")
    assert url.endswith("hurl-5.0.1-aarch64-pc-windows-msvc.zip")
    assert kind == "zip" and binname == "hurl.exe"


def test_hurl_asset_url_macos(monkeypatch):
    import platform
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    monkeypatch.setattr(platform, "machine", lambda: "arm64")
    url, kind, binname = inst._hurl_asset("5.0.1")
    assert url.endswith("hurl-5.0.1-aarch64-apple-darwin.tar.gz")


def test_hurl_asset_unsupported_os(monkeypatch):
    import platform
    monkeypatch.setattr(platform, "system", lambda: "Plan9")
    assert inst._hurl_asset("5.0.1") is None


def test_install_binary_downloads_and_extracts_current_os(tmp_path, monkeypatch):
    # Runs the REAL extract path on whatever OS this is; only the download is mocked.
    import shutil, urllib.request
    info = inst._hurl_asset("5.0.1")
    assert info, "unsupported OS for this test"
    _url, kind, binname = info

    inner = tmp_path / "src"
    inner.mkdir()
    (inner / binname).write_text("#!/bin/sh\n", encoding="utf-8")
    if kind == "zip":
        import zipfile
        archive = tmp_path / "rel.zip"
        with zipfile.ZipFile(archive, "w") as z:
            z.write(inner / binname, f"hurl-5.0.1/bin/{binname}")
    else:
        import tarfile
        archive = tmp_path / "rel.tar.gz"
        with tarfile.open(archive, "w:gz") as t:
            t.add(inner / binname, arcname=f"hurl-5.0.1/bin/{binname}")

    monkeypatch.setattr(urllib.request, "urlretrieve", lambda u, o: shutil.copy(archive, o))
    dest = tmp_path / "dest"
    ok = inst._install_binary(RUNNER_SPECS["hurl"], dest)
    assert ok is True
    assert (dest / binname).exists()


def test_install_binary_checksum_mismatch_fails(tmp_path, monkeypatch):
    import shutil, urllib.request, tarfile
    info = inst._hurl_asset("5.0.1")
    _url, kind, binname = info
    inner = tmp_path / "src"; inner.mkdir()
    (inner / binname).write_text("x", encoding="utf-8")
    if kind == "zip":
        import zipfile
        archive = tmp_path / "rel.zip"
        with zipfile.ZipFile(archive, "w") as z:
            z.write(inner / binname, f"h/{binname}")
    else:
        archive = tmp_path / "rel.tar.gz"
        with tarfile.open(archive, "w:gz") as t:
            t.add(inner / binname, arcname=f"h/{binname}")
    monkeypatch.setattr(urllib.request, "urlretrieve", lambda u, o: shutil.copy(archive, o))
    from icx_engine.testing.runners.install import RunnerSpec
    bad = RunnerSpec("hurl", "binary", "hurl", "5.0.1", checksum="deadbeef")
    assert inst._install_binary(bad, tmp_path / "d") is False


def test_npm_for_finds_next_to_node(tmp_path):
    (tmp_path / "npm.cmd").write_text("", encoding="utf-8")
    assert inst._npm_for(str(tmp_path)).endswith("npm.cmd")


def test_npm_for_bin_subdir(tmp_path):
    b = tmp_path / "bin"; b.mkdir()
    (b / "npm").write_text("", encoding="utf-8")
    assert inst._npm_for(str(tmp_path)).endswith("npm")


def test_npm_for_fallback_to_path(tmp_path):
    assert inst._npm_for(str(tmp_path)) == "npm"    # empty dir -> bare npm
    assert inst._npm_for(None) == "npm"


def test_ui_bundle_uses_node_local_npm(tmp_path, monkeypatch):
    # npm must be taken from the chosen Node dir, not a bare 'npm'.
    monkeypatch.setattr("icx_engine._proc.win_argv", lambda c: c)
    nodedir = tmp_path / "nodedir"; nodedir.mkdir()
    (nodedir / "npm.cmd").write_text("", encoding="utf-8")
    calls = []
    def _run(cmd, **k):
        calls.append(cmd)
        class _R: returncode = 0
        return _R()
    monkeypatch.setattr(inst.subprocess, "run", _run)
    dest = tmp_path / "playwright" / RUNNER_SPECS["playwright"].version
    ok = inst._do_install(RUNNER_SPECS["playwright"], dest, node_dir=str(nodedir))
    assert ok is True
    assert calls[0][0].endswith("npm.cmd")          # node-local npm, not bare "npm"


# -- completeness: a partial install must NOT count as installed --------------------

def test_is_installed_ui_bundle_requires_full_install(tmp_path, monkeypatch):
    _fake_root(tmp_path, monkeypatch)
    home = tmp_path / "playwright" / RUNNER_SPECS["playwright"].version
    nm = home / "node_modules"
    nm.mkdir(parents=True)
    assert is_installed("playwright") is False                # npm ran but nothing else
    (nm / "playwright").mkdir(parents=True)
    assert is_installed("playwright") is False                # no @playwright/test yet
    (nm / "@playwright" / "test").mkdir(parents=True)
    assert is_installed("playwright") is False                 # no Chromium yet
    chrome = home / "browsers" / "chromium-1000"
    chrome.mkdir(parents=True)
    (chrome / "chrome").write_text("", encoding="utf-8")
    assert is_installed("playwright") is True                 # complete


def test_is_installed_binary_needs_the_file(tmp_path, monkeypatch):
    _fake_root(tmp_path, monkeypatch)
    home = tmp_path / "hurl" / RUNNER_SPECS["hurl"].version
    home.mkdir(parents=True)
    assert is_installed("hurl") is False                      # empty dir
    (home / "hurl.exe").write_text("", encoding="utf-8")
    assert is_installed("hurl") is True


def test_remove_runner_wipes_home(tmp_path, monkeypatch):
    from icx_engine.testing.runners.install import remove_runner
    _fake_root(tmp_path, monkeypatch)
    home = tmp_path / "hurl" / RUNNER_SPECS["hurl"].version
    home.mkdir(parents=True)
    (home / "hurl").write_text("", encoding="utf-8")
    remove_runner("hurl")
    assert not home.exists()


# -- ESM harness must run from inside the install dir (next to node_modules) --------

def test_runtime_harness_path_copies_into_install_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(inst, "installed_path", lambda name: str(tmp_path))
    packaged = inst.discover_harness_path()
    out = inst.runtime_harness_path("icx-discover.mjs", packaged)
    assert out == str(tmp_path / "icx-discover.mjs")
    assert (tmp_path / "icx-discover.mjs").is_file()          # copied next to node_modules


def test_runtime_harness_path_falls_back_when_not_installed(monkeypatch):
    monkeypatch.setattr(inst, "installed_path", lambda name: None)
    p = inst.runtime_harness_path("icx-auth.mjs", inst.auth_harness_path())
    assert p == inst.auth_harness_path()                    # packaged fallback


def test_ui_bundle_browser_uses_resolved_playwright_cli(tmp_path, monkeypatch):
    # Chromium must be installed via node node_modules/playwright/cli.js (the imported Playwright),
    # NOT the .bin shim - so the browser build matches the runtime version.
    monkeypatch.setattr("icx_engine._proc.win_argv", lambda c: c)
    dest = tmp_path / "playwright" / RUNNER_SPECS["playwright"].version
    cli = dest / "node_modules" / "playwright" / "cli.js"
    cli.parent.mkdir(parents=True)
    cli.write_text("//cli", encoding="utf-8")
    nodedir = tmp_path / "nodedir"; nodedir.mkdir()
    (nodedir / "node.exe").write_text("", encoding="utf-8")
    (nodedir / "npm.cmd").write_text("", encoding="utf-8")
    calls = []
    def _run(cmd, **k):
        calls.append(cmd)
        class _R: returncode = 0
        return _R()
    monkeypatch.setattr(inst.subprocess, "run", _run)
    ok = inst._do_install(RUNNER_SPECS["playwright"], dest, node_dir=str(nodedir))
    assert ok is True
    browser_cmd = calls[1]
    assert browser_cmd[0].endswith("node.exe")               # node runs it
    assert browser_cmd[1].endswith("cli.js")                 # the resolved playwright cli
    assert browser_cmd[-2:] == ["install", "chromium"]


def test_runtime_harness_path_refreshes_stale_copy(tmp_path, monkeypatch):
    # After an ICX upgrade, an existing install's old harness must be refreshed to the packaged one.
    monkeypatch.setattr(inst, "installed_path", lambda name: str(tmp_path))
    packaged = inst.discover_harness_path()
    stale = tmp_path / "icx-discover.mjs"
    stale.write_text("// OLD stale harness", encoding="utf-8")
    out = inst.runtime_harness_path("icx-discover.mjs", packaged)
    assert out == str(stale)
    assert stale.read_bytes() == Path(packaged).read_bytes()   # refreshed to current
