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


def _make_icx_entry(entry_type: str | None = None) -> dict:
    entry = {"command": _resolve_icx_command(), "args": ["mcp", "run"]}
    if entry_type:
        entry = {"type": entry_type, **entry}
    return entry

ICX_MCP_ENTRY: dict = _make_icx_entry()


# -- Path helpers --------------------------------------------------------------

def _home() -> Path:
    return Path.home()


def _devin_config_dir() -> Path:
    """Devin Desktop (formerly Windsurf - Cognition's June 2026 rename) moved its MCP config out of
    ~/.codeium/windsurf/ to a dedicated per-app config dir. Windows path confirmed directly from a
    live Devin Desktop migration prompt (2026-07: "...AppData\\Roaming\\devin\\mcp_config.json");
    macOS/Linux derived from the same single-app-name config-dir convention (matches Python's
    platformdirs.user_config_dir("devin")) - not independently confirmed on those platforms, revisit
    if a Mac/Linux user reports a different actual path. Derived from _home() (not a raw os.environ
    read) so the existing fake_home test fixture can redirect this too, same as every other host path
    in this file."""
    home = _home()
    if sys.platform == "win32":
        return home / "AppData" / "Roaming" / "devin"
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "devin"
    return home / ".config" / "devin"


# -- Data models ---------------------------------------------------------------

@dataclass(frozen=True)
class MCPHost:
    name: str
    label: str
    config_path: Path
    detect_path: Path
    config_format: str  # "json" | "toml"
    extra_config_paths: tuple[Path, ...] = ()
    # Additional install-detection locations checked alongside detect_path (OR, not AND) - a host
    # is considered installed if ANY of these exists. Needed when an editor migrated its config dir
    # (e.g. Windsurf -> Devin Desktop): detect_path alone might name only the OLD dir, so a
    # new-install-only user (never had the old dir) would be missed and silently fall back to
    # cwd/.mcp.json instead of writing to their real config.
    extra_detect_paths: tuple[Path, ...] = ()
    mcp_key: str = "mcpServers"    # JSON key the MCP server entry is nested under (VS Code uses "servers")
    entry_type: str | None = None  # optional "type" field the entry needs (VS Code stdio servers: "stdio")
    enforces: bool = False        # install ICX-first ticket/testing/sonar routing enforcement
    # enforcement mechanism: "hook" = Claude Code UserPromptSubmit hook + CLAUDE.md (hard, pre-agent);
    # "rules" = write the ICX rule into the editor's global rules file (instruction-based). None = no
    # enforcement. rules_path is the editor's documented global-rules file (see per-editor research).
    enforce_kind: str = ""        # "hook" | "rules" | ""
    rules_path: Path | None = None
    rules_note: str = ""          # honest per-editor caveat shown after install
    command_path: Path | None = None    # native /icx-boost command/skill/workflow file for this editor
    command_content: str = ""           # its full file content


@dataclass(frozen=True)
class WriteResult:
    path: Path
    fallback: bool


# -- Native /icx-boost command files --------------------------------------------
#
# One command, one deterministic call: the icx_boost tool now auto-applies the refine pass itself
# (see mcp_server.py _boosted()), so every editor's command body is the same - call icx_boost once,
# work from boosted_prompt, optionally enrich with icx_boost_refine. This file is ONLY written/removed
# by `icx mcp setup`/`icx mcp remove` - it must never nag on every message, only fire when the user (or
# the editor's MCP-prompt auto-surfacing) explicitly invokes /icx-boost.

_BOOST_COMMAND_BODY = (
    "Boost this into a CTO-grade ICX working spec, in one call - then work from it.\n\n"
    "Take the text that follows this command as the user's raw request, then:\n"
    "1. Call the ICX MCP tool `icx_boost` (server: icx) with that text as `prompt` (add "
    "`repo_path`/`current_file` when known).\n"
    "2. Its `boosted_prompt` already ran an auto-refine pass - work from it directly. No second tool "
    "call is required.\n"
    "3. Optional: call `icx_boost_refine` yourself with a hand-drafted objective/requirements/"
    "constraints/acceptance/dims for an even stronger spec.\n\n"
    "Only run this when this command is explicitly invoked - never automatically on every message."
)

_BOOST_DESCRIPTION = ("Boost this request into a CTO-grade ICX working spec - boost + auto-refine "
                      "in one call.")

