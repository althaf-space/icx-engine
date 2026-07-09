from __future__ import annotations
import json
import os
import shutil
import sys
import tomllib
import tomli_w
from dataclasses import dataclass
from pathlib import Path


def _resolve_icx_command() -> str:
    """Return the icx executable path for the running Python environment.

    Resolution order:
      1. shutil.which("icx") - searches PATH; covers pip, pipx, conda, activated venv.
         No .resolve() - avoids following pipx symlinks/shims to internal venv paths.
      2. Scripts/bin next to sys.executable - covers non-activated venv / PATH not set.
         Checks icx.exe (Windows) then icx (macOS/Linux).
      3. Bare "icx" - last resort; relies on PATH at editor launch time.
    """
    found = shutil.which("icx")
    if found:
        return found
    scripts_dir = Path(sys.executable).parent
    for name in ("icx.exe", "icx"):
        candidate = scripts_dir / name
        if candidate.exists():
            return str(candidate)
    return "icx"


def _make_icx_entry() -> dict:
    return {"command": _resolve_icx_command(), "args": ["mcp", "run"]}

ICX_MCP_ENTRY: dict = _make_icx_entry()


# -- Path helpers --------------------------------------------------------------

def _home() -> Path:
    return Path.home()


# -- Data models ---------------------------------------------------------------

@dataclass(frozen=True)
class MCPHost:
    name: str
    label: str
    config_path: Path
    detect_path: Path
    config_format: str  # "json" | "toml"
    extra_config_paths: tuple[Path, ...] = ()
    enforces: bool = False  # install ICX-first ticket-routing enforcement (Claude Code only)


@dataclass(frozen=True)
class WriteResult:
    path: Path
    fallback: bool


# -- Host registry -------------------------------------------------------------

def list_hosts() -> list[MCPHost]:
    """All known MCP hosts with their config file paths."""
    home = _home()
    return [
        MCPHost(
            "claude", "Claude Code",
            home / ".claude.json",
            home / ".claude",
            "json",
            enforces=True,
        ),
        MCPHost(
            "cursor", "Cursor",
            home / ".cursor" / "mcp.json",
            home / ".cursor",
            "json",
        ),
        MCPHost(
            "windsurf", "Windsurf",
            home / ".codeium" / "windsurf" / "mcp_config.json",
            home / ".codeium" / "windsurf",
            "json",
        ),
        MCPHost(
            "codex", "Codex",
            home / ".codex" / "config.toml",
            home / ".codex",
            "toml",
        ),
        MCPHost(
            "antigravity", "Antigravity",
            home / ".gemini" / "antigravity" / "mcp_config.json",
            home / ".gemini",
            "json",
        ),
    ]


def detect_installed_hosts() -> list[MCPHost]:
    """Hosts whose install directory exists on this machine."""
    return [h for h in list_hosts() if h.detect_path.exists()]


def get_host(name: str) -> MCPHost | None:
    """Look up a host by its name identifier."""
    return next((h for h in list_hosts() if h.name == name), None)


# -- Write / remove ------------------------------------------------------------

def write_icx_entry(host: MCPHost) -> WriteResult:
    """Write (or overwrite) the ICX entry in a host's MCP config file.

    Returns WriteResult(path, fallback=False) on success.
    Returns WriteResult(cwd/.mcp.json, fallback=True) when detect_path is absent.
    """
    if not host.detect_path.exists():
        fallback_path = Path.cwd() / ".mcp.json"
        _write_json(fallback_path)
        return WriteResult(path=fallback_path, fallback=True)

    host.config_path.parent.mkdir(parents=True, exist_ok=True)
    if host.config_format == "toml":
        _write_toml(host.config_path)
    else:
        _write_json(host.config_path)
    for extra in host.extra_config_paths:
        extra.parent.mkdir(parents=True, exist_ok=True)
        _write_json(extra)
    return WriteResult(path=host.config_path, fallback=False)


def remove_icx_entry(host: MCPHost) -> bool:
    """Remove ICX entry from host config. Returns True if entry was present."""
    if not host.config_path.exists():
        return False
    if host.config_format == "toml":
        removed = _remove_toml(host.config_path)
    else:
        removed = _remove_json(host.config_path)
    for extra in host.extra_config_paths:
        if extra.exists():
            _remove_json(extra)
    return removed


# -- Atomic write --------------------------------------------------------------

