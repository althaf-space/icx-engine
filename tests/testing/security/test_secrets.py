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
