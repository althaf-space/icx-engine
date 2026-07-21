from pathlib import Path
import icx_engine.testing.runners.install as _install

_HARNESS = Path(_install.harness_path())


def test_harness_has_screenshot_action():
    src = _HARNESS.read_text(encoding="utf-8")
    assert 'step.action === "screenshot"' in src
    assert "ICX_UI_VISUAL" in src
    assert "pixelmatch" in src and "pngjs" in src
    assert "baseline" in src.lower()
    assert all(ord(c) < 128 for c in src)
