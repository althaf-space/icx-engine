from pathlib import Path

import icx_engine.testing.runners.install as _install

# anchor to the packaged harness path (cwd-independent - other tests may chdir).
_HARNESS = Path(_install.harness_path())


def test_harness_has_raw_playwright_engine_branch():
    src = _HARNESS.read_text(encoding="utf-8")
    # reads the new env vars
    assert "ICX_UI_ENGINE" in src and "ICX_UI_DEVICE" in src
    # imports the three engines + device descriptors from playwright
    assert "firefox" in src and "webkit" in src and "devices" in src
    # still constructs Stagehand for the default path (backward compatible)
    assert "new Stagehand(" in src
    # ASCII only
    assert all(ord(c) < 128 for c in src)


def test_harness_default_path_unchanged_marker():
    src = _HARNESS.read_text(encoding="utf-8")
    # the raw path is guarded so the default (no engine/device) still uses Stagehand's page
    assert "stagehand.page" in src