_CLAUDE_SKILL_CONTENT = f"""---
name: icx-boost
description: {_BOOST_DESCRIPTION}
argument-hint: <your request>
---

Call the ICX MCP tool `icx_boost` with prompt=$ARGUMENTS (add repo_path/current_file when known).
Its `boosted_prompt` already ran an auto-refine pass - work from it directly, no second tool call
required. Optional: call `icx_boost_refine` yourself with a hand-drafted objective/requirements/
constraints/acceptance/dims for an even stronger spec.

Only run this when /icx-boost is explicitly invoked - never automatically on every message.
"""

_VSCODE_PROMPT_CONTENT = f"""---
description: {_BOOST_DESCRIPTION}
---

{_BOOST_COMMAND_BODY}
"""

_CURSOR_COMMAND_CONTENT = _BOOST_COMMAND_BODY + "\n"

_WINDSURF_WORKFLOW_CONTENT = f"""---
description: {_BOOST_DESCRIPTION}
---

{_BOOST_COMMAND_BODY}
"""

_CODEX_PROMPT_CONTENT = f"""---
description: {_BOOST_DESCRIPTION}
---

Call the ICX MCP tool `icx_boost` with prompt=$ARGUMENTS (add repo_path/current_file when known).
Its `boosted_prompt` already ran an auto-refine pass - work from it directly, no second tool call
required. Optional: call `icx_boost_refine` yourself with a hand-drafted objective/requirements/
constraints/acceptance/dims for an even stronger spec.

Only run this when /icx-boost is explicitly invoked - never automatically on every message.
"""

# Antigravity's workflow-file frontmatter is not documented beyond "description" (research: sparse
# official docs) - reuse the Windsurf-style body, which needs only that one confirmed field.
_ANTIGRAVITY_WORKFLOW_CONTENT = _WINDSURF_WORKFLOW_CONTENT


# -- Host registry -------------------------------------------------------------

def list_hosts() -> list[MCPHost]:
    """All known MCP hosts with their config file paths."""
    home = _home()
    cwd = Path.cwd()
    return [
        MCPHost(
            "claude", "Claude Code",
            home / ".claude.json",
            home / ".claude",
            "json",
            enforces=True, enforce_kind="hook",
            command_path=home / ".claude" / "skills" / "icx-boost" / "SKILL.md",
            command_content=_CLAUDE_SKILL_CONTENT,
        ),
        MCPHost(
            # Cursor does NOT natively support a global ~/.cursor/rules file (official: feature request,
            # no ETA as of 2026). ~/.cursor/rules/icx.mdc with alwaysApply is best-effort; the guaranteed
            # path is a one-line User Rule in Settings, which we cannot write - so we say so honestly.
            "cursor", "Cursor",
            home / ".cursor" / "mcp.json",
            home / ".cursor",
            "json",
            enforces=True, enforce_kind="rules",
            rules_path=home / ".cursor" / "rules" / "icx.mdc",
            rules_note=("Cursor does not guarantee global file-rules - if ticket/testing/sonar routing "
                        "is not applied, add this one line in Cursor Settings > Rules (User Rules): "
                        "'Route work-tracker tickets, testing requests, and Sonar/code-quality requests "
                        "through the ICX MCP tools.'"),
            command_path=home / ".cursor" / "commands" / "icx-boost.md",
            command_content=_CURSOR_COMMAND_CONTENT,
        ),
        MCPHost(
            # Devin Desktop (formerly Windsurf) moved its MCP config to _devin_config_dir() - see that
            # function's docstring. The old ~/.codeium/windsurf/mcp_config.json is kept as an extra
            # write/remove target (not the primary) so an install that still reads the old path (e.g.
            # Devin CLI, which per its own docs reads ~/.codeium/<channel>/mcp_config.json) stays in
            # sync, and so a stale pre-migration entry there gets cleaned up on `icx mcp remove`.
            # rules_path/command_path (global_rules.md/global_workflows) are UNCHANGED - only the MCP
            # server config file itself is confirmed to have moved.
            "windsurf", "Windsurf",
            _devin_config_dir() / "mcp_config.json",
            home / ".codeium" / "windsurf",
            "json",
            extra_config_paths=(home / ".codeium" / "windsurf" / "mcp_config.json",),
            # detect_path above is the OLD pre-migration dir - a machine with ONLY the new Devin
            # Desktop installed (never had old Windsurf) would otherwise be missed entirely. Detecting
            # either the old dir OR the new Devin config dir covers both populations.
            extra_detect_paths=(_devin_config_dir(),),
            enforces=True, enforce_kind="rules",
            rules_path=home / ".codeium" / "windsurf" / "memories" / "global_rules.md",
            command_path=home / ".codeium" / "windsurf" / "global_workflows" / "icx-boost.md",
            command_content=_WINDSURF_WORKFLOW_CONTENT,
        ),
        MCPHost(
            "codex", "Codex",
            home / ".codex" / "config.toml",
            home / ".codex",
            "toml",
            enforces=True, enforce_kind="rules",
            rules_path=home / ".codex" / "AGENTS.md",
            command_path=home / ".codex" / "prompts" / "icx-boost.md",
            command_content=_CODEX_PROMPT_CONTENT,
        ),
        MCPHost(
            "antigravity", "Antigravity",
            home / ".gemini" / "antigravity" / "mcp_config.json",
            home / ".gemini",
            "json",
            enforces=True, enforce_kind="rules",
            # Antigravity's own global rules file is GEMINI.md (not AGENTS.md - that was an earlier,
            # incorrect assumption; see per-editor research).
            rules_path=home / ".gemini" / "GEMINI.md",
            command_path=home / ".gemini" / "antigravity" / "global_workflows" / "icx-boost.md",
            command_content=_ANTIGRAVITY_WORKFLOW_CONTENT,
        ),
        MCPHost(
            # VS Code's MCP config is workspace-scoped (.vscode/mcp.json, "servers" key, "type": "stdio") -
            # there is no stable, documented cross-platform path for its user-profile MCP config, unlike
            # the other hosts' home-relative globals. Detected/written relative to the current project.
            "vscode", "VS Code",
            cwd / ".vscode" / "mcp.json",
            cwd / ".vscode",
            "json",
            mcp_key="servers", entry_type="stdio",
            enforces=True, enforce_kind="rules",
            rules_path=cwd / ".github" / "copilot-instructions.md",
            command_path=cwd / ".github" / "prompts" / "icx-boost.prompt.md",
            command_content=_VSCODE_PROMPT_CONTENT,
        ),
    ]


