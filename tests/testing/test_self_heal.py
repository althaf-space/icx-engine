from pathlib import Path
import icx_engine.testing.runners.install as _install

_HARNESS = Path(_install.harness_path())


def test_harness_has_fingerprint_capture():
    src = _HARNESS.read_text(encoding="utf-8")
    assert "fingerprints.json" in src
    assert "captureFp" in src              # the real fingerprint-capture function name
    assert "ICX_UI_HEAL" in src            # guarded by the heal flag
    assert all(ord(c) < 128 for c in src)  # ASCII


def test_harness_has_heal_scorer_and_log():
    src = _HARNESS.read_text(encoding="utf-8")
    assert "heals.json" in src
    assert "HEAL:" in src                          # heal testcase label
    # weighted scoring markers (text/domPath weights from the contract)
    assert "0.30" in src and "0.20" in src
    assert all(ord(c) < 128 for c in src)


def test_harness_heal_capture_is_durable():
    src = _HARNESS.read_text(encoding="utf-8")
    # fingerprint capture keyed by origTarget (the flow's original selector), not the healed target
    assert "const origTarget = step.target;" in src
    assert "fpStore[origTarget]" in src
    # sidecar write merges prior fingerprints forward so unexercised selectors are not dropped
    assert "...priorFp, ...fpStore" in src
    assert all(ord(c) < 128 for c in src)
