"""Native secrets scanner: each rule fires on a planted secret; clean/placeholder values stay clean."""
from __future__ import annotations

from icx_engine.testing.security.secrets import scan_secrets


def _rules(findings):
    return {f.rule for f in findings}


def test_detects_aws_and_private_key_and_stripe(tmp_path):
    (tmp_path / "c.py").write_text(
        'AWS = "AKIAIOSFODNN7EXAMPLE"\n'
        'KEY = "-----BEGIN RSA PRIVATE KEY-----"\n'
        'S = "sk_live_ABCDEFGHIJKLMNOPQRSTUVWX"\n', encoding="utf-8")
    rules = _rules(scan_secrets(tmp_path))
    assert "aws-access-key" in rules
    assert "private-key" in rules
    assert "stripe-key" in rules


def test_detects_hardcoded_credential_high_entropy(tmp_path):
    (tmp_path / "c.py").write_text('password = "S3cr3tP@ssw0rd123XZ"\n', encoding="utf-8")
    f = scan_secrets(tmp_path)
    assert any(x.rule == "hardcoded-credential" and x.severity == "high" for x in f)


def test_placeholder_and_env_indirection_not_flagged(tmp_path):
    (tmp_path / "c.py").write_text(
        'password = "changeme"\n'
        'secret = os.environ["X"]\n'
        'api_key = "your-key-here"\n'
        'token = "xxxxxxxx"\n', encoding="utf-8")
    assert not any(x.rule == "hardcoded-credential" for x in scan_secrets(tmp_path))


def test_snippet_is_masked_no_releak(tmp_path):
    (tmp_path / "c.py").write_text('S = "sk_live_ABCDEFGHIJKLMNOPQRSTUVWX"\n', encoding="utf-8")
    f = [x for x in scan_secrets(tmp_path) if x.rule == "stripe-key"][0]
    assert "ABCDEFGHIJKLMNOPQRSTUVWX" not in f.snippet
    assert "..." in f.snippet


def test_hardcoded_credential_value_not_releaked_in_snippet(tmp_path):
    secret = "S3cr3t@P#ssw0rd!123"
    (tmp_path / "c.py").write_text(f'password = "{secret}"\n', encoding="utf-8")
    f = [x for x in scan_secrets(tmp_path) if x.rule == "hardcoded-credential"][0]
    assert secret not in f.snippet


def test_clean_repo_no_findings(tmp_path):
    (tmp_path / "c.py").write_text("x = 1\ndef f(): return x\n", encoding="utf-8")
    assert scan_secrets(tmp_path) == []


def test_skips_vendored_dirs(tmp_path):
    nm = tmp_path / "node_modules" / "pkg"
    nm.mkdir(parents=True)
    (nm / "c.py").write_text('S = "sk_live_ABCDEFGHIJKLMNOPQRSTUVWX"\n', encoding="utf-8")
    assert scan_secrets(tmp_path) == []


def test_raised_threshold_excludes_ordinary_low_entropy_value(tmp_path):
    # entropy ~3.18 - would have fired under the old 3.0 threshold, correctly excluded now.
    (tmp_path / "c.py").write_text('password = "mysecretpass1"\n', encoding="utf-8")
    assert not any(x.rule == "hardcoded-credential" for x in scan_secrets(tmp_path))


def test_placeholder_substring_marker_excludes_fake_prefixed_value(tmp_path):
    # entropy 4.0, 16 chars - would otherwise fire; "fake" substring must exclude it even though
    # it isn't a pure exact match against the whole-value placeholder list.
    (tmp_path / "c.py").write_text('api_key = "fakeK9!zR2mXq7pL"\n', encoding="utf-8")
    assert not any(x.rule == "hardcoded-credential" for x in scan_secrets(tmp_path))


def test_git_sha_like_value_not_flagged(tmp_path):
    # 40-hex, entropy ~3.77 - would otherwise fire; a git SHA is a well-known non-secret format.
    (tmp_path / "c.py").write_text(
        'commit_secret = "a3f5c9e1b2d4f6a8c0e2b4d6f8a0c2e4b6d8f0a2"\n', encoding="utf-8")
    assert not any(x.rule == "hardcoded-credential" for x in scan_secrets(tmp_path))


def test_real_looking_secret_in_test_path_is_downgraded_not_dropped(tmp_path):
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_foo.py").write_text('password = "S3cr3tP@ssw0rd123XZ"\n', encoding="utf-8")
    f = [x for x in scan_secrets(tmp_path) if x.rule == "hardcoded-credential"]
    assert f and f[0].severity == "info"


def test_same_value_outside_test_path_stays_high_severity(tmp_path):
    (tmp_path / "config.py").write_text('password = "S3cr3tP@ssw0rd123XZ"\n', encoding="utf-8")
    f = [x for x in scan_secrets(tmp_path) if x.rule == "hardcoded-credential"]
    assert f and f[0].severity == "high"