def _is_installed(host: MCPHost) -> bool:
    """A host is considered installed if its primary detect_path exists OR any
    extra_detect_paths entry exists - covers a migrated editor (e.g. Windsurf -> Devin
    Desktop) where either the old-only or new-only population would otherwise be missed."""
    return host.detect_path.exists() or any(p.exists() for p in host.extra_detect_paths)


def detect_installed_hosts() -> list[MCPHost]:
    """Hosts whose install directory exists on this machine."""
    return [h for h in list_hosts() if _is_installed(h)]


def get_host(name: str) -> MCPHost | None:
    """Look up a host by its name identifier."""
    return next((h for h in list_hosts() if h.name == name), None)


# -- Write / remove ------------------------------------------------------------

def write_icx_entry(host: MCPHost) -> WriteResult:
    """Write (or overwrite) the ICX entry in a host's MCP config file.

    Returns WriteResult(path, fallback=False) on success.
    Returns WriteResult(cwd/.mcp.json, fallback=True) when neither detect_path nor any
    extra_detect_paths entry exists.
    """
    if not _is_installed(host):
        fallback_path = Path.cwd() / ".mcp.json"
        _write_json(fallback_path)
        return WriteResult(path=fallback_path, fallback=True)

    host.config_path.parent.mkdir(parents=True, exist_ok=True)
    if host.config_format == "toml":
        _write_toml(host.config_path)
    else:
        _write_json(host.config_path, mcp_key=host.mcp_key, entry_type=host.entry_type)
    for extra in host.extra_config_paths:
        extra.parent.mkdir(parents=True, exist_ok=True)
        _write_json(extra, mcp_key=host.mcp_key, entry_type=host.entry_type)
    return WriteResult(path=host.config_path, fallback=False)


def remove_icx_entry(host: MCPHost) -> bool:
    """Remove ICX entry from host config (and every extra_config_paths location, e.g. a stale
    pre-migration path kept in sync for Windsurf/Devin Desktop). Returns True if the entry was
    present anywhere. The primary config_path missing must not short-circuit cleanup of an extra
    path that still has a stale entry - each location is checked independently."""
    removed = False
    if host.config_path.exists():
        if host.config_format == "toml":
            removed = _remove_toml(host.config_path) or removed
        else:
            removed = _remove_json(host.config_path, mcp_key=host.mcp_key) or removed
    for extra in host.extra_config_paths:
        if extra.exists():
            removed = _remove_json(extra, mcp_key=host.mcp_key) or removed
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

