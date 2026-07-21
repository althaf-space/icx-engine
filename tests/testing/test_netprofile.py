from pathlib import Path
import icx_engine.testing.runners.install as _install

_HARNESS = Path(_install.harness_path())


def test_harness_has_netprofile_action():
    src = _HARNESS.read_text(encoding="utf-8")
    assert 'step.action === "netprofile"' in src
    assert "ICX_NET_SLOW_MS" in src
    assert "setOffline" in src            # offline profile reuses the existing mechanism
    assert all(ord(c) < 128 for c in src)
