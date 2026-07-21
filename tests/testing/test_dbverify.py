from pathlib import Path
import icx_engine.testing.runners.install as _install

_HARNESS = Path(_install.harness_path())


def test_harness_has_dbverify_action():
    src = _HARNESS.read_text(encoding="utf-8")
    assert 'step.action === "dbverify"' in src
    assert "ICX_SQL_VERIFY_CMD" in src
    assert "ICX_DB_VALUE" in src          # value passed via env, not interpolated
    assert "spawnSync" in src
    assert all(ord(c) < 128 for c in src)