def _write_json(path: Path, mcp_key: str = "mcpServers", entry_type: str | None = None) -> None:
    existing: dict = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    existing.setdefault(mcp_key, {})
    existing[mcp_key]["icx"] = _make_icx_entry(entry_type)
    _atomic_write(path, json.dumps(existing, indent=2))


def _remove_json(path: Path, mcp_key: str = "mcpServers") -> bool:
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False
    removed = existing.get(mcp_key, {}).pop("icx", None)
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


# -- Native /icx-boost command file (all 6 hosts) -------------------------------
#
# Fully ICX-owned file (never merged with user content, unlike the rule files below) - a plain
# overwrite is idempotent by construction. This is the on-demand replacement for the old
# every-message boost mandate: boost only runs when the user (or the editor's own MCP-prompt
# auto-surfacing) explicitly invokes /icx-boost.

def install_boost_command(host: MCPHost) -> str | None:
    """Write the host's native /icx-boost command/skill/workflow file. Returns a status message, or
    None if this host has no command file configured."""
    if host.command_path is None or not host.command_content:
        return None
    host.command_path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(host.command_path, host.command_content)
    return f"/icx-boost command installed ({host.command_path})"


def remove_boost_command(host: MCPHost) -> bool:
    """Remove the host's native /icx-boost command file. Returns True if it was present."""
    if host.command_path is None or not host.command_path.exists():
        return False
    host.command_path.unlink()
    return True


# ---------------------------------------------------------------------------
# ICX enforcement (Claude Code) - ticket/testing/sonar routing
#
# On `icx mcp setup --host <name>`, ICX installs two independent layers:
#   1. The native /icx-boost command file (install_boost_command, above) - boost now runs ONLY on
#      demand, never injected into every message.
#   2. Always-on routing enforcement (this section) for the narrower, high-precision triggers that
#      are NOT the boost mandate: a work-tracker ticket reference (ABC-123, a Jira/GitHub/Linear/
#      GitLab issue URL) always routes through analyze_issue_fast, testing requests through
#      start_testing_session, and Sonar/code-quality requests through the sonar_* tools. Claude Code
#      gets a hard pre-agent UserPromptSubmit hook (a standalone detector script written to
#      ~/.icx/hooks/); the other hosts get an instruction written into their global rules file (they
#      expose no pre-agent shell hook). Directives are CONDITIONAL - they self-neutralize when ICX is
#      not connected.
# Both layers are idempotent and merge-safe (existing hooks / rules content preserved), and are
# removed by `icx mcp remove --host <name>`. Everything MCP-related lives in `icx mcp setup` /
# `icx mcp remove` - there is no separate hook command.
# ---------------------------------------------------------------------------

_HOOK_FILENAME = "icx-boost-gate.py"
# Older installs shipped this name; detected + cleaned up so a re-run of `icx mcp setup`
# (or `icx mcp remove`) migrates them without leaving a stale hook behind.
_LEGACY_HOOK_FILENAMES = ("icx-ticket-gate.py",)
_RULE_START = "<!-- ICX-ROUTING:START (managed by `icx mcp setup`) -->"
_RULE_END = "<!-- ICX-ROUTING:END -->"

