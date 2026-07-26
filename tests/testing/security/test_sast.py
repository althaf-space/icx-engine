"""Native SAST-lite: python AST rules + cross-language regex sink rules. Vuln fires; safe stays clean."""
from __future__ import annotations

from icx_engine.testing.security.sast import scan_sast


def _rules(tmp_path):
    return {f.rule for f in scan_sast(tmp_path)}


def test_python_eval_exec_critical(tmp_path):
    (tmp_path / "a.py").write_text("def f(u):\n    return eval(u)\n\ndef g(u):\n    exec(u)\n",
                                   encoding="utf-8")
    f = scan_sast(tmp_path)
    rules = {x.rule for x in f}
    assert "py-eval" in rules and "py-exec" in rules
    assert all(x.severity == "critical" for x in f if x.rule in ("py-eval", "py-exec"))


def test_python_shell_true_and_tls_off(tmp_path):
    (tmp_path / "a.py").write_text(
        "import subprocess, requests\n"
        "def f(c):\n    subprocess.run(c, shell=True)\n"
        "def g(u):\n    requests.get(u, verify=False)\n", encoding="utf-8")
    rules = _rules(tmp_path)
    assert "py-shell-true" in rules
    assert "py-tls-verify-off" in rules


def test_python_weak_hash_and_pickle_and_debug(tmp_path):
    (tmp_path / "a.py").write_text(
        "import hashlib, pickle\n"
        "DEBUG = True\n"
        "def h(): return hashlib.md5(b'x')\n"
        "def p(d): return pickle.loads(d)\n", encoding="utf-8")
    rules = _rules(tmp_path)
    assert "py-weak-hash" in rules
    assert "py-pickle" in rules
    assert "py-debug-true" in rules


def test_python_safe_code_clean(tmp_path):
    (tmp_path / "a.py").write_text(
        "import subprocess, hashlib\n"
        "DEBUG = False\n"
        "def f(c):\n    subprocess.run(['ls', c])\n"
        "def h(): return hashlib.sha256(b'x')\n", encoding="utf-8")
    assert scan_sast(tmp_path) == []


def test_js_dom_xss_sinks(tmp_path):
    (tmp_path / "a.jsx").write_text(
        "el.innerHTML = userInput;\n"
        "const x = <div dangerouslySetInnerHTML={{__html: u}} />;\n"
        "document.write(u);\n", encoding="utf-8")
    rules = _rules(tmp_path)
    assert "js-innerhtml" in rules
    assert "js-dangerously-set" in rules
    assert "js-document-write" in rules


def test_sql_concat_and_cors_wildcard(tmp_path):
    (tmp_path / "a.py").write_text(
        'q = "SELECT * FROM users WHERE id = " + uid\n', encoding="utf-8")
    (tmp_path / "cfg.js").write_text('const cors = { origin: "*" };\n', encoding="utf-8")
    rules = _rules(tmp_path)
    assert "sql-concat" in rules
    assert "cors-wildcard-cfg" in rules


def test_malformed_python_does_not_crash(tmp_path):
    (tmp_path / "a.py").write_text("def broken( : pass\n", encoding="utf-8")
    # unparsable python yields no AST findings, no exception
    assert isinstance(scan_sast(tmp_path), list)


def test_regex_rule_still_runs_on_unparsable_python(tmp_path):
    # even if AST fails, the line-level regex rules apply
    (tmp_path / "a.py").write_text('def broken( : q = "SELECT x FROM t WHERE a=" + b\n',
                                   encoding="utf-8")
    assert "sql-concat" in _rules(tmp_path)
