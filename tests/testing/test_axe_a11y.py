from pathlib import Path
import icx_engine.testing.runners.install as _install

_HARNESS = Path(_install.harness_path())


def test_harness_has_axe_a11y_path():
    src = _HARNESS.read_text(encoding="utf-8")
    assert "axe-core" in src or "window.axe" in src
    assert "ICX_A11Y_ENGINE" in src
    assert "runA11yAudit" in src                 # builtin fallback preserved
    assert "wcag2aa" in src or "wcag21aa" in src
    assert "impact" in src.lower()
    assert all(ord(c) < 128 for c in src)