_RULE_BLOCK = f"""{_RULE_START}
## ICX ROUTING RULE: tracker, testing, git, and code-quality requests always go through ICX

When the ICX MCP server is connected (any `mcp__icx__*` tool is available), route these request
types through ICX - this applies on every message, independent of whether /icx-boost was invoked:

1. ANY WORK-TRACKER ACTION - analyzing an existing ticket, searching, creating, updating,
   commenting, linking, attaching, assigning, watching, logging work, or looking up a
   project/user/field (Jira/GitHub/Linear/GitLab) - goes through ICX's own tools, never a
   separate or native tracker connector (e.g. a built-in Atlassian/Jira integration), even if
   one is also available in the same session. For an existing ticket key (`ABC-123`) or issue
   URL, call `mcp__icx__analyze_issue_fast` FIRST, before any other tracker tool. ICX is the
   sole tracker interface for every action above, not analysis alone.
2. TESTING an app/screen/UI/API/endpoint ("test this", "write tests", "check this
   screen/flow", QA, coverage): call `mcp__icx__start_testing_session` FIRST; drive testing
   through ICX, never hand-roll tests.
3. CODE QUALITY / SonarQube ("sonar", "quality gate", "code smells", "vulnerabilities",
   "coverage report"): call the ICX `mcp__icx__sonar_*` tools (start with sonar_status /
   sonar_report) - do not fetch Sonar data another way.
4. ANY GIT OPERATION - checking status, creating a branch, staging/committing, syncing/
   merging, pushing, opening or finishing a merge request, tagging - goes through ICX's own
   `mcp__icx__git_*`/`mcp__icx__gitlab_*` tools, never raw `git`/`gh`/`glab` commands or another
   git integration, even if one is also available in the same session. Call
   `mcp__icx__git_repo_status` FIRST, before any other git tool. ICX is the sole git-workflow
   interface for every action above - this is what enforces the "no rebase, no force-push"
   safety doctrine and the "no commit/branch without going through ICX" mandate; bypassing it
   with a raw git command defeats both.
5. ANY OTHER URL pasted by the user, or found inside a ticket (Figma, Slack, docs, etc.):
   if ICX has a connector for it, use ICX. If ICX does not but you have your own connector/
   tool for it, use that. If neither is available, tell the user to fetch/paste it
   themselves - never fabricate its content.
6. WORKSTATUS time-tracking requests (timesheets, clock in/out, attendance, or logging time
   against a project/task) - PREFER ICX's own `mcp__icx__workstatus_*` tools when applicable.
   This is a SOFT preference, not the same mandate as 1-5: Workstatus coverage is partial
   (~24 endpoints implemented; some actions may not be covered yet), so fall back to another
   approach if ICX doesn't support the specific action needed - never block the user's request
   waiting on ICX alone.

Items 1-5 above are mandatory whenever ICX is connected - never bypass them. Item 6
(Workstatus) is a preference given partial tool coverage, not a hard mandate. Skip any item
only when ICX is genuinely not connected.

BOOSTING a request into a CTO-grade working spec is a SEPARATE, on-demand action - run
`/icx-boost <request>` (or call `mcp__icx__icx_boost` directly) only when explicitly
invoked, never automatically on every message. It already runs an auto-refine pass in one
call; `mcp__icx__icx_boost_refine` remains available afterwards for an optional,
hand-drafted enrichment pass.
{_RULE_END}"""

