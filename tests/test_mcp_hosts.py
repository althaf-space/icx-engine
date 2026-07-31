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
    list_hosts,
    write_icx_entry,
    remove_icx_entry,
    install_enforcement,
    remove_enforcement,
    install_boost_command,
    remove_boost_command,
    _HOOK_FILENAME,
    _RULE_START,
    _RULE_END,
)

_ALL_HOSTS = ("claude", "cursor", "windsurf", "codex", "antigravity", "vscode")


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """Redirect ~ to a temp dir so no real config is touched."""
    monkeypatch.setattr(mcp_hosts, "_home", lambda: tmp_path)
    (tmp_path / ".claude").mkdir()  # make claude a "detected" host
    return tmp_path


@pytest.fixture
def fake_cwd(tmp_path, monkeypatch):
    """Redirect cwd to a temp project dir - vscode's host paths are workspace-relative."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


# -- enforces flag -------------------------------------------------------------

def test_all_hosts_enforce():
    # every supported editor now gets ICX routing enforcement (ticket/testing/sonar)
    for name in _ALL_HOSTS:
        assert get_host(name).enforces is True


def test_enforcement_kind_per_host():
    assert get_host("claude").enforce_kind == "hook"          # hard pre-agent hook
    for name in ("cursor", "windsurf", "codex", "antigravity", "vscode"):
        h = get_host(name)
        assert h.enforce_kind == "rules"                       # instruction-based rules file
        assert h.rules_path is not None


def test_six_hosts_registered():
    names = {h.name for h in list_hosts()}
    assert names == set(_ALL_HOSTS)


def test_rules_host_writes_routing_rule_to_its_global_file(fake_home):
    # windsurf global_rules.md, codex AGENTS.md, antigravity GEMINI.md, cursor .mdc all get the rule
    for name, rel in (
        ("windsurf", (".codeium", "windsurf", "memories", "global_rules.md")),
        ("codex", (".codex", "AGENTS.md")),
        ("antigravity", (".gemini", "GEMINI.md")),
        ("cursor", (".cursor", "rules", "icx.mdc")),
    ):
        install_enforcement(get_host(name))
        p = fake_home.joinpath(*rel)
        assert p.exists(), f"{name} rule file not written"
        text = p.read_text(encoding="utf-8")
        assert _RULE_START in text and _RULE_END in text
        assert "analyze_issue_fast" in text
        assert "icx_boost" in text and "/icx-boost" in text   # points to the on-demand command


def test_rules_host_preserves_existing_content(fake_home):
    gr = fake_home / ".codeium" / "windsurf" / "memories" / "global_rules.md"
    gr.parent.mkdir(parents=True, exist_ok=True)
    gr.write_text("# my rules\nkeep this line\n", encoding="utf-8")
    install_enforcement(get_host("windsurf"))
    text = gr.read_text(encoding="utf-8")
    assert "keep this line" in text and _RULE_START in text
    remove_enforcement(get_host("windsurf"))
    text = gr.read_text(encoding="utf-8")
    assert "keep this line" in text and _RULE_START not in text   # ICX block stripped, user text kept


def test_cursor_surfaces_honest_manual_caveat(fake_home):
    msgs = install_enforcement(get_host("cursor"))
    assert any("Settings" in m and "User Rules" in m for m in msgs)


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


# -- windsurf/devin desktop path migration --------------------------------------

def _devin_dir(fake_home):
    import sys
    if sys.platform == "win32":
        return fake_home / "AppData" / "Roaming" / "devin"
    if sys.platform == "darwin":
        return fake_home / "Library" / "Application Support" / "devin"
    return fake_home / ".config" / "devin"


def test_windsurf_config_path_is_new_devin_location(fake_home):
    h = get_host("windsurf")
    assert h.config_path == _devin_dir(fake_home) / "mcp_config.json"


def test_windsurf_old_codeium_path_kept_as_extra(fake_home):
    h = get_host("windsurf")
    old = fake_home / ".codeium" / "windsurf" / "mcp_config.json"
    assert old in h.extra_config_paths


def test_windsurf_write_creates_entry_at_both_new_and_old_paths(fake_home):
    (fake_home / ".codeium" / "windsurf").mkdir(parents=True)  # simulate windsurf/devin installed
    write_icx_entry(get_host("windsurf"))
    new_path = _devin_dir(fake_home) / "mcp_config.json"
    old_path = fake_home / ".codeium" / "windsurf" / "mcp_config.json"
    assert new_path.exists()
    assert old_path.exists()
    assert json.loads(new_path.read_text())["mcpServers"]["icx"]
    assert json.loads(old_path.read_text())["mcpServers"]["icx"]


def test_windsurf_remove_cleans_stale_old_path_entry(fake_home):
    # Exact reported scenario: user already had an ICX entry at the old pre-migration path (icx
    # setup ran before this fix shipped) - `icx mcp remove` must still clean it up.
    old_path = fake_home / ".codeium" / "windsurf" / "mcp_config.json"
    old_path.parent.mkdir(parents=True, exist_ok=True)
    old_path.write_text(json.dumps({"mcpServers": {"icx": {"command": "icx", "args": ["mcp", "run"]}}}))
    assert remove_icx_entry(get_host("windsurf")) is True
    assert "icx" not in json.loads(old_path.read_text())["mcpServers"]


def test_windsurf_remove_cleans_old_path_even_when_new_path_never_created(fake_home):
    # The bug this fix closes: remove_icx_entry used to bail out entirely if the PRIMARY
    # config_path didn't exist, silently skipping extra_config_paths cleanup.
    old_path = fake_home / ".codeium" / "windsurf" / "mcp_config.json"
    old_path.parent.mkdir(parents=True, exist_ok=True)
    old_path.write_text(json.dumps({"mcpServers": {"icx": {"command": "icx", "args": ["mcp", "run"]}}}))
    new_path = _devin_dir(fake_home) / "mcp_config.json"
    assert not new_path.exists()
    assert remove_icx_entry(get_host("windsurf")) is True
    assert "icx" not in json.loads(old_path.read_text())["mcpServers"]


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


def test_detector_silent_on_plain_request(fake_home):
    # No ticket/testing/sonar signal -> the hook is silent. Boost is on-demand only (/icx-boost),
    # never injected here - this is the behavior change this build makes.
    out = _run_detector(fake_home, "refactor the auth module")
    assert out == ""


def test_detector_silent_on_plain_question(fake_home):
    out = _run_detector(fake_home, "decode this UTF-8 please")
    assert out == ""           # UTF-8 is denied as a ticket, and nothing else matches


def test_detector_adds_ticket_routing_on_bare_key(fake_home):
    out = _run_detector(fake_home, "VILMA-2048 login broken")
    assert "analyze_issue_fast" in out
    assert "icx_boost" not in out                    # boost is not injected by the hook anymore


def test_detector_adds_ticket_routing_on_issue_url(fake_home):
    out = _run_detector(fake_home, "see https://github.com/org/repo/issues/12")
    assert "analyze_issue_fast" in out
    assert "icx_boost" not in out


def test_detector_silent_on_empty_prompt(fake_home):
    assert _run_detector(fake_home, "") == ""


def test_install_migrates_legacy_hook_file(fake_home):
    # Simulate an older install: a stale icx-ticket-gate.py present.
    hooks = fake_home / ".icx" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    legacy = hooks / "icx-ticket-gate.py"
    legacy.write_text("# old", encoding="utf-8")
    install_enforcement(get_host("claude"))
    assert not legacy.exists()                       # legacy removed
    assert (hooks / _HOOK_FILENAME).exists()          # current present


def test_remove_cleans_legacy_hook_file(fake_home):
    hooks = fake_home / ".icx" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    (hooks / "icx-ticket-gate.py").write_text("# old", encoding="utf-8")
    install_enforcement(get_host("claude"))
    remove_enforcement(get_host("claude"))
    assert not (hooks / "icx-ticket-gate.py").exists()
    assert not (hooks / _HOOK_FILENAME).exists()


def test_detector_routes_testing_requests(fake_home):
    out = _run_detector(fake_home, "please test the login screen and check coverage")
    assert "start_testing_session" in out
    assert "analyze_issue_fast" not in out and "sonar_" not in out
    assert "icx_boost" not in out


def test_detector_routes_sonar_requests(fake_home):
    out = _run_detector(fake_home, "show me the sonarqube quality gate and vulnerabilities")
    assert "sonar_" in out
    assert "start_testing_session" not in out
    assert "icx_boost" not in out


def test_rule_block_mandates_ticket_testing_sonar_routing():
    from icx_engine.mcp_hosts import _RULE_BLOCK
    b = _RULE_BLOCK
    assert "analyze_issue_fast" in b
    assert "start_testing_session" in b
    assert "sonar_" in b


def test_rule_block_points_to_on_demand_boost_not_every_message():
    from icx_engine.mcp_hosts import _RULE_BLOCK
    b = _RULE_BLOCK
    assert "/icx-boost" in b and "icx_boost_refine" in b
    assert "on-demand" in b.lower() or "SEPARATE" in b
    assert "EVERY single message" not in b           # the old blanket mandate is gone


def test_rule_block_covers_agent_connector_fallback_for_other_links():
    # any other URL (Figma/Slack/etc): ICX connector -> agent's own connector -> tell the user
    from icx_engine.mcp_hosts import _RULE_BLOCK
    b = _RULE_BLOCK.lower()
    assert "connector" in b


def test_rule_block_covers_full_tracker_crud_not_just_analysis():
    # regression: a create/search/lookup request has no ticket key yet, so it must not be
    # scoped out of ICX routing just because analyze_issue_fast doesn't apply to it
    from icx_engine.mcp_hosts import _RULE_BLOCK
    b = _RULE_BLOCK.lower()
    assert "creating" in b and "searching" in b
    assert "native tracker connector" in b or "separate or native tracker connector" in b
    assert "not analysis alone" in b


def test_rule_block_mandates_git_workflow_through_icx():
    # regression: nothing previously told the agent git operations must go through ICX, so it
    # fell back to raw git commands for branch creation/commits, bypassing the safety doctrine
    from icx_engine.mcp_hosts import _RULE_BLOCK
    b = _RULE_BLOCK.lower()
    assert "git_repo_status" in b
    assert "raw `git`" in b or "raw git" in b
    assert "sole git-workflow" in b and "interface" in b


# -- vscode host: different config shape ("servers", not "mcpServers") ---------

def test_vscode_host_uses_servers_key_and_stdio_type():
    h = get_host("vscode")
    assert h.mcp_key == "servers"
    assert h.entry_type == "stdio"


def test_vscode_write_uses_servers_key(fake_cwd):
    (fake_cwd / ".vscode").mkdir()
    h = get_host("vscode")
    result = write_icx_entry(h)
    assert not result.fallback
    data = json.loads(h.config_path.read_text(encoding="utf-8"))
    assert "servers" in data and "mcpServers" not in data
    assert data["servers"]["icx"]["type"] == "stdio"
    assert remove_icx_entry(h) is True
    assert "icx" not in json.loads(h.config_path.read_text(encoding="utf-8"))["servers"]


def test_vscode_not_detected_falls_back(fake_cwd):
    # no .vscode dir present -> fallback path, matching every other host's fallback contract
    h = get_host("vscode")
    result = write_icx_entry(h)
    assert result.fallback and result.path == fake_cwd / ".mcp.json"


# -- native /icx-boost command file (all 6 hosts) -------------------------------

def test_every_host_has_a_command_file_configured():
    for name in _ALL_HOSTS:
        h = get_host(name)
        assert h.command_path is not None
        assert h.command_content.strip()


def test_install_boost_command_writes_file_per_host(fake_home, fake_cwd):
    for name in _ALL_HOSTS:
        h = get_host(name)
        msg = install_boost_command(h)
        assert msg is not None and str(h.command_path) in msg
        assert h.command_path.exists()
        text = h.command_path.read_text(encoding="utf-8")
        assert "icx_boost" in text
        assert "explicitly invoked" in text            # on-demand, not every message


def test_remove_boost_command_deletes_file_per_host(fake_home, fake_cwd):
    for name in _ALL_HOSTS:
        h = get_host(name)
        install_boost_command(h)
        assert remove_boost_command(h) is True
        assert not h.command_path.exists()
        assert remove_boost_command(h) is False         # already gone - safe, no crash


def test_install_boost_command_is_idempotent(fake_home):
    h = get_host("claude")
    install_boost_command(h)
    first = h.command_path.read_text(encoding="utf-8")
    install_boost_command(h)
    second = h.command_path.read_text(encoding="utf-8")
    assert first == second


def test_claude_skill_uses_short_command_name(fake_home):
    h = get_host("claude")
    install_boost_command(h)
    text = h.command_path.read_text(encoding="utf-8")
    assert "name: icx-boost" in text
    assert h.command_path.name == "SKILL.md"
    assert h.command_path.parent.name == "icx-boost"


def test_antigravity_rules_path_is_gemini_md_not_agents_md():
    # earlier assumption (.gemini/AGENTS.md) was wrong per editor research - fixed to GEMINI.md
    h = get_host("antigravity")
    assert h.rules_path.name == "GEMINI.md"
