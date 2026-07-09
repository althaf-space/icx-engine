"""Tests for MCP host registration + ICX-first ticket-routing enforcement."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from icx_engine import mcp_hosts
from icx_engine.mcp_hosts import (
    get_host,
    install_enforcement,
    remove_enforcement,
    _HOOK_FILENAME,
    _RULE_START,
    _RULE_END,
)


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """Redirect ~ to a temp dir so no real config is touched."""
    monkeypatch.setattr(mcp_hosts, "_home", lambda: tmp_path)
    (tmp_path / ".claude").mkdir()  # make claude a "detected" host
    return tmp_path


# -- enforces flag -------------------------------------------------------------

def test_claude_host_enforces():
    assert get_host("claude").enforces is True


def test_other_hosts_do_not_enforce():
    for name in ("cursor", "windsurf", "codex", "antigravity"):
        assert get_host(name).enforces is False


def test_install_enforcement_noop_for_non_claude(fake_home):
    assert install_enforcement(get_host("cursor")) == []
    assert remove_enforcement(get_host("cursor")) == []


# -- install -------------------------------------------------------------------

def test_install_writes_hook_script(fake_home):
    install_enforcement(get_host("claude"))
    script = fake_home / ".icx" / "hooks" / _HOOK_FILENAME
    assert script.exists()
    assert "analyze_issue_fast" in script.read_text(encoding="utf-8")


def test_install_merges_userpromptsubmit_preserving_existing(fake_home):
    settings = fake_home / ".claude" / "settings.json"
    settings.write_text(json.dumps({
        "hooks": {"UserPromptSubmit": [
            {"hooks": [{"type": "command", "command": "node other-hook.js"}]}
        ]}
    }), encoding="utf-8")
    install_enforcement(get_host("claude"))
    data = json.loads(settings.read_text(encoding="utf-8"))
    ups = data["hooks"]["UserPromptSubmit"]
    cmds = [h["command"] for g in ups for h in g["hooks"]]
    assert any("other-hook.js" in c for c in cmds)          # preserved
    assert any(_HOOK_FILENAME in c for c in cmds)            # added


def test_install_is_idempotent(fake_home):
    install_enforcement(get_host("claude"))
    install_enforcement(get_host("claude"))
    data = json.loads((fake_home / ".claude" / "settings.json").read_text(encoding="utf-8"))
    ups = data["hooks"]["UserPromptSubmit"]
    icx_groups = [g for g in ups if any(_HOOK_FILENAME in h["command"] for h in g["hooks"])]
    assert len(icx_groups) == 1                              # no duplicate


def test_install_inserts_claude_md_rule(fake_home):
    install_enforcement(get_host("claude"))
    md = (fake_home / ".claude" / "CLAUDE.md").read_text(encoding="utf-8")
    assert _RULE_START in md and _RULE_END in md
    assert "analyze_issue_fast" in md


def test_install_claude_md_idempotent_and_preserves_user_text(fake_home):
    md_path = fake_home / ".claude" / "CLAUDE.md"
    md_path.write_text("# My rules\n\nkeep this line\n", encoding="utf-8")
    install_enforcement(get_host("claude"))
    install_enforcement(get_host("claude"))
    md = md_path.read_text(encoding="utf-8")
    assert md.count(_RULE_START) == 1                        # not duplicated
    assert "keep this line" in md                            # user text preserved


# -- remove --------------------------------------------------------------------

def test_remove_strips_hook_and_rule_leaving_other_content(fake_home):
    settings = fake_home / ".claude" / "settings.json"
    settings.write_text(json.dumps({
        "hooks": {"UserPromptSubmit": [
            {"hooks": [{"type": "command", "command": "node other-hook.js"}]}
        ]}
    }), encoding="utf-8")
    md_path = fake_home / ".claude" / "CLAUDE.md"
    md_path.write_text("# My rules\n\nkeep this line\n", encoding="utf-8")

    install_enforcement(get_host("claude"))
    remove_enforcement(get_host("claude"))

    data = json.loads(settings.read_text(encoding="utf-8"))
    cmds = [h["command"] for g in data["hooks"]["UserPromptSubmit"] for h in g["hooks"]]
    assert any("other-hook.js" in c for c in cmds)           # other hook kept
    assert not any(_HOOK_FILENAME in c for c in cmds)        # icx hook gone

    md = md_path.read_text(encoding="utf-8")
    assert _RULE_START not in md                             # rule block gone
    assert "keep this line" in md                            # user text kept

    assert not (fake_home / ".icx" / "hooks" / _HOOK_FILENAME).exists()  # script deleted


def test_remove_when_nothing_installed_is_safe(fake_home):
    # No prior install - remove must not raise and reports nothing.
    assert remove_enforcement(get_host("claude")) == []


# -- shipped detector script behavior -----------------------------------------

def _run_detector(fake_home, prompt: str) -> str:
    install_enforcement(get_host("claude"))
    script = fake_home / ".icx" / "hooks" / _HOOK_FILENAME
    proc = subprocess.run(
        [sys.executable, str(script)],
        input=json.dumps({"prompt": prompt}),
        capture_output=True, text=True,
    )
    return proc.stdout


def test_detector_fires_on_bare_ticket_key(fake_home):
    out = _run_detector(fake_home, "VILMA-2048 login broken")
    assert "analyze_issue_fast" in out


def test_detector_fires_on_issue_url(fake_home):
    out = _run_detector(fake_home, "see https://github.com/org/repo/issues/12")
    assert "analyze_issue_fast" in out


def test_detector_silent_on_non_ticket(fake_home):
    assert _run_detector(fake_home, "decode this UTF-8 please") == ""
    assert _run_detector(fake_home, "refactor the auth module") == ""