# Standalone detector. Pure stdlib, no ICX import, so it adds no startup latency
# on every prompt. Written verbatim to ~/.icx/hooks/icx-boost-gate.py.
_HOOK_SCRIPT = r'''#!/usr/bin/env python
"""ICX routing gate - Claude Code UserPromptSubmit hook (installed by `icx mcp setup`).

Narrow, high-precision routing only - NOT a boost trigger (boost is on-demand via /icx-boost, never
injected here). When the prompt references a work-tracker ticket (bare key like ABC-123, or a
Jira/GitHub/Linear/GitLab issue URL) it injects the ticket-routing directive
(mcp__icx__analyze_issue_fast); similarly for testing requests (mcp__icx__start_testing_session) and
Sonar/code-quality requests (mcp__icx__sonar_*). Silent when none apply. Pure stdlib, no ICX import,
so it adds no startup latency. Fully guarded: on any error it emits nothing and exits 0, so it can
never block a turn. All directives are conditional on ICX actually being connected, so they are
harmless when it is not.
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
_TESTING_RE = re.compile(
    r"\b(test|tests|testing|qa|coverage|e2e|end.to.end|regression|write.*tests?|"
    r"check.*(screen|page|flow|ui)|test.*(screen|page|app|flow|endpoint|api))\b", re.IGNORECASE
)
_SONAR_RE = re.compile(
    r"\b(sonar|sonarqube|quality.gate|code.smell|static.analysis|vulnerabilit|"
    r"code.quality|coverage.report)\b", re.IGNORECASE
)
_WORKSTATUS_RE = re.compile(
    r"\b(workstatus|time.?sheets?|clock.?(?:in|out)|time.?tracking)\b", re.IGNORECASE
)

_TICKET_DIRECTIVE = (
    "[ICX ROUTING - MANDATORY WHEN APPLICABLE]\n"
    "A work-tracker ticket reference was also detected. IF ICX is connected AND this is genuinely "
    "a work-tracker issue (Jira, GitHub, Linear, GitLab, etc.), call mcp__icx__analyze_issue_fast "
    "for the ticket - passing the ticket key or URL - before any other issue-tracker MCP or "
    "integration. ICX is the sole tracker interface when it is connected. IF the reference is "
    "clearly NOT a work item (e.g. UTF-8, COVID-19, a version string) or ICX is not connected, "
    "ignore this note."
)

_TESTING_DIRECTIVE = (
    "[ICX TESTING - MANDATORY WHEN APPLICABLE]\n"
    "This request looks like testing an app/screen/UI/API (test, write tests, check a screen/flow, "
    "QA, coverage). IF ICX is connected, call mcp__icx__start_testing_session FIRST and drive "
    "testing through ICX - do not hand-roll tests or use another testing tool. IF this is not "
    "actually about testing, or ICX is not connected, ignore this note."
)

_SONAR_DIRECTIVE = (
    "[ICX CODE QUALITY - MANDATORY WHEN APPLICABLE]\n"
    "This request looks like SonarQube / code-quality (sonar, quality gate, code smells, "
    "vulnerabilities, static-analysis findings, coverage report). IF ICX is connected, use the ICX "
    "mcp__icx__sonar_* tools (start with sonar_status / sonar_report) - do not fetch Sonar data "
    "another way. IF this is not about code quality, or ICX is not connected, ignore this note."
)

_WORKSTATUS_DIRECTIVE = (
    "[ICX WORKSTATUS - SUGGESTED WHEN APPLICABLE]\n"
    "This looks like a Workstatus time-tracking request (timesheet, clock in/out, time tracking). "
    "IF ICX is connected, PREFER its mcp__icx__workstatus_* tools for this. This is a SOFT "
    "preference, not a hard mandate - Workstatus coverage is partial (~24 endpoints; some actions "
    "may not be covered yet), so fall back to another approach if ICX doesn't support the specific "
    "action needed, rather than blocking the user. IF this is not actually about Workstatus, or ICX "
    "is not connected, ignore this note."
)


def _is_ticket(prompt):
    for m in _TICKET_RE.findall(prompt):
        if m.upper() not in _DENY:
            return True
    return bool(_URL_RE.search(prompt) or _REPO_ISSUE_RE.search(prompt))


# Conservative triviality: a purely conversational / acknowledgement / continuation message (thanks / ok /
# yes / continue / do it) does not warrant a boost - the hook stays silent so a simple reply is not forced
# through the boost flow. Any task verb or real question is NOT trivial.
_CONVO = {"thanks", "thank", "thankyou", "ty", "ok", "okay", "k", "kk", "cool", "nice", "great",
          "perfect", "awesome", "good", "fine", "yes", "yep", "yeah", "yup", "y", "no", "nope", "sure",
          "correct", "right", "exactly", "agreed", "agree", "done", "got", "it", "understood",
          "continue", "proceed", "go", "ahead", "keep", "going", "next", "please", "do", "pls", "plz",
          "looks", "sounds", "lgtm", "makes", "sense", "and", "then", "now", "yea", "ya", "hmm", "hm",
          "alright", "carry", "on", "for"}
_TRIVIAL_PHRASES = {"continue", "proceed", "go ahead", "go on", "keep going", "please continue",
    "continue please", "do it", "please do", "just do it", "looks good", "lgtm", "sounds good",
    "makes sense", "got it", "thanks", "thank you", "ok", "okay", "yes", "no", "sure", "perfect",
    "great", "nice", "cool", "yes please", "no thanks", "carry on", "next", "go for it", "please proceed"}
_TASK_HINT = re.compile(
    r"\b(fix|add|build|create|write|make|implement|refactor|debug|test|check|review|analyz|design|"
    r"optimi|update|change|remove|delete|explain|why|how|what|which|when|where|who|can you|could you|"
    r"should|is|are|does|error|bug|fail|slow|secure)\b", re.IGNORECASE)


def _is_trivial(prompt):
    t = str(prompt or "").strip().lower().rstrip(".!?, ")
    if not t:
        return True
    if t in _TRIVIAL_PHRASES:
        return True
    words = [w.strip(".,;:!?()[]{}\"'") for w in t.split()]
    words = [w for w in words if w]
    if len(words) > 5:
        return False
    if any(w not in _CONVO for w in words):
        return False
    return not bool(_TASK_HINT.search(t))


def main():
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
        prompt = str(data.get("prompt", ""))
        if not prompt.strip():
            return
        if _is_trivial(prompt):
            return   # conversational / continuation - nothing to route
        parts = []
        if _is_ticket(prompt):
            parts.append(_TICKET_DIRECTIVE)
        if _TESTING_RE.search(prompt):
            parts.append(_TESTING_DIRECTIVE)
        if _SONAR_RE.search(prompt):
            parts.append(_SONAR_DIRECTIVE)
        if _WORKSTATUS_RE.search(prompt):
            parts.append(_WORKSTATUS_DIRECTIVE)
        if not parts:
            return   # nothing to route - boost is on-demand only (/icx-boost), never injected here
        out = {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": "\n\n".join(parts),
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
    names = (_HOOK_FILENAME,) + _LEGACY_HOOK_FILENAMES
    for h in group.get("hooks", []):
        cmd = str(h.get("command", "")) if isinstance(h, dict) else ""
        if any(n in cmd for n in names):
            return True
    return False


def _write_hook_script() -> None:
    d = _icx_hooks_dir()
    d.mkdir(parents=True, exist_ok=True)
    _atomic_write(_hook_script_path(), _HOOK_SCRIPT)
    # Remove any superseded legacy hook script so only the current one remains.
    for legacy in _LEGACY_HOOK_FILENAMES:
        lp = d / legacy
        if lp.exists():
            try:
                lp.unlink()
            except OSError:
                pass


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
            "statusMessage": "ICX: routing your request...",
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


def _remove_claude_md_rule() -> bool:
    return _remove_rule_file(_claude_md_path())


def _install_rule_file(path: Path) -> None:
    """Insert or replace the marker-delimited ICX rule block in a generic global-rules file (Windsurf
    global_rules.md, Codex/Antigravity AGENTS.md, Cursor .mdc). Creates the file, preserves all other
    content, replaces the block in place on re-run. Same idempotent, merge-safe contract as CLAUDE.md."""
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


def _remove_rule_file(path: Path) -> bool:
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
    """Install ICX routing enforcement for a host (ticket/testing/sonar - NOT boost, which is the
    on-demand /icx-boost command installed separately by install_boost_command). Claude Code uses a
    hard pre-agent hook; the other hosts use their global rules file (instruction-based - they expose
    no pre-agent shell hook). No-op ([]) for non-enforcing hosts. Guarded per-step so a failure never
    aborts MCP registration."""
    if not host.enforces:
        return []
    messages: list[str] = []
    if host.enforce_kind == "hook":
        try:
            _write_hook_script()
            _install_settings_hook()
            messages.append(f"routing hook installed ({_claude_settings_path()})")
        except Exception as exc:
            messages.append(f"could not install routing hook: {exc}")
        try:
            _install_rule_file(_claude_md_path())
            messages.append(f"routing rule written to {_claude_md_path()}")
        except Exception as exc:
            messages.append(f"could not write routing rule: {exc}")
    elif host.enforce_kind == "rules" and host.rules_path is not None:
        try:
            _install_rule_file(host.rules_path)
            messages.append(f"routing rule written to {host.rules_path}")
            if host.rules_note:
                messages.append(host.rules_note)
        except Exception as exc:
            messages.append(f"could not write {host.label} rule: {exc}")
    return messages


def remove_enforcement(host: MCPHost) -> list[str]:
    """Remove ICX routing enforcement for a host. No-op ([]) for non-enforcing hosts."""
    if not host.enforces:
        return []
    messages: list[str] = []
    if host.enforce_kind == "hook":
        try:
            if _remove_settings_hook():
                messages.append("routing hook removed")
            for name in (_HOOK_FILENAME,) + _LEGACY_HOOK_FILENAMES:
                script = _icx_hooks_dir() / name
                if script.exists():
                    script.unlink()
        except Exception as exc:
            messages.append(f"could not remove routing hook: {exc}")
        try:
            if _remove_rule_file(_claude_md_path()):
                messages.append("routing rule removed from CLAUDE.md")
        except Exception as exc:
            messages.append(f"could not remove routing rule: {exc}")
    elif host.enforce_kind == "rules" and host.rules_path is not None:
        try:
            if _remove_rule_file(host.rules_path):
                messages.append(f"routing rule removed from {host.rules_path}")
        except Exception as exc:
            messages.append(f"could not remove {host.label} rule: {exc}")
    return messages
