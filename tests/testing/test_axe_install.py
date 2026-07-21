import icx_engine.testing.runners.install as I


def test_ui_bundle_installs_axe_core(monkeypatch, tmp_path):
    seen = {}
    def _fake_run(cmd, **kw):
        seen.setdefault("cmds", []).append(list(cmd))
        class R:
            returncode = 0
            stdout = ""
            stderr = ""
        (tmp_path / "node_modules").mkdir(exist_ok=True)
        return R()
    monkeypatch.setattr(I.subprocess, "run", _fake_run)
    monkeypatch.setattr(I, "_node_path_env", lambda node_dir: {})
    monkeypatch.setattr(I, "_npm_for", lambda node_dir: "npm")
    monkeypatch.setattr(I, "browsers_dir", lambda dest: tmp_path / "browsers")
    spec = I.RUNNER_SPECS["stagehand"]
    I._install_ui_bundle(spec, tmp_path, None)
    npm_cmd = next((c for c in seen["cmds"] if "install" in c and any("stagehand" in str(x) for x in c)), None)
    assert npm_cmd is not None and "axe-core@4.10.0" in " ".join(npm_cmd)