def _atomic_write(path: Path, content: str) -> None:
    tmp = path.parent / f"{path.name}.{os.getpid()}.{os.urandom(4).hex()}.tmp"
    tmp.write_text(content, encoding="utf-8")
    try:
        tmp.replace(path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        raise


# -- JSON helpers --------------------------------------------------------------

def _write_json(path: Path) -> None:
    existing: dict = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    existing.setdefault("mcpServers", {})
    existing["mcpServers"]["icx"] = _make_icx_entry()
    _atomic_write(path, json.dumps(existing, indent=2))


def _remove_json(path: Path) -> bool:
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False
    removed = existing.get("mcpServers", {}).pop("icx", None)
    if removed is None:
        return False
    _atomic_write(path, json.dumps(existing, indent=2))
    return True


# -- TOML helpers (Codex) ------------------------------------------------------

def _write_toml(path: Path) -> None:
    existing: dict = {}
    if path.exists():
        try:
            existing = tomllib.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    existing.setdefault("mcp_servers", {})
    existing["mcp_servers"]["icx"] = _make_icx_entry()
    _atomic_write(path, tomli_w.dumps(existing))


def _remove_toml(path: Path) -> bool:
    try:
        existing = tomllib.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    removed = existing.get("mcp_servers", {}).pop("icx", None)
    if removed is None:
        return False
    _atomic_write(path, tomli_w.dumps(existing))
    return True


# ---------------------------------------------------------------------------
# ICX-first ticket-routing enforcement (Claude Code)
#
# On `icx mcp setup --host claude`, ICX installs two layers so a bare ticket
# reference (ABC-123, a Jira/GitHub/Linear/GitLab issue URL) the user types is
# always routed through the ICX MCP server before any other tracker or a repo
# read - the user never has to say "use icx":
#   1. A UserPromptSubmit hook (a standalone detector script written to
#      ~/.icx/hooks/) that injects a mandatory routing directive when it spots
#      a ticket reference. The directive is CONDITIONAL - it self-neutralizes
#      when the match is not really a ticket or when ICX is not connected.
#   2. A marker-delimited rule block in ~/.claude/CLAUDE.md.
# Both are idempotent and merge-safe (existing hooks / memory are preserved),
# and are removed by `icx mcp remove --host claude`.
# ---------------------------------------------------------------------------

_HOOK_FILENAME = "icx-ticket-gate.py"
_RULE_START = "<!-- ICX-ROUTING:START (managed by `icx mcp setup`) -->"
_RULE_END = "<!-- ICX-ROUTING:END -->"

_RULE_BLOCK = f"""{_RULE_START}
## ICX routing (work-tracker tickets)

When my message references a work-tracker issue - a ticket key like `ABC-123` or a
Jira / GitHub / Linear / GitLab issue URL - and the ICX MCP server is connected
(`mcp__icx__*` tools are available), you MUST call `mcp__icx__analyze_issue_fast`
FIRST, before reading any code, running grep/glob/Read, or using any other
issue-tracker MCP or integration. ICX is the sole tracker interface when it is
connected. Only skip this when ICX is not connected or the reference is clearly not a
work item.
{_RULE_END}"""

# Standalone detector. Pure stdlib, no ICX import, so it adds no startup latency
# on every prompt. Written verbatim to ~/.icx/hooks/icx-ticket-gate.py.
_HOOK_SCRIPT = r'''#!/usr/bin/env python
"""ICX routing gate - Claude Code UserPromptSubmit hook (installed by `icx mcp setup`).

Detects a work-tracker ticket reference in the user's prompt (bare key like ABC-123, or a
Jira/GitHub/Linear/GitLab issue URL) and injects a mandatory directive telling Claude to
route through the ICX MCP server first. Fully guarded: on any error it emits nothing and
exits 0, so it can never block a turn. The directive is conditional, so a false match or a
disconnected ICX is harmless.
"""
import sys
import json
import re

_TICKET_RE = re.compile(r"\b[A-Z][A-Z0-9]{1,9}-\d+\b")
_DENY = {
    "UTF-8", "UTF-16", "UTF-32", "COVID-19", "ISO-8601", "ISO-9001",
    "SHA-1", "SHA-256", "SHA-512", "MD-5", "RFC-822", "IPV-4", "IPV-6",
    "BASE-64", "X-11", "PGP-2", "MP-3", "MP-4", "H-264", "H-265",
}
_URL_RE = re.compile(
    r"(atlassian\.net|/browse/|/issues/\d|/-/issues/|linear\.app)", re.IGNORECASE
)
_REPO_ISSUE_RE = re.compile(r"\b[\w.-]+/[\w.-]+#\d+\b")

_DIRECTIVE = (
    "[ICX ROUTING - MANDATORY WHEN APPLICABLE]\n"
    "A work-tracker ticket reference was detected in the user's message. "
    "IF the ICX MCP server is connected (tools named mcp__icx__* are available) AND this "
    "is genuinely a work-tracker issue (Jira, GitHub, Linear, GitLab, etc.), you MUST call "
    "mcp__icx__analyze_issue_fast FIRST - before reading any code, running grep/glob/Read, "
    "or calling any other issue-tracker MCP or integration - passing the ticket key or URL. "
    "ICX is the sole tracker interface when it is connected; do not fetch the ticket through "
    "any other connector. "
    "IF the reference is clearly NOT a work item (e.g. UTF-8, COVID-19, a version string) or "
    "ICX is not connected, ignore this note and proceed normally."
)


def main():
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
        prompt = str(data.get("prompt", ""))
        if not prompt:
            return
        hit = False
        for m in _TICKET_RE.findall(prompt):
            if m.upper() not in _DENY:
                hit = True
                break
        if not hit and (_URL_RE.search(prompt) or _REPO_ISSUE_RE.search(prompt)):
            hit = True
        if not hit:
            return
        out = {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": _DIRECTIVE,
            }
        }
        sys.stdout.write(json.dumps(out))
    except Exception:
        return


if __name__ == "__main__":
    main()
'''


def _icx_hooks_dir() -> Path:
    return _home() / ".icx" / "hooks"


def _hook_script_path() -> Path:
    return _icx_hooks_dir() / _HOOK_FILENAME


def _claude_settings_path() -> Path:
    return _home() / ".claude" / "settings.json"


def _claude_md_path() -> Path:
    return _home() / ".claude" / "CLAUDE.md"


def _hook_command() -> str:
    """Command string for the UserPromptSubmit hook. Uses the interpreter running ICX so a
    working Python is guaranteed at hook time; both paths quoted for spaces."""
    return f'"{sys.executable}" "{_hook_script_path()}"'


def _is_icx_hook_group(group: dict) -> bool:
    for h in group.get("hooks", []):
        if isinstance(h, dict) and _HOOK_FILENAME in str(h.get("command", "")):
            return True
    return False


def _write_hook_script() -> None:
    d = _icx_hooks_dir()
    d.mkdir(parents=True, exist_ok=True)
    _atomic_write(_hook_script_path(), _HOOK_SCRIPT)


def _install_settings_hook() -> None:
    """Merge the ICX UserPromptSubmit hook into ~/.claude/settings.json, preserving all
    other hooks. Idempotent - replaces any prior ICX entry rather than stacking."""
    path = _claude_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    existing: dict = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing = {}
    hooks = existing.setdefault("hooks", {})
    ups = hooks.setdefault("UserPromptSubmit", [])
    # Drop any prior ICX group, then append a fresh one.
    ups = [g for g in ups if not _is_icx_hook_group(g)]
    ups.append({
        "hooks": [{
            "type": "command",
            "command": _hook_command(),
            "timeout": 5,
            "statusMessage": "ICX: checking for ticket reference...",
        }]
    })
    hooks["UserPromptSubmit"] = ups
    _atomic_write(path, json.dumps(existing, indent=2))


def _remove_settings_hook() -> bool:
    path = _claude_settings_path()
    if not path.exists():
        return False
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False
    ups = existing.get("hooks", {}).get("UserPromptSubmit", [])
    kept = [g for g in ups if not _is_icx_hook_group(g)]
    if len(kept) == len(ups):
        return False
    if kept:
        existing["hooks"]["UserPromptSubmit"] = kept
    else:
        existing["hooks"].pop("UserPromptSubmit", None)
    _atomic_write(path, json.dumps(existing, indent=2))
    return True


def _install_claude_md_rule() -> None:
    """Insert or replace the marker-delimited ICX routing rule in ~/.claude/CLAUDE.md."""
    path = _claude_md_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    if _RULE_START in text and _RULE_END in text:
        pre = text.split(_RULE_START, 1)[0]
        post = text.split(_RULE_END, 1)[1]
        new = pre + _RULE_BLOCK + post
    elif text.strip():
        new = text.rstrip() + "\n\n" + _RULE_BLOCK + "\n"
    else:
        new = _RULE_BLOCK + "\n"
    _atomic_write(path, new)


def _remove_claude_md_rule() -> bool:
    path = _claude_md_path()
    if not path.exists():
        return False
    text = path.read_text(encoding="utf-8")
    if _RULE_START not in text or _RULE_END not in text:
        return False
    pre = text.split(_RULE_START, 1)[0]
    post = text.split(_RULE_END, 1)[1]
    new = (pre.rstrip() + "\n" + post.lstrip()).strip()
    new = (new + "\n") if new else ""
    _atomic_write(path, new)
    return True


def install_enforcement(host: MCPHost) -> list[str]:
    """Install ICX-first ticket-routing enforcement for a host. No-op (returns []) for
    hosts that do not support it. Returns human-readable messages of what was installed.
    Guarded per-step so a failure in one layer never aborts MCP registration."""
    if not host.enforces:
        return []
    messages: list[str] = []
    try:
        _write_hook_script()
        _install_settings_hook()
        messages.append(f"ticket-routing hook installed ({_claude_settings_path()})")
    except Exception as exc:
        messages.append(f"could not install ticket-routing hook: {exc}")
    try:
        _install_claude_md_rule()
        messages.append(f"routing rule written to {_claude_md_path()}")
    except Exception as exc:
        messages.append(f"could not write routing rule: {exc}")
    return messages


def remove_enforcement(host: MCPHost) -> list[str]:
    """Remove ICX-first enforcement for a host. No-op (returns []) for non-enforcing hosts."""
    if not host.enforces:
        return []
    messages: list[str] = []
    try:
        if _remove_settings_hook():
            messages.append("ticket-routing hook removed")
        script = _hook_script_path()
        if script.exists():
            script.unlink()
    except Exception as exc:
        messages.append(f"could not remove ticket-routing hook: {exc}")
    try:
        if _remove_claude_md_rule():
            messages.append("routing rule removed from CLAUDE.md")
    except Exception as exc:
        messages.append(f"could not remove routing rule: {exc}")
    return messages
