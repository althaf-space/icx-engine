import icx_engine.testing.runners.install as I


def test_ensure_browser_already_present(monkeypatch, tmp_path):
    (tmp_path / "firefox-1450").mkdir()
    monkeypatch.setattr(I, "browsers_dir", lambda dest=None: tmp_path)
    monkeypatch.setattr(I, "installed_path", lambda name: str(tmp_path))
    assert I.ensure_browser("firefox") is True          # present -> no install attempted


def test_ensure_browser_installs_when_missing_and_approved(monkeypatch, tmp_path):
    monkeypatch.setattr(I, "browsers_dir", lambda dest=None: tmp_path)
    monkeypatch.setattr(I, "installed_path", lambda name: str(tmp_path))
    # Hermetic: ensure_browser calls the REAL _harness_node_dir() -> runtime_manager.resolve_harness_node()
    # (not otherwise mocked here), which on some environments discovers a real node candidate and
    # calls subprocess.run itself to validate it - hitting the SAME globally-patched subprocess.run
    # below (I.subprocess IS the shared stdlib module, not a copy). Stub it out directly so this test
    # only ever exercises ensure_browser's own single subprocess call, regardless of what node
    # candidates happen to exist on the machine running the suite.
    monkeypatch.setattr(I, "_harness_node_dir", lambda: None)
    calls = {}
    def _fake_run(cmd, **kw):
        calls["cmd"] = cmd
        (tmp_path / "webkit-2050").mkdir(exist_ok=True)  # simulate a successful install; idempotent
                                                          # in case something else also calls this fake
        class R:
            returncode = 0
            stdout = ""
            stderr = ""
        return R()
    monkeypatch.setattr(I.subprocess, "run", _fake_run)
    ok = I.ensure_browser("webkit", approve=lambda name: True)
    assert ok is True
    assert "install" in calls["cmd"] and "webkit" in calls["cmd"]


def test_ensure_browser_not_approved_returns_false(monkeypatch, tmp_path):
    monkeypatch.setattr(I, "browsers_dir", lambda dest=None: tmp_path)
    monkeypatch.setattr(I, "installed_path", lambda name: str(tmp_path))
    assert I.ensure_browser("firefox", approve=lambda name: False) is False


def test_ensure_browser_rejects_unknown_engine(monkeypatch, tmp_path):
    assert I.ensure_browser("notabrowser") is False


def test_ensure_browser_mkdir_failure_returns_false(monkeypatch, tmp_path):
    monkeypatch.setattr(I, "browsers_dir", lambda dest=None: tmp_path / "browsers")
    monkeypatch.setattr(I, "installed_path", lambda name: str(tmp_path))
    monkeypatch.setattr(I, "_harness_node_dir", lambda: None)  # hermetic - see the test above

    def _boom(self, parents=True, exist_ok=True):
        raise OSError("disk full")
    monkeypatch.setattr(I.Path, "mkdir", _boom)

    def _fake_run(cmd, **kw):
        raise AssertionError("mkdir failure must short-circuit before subprocess.run")
    monkeypatch.setattr(I.subprocess, "run", _fake_run)

    assert I.ensure_browser("firefox", approve=lambda name: True) is False  # never raises
