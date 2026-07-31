"""
ICX MCP server - stdio transport.
Spawned by: icx mcp run
Communicates over: stdin/stdout (MCP JSON-RPC protocol)
"""
from __future__ import annotations
import asyncio
import functools
import json
import logging
import re
import threading as _threading
from concurrent.futures import ThreadPoolExecutor as _ThreadPoolExecutor
from pathlib import Path

_SAFE_KEY_RE = re.compile(r'^[A-Z][A-Z0-9]*-[0-9]+$')


def _validate_issue_key_arg(args: dict, arg_name: str) -> tuple[str | None, list[TextContent] | None]:
    """Shared required-field + PROJ-123-format validation, used by the 3 issue-key-shaped
    args in reinforce_memory_usage/get_memory_audit's dispatch (source_key/new_ticket_key/
    issue_key). Returns (cleaned_value, None) on success, or (None, error_response) - the
    caller does `value, err = _validate_issue_key_arg(args, "name"); if err: return err`.
    Error text/shape is byte-for-byte identical to what each call site duplicated before
    this helper existed - a plain {"error": ...} dict, no "ok" key, matching this
    dispatcher's own established convention."""
    value = args.get(arg_name, "")
    if not isinstance(value, str) or not value.strip():
        return None, [TextContent(type="text", text=json.dumps({"error": f"{arg_name} is required."}))]
    cleaned = value.strip()
    if not _SAFE_KEY_RE.match(cleaned.upper()):
        return None, [TextContent(type="text", text=json.dumps({"error": f"{arg_name} must be in PROJ-123 format."}))]
    return cleaned, None


_log = logging.getLogger(__name__)

MCP_MEMORY_TIMEOUT_SECONDS = 2.0

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent, Prompt, PromptArgument, PromptMessage, GetPromptResult

from icx_engine.config_manager import ConfigManager
from icx_engine import engine
from icx_engine.exceptions import (
    ICXError,
    IssueNotFound,
    AuthError,
    NoConnectionError,
    RateLimited,
    InvalidInput,
)
from icx_engine.skills.hints import attach_skill_hint
from icx_engine.skills.router import rank_skills, rank_skills_for_tags
from icx_engine.skills.storage import SkillStorage

server = Server("icx")

# ---------------------------------------------------------------------------
# Memory singleton - one shared MemoryManager, one dedicated thread.
#
# Why a dedicated thread with its own asyncio event loop:
#   LanceDB's sync API internally calls asyncio.get_event_loop(). When invoked
#   from run_in_executor(None, ...) (the default thread pool), Python 3.12+
#   raises RuntimeError because there is no running loop in that thread.
#   The initializer sets one up so LanceDB's shim finds it correctly.
#
# Why max_workers=1 / singleton MemoryManager:
#   Avoids reloading the 110 MB ONNX model on every search call. The model is
#   loaded once when the first search runs and stays resident for the process
#   lifetime. Single-threaded access also removes all LanceDB concurrency issues.
# ---------------------------------------------------------------------------

_MEMORY_EXECUTOR: _ThreadPoolExecutor | None = None
_MEMORY_EXECUTOR_LOCK = _threading.Lock()
_SHARED_MEMORY_MANAGER: "MemoryManager | None" = None  # created inside memory thread on first use

# Memory readiness state - transitions: cold -> warming -> ready | failed
# Tool responses read this to decide whether to submit a search or return immediately.
_memory_state: str = "cold"  # cold | warming | ready | failed
_memory_state_lock = _threading.Lock()
_memory_setup_required: bool = False  # True when prewarm failed because models weren't downloaded

# Session context - process-scoped, cleared on server restart.
# Accumulates work items analyzed in this session so subsequent calls
# see prior items as context. Keyed by issue_key; max _SESSION_MAX entries.
_SESSION_CONTEXT: list[dict] = []
_SESSION_MAX = 10

# Per-issue session data for causal chain and intelligence (Phase 8/10)
_SESSION_CONTEXT_DATA: dict[str, dict] = {}


def _session_set(issue_key: str, key: str, value) -> None:
    global _SESSION_CONTEXT_DATA
    if issue_key not in _SESSION_CONTEXT_DATA:
        _SESSION_CONTEXT_DATA[issue_key] = {}
    _SESSION_CONTEXT_DATA[issue_key][key] = value


def _session_get(issue_key: str, key: str, default=None):
    return _SESSION_CONTEXT_DATA.get(issue_key, {}).get(key, default)


def _get_memory_state() -> str:
    return _memory_state


def _set_memory_state(state: str) -> None:
    global _memory_state
    with _memory_state_lock:
        _memory_state = state


def _session_append(issue_key: str, summary: str, issue_type: str) -> None:
    global _SESSION_CONTEXT
    _SESSION_CONTEXT = (
        [e for e in _SESSION_CONTEXT if e["issue_key"] != issue_key]
        + [{"issue_key": issue_key, "summary": summary[:120], "issue_type": issue_type}]
    )[-_SESSION_MAX:]


def _init_memory_thread() -> None:
    asyncio.set_event_loop(asyncio.new_event_loop())


def _get_memory_executor() -> _ThreadPoolExecutor:
    global _MEMORY_EXECUTOR
    if _MEMORY_EXECUTOR is None:
        with _MEMORY_EXECUTOR_LOCK:
            if _MEMORY_EXECUTOR is None:
                _MEMORY_EXECUTOR = _ThreadPoolExecutor(
                    max_workers=1,
                    thread_name_prefix="icx-memory",
                    initializer=_init_memory_thread,
                )
    return _MEMORY_EXECUTOR


def _ensure_memory_manager():
    """Return the shared MemoryManager. Always called from inside the memory thread."""
    global _SHARED_MEMORY_MANAGER
    if _SHARED_MEMORY_MANAGER is None:
        from icx_engine.memory import MemoryManager
        _SHARED_MEMORY_MANAGER = MemoryManager()
    return _SHARED_MEMORY_MANAGER


def _save_memory_sync(entry) -> None:
    """Save a MemoryEntry via the shared manager. Runs inside the memory thread."""
    _ensure_memory_manager().save(entry)


def _show_entry_sync(issue_key: str):
    """Look up a saved MemoryEntry by issue_key. Runs inside the memory thread."""
    return _ensure_memory_manager().show(issue_key)


def _delete_entry_sync(issue_key: str) -> None:
    """Delete a memory entry via the shared manager. Runs inside the memory thread."""
    _ensure_memory_manager().delete(issue_key)


def _update_entry_sync(issue_key: str, fields: dict):
    """Update a memory entry via the shared manager. Runs inside the memory thread."""
    return _ensure_memory_manager().update(issue_key, **fields)


def _negate_resolution_sync(issue_key: str, reason: str) -> dict:
    """Negate a resolution. Runs inside the memory thread."""
    return _ensure_memory_manager().negate_resolution(issue_key, reason)


def _verify_resolution_sync(issue_key: str, feedback_note: str) -> dict:
    """Verify a resolution. Runs inside the memory thread."""
    return _ensure_memory_manager().verify_resolution(issue_key, feedback_note)


# ---------------------------------------------------------------------------
# Senior-persona planning layer (additive; prepended to _icx_next.instruction).
# ---------------------------------------------------------------------------

# The persona data + selectors live in the shared pure module `personas.py` (reused by the boost refine
# CTO-grade prompt). Aliased to the historical private names so this module + its tests are unchanged.
from icx_engine import personas as _personas

_PERSONA_SLUGS = _personas.PERSONA_SLUGS
_DEFAULT_PERSONA = _personas.DEFAULT_PERSONA
_UI_PERSONAS = _personas.UI_PERSONAS
_PERSONA_KEYWORDS = _personas.PERSONA_KEYWORDS
_UI_VOCAB = _personas.UI_VOCAB
_BACKEND_VOCAB = _personas.BACKEND_VOCAB
_PERSONA_PROFILE = _personas.PERSONA_PROFILE
_kw_hit = _personas.kw_hit


def _persona_text(analysis: dict) -> str:
    parts = [
        str(analysis.get("problem_summary") or analysis.get("summary", "")),
        str(analysis.get("detailed_description") or analysis.get("description", "")),
        str(analysis.get("impact", "")),
    ]
    return " ".join(parts).lower()


def _persona_text(analysis: dict) -> str:
    parts = [
        str(analysis.get("problem_summary") or analysis.get("summary", "")),
        str(analysis.get("detailed_description") or analysis.get("description", "")),
        str(analysis.get("impact", "")),
    ]
    return " ".join(parts).lower()


def _keyword_persona(text: str, issue_type: str) -> str | None:
    for slug, kws in _PERSONA_KEYWORDS:
        if any(_kw_hit(text, kw) for kw in kws):
            return slug
    if issue_type.lower() == "epic":
        return "system-architect"
    return None


def _select_persona(analysis: dict) -> tuple[str, str]:
    """Return (persona_slug, source). source in {'llm','keyword','default'}.

    LLM pick wins when valid, except a UI pick with zero UI vocabulary and strong backend
    vocabulary is clamped to the keyword persona. No graph/file signal is available at
    analyze time, so the keyword heuristic operates on ticket text only.
    """
    text = _persona_text(analysis)
    issue_type = str(analysis.get("issue_type", ""))
    llm_pick = str(analysis.get("recommended_persona", "")).strip()

    if llm_pick in _PERSONA_SLUGS:
        if llm_pick in _UI_PERSONAS:
            has_ui = any(_kw_hit(text, w) for w in _UI_VOCAB)
            has_backend = any(_kw_hit(text, w) for w in _BACKEND_VOCAB)
            if not has_ui and has_backend:
                return (_keyword_persona(text, issue_type) or _DEFAULT_PERSONA, "keyword")
        return (llm_pick, "llm")

    kw = _keyword_persona(text, issue_type)
    if kw:
        return (kw, "keyword")
    return (_DEFAULT_PERSONA, "default")


_CONFIDENCE_GATE = 0.6
_COMPLETENESS_GATE = 0.5

def _persona_preamble(slug: str, confidence: float | None, completeness: float | None) -> str:
    title, focus = _PERSONA_PROFILE.get(slug, _PERSONA_PROFILE[_DEFAULT_PERSONA])
    lines = [
        "==============================================================================",
        f"OPERATING PERSONA: you are acting as a {title}.",
        "Hold every decision to that bar. Junior-level guessing is not acceptable for",
        f"this ticket. As a {title}, {focus}.",
        "",
        "SENIOR PLANNING RUBRIC - satisfy this before proposing the plan in the",
        "confirmation format below (it raises what must appear in your Approach):",
        "  1. For bugs: establish the root cause with concrete evidence before proposing a",
        "     fix. For features: pin the exact requirement and the interface/data contracts.",
        "  2. Consider at least two approaches and state why the chosen one wins.",
        "  3. State the blast radius and the affected callers (use the graph impact and",
        "     blast-radius steps already in this workflow).",
        "  4. Define done: the test strategy and how correctness will be verified.",
        "  5. Name the risks, edge cases, failure modes, and the rollback.",
    ]
    gate_hit = (
        (confidence is not None and confidence < _CONFIDENCE_GATE)
        or (completeness is not None and completeness < _COMPLETENESS_GATE)
    )
    if gate_hit:
        lines.append(
            "  6. CONFIDENCE GATE: this ticket scored low on clarity/completeness. You MUST"
        )
        lines.append(
            "     ask the user targeted clarifying questions before presenting a plan - do"
        )
        lines.append("     not guess.")
    lines.append(
        "=============================================================================="
    )
    return "\n".join(lines)


def _apply_persona(analysis: dict | None, icx_instruction: str) -> tuple[str, dict | None]:
    """Prepend the persona preamble to icx_instruction. Fully guarded: any failure
    returns the instruction unchanged and persona=None, so analyze_issue can never break."""
    try:
        if not isinstance(analysis, dict):
            return icx_instruction, None
        slug, source = _select_persona(analysis)
        conf = analysis.get("confidence_score")
        comp = analysis.get("completeness_score")
        conf = conf if isinstance(conf, (int, float)) else None
        comp = comp if isinstance(comp, (int, float)) else None
        preamble = _persona_preamble(slug, conf, comp)
        return preamble + "\n\n" + icx_instruction, {"role": slug, "source": source}
    except Exception:
        _log.warning("persona layer skipped", exc_info=True)
        return icx_instruction, None


def _apply_dod(analysis: dict | None, icx_instruction: str, graphs: list | None) -> tuple[str, dict | None]:
    """Append a Definition-of-Done VERIFY phase + checklist to the instruction. Guarded: any
    failure returns the instruction unchanged and dod=None so analyze_issue never breaks.
    Risk tier + recommended layers are a RECOMMENDATION; the user selects at the gate."""
    try:
        if not isinstance(analysis, dict):
            return icx_instruction, None
        from icx_engine.verification import build_dod_checklist, compute_risk_tier, recommend_layers
        checklist = build_dod_checklist(analysis)
        tier = compute_risk_tier(analysis, graphs)
        layers = recommend_layers(tier)
        lines = [
            "",
            "==============================================================================",
            "DEFINITION OF DONE - you MUST verify with evidence before declaring done:",
            f"  Recommended verification (risk tier: {tier}) - the user chooses which to run: "
            + ", ".join(layers),
            "  Checklist (each must be proven, not asserted):",
        ]
        for i, it in enumerate(checklist, 1):
            lines.append(f"    {i}. [{it['method']}] {it['check']}")
        lines += [
            "  Flow: reproduce (bug) / set up check (feature) -> implement -> EXECUTE the check ->",
            "  capture the exact command + output -> adversarial self-review of the diff -> done.",
            "  Then call record_verification with the evidence. save_memory will refuse a verified",
            "  success without it (or pass verified_by_human=true if you verified manually).",
            "==============================================================================",
        ]
        block = "\n".join(lines)
        dod = {"risk_tier": tier, "recommended_layers": layers, "checklist": checklist}
        return icx_instruction + "\n" + block, dod
    except Exception:
        _log.debug("dod layer skipped", exc_info=True)
        return icx_instruction, None


def _apply_methodology(analysis: dict | None, icx_instruction: str) -> tuple[str, dict | None]:
    """Prepend the MANDATORY ICX methodology one-pager to the instruction and return the per-ticket
    checklist for response['methodology']. Guarded - never breaks analyze."""
    try:
        from icx_engine.methodology import build_checklist, ONE_PAGER
        block = (
            "\n=============================================================================="
            "\nMANDATORY METHODOLOGY - follow this on EVERY ticket (call get_methodology for full detail):"
            f"\n{ONE_PAGER}"
            "=============================================================================="
        )
        return icx_instruction + "\n" + block, build_checklist(analysis)
    except Exception:
        _log.debug("methodology layer skipped", exc_info=True)
        return icx_instruction, None


def _write_attachment_files(result, issue_key_val: str) -> dict[str, dict[str, str]]:
    """Write full-text sidecars (<name>.full.md) and raw originals (<name>) for non-image
    attachments to the per-issue temp dir. Returns {filename: {full_text, raw}}. Fully guarded -
    any failure skips that entry and never raises."""
    attachment_paths: dict[str, dict[str, str]] = {}
    full_texts = getattr(result, "attachment_full_texts", None) or {}
    raw_b64 = getattr(result, "attachment_raw", None) or {}
    if not full_texts and not raw_b64:
        return attachment_paths
    import base64 as _b64
    from icx_engine.graph.storage import ensure_issue_temp_dir as _ensure_dir, safe_temp_filename
    try:
        a_dir = _ensure_dir(issue_key_val)
    except Exception:
        return attachment_paths
    _used: set[str] = set()
    for fname in sorted(set(full_texts) | set(raw_b64)):
        safe = safe_temp_filename(fname, _used)
        entry: dict[str, str] = {}
        ft = full_texts.get(fname)
        if ft:
            try:
                p = a_dir / (safe + ".full.md")
                p.write_text(ft, encoding="utf-8")
                entry["full_text"] = str(p)
            except Exception:
                pass
        rb = raw_b64.get(fname)
        if rb:
            try:
                p = a_dir / safe
                p.write_bytes(_b64.b64decode(rb))
                entry["raw"] = str(p)
            except Exception:
                pass
        if entry:
            attachment_paths[fname] = entry
    return attachment_paths


# Strong reference to the background sweep task - without it the event loop keeps only a weak
# ref and the task can be GC'd mid-flight ("Task was destroyed but it is pending!").
_SWEEP_TASK: "asyncio.Task | None" = None


async def _periodic_temp_sweep(interval_seconds: int = 3600) -> None:
    """Background daemon: sweep stale temp dirs (24h TTL) every interval. Guarantees cleanup
    even when ICX is idle, for the lifetime of the MCP server. Guarded and non-fatal."""
    from icx_engine.graph.storage import sweep_stale_temp_dirs
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            sweep_stale_temp_dirs()
        except Exception:
            _log.debug("periodic temp sweep failed", exc_info=True)


# ---------------------------------------------------------------------------
# Tool names
# ---------------------------------------------------------------------------

_FAST_TOOL_NAME = "analyze_issue_fast"
_FULL_TOOL_NAME = "analyze_issue"
_MEM_SEARCH_TOOL = "memory_search"
_GRAPH_CONTEXT_TOOL = "graph_find_context"
_GRAPH_SUBSYSTEM_TOOL = "graph_subsystem"
_GRAPH_CHAIN_TOOL = "graph_call_chain"
_GRAPH_IMPACT_TOOL = "graph_impact"
_GRAPH_CROSS_LINKS_TOOL = "graph_cross_links"
_GRAPH_IMPORTANT_NODES_TOOL = "graph_important_nodes"
_MEM_HOTSPOTS_TOOL = "memory_get_hotspots"
_MEM_BY_FILE_TOOL = "memory_find_by_file"
_MEM_RELATED_TOOL = "memory_get_related"
_MEM_PATTERNS_TOOL = "memory_get_patterns"
_SAVE_TOOL_NAME = "save_memory"
_RECORD_VERIFICATION_TOOL = "record_verification"
_GET_METHODOLOGY_TOOL = "get_methodology"
_LOCK_PLAN_TOOL = "lock_plan"
_BOOST_TOOL = "icx_boost"
_BOOST_REFINE_TOOL = "icx_boost_refine"
_BOOST_PROMPT_NAME = "icx-boost"
_SKILL_GET_TOOL = "icx_skill_get"
_SKILLS_INDEX_TOOL = "icx_skills_index"
_DRAFT_SKILL_TOOL = "draft_skill"
_CREATE_SKILL_TOOL = "create_skill"
_MEM_DELETE_TOOL = "memory_delete"
_MEM_UPDATE_TOOL = "memory_update"
_UI_AUTH_CAPTURE_TOOL = "ui_auth_capture"
_UI_AUTH_INLINE_TOOL = "ui_auth_inline"
_GRAPH_BLAST_RADIUS_TOOL = "graph_blast_radius"
_GRAPH_CYCLES_TOOL = "graph_cycles"
_GRAPH_DEAD_CODE_TOOL = "graph_dead_code"
_GRAPH_OWNERSHIP_TOOL = "graph_ownership"
_REINFORCE_TOOL_NAME = "reinforce_memory_usage"
_AUDIT_TOOL_NAME = "get_memory_audit"

# Testing session tools (LangGraph entry - local engine)
_TESTING_START_TOOL = "start_testing_session"
_TESTING_RESUME_TOOL = "resume_testing_session"
_TESTING_STATUS_TOOL = "get_testing_session_status"

# Background-task registry for testing-session gates that trigger real browser work (verify/heal,
# scored execution). A gate that answers within _TESTING_QUICK_TIMEOUT behaves exactly as before
# (inline result, no contract change). One that runs longer is detached into a tracked asyncio.Task
# so the MCP call returns immediately with status:"running" instead of blocking - the caller polls
# get_testing_session_status instead of staring at one opaque call with no way to tell "working"
# from "stuck". Keyed by session_id; best-effort only (an MCP server restart drops tracking, but
# get_testing_session_status still falls back to a plain checkpoint read in that case).
_TESTING_RUNNING: dict[str, "asyncio.Task"] = {}
_TESTING_ERRORS: dict[str, str] = {}
_TESTING_QUICK_TIMEOUT = 20.0


def _testing_task_done_cb(session_id: str, task) -> None:
    _TESTING_RUNNING.pop(session_id, None)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        _TESTING_ERRORS[session_id] = str(exc)


async def _testing_invoke_tracked(session_id: str, coro) -> bool:
    """Run a testing-graph coroutine (ainvoke) as a tracked background task. Returns True if it
    completed within the quick timeout - caller reads the result exactly as before. Returns False if
    it is still running past the timeout; the task keeps executing in the background (asyncio.shield
    means the wait_for timeout never cancels it) and get_testing_session_status polls it to done."""
    task = asyncio.create_task(coro)
    task.add_done_callback(functools.partial(_testing_task_done_cb, session_id))
    _TESTING_RUNNING[session_id] = task
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=_TESTING_QUICK_TIMEOUT)
        return True
    except asyncio.TimeoutError:
        return False


def _testing_gate_snapshot(session_id: str, snapshot) -> dict:
    """Shared shape for start/resume/status: extract the pending gate (if any) and done-ness from a
    LangGraph state snapshot. See resume_testing_session for why `not snapshot.next` alone is not a
    reliable done signal."""
    gate_data = {}
    has_interrupt = bool(snapshot.tasks and snapshot.tasks[0].interrupts)
    if has_interrupt:
        gate_data = snapshot.tasks[0].interrupts[0].value
    is_done = (not snapshot.next) and (not has_interrupt)
    vals = getattr(snapshot, "values", None) or {}
    return {
        "session_id": session_id, "done": is_done, "gate": gate_data,
        "status": vals.get("status"), "error": vals.get("last_error") if is_done else None,
    }

def _ICX_FALLBACK(kind: str, connect_cmd: str) -> str:
    """Graceful-fallback instruction when ICX supports an integration but it is not connected. ICX stays
    the preferred path; the agent reuses its own connector meanwhile, else proceeds normally - and always
    tells the user it is not enabled. Same 3-tier intelligence as the boost link handling."""
    return (
        f"ICX supports the {kind} integration but it is not connected. Do this, in order: "
        f"(1) TELL the user it is not enabled and to connect ICX (`{connect_cmd}`) - the preferred path; "
        f"(2) MEANWHILE, if you have your own {kind} tool/MCP connector, use it to get the data and "
        f"continue; (3) if neither is available, proceed with the normal flow and note that the ICX "
        f"{kind} integration is off. Do not fabricate the data."
    )


# Sonar code-quality tools (direct SonarQube reader, gated by sonar_enabled)
_SONAR_STATUS_TOOL = "sonar_status"
_SONAR_PROJECTS_TOOL = "sonar_projects"
_SONAR_BRANCHES_TOOL = "sonar_branches"
_SONAR_MEASURES_TOOL = "sonar_measures"
_SONAR_QUALITY_GATE_TOOL = "sonar_quality_gate"
_SONAR_FINDINGS_TOOL = "sonar_findings"
_SONAR_REPORT_TOOL = "sonar_report"
_SONAR_TOP_FILES_TOOL = "sonar_top_files"
_SONAR_HISTORY_TOOL = "sonar_history"
_SONAR_ANALYSES_TOOL = "sonar_analyses"
_SONAR_RULE_TOOL = "sonar_rule"
_SONAR_RULES_TOOL = "sonar_rules"
_SONAR_HOTSPOT_TOOL = "sonar_hotspot"
_SONAR_SOURCE_TOOL = "sonar_source"
_SONAR_METRICS_TOOL = "sonar_metrics"
_SONAR_QUALITY_GATE_DEFINITION_TOOL = "sonar_quality_gate_definition"
_SONAR_QUALITY_PROFILES_TOOL = "sonar_quality_profiles"
_SONAR_ISSUE_AUTHORS_TOOL = "sonar_issue_authors"
_SONAR_ISSUE_TAGS_TOOL = "sonar_issue_tags"
_SONAR_ISSUE_CHANGELOG_TOOL = "sonar_issue_changelog"
_SONAR_SYSTEM_HEALTH_TOOL = "sonar_system_health"
_SONAR_LANGUAGES_TOOL = "sonar_languages"

# Shared input schema for the two scoped Sonar tools (findings + report).
_SONAR_SCOPE_SCHEMA = {
    "type": "object",
    "properties": {
        "project": {"type": "string"},
        "branch": {"type": "string"},
        "files": {"type": "array", "items": {"type": "string"}},
        "types": {"type": "array", "items": {"type": "string"}},
        "severities": {"type": "array", "items": {"type": "string"}},
        "statuses": {"type": "array", "items": {"type": "string"}},
        "rules": {"type": "array", "items": {"type": "string"}},
        "tags": {"type": "array", "items": {"type": "string"}},
        "author": {"type": "string"},
        "assignee": {"type": "string"},
        "new_code_only": {"type": "boolean"},
        "limit": {"type": "integer"},
    },
    "required": ["project"],
}


def _sonar_opt_str(args: dict | None, key: str) -> str | None:
    val = (args or {}).get(key)
    if isinstance(val, str) and val.strip():
        return val.strip()
    return None


def _sonar_require_project(args: dict | None) -> str | None:
    return _sonar_opt_str(args, "project")


def _sonar_str_list(args: dict | None, key: str) -> list[str]:
    val = (args or {}).get(key)
    if not isinstance(val, list):
        return []
    return [x.strip() for x in val if isinstance(x, str) and x.strip()]


def _sonar_scope_args(args: dict | None) -> dict | None:
    project = _sonar_require_project(args)
    if project is None:
        return None
    a = args or {}
    limit = a.get("limit")
    if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        limit = 1000
    return {
        "project": project,
        "branch": _sonar_opt_str(args, "branch"),
        "files": _sonar_str_list(args, "files"),
        "types": _sonar_str_list(args, "types"),
        "severities": _sonar_str_list(args, "severities"),
        "statuses": _sonar_str_list(args, "statuses"),
        "rules": _sonar_str_list(args, "rules"),
        "tags": _sonar_str_list(args, "tags"),
        "author": _sonar_opt_str(args, "author"),
        "assignee": _sonar_opt_str(args, "assignee"),
        "new_code_only": bool(a.get("new_code_only")),
        "limit": limit,
    }

_TESTING_START_DESCRIPTION = """\
Begin a testing session for a set of changed files. <- you are here: [2] automated or [1] manual

ICX expands file_paths via the codebase graph (blast_radius + subsystem cluster + co-change partners
+ semantic find_context) and starts a LangGraph session. Gate 1 fires immediately after.

RETURNS: {session_id: "uuid", gate: {gate: 1, ...}}
Pass session_id to ALL subsequent resume_testing_session calls. Never reuse a session_id.
graph_available: false means codebase graph not built - only seed files shown at Gate 1.

file_paths: FRONTEND/UI source SEED file(s) for the screen being tested (.js/.jsx/.tsx/.vue).
  The UI verification layer needs frontend screen files, NEVER backend files
  (.java/.py/.go/.cs). ICX expands whatever seeds you pass; you only need to produce the seed.

  PHASE A - PICK THE SEED UI FILE(S) before calling this tool. Ask the user how to choose:
    "How do you want to pick the UI files to test?
       1. Give me the UI endpoint/route you mapped (e.g. /work-order/broadcast or the screen URL).
       2. Use the files you changed."
    Then resolve seeds by grep (no path guessing):
      - Option 1 (endpoint/route): grep the codebase for the route/path string the user gave;
        the .jsx/.tsx/.js file(s) that reference it are the seeds.
      - Option 2, changed set INCLUDES UI files: those UI files are the seeds.
      - Option 2, changed set is BACKEND-ONLY: grep the changed backend file for its API
        route/path string (e.g. the @RequestMapping / route constant), then grep the UI repo
        for that same string; the UI file(s) that call it are the seeds.
    Pass the resolved frontend seed(s) as file_paths. ICX expands them to the full screen at
    Gate 1 (graph), or you grep imports as a fallback when graph_available is false (see resume).
    NEVER pass backend-only files. If grep finds no UI seed, ask the user for the screen file(s).
context: one-line description of what changed - used for graph expansion and shown at all gates.
test_mode: REQUIRED - "automated" or "manual". You MUST ask the user before calling this tool.
max_iterations: max automated fix loops before Limit Gate. Default from config (3).
nl_intent / acceptance_criteria: optional, seed extra NL/ticket-driven scenarios (agent test_type).

DEFAULT POSTURE - ICX gate data is for the USER to read and decide. You never advance the
workflow on your own except to GENERATE the test spec at Gate 2b. Everywhere else: present the
data, ask the user, wait for their reply, then act on it.

================================================================================
THESE ARE HARD RULES. THEY ARE NOT SUGGESTIONS. VIOLATIONS ARE NOT ACCEPTABLE.
================================================================================

RULE 0 - ASK MODE FIRST, BEFORE ANY TOOL CALL, NO EXCEPTIONS:
When development is complete, you MUST ask the user:
  "Do you want automated testing (ICX runs it) or manual testing (you run it yourself)?"
Do NOT call start_testing_session. Do NOT read any file.
This is the FIRST action. Skipping it or calling any tool before getting the answer is a CRITICAL VIOLATION.

RULE 1 - AUTOMATED PATH: after the user says "automated", call
  start_testing_session(test_mode="automated"). ICX runs the verification suite locally and async;
  there is no external tester and no health check.

RULE 2 - MANUAL PATH: after the user says "manual", call
  start_testing_session(test_mode="manual") directly.

RULE 3 - EVERY GATE REQUIRES HUMAN INPUT BEFORE RESUMING. NO SKIPPING. NO AUTO-FILL:
  done: false means a gate is waiting. BEFORE calling resume_testing_session:
    Show the gate data to the user. Ask. Wait for their reply. Then resume.
  done: true means the session is complete. Stop calling resume_testing_session.
  Auto-filling any gate response without user input is a CRITICAL VIOLATION.
  Gate 1 specifically: ALWAYS show the file list and ask the user to confirm it.
  Never assume the seed file list is correct without user verification.
  Gate 2b specifically: Read ALL files completely before generating json_spec.
  Never generate json_spec without reading every file in gate.file_paths first.
  Follow gate.rules exactly - all required sections must be present.

RULE 4 - URL MUST BE USER-CONFIRMED AT GATE 3:
  Show the full URL to the user. Wait for explicit confirmation before responding to Gate 3.
  NEVER submit a URL you guessed, constructed, or assumed. Doing so is a VIOLATION.

RULE 5 - GATE ui_check IS MANDATORY BEFORE memory_save:
  After the test loop ends, Gate ui_check fires. Instruct the user to open the app and visually
  verify the UI. Wait for explicit confirmation. NEVER skip to memory_save without this. VIOLATION.

RULE 6 - GATE memory_save IS MANDATORY, NEVER SKIP:
  After ui_check, Gate memory_save fires. Always respond with {"save": "yes"}.
  The testing workflow is not complete until memory_save has been responded to. VIOLATION if skipped.

================================================================================

AUTOMATED TOOL SEQUENCE (all steps in exact call order):
  [1]  start_testing_session        [1-5s] <- you are here
  [2]  resume_testing_session       Gate 1 - file list confirmation
  [3]  resume_testing_session       Gate 2b / author_flow - AI authors the test flow (UI) - AGENT-GENERATE
  [4]  resume_testing_session       Gate 3 - layer selection + config (RULE 4: URL confirmation)
       --- ICX runs the local verification suite (unit/api/ui), no external tester ---
  [5]  resume_testing_session       Gate 4 - show issues, propose fixes to user
  [6]  resume_testing_session       Gate 5 - user confirms fixes applied
       (loop [4]-[6] until issues = 0 or Limit Gate fires)
  [7]  resume_testing_session       Gate ui_check - MANDATORY (RULE 5)
  [8]  resume_testing_session       Gate memory_save - MANDATORY (RULE 6)

MANUAL TOOL SEQUENCE (all steps in exact call order):
  [1]  start_testing_session(test_mode="manual")  <- you are here
  [2]  resume_testing_session       Gate 1 - file list confirmation
  [3]  resume_testing_session       Gate manual - user runs test, confirms done
  [4]  resume_testing_session       Gate manual_result - user reports result + issues
  [5]  resume_testing_session       Gate ui_check - MANDATORY (RULE 5)
  [6]  resume_testing_session       Gate memory_save - MANDATORY (RULE 6)

SIDE GATES (automated path only - fire when conditions are met):
  Error Gate ("error"): a verification run failed.
    options: retry / skip_iteration / end_session
  Limit Gate ("limit"): max iterations reached with issues remaining.
    options: continue (+3 iterations) / end_session
RUNTIME: 1-5 seconds for graph expansion, then Gate 1 interrupt.\
"""

_TESTING_RESUME_DESCRIPTION = """\
Resume a paused testing session at the next gate. <- you are here for gates [3]-[10] automated or [2]-[6] manual.

session_id: UUID from start_testing_session. REQUIRED on every call. Never omit.
response: object matching the current gate.gate value exactly. Use ONLY the format for that gate.

================================================================================
GATE POSTURE CLASSIFICATION - THE SINGLE SOURCE OF TRUTH. READ THIS FIRST.
Every gate is exactly ONE of two kinds. How you respond depends entirely on which.
================================================================================

USER-DECISION gates - the answer belongs to the USER. You MUST stop, show every field,
ask, and wait for the user's reply before responding. NEVER auto-fill, default, or assume:
    mode, pick_type, expand, compat_check, 2a, api_manual, 3, auth_gate,
    4, 5, error, limit, manual, manual_result, ui_check, memory_save

AGENT-GENERATE gates - the answer is YOURS to produce. You generate each fully and submit it
directly. You MUST NOT delegate these to the user or ask them to write them:
    2b, compat_scan, author_flow, expand_scan, analyze_screen, unit_author
    (2b: json_spec generation; compat_scan: file compatibility detection; author_flow: write AND RUN a
    real Playwright test yourself, self-healing until the checklist is covered; expand_scan: repo grep
    for related files; analyze_screen: framework Element Census; unit_author: write unit tests from
    the census)

DEFAULT POSTURE - ICX gate data is for the USER to read and decide. You never advance the
workflow on your own except to generate the spec at Gate 2b. Everywhere else: present the
data, ask the user, wait, then act.

================================================================================
THESE ARE HARD RULES. THEY ARE NOT SUGGESTIONS. VIOLATIONS ARE NOT ACCEPTABLE.
================================================================================

RULE 0 - NEVER AUTO-RESPOND TO A USER-DECISION GATE. HUMAN IN THE LOOP IS MANDATORY:
  For every USER-DECISION gate (see classification above):
    1. Display ALL data from gate to the user (file list, options, issues, URL - everything).
    2. Ask the user explicitly what they want.
    3. WAIT for the user to reply.
    4. Only AFTER the user replies: call resume_testing_session with their answer.
  Responding to a USER-DECISION gate using defaults, assumptions, or auto-fill WITHOUT user
  input is a CRITICAL VIOLATION. Even if the answer seems obvious - ASK.
  AGENT-GENERATE gates (2b, compat_scan, author_flow, expand_scan) are the opposite: you produce
  the output yourself and submit it directly - never hand these to the user. Your read, your generation.

RULEBOOK RULE - gate.rules is BINDING, read it every time:
  Many gates include gate.rules - the mandatory rulebook for that gate, loaded fresh from the
  user's ~/.icx/testing_rules/<gate>.md (path in gate.rules_path). This is the source of truth
  and OVERRIDES your assumptions, habits, and memory. Read gate.rules in full on every gate that
  carries it and obey it exactly. The user can edit these files to tighten the rules; treat the
  text you receive as law for that step. Never ignore it because you "already know" the gate.

RE-READ RULE - applies to every AGENT-GENERATE gate (2b, compat_scan, author_flow, expand_scan):
  You MUST open and read every file in file_paths fully, start to end, in that step. Earlier
  reads, summaries, or memory are STALE and forbidden as a basis - read each file again
  completely even if you read it before. Partial reading or relying on memory causes missed
  details and wrong output. Each of these gates requires you to return read_receipts: a list
  of {path, line_count, last_line} for every file you read in this step, as proof of a full read.

RULE 1 - CHECK gate.gate BEFORE RESPONDING:
  Read gate.gate from the previous call's return value.
  Use ONLY the response format listed below for that exact gate value.
  Using the wrong format silently drops fields and corrupts the session. That is a VIOLATION.

RULE 2 - NEVER SKIP A GATE:
  done: false + gate set means a gate is waiting. Call resume_testing_session immediately after
  user input.
  status: "running" (gate is null) means no gate is waiting yet - real browser work is still in
  progress. Call get_testing_session_status(session_id) to poll instead of calling this tool
  again; only come back to resume_testing_session once polling returns an actual gate.
  done: true means the session is over. Stop calling this tool.
  Calling this tool after done: true, calling it again while status is "running", or failing to
  call it when a real gate is waiting, are all VIOLATIONS.

RULE 3 - GATE 3 URL IS ALWAYS USER-CONFIRMED:
  Display the URL to the user. Wait for explicit confirmation.
  NEVER respond to Gate 3 with a URL the user did not confirm. That is a VIOLATION.

RULE 4 - GATE 4 IS DISPLAY-ONLY, ALWAYS FOLLOWED BY GATE 5:
  Gate 4 delivers issues. Show them all to the user and propose fixes.
  Respond with {} and immediately wait for Gate 5. NEVER skip Gate 5 after Gate 4.

RULE 5 - GATE ui_check REQUIRES REAL USER CONFIRMATION:
  Tell the user: "Open the app and visually check the UI. Tell me when it looks correct."
  NEVER respond with {"confirmed": true} until the user has explicitly said the UI looks correct.
  Assuming it looks fine without asking is a VIOLATION.

RULE 6 - GATE memory_save IS ALWAYS {"save": "yes"|"no"}:
  Always respond with {"save": "yes"}. Never skip or respond with {"save": "no"}.
  Skipping memory_save is a VIOLATION.

RULE 7 - confirmed_files AT GATE "expand" MUST BE FRONTEND/UI FILES ONLY:
  The UI verification layer is a UI tester. It uses source files to author test flows for screens.
  confirmed_files MUST be frontend files: .js .jsx .tsx .vue .html
  NEVER include backend files (.java .py .go .cs .rb .kt) in confirmed_files.
  If the user only provides backend files, ask: "Which UI screen file(s) test this feature?"
  Wait for the user to give frontend file paths. Use those.

RULE G-1 - NEVER AUTO-RESPOND TO A USER-DECISION GATE WITHOUT USER INPUT:
  For USER-DECISION gates (see GATE POSTURE CLASSIFICATION): show all gate data to the user,
  ask explicitly, wait for their reply, then respond. "The answer is obvious" is not a valid
  reason to skip asking. Ask anyway. (Gate 2b is AGENT-GENERATE - you produce it yourself.)

RULE G-2 - NEVER SKIP A REQUIRED ACTION IN THE WORKFLOW:
  If the workflow says read files: read them. If it says show options: show them.
  If it says call a tool: call it. "I already know" is NOT a valid skip condition.
  Skipping any required action is a CRITICAL VIOLATION regardless of confidence level.

RULE G-3 - NEVER SUBSTITUTE YOUR OWN JUDGMENT FOR THE INSTRUCTION:
  If the instruction says "generate exhaustive JSON" - generate exhaustive JSON.
  If the prompt says "include ALL fields" - include ALL fields.
  Do not decide what is "enough". Follow the instruction completely as written.
  Deciding the instruction is "mostly done" and stopping early is a CRITICAL VIOLATION.

RULE G-4 - WHEN IN DOUBT, DO MORE NOT LESS:
  If unsure whether a section should be included: include it.
  If unsure whether a selector is correct: extract it from the file and use it.
  If unsure whether a notification needs documenting: document it.
  Omission is always worse than over-inclusion for test spec generation.

================================================================================
GATE DISPLAY REQUIREMENTS - EVERY FIELD LISTED IS MANDATORY TO SHOW TO THE USER.
RESPONDING WITHOUT SHOWING EVERY FIELD = CRITICAL VIOLATION. NO EXCEPTIONS.
================================================================================

Gate "mode" - Test mode selection [USER-DECISION]:
  YOU MUST SHOW THE USER AND WAIT FOR THEIR REPLY - VIOLATION IF SKIPPED:
    "Select test mode:
       1. automated - Full automated pipeline (ICX runs the verification for you).
       2. manual    - You run the test manually and report results.
     What is your choice?"
  WAIT for reply. Response: {"choice": "automated"|"manual"}

Gate "pick_type" - Test type selection [USER-DECISION]:
  This is the ONLY gate that asks the test type. Gate 3 later just confirms the URL - it does NOT
  re-ask the type. Do not ask the user to re-pick the type anywhere else.
  YOU MUST SHOW THE USER AND WAIT FOR THEIR REPLY - VIOLATION IF SKIPPED:
    "Select test type:
       1. agent - YOU write a real Playwright test covering the screen's Element Census, run it
                  yourself, and self-heal until it passes (frontend, needs a URL).
       2. api   - REST endpoint test (backend, needs a URL).
       3. unit  - Run the repo's own unit tests (no URL, no running app).
     What is your choice?"
  WAIT for reply. Response: {"test_type": "agent"|"api"|"unit"}

Gate "known_screen" - Known-screen fast path [USER-DECISION, agent-type only, RARE]:
  This gate ONLY appears when ICX found a PROVABLY FRESH cached clearance of this EXACT screen from
  a prior session - every cached file byte-identical to then, AND a fresh check found no new related
  file. If it does not appear, there was no cache, it was stale, or a new file was found - in every
  one of those cases ICX already moved straight to expand_scan, no action needed from you.
  When it DOES appear, show the user: "Found a prior cleared run of this screen from gate.cached_at
  (gate.confirmed_files count files, gate.functionality_count functionalities, coverage
  gate.census_coverage). Reuse it and skip straight to URL/layer confirmation, or redo file discovery
  and the census from scratch?"
  WAIT for reply. Response: {"decision": "fast_path"|"rescan"}. Anything other than "fast_path" is
  treated as "rescan" - the normal expand_scan/expand/analyze_screen/compat_scan pipeline runs exactly
  as if there had been no cache.

Gate "expand_scan" - Related file discovery [AGENT-GENERATE]:
  This gate is YOURS to produce. Do NOT show it to the user or wait for their reply.
  Search the repository for files related to gate.seeds (importers, callers, same-feature
  components, and the route or page that renders them). Read with your own tools.
  gate.graph_expanded already lists files the graph found; add what the graph missed.
  Resume immediately with your findings.
  Response: {"related_files": [<repo paths>], "read_receipts": [{"path": "<p>", "line_count": <n>, "last_line": "<text>"}]}
  - related_files: list of file paths you found via your own repo search.
  - read_receipts: one entry per file you opened and read fully this step (path, total line count, text of the last line).
  - Do NOT include files already in gate.graph_expanded unless you independently confirmed them.
  - This is your search, not the user's. Produce it and submit it directly.

Gate "expand" - File confirmation [USER-DECISION]:
  ICX has expanded the seed file(s) you passed into the full related screen set
  (blast_radius + subsystem cluster + co-change + find_context).
  WHEN gate.graph_available IS FALSE, ICX could not expand (UI repo graph not built) and only
  the seeds are shown. In that case, BEFORE asking the user, expand the seeds yourself by grep:
  for each seed, grep its own imports AND grep the repo for files that import it (1-2 hops),
  and add those frontend files to the list you present. This is the no-graph fallback.

  YOU MUST SHOW ALL OF THIS TO THE USER - SKIPPING ANY LINE IS A VIOLATION:
    "Files ICX identified:
     Changed (what you modified): <list gate.changed_files one per line>
     Expanded by graph:           <list gate.expanded_files one per line>
     <if graph_available is false: 'Expanded by grep (graph not built): <files you grep-expanded>'>
     Graph available: <gate.graph_available>

     IMPORTANT: the UI verification layer is a UI tester. It needs FRONTEND files (.js .jsx .tsx .vue).
     Do NOT send backend files (.java .py .go .cs .rb .kt) to the UI layer.

     Which frontend/UI screen file(s) should be verified?
     Confirm this set, or add/remove files."
  WAIT for user reply. Use ONLY frontend files they confirm.
  NEVER include backend files in confirmed_files. That is a VIOLATION.
  (Seed selection - endpoint/route, changed UI, or backend->UI grep bridge - happened before
  start_testing_session; see that tool. Here you only expand + confirm.)
  Response: {"confirmed_files": ["abs/path/Screen.jsx", ...], "url": "<optional>"}
  This list IS the file set for the rest of the session - every later gate (analyze_screen,
  compat_scan, ...) only ever sees what you confirm here. Omit a file you want excluded; it will
  NOT reappear later. If you resume without confirmed_files, ICX keeps the full candidate list.

Gate "analyze_screen" - Element Census [AGENT-GENERATE]:
  ICX selected the framework-specific analyzer prompt (gate.analyzer_id / gate.analyzer_family) and
  put its FULL text in gate.analyzer_prompt. APPLY that prompt to the confirmed files and return its
  STRICT JSON census - EXACTLY the schema the prompt defines - wrapped as {"screen_model": {...}}.
  The census enumerates EVERY interactive element, field, validation, and message and reconciles the
  counts (coverageReport.reconciliation). This model is what makes authoring miss NOTHING - a missed
  census element is a missed test. If the reconciliation counts do not add up, ICX re-asks naming the
  shortfall; fix it and resubmit. Read every file fully first (RE-READ RULE).
  ICX ALSO LINTS the census structurally (agent-independent) and RE-ASKS on hard defects, so no agent's
  mistake slips through: (a) CREATE and EDIT/MODIFY must have DIFFERENT submit selectors - never copy
  one onto the other; (b) every create/edit form needs its own submit + trigger; (c) every field needs
  a domSelectors/selector; (d) no duplicate functionality ids. Soft advisories (a text field with no
  captured length/format constraint) are recorded, not blocking - but capture length/format from the
  code (maxLength/minLength/min/max/pattern, type email/tel/url/number) because the save uses them.
  (At authoring time ICX also crawls the LIVE screen and FUSES that discovered census with yours -
  the COMBINED census - so real rendered selectors/wizard-nav back your JS-hidden constraints. You
  only produce the source census here; the live crawl and merge are automatic.)
  Response: {"screen_model": { ...the analyzer prompt's strict JSON... }, "read_receipts": [...]}

Gate "unit_author" - Write unit tests from the census [AGENT-GENERATE]:
  For a unit test, ICX gives you the Element Census (gate.screen_model) enumerating every testable
  unit/routine/function of the module. WRITE COMPREHENSIVE tests covering EVERY one of them - happy
  path + edge/invalid/error cases + every validation - using YOUR editor to create the test files IN
  THE REPO (framework in gate.message, keyed to gate.analyzer_family: GoogleTest/Catch2 for C/C++,
  utPLSQL/tSQLt/pgTAP for SQL, pytest/JUnit/jest/go test/cargo/rspec/phpunit for language units). The
  runner discovers and runs them on the next step. Do not skip any censused unit. Confirm when done.
  Response: {"read_receipts": [...]}   (acknowledge; the tests you wrote are in the repo)

Gate "compat_scan" - File compatibility detection [AGENT-GENERATE]:
  This gate is YOURS to produce. Do NOT show it to the user or wait for their reply.
  Read EVERY file in gate.file_paths completely, right now, in this step.
  ICX does NOT judge compatibility and does NOT check your answer - completeness is entirely YOUR
  responsibility, so nothing may be left unexamined.

  COMPLETENESS - LEAVE NOTHING:
    Reason from first principles about everything a test physically must do for gate.test_type:
    reach the screen, locate each control, see it, interact with it as a real user would, and
    observe the result. Examine every interactive element and every state involved. There is NO
    fixed checklist - anything that could stop a deterministic test is in scope, and it is on you
    to think of it.

  FORBIDDEN - DEFERRING TO THE RUNNER:
    You may NOT pass anything by assuming the test tool, the browser-use agent, or Playwright will
    "work around it", "still manage", "figure it out", or be "less robust but fine". The runner's
    tolerance is never your excuse. If a real user or a deterministic test would struggle with a
    control as written, it IS a finding. "Probably works" / "should be ok" / "optional improvement"
    are NOT verdicts - if you are not certain a thing is cleanly testable as-is, it is a finding.

  REPORT, DO NOT DECIDE:
    Every concern, however small, becomes a finding: what you saw (path + line), why it impedes
    testing, and the concrete change you propose. You do NOT silently accept, skip, or drop anything.
    ICX routes your findings to the user at the compat_check gate; the USER decides each one, and you
    then execute exactly that decision.

  Response: {"all_compatible": true|false,
             "findings": [{"path": "<p>", "compatible": true|false,
                           "reasons": ["what you saw, with path:line"],
                           "required_changes": ["concrete edit you propose"]}],
             "read_receipts": [{"path": "<p>", "line_count": <n>, "last_line": "<text>"}]}
  - all_compatible: true ONLY if you genuinely found nothing by inspection - never by assuming the
    tool will cope.
  - findings: one entry per file; required_changes must be specific, actionable edits.
  - read_receipts: one entry per file you opened and read fully this step (path, total line count, text of the last line).
  - This is your read and your judgment - not the user's, and not ICX's.

Gate "compat_check" - Compatibility review [USER-DECISION]:
  Present the agent findings from the compat_scan you just completed. The user decides.
  YOU MUST SHOW ALL OF THIS TO THE USER - SKIPPING ANY = VIOLATION:
    "Compatibility issues detected (agent scan). Incompatible files:
     <for each entry in gate.incompatible: show path, reasons, and required_changes>

     Options:
       approve - Apply the required_changes to each incompatible file yourself, then resume.
                 ICX re-scans after you resume (compat_scan fires again to verify the fixes).
       reject  - Do not apply. Specify per-file: drop (remove from test set), manual (user tests
                 it by hand), or accept (test it as-is with no change - user knowingly accepts
                 the finding and keeps the file in the run)."
  WAIT for user reply. The user decides each file - never choose on their behalf.
  approve means you have ALREADY applied the required_changes to the source files; ICX re-scans.
  Response (approve): {"decision": "approve", "edited_files": ["<path>", ...]}
  Response (reject):  {"decision": "reject", "resolution": {"<path>": "drop"|"manual"|"accept"}}

Gate "2" - Detection mode + scope [automated only]:
  YOU MUST SHOW ALL FIELDS TO THE USER AND GET ANSWERS FOR EACH - SKIPPING ANY = VIOLATION:
    "DETECTION MODE - how the UI layer generates the test spec:
       1. auto_detect - the UI layer opens the URL with Playwright and scans live page fields.
                        App must be running and URL must be accessible.
       2. json_spec   - AI reads your JSX/TSX source files directly. No browser needed.
                        Use when URL requires VPN or auth.
     Default: <gate.defaults.mode shown as 1 or 2>. What is your choice?

     SCOPE - what to test:
       1. ticket - Only test functionality changed by the listed files. (recommended)
       2. full   - Full end-to-end test of the entire screen.
     Default: 1. ticket. What is your choice?

     MERGE FILES - combine multiple JSX files into one spec (shown only when >1 file):
       1. yes - Merge all files into one combined spec.
       2. no  - Separate spec per file.
     Default: <1 or 2>. What is your choice?

     URL: <gate.defaults.url or 'not set'>
     Required for auto_detect. Confirm current URL or provide a new one.

     Answer each (type 1 or 2 for each choice):"
  WAIT for user reply on ALL fields. Responding before getting all answers is a VIOLATION.
  Response: {"mode":"1"|"2"|"auto_detect"|"json_spec", "scope":"1"|"2"|"ticket"|"full",
             "merge_files":"1"|"2"|true|false, "url":"http://..."}

Gate "2a" - Detected fields confirmation [auto_detect only, fires before Gate 2b]:
  YOU MUST SHOW ALL OF THIS TO THE USER - SKIPPING ANY = VIOLATION:
    "The UI layer scanned the page. Review before generating the test spec:
     URL:         <gate.url>
     Page title:  <gate.page_title>
     Field groups detected (<gate.group_count> groups):
       <list gate.detected_groups one per line>
     Is the URL correct and are these the right fields?
     Confirm or provide a corrected URL."
  WAIT. Response: {"url": "<confirmed url>"}

Gate "2b" - JSON spec generation [both modes]:
  THIS GATE HAS STRICT MANDATORY RULES. VIOLATING ANY = CRITICAL VIOLATION. READ ALL BEFORE ACTING.

  RULE 2b-1 - READ ALL FILES FIRST, GENERATE SECOND. NO EXCEPTIONS:
    You MUST read every file in gate.file_paths completely before writing a single word of JSON.
    If a file exceeds 1000 lines, read it in chunks until the ENTIRE file is consumed.
    Do NOT begin generating json_spec until every listed file is fully read.
    "I already know what the file contains" is NOT a valid reason to skip reading. Read it.

  RULE 2b-2 - FOLLOW gate.rules EXACTLY. THE RULEBOOK IS THE SPECIFICATION:
    gate.rules contains the complete output format. Follow it without deviation.
    The output JSON MUST include ALL of these top-level sections - missing any = VIOLATION:
      - screenName, fileName, filePath, associatedFiles, moduleName, description
      - rootFile                    (fileName, filePath, describesUrl, containsTriggers[])
      - modalFiles[]
      - techStack                   (framework, stateManagement, uiLibrary[], notifications[], httpClient, caching)
      - functionalitySummaryTable   (ALL functionalities detected)
      - functionalities[]           (one entry per functionality, fully populated)
      - dependencyGraph
      - validationMatrix            (each entry: errorDisplayMode toast|inline|both)
      - apiMappingSummary           (each entry: callerFunction)
      - responseCodeMappingSummary
      - permissionsMatrix
      - modalsSummary
      - notificationsSummary
      - inlineErrorsSummary
      - loaderHandling
      - selectorAudit               (EVERY selector produced must appear here)
    Every functionalities[] entry and every field within it also has a required key set -
    gate.rules (from ~/.icx/testing_rules/2b.md) carries the full per-functionality and
    per-field checklist. Read gate.rules and satisfy it in full.
    Do not simplify, condense, rename, or reorder the structure. Use the prompt's exact format.

  RULE 2b-3 - FIELD SELECTORS ARE MANDATORY. NEVER LEAVE domSelectors AS []:
    For every field in every functionality, domSelectors MUST contain at least one working
    Playwright selector. Selection priority:
      1. id="..." -> use #id
      2. data-testid="..." -> use [data-testid="..."]
      3. placeholder="..." -> use input[placeholder="..."]
      4. name="..." (only if literally present in JSX) -> input[name="..."]
    Never guess selectors. Only use selectors you can see in the source files you read.

  RULE 2b-4 - NOTIFICATIONS AND INLINE ERRORS ARE MANDATORY:
    For every functionality that calls pushNotify / toast / NotificationManager:
      - Add a notifications.messages[] entry for each call site with the exact message text.
    For every functionality that sets an error state variable or shows field validation:
      - Add an inlineErrors.messages[] entry for each field with exact message text.
    "I don't see any notifications" is only valid if you actually read the full file and
    found zero pushNotify/toast/NotificationManager calls. Otherwise it is a VIOLATION.

  RULE 2b-5 - DO NOT INVENT A SHORTCUT SPEC:
    You are NOT permitted to produce a simplified spec based on your own judgment.
    The output format is dictated entirely by gate.rules.
    Any deviation - any section omitted, any field left empty without a real reason,
    any selector guessed instead of extracted - is a CRITICAL VIOLATION.

  RULE 2b-6 - TOKEN BUDGET DOES NOT JUSTIFY SKIPPING:
    If the output is large, produce it in full anyway.
    Never use context window size, token limits, or response length as an excuse to omit
    sections, truncate arrays, or abbreviate entries. Do more, not less.

  RULE 2b-7 - SUBMIT ONLY WHEN ALL CONDITIONS ARE MET:
    Do NOT respond with json_spec until you can confirm ALL of these:
      [x] Every file in gate.file_paths has been fully read
      [x] Every functionality detected in the source is documented
      [x] Every field has at least one Playwright selector in domSelectors
      [x] Every notification call site has an entry in notifications.messages[]
      [x] Every inline error has an entry in inlineErrors.messages[]
      [x] selectorAudit lists every selector used anywhere in the spec
      [x] All sections from RULE 2b-2 are present in the output
    If any condition is not met: keep reading and generating until it is.

  RULE 2b-8 - ICX ENFORCES COMPLETENESS. YOU WILL BE RE-ASKED:
    gate.rules (from ~/.icx/testing_rules/2b.md) lists the required top-level sections, the
    per-functionality keys, and the per-field keys. After you submit, ICX checks that each is
    present (top-level content sections must also be non-empty), including inside every
    functionalities[] entry and every field. If anything is missing, ICX re-asks you with
    gate.missing_sections naming the exact paths (e.g. functionalities[2].businessLogic,
    functionalities[0].fields[3].interactionPattern) - regenerate a COMPLETE spec, do not
    argue. ICX never silently submits an incomplete spec. Only if the user has reviewed and
    KNOWINGLY accepts an incomplete spec may you resume with accept_incomplete:true.

  Response: {"json_spec": "{ \"functionalitySummaryTable\": [...], \"functionalities\": [...], ... }", "read_receipts": [{"path": "<p>", "line_count": <n>, "last_line": "<text>"}]}
  Response (only after the user accepts an incomplete spec): {"json_spec": "{...}", "accept_incomplete": true, "read_receipts": [...]}

Gate "api_manual" - Manual API endpoint entry [USER-DECISION, api test type only]:
  YOU MUST SHOW THE USER AND WAIT FOR THEIR REPLY - VIOLATION IF SKIPPED:
    "Provide the API endpoint details for the test:
     Endpoint URL (e.g. https://api.example.com/v1/resource):
     HTTP method (GET/POST/PUT/PATCH/DELETE):
     Payload (JSON body or query params, leave empty if none):
     Payload type (json / form / none):"
  WAIT for all four answers. Never assume or pre-fill any field.
  Response: {"api_endpoint": "<url>", "api_method": "<method>",
             "api_payload": "<payload or empty>", "api_payload_type": "json"|"form"|"none"}

Gate "3" - URL confirmation [automated only]:
  The test type was ALREADY chosen at gate "pick_type" (gate.test_type). DO NOT re-ask it here.
  The layer that runs is gate.test_type by default (gate.recommended_layers). Extra layers in
  gate.optional_layers are OPTIONAL - only mention them if the user asks; never force a re-pick.
  For test_type "unit" there is NO URL - just confirm and proceed.
  SHOW THE USER:
    "You chose the '<gate.test_type>' test - that layer will run. Confirm the target URL:
     TARGET URL: <gate.current.url or 'NOT SET'>
     (unit needs no URL.) Reply 'accept' to run your chosen type, or list layers to override.
     For agent: you will run your test HEADLESS (hidden) by default. Ask if the user wants to WATCH
     it (visible browser); if yes, include visible:true. If visible, ALSO ask the user the SLOWMO
     pace in ms (how long to slow + pause on each step so they can follow) - DEFAULT 1000 (1s) when
     visible, 0 when headless - and pass it as slowmo."
  WAIT for user reply on ALL fields. Responding without all answers is a CRITICAL VIOLATION.
  (RULE 3: URL must be explicitly confirmed by user. Never submit a URL you assumed.)
  Response: {"layers":["unit","api",...],
             "url":"http://...",
             "visible": true|false,   (agent only - true = watch the browser)
             "slowmo": 1000}          (agent + visible only - ms slowed+paused per step; default 1000, headless forces 0)
  --- After this response ICX runs the local verification suite. This can take minutes - expect
  {"status": "running"} back and poll get_testing_session_status(session_id) rather than assuming
  a hang; see RUNNING in this tool's top-level RETURNS section. ---

Gate "auth_gate" - Authentication configuration [USER-DECISION]:
  YOU MUST SHOW THE USER AND WAIT FOR THEIR REPLY - VIOLATION IF SKIPPED:
    "Authentication required for the test target. Choose auth mode:
       public  - No login needed. Target is publicly accessible.
       reuse   - Reuse a previously stored session.
       capture - ICX opens a REAL browser; you log in BY HAND; ICX saves the session.
       inline  - You provide the APP credentials; ICX drives the login form and saves it.
     What is your choice?"
  capture: call the ui_auth_capture tool (url + file_paths); it opens a browser for MANUAL login -
    NEVER ask the user for their username/password in chat for capture.
  inline: call the ui_auth_inline tool (url + file_paths + username + password); ONLY inline collects
    credentials, and they go to ICX's browser process, never into chat history.
  reuse: uses the stored session for this project + host. public: no auth.
  Response (any mode, AFTER the capture/inline tool returns ok): {"auth_mode": "public"|"reuse"|"capture"|"inline"}
  Port drift: if gate.other_host_sessions is non-empty, the exact host has no session but this SAME
  project has one at a different port (a dev server auto-incrementing past a taken port is the usual
  cause). Show it to the user; reuse it with {"auth_mode": "reuse", "reuse_host": "<host>"} - cookie
  auth transfers across a port change, but localStorage/sessionStorage auth (common in SPAs) does NOT
  (origin-scoped including port), so warn the user and be ready to fall back to capture if the app
  still looks logged out.

Gate "author_flow" - write AND run a real Playwright test [AGENT-GENERATE, agent test type only]:
  This gate is YOURS to produce. Do NOT show it to the user or wait for their reply.
  gate.screen_model is the Element Census (COMBINED: live-DOM crawl fused with the source census,
  so every selector already resolves). gate.rules carries the mandatory checklist (RULEBOOK RULE
  applies - read it in full, every time) - CRUD lifecycle, validation, security (XSS/SQLi), a11y,
  error-handling, data safety. Follow it; it is binding.
  WRITE a real Playwright test file in the repo (your own editor tools) covering the checklist for
  every functionality in gate.screen_model. RUN it YOURSELF (your Bash tool) against ICX's OWN
  pinned Playwright install - gate.playwright gives {node, env: {NODE_PATH, PLAYWRIGHT_BROWSERS_PATH}}
  to use, so you run ICX's install, never a bare npx/global one. Point the run's JUnit reporter at
  gate.report_path (e.g. `playwright test <file> --reporter=junit --output=<gate.report_path>`).
  READ Playwright's own failures (real stack traces, real selector mismatches) and FIX YOUR OWN
  script, then re-run - repeat until the checklist is covered or you have confirmed a genuine
  application bug (report it as a finding, never force a false pass by weakening an assertion).
  BROWSER: gate.headless / gate.slowmo carry the user's visible/slowmo choice from gate 3 - launch
  your own browser context accordingly (headed + slowMo when the user asked to watch).
  AUTH: if gate.auth_mode is capture/inline/reuse, gate.storage_state is a Playwright storageState
  path - load it into your browser context and go straight to gate.url; do NOT author login steps.
  For a public app with no saved session, author real login steps yourself (read the actual form).
  Response: {"report_path": "<path you actually wrote the JUnit report to>",
             "test_file": "<path to the Playwright file you wrote>",
             "covered": ["<functionality names/ids from screen_model you covered>"],
             "findings": ["<genuine app bugs found, if any>"]}
  - This is your generation AND your execution - not the user's, not ICX's. Produce it, run it,
    self-heal it, submit the result directly.

Gate "4" - Issue review [automated only]:
  YOU MUST SHOW THE USER (VIOLATION IF SKIPPED):
    List every issue from gate.issues with: name, description, severity.
    Propose specific code fixes for each issue.
    Explain what needs to be changed and why.
  Respond with {} ONLY after presenting all issues and proposed fixes.
  (RULE 4: Gate 5 always follows Gate 4. Never skip Gate 5.)
  Response: {}

Gate "5" - Fix confirmation [automated only]:
  YOU MUST ASK THE USER (VIOLATION IF SKIPPED):
    "Have you applied the proposed fixes for this iteration?
     Approve this iteration to continue, or reject to stop fixing.
     If approved, list what was changed."
  WAIT for user reply.
  Response: {"approve_iteration": true|false, "fixes_applied": ["fix 1 description", ...]}

Gate "manual" - Manual test wait [manual path]:
  YOU MUST TELL THE USER (VIOLATION IF SKIPPED):
    "Run the test manually against the application now.
     Files in scope: <list file_paths one per line>
     Reply when you are finished."
  WAIT for user to say they are done. Response: {"done": true}

Gate "manual_result" - Manual result report [manual path]:
  YOU MUST SHOW ALL THREE FIELDS AND WAIT FOR USER ANSWERS - SKIPPING ANY = VIOLATION:
    "DID THE TEST PASS?
       1. yes - all functionality works correctly.
       2. no  - found issues.
     Your answer?

     ISSUES FOUND (list each issue on a new line, or leave empty if passed):

     NOTES (any additional observations, optional):"
  WAIT for reply on all three. Response:
    {"passed": "yes"|"no", "issues": ["issue 1", ...], "notes": "<text>"}

Gate "ui_check" - Visual UI verification [both paths]:
  YOU MUST SHOW THE USER AND WAIT FOR THEIR REPLY - VIOLATION IF SKIPPED:
    "Open the application and visually verify the UI now.
     Check: layout, navigation, all functionality touched by the changed files, error states.
     Files tested: <list file_paths>

     RESULT:
       1. yes - UI looks correct, everything is working as expected.
       2. no  - Found visual issues (describe them below)."
  WAIT for explicit user reply. Never assume UI is fine without asking. (RULE 5)
  Response: {"choice": "yes"|"no", "notes": "<optional>"}

Gate "memory_save" - Save session record [both paths]:
  YOU MUST SHOW THE FULL SUMMARY AND ASK - VIOLATION IF SKIPPED:
    "Test session summary:
     Files:      <list gate.summary.files>
     Mode:       <gate.summary.test_mode>
     Result:     <gate.summary.result>
     Iterations: <gate.summary.iterations>

     SAVE TO ICX TESTING HISTORY?
       1. yes - save this session record.
       2. no  - discard, do not save."
  WAIT for user reply. Never auto-save without asking. (RULE 6)
  Response: {"save": "yes"|"no"}

Gate "error" - verification run failed [automated only]:
  YOU MUST SHOW THE USER (VIOLATION IF SKIPPED):
    "Test stopped: <gate.message>
       1. retry          - Re-run the same verification.
       2. skip_iteration - Count this iteration as 0 issues and continue.
       3. end_session    - Stop testing and go to UI check."
  WAIT. Response: {"choice":"1"|"2"|"3"|"retry"|"skip_iteration"|"end_session"}

Gate "limit" - Max iterations reached [automated only]:
  YOU MUST SHOW THE USER (VIOLATION IF SKIPPED):
    "Reached max iterations (<gate.max_iterations>). <N> issues still found.
       1. continue    - Add 3 more iterations and keep testing.
       2. end_session - Stop testing and go to UI check."
  WAIT. Response: {"choice":"1"|"2"|"continue"|"end_session"}

RETURNS: {session_id, done: bool, gate: {gate: "...", message: "...", ...} | null}
done: false -> gate.gate is set -> use that gate's format above for the next call.
done: true  -> gate is null -> session complete, workflow finished.

RUNNING (real browser work - verify/heal, scored execution - can take minutes): instead of gate,
you may get {"session_id", "status": "running", "done": false, "gate": null, "poll": "..."}.
This means the call returned before the work finished - it is NOT stuck and NOT an error. Do:
  1. Tell the user ICX is still running (do not go silent - say what is happening).
  2. Call get_testing_session_status(session_id) to check progress. Space out polls (e.g. every
     15-30s) rather than hammering the tool - the work is bounded internally and will finish.
  3. Once status is no longer "running", the response has the normal {done, gate} shape above -
     resume from there exactly as usual.
  NEVER call resume_testing_session again while status is "running" - there is no gate waiting
  for an answer yet, and a stray resume on a still-executing session is rejected.

RUNTIME: <2s for most gates. Gate 3 response and any AGENT-GENERATE gate that follows a live-DOM
verify/heal pass can run long - expect "status": "running" and poll rather than assuming a hang.\
"""

_TESTING_STATUS_DESCRIPTION = """\
Poll a testing session that returned {"status": "running"} from start_testing_session or \
resume_testing_session. Cheap, read-only, safe to call repeatedly.

session_id: the same UUID from start_testing_session.

RETURNS one of:
  {"session_id", "status": "running", "done": false, "gate": null}
    Still executing. Wait and poll again (every 15-30s is enough - do not busy-poll).
  {"session_id", "done": false, "gate": {...}, "status": "..."}
    Finished and a new gate is waiting - handle it exactly like any resume_testing_session gate
    response (see resume_testing_session's GATE POSTURE CLASSIFICATION and per-gate rules).
  {"session_id", "done": true, "gate": null, "status": "...", "error": null|"..."}
    Session complete.
  {"error": "..."}
    session_id unknown/malformed, or the session's state could not be read.

Never call resume_testing_session while a status poll still returns "running" - only call it again
once you have a gate to answer.\
"""

# ---------------------------------------------------------------------------
# Tool descriptions
# ---------------------------------------------------------------------------

_FAST_DESCRIPTION = """\
ICX TOOL SEQUENCE - WORKFLOW ORDER (read this first):
  [1]  analyze_issue_fast / analyze_issue  <- you are here
  [2]  memory_search          [<1s]  MANDATORY after analysis - search with agent-generated tags
  [3]  graph_important_nodes  [~1s]  architectural hotspots - call first on unfamiliar codebase
  [4]  graph_find_context     [~5s]  MANDATORY - replaces grep/glob entirely
  [5]  graph_subsystem        [~2s]  expand one file to its full feature cluster
  [6]  graph_ownership        [~1s]  who owns these files - call when crossing team boundaries
  [7]  graph_call_chain       [~5s]  trace data flow through a specific component
  [8]  graph_impact           [~12s] MANDATORY before changing shared code
  [9]  graph_cross_links      [~2s]  microservices only - SKIP for monolith projects
  [10] graph_blast_radius     [~3s]  MANDATORY before committing - full scope + missing changes
  [11] graph_cycles           [~2s]  circular dependency audit - call when debugging imports
  [12] graph_dead_code        [~1s]  unused module detection - call during cleanup
  [13] memory_get_hotspots    [~1s]  fragile file ranking - call at start of investigation
  [14] memory_find_by_file    [<1s]  MANDATORY before editing each file
  [15] memory_get_related     [<1s]  hidden coupling - call after finding bug location
  [16] memory_get_patterns    [<1s]  systemic analysis - call for recurring bug categories
       --- LOCK THE PLAN before writing any code ---
  [17] lock_plan              [<1s]  MANDATORY - submit the files you will change; blocks on any
                                     high-signal file you missed (fuses graph+grep+semantic+memory)
       --- implement fix here, only after lock_plan returns ok AND explicit user approval ---
       --- ask: "How would you like to test? 1. automated  2. manual" ---
  [18] start_testing_session   [1-5s] begin test session (pass test_mode from user's answer)
  [19] resume_testing_session         respond to every gate in sequence until done: true
                                       (a "status":"running" reply means poll
                                        get_testing_session_status instead of resuming again)
       --- after testing confirms fix works ---
  [20] reinforce_memory_usage [<1s]  MANDATORY first if any memory_search result influenced your approach
  [21] save_memory                   MANDATORY - only after testing confirms fix works - always after [20]
  [22] draft_skill                   MANDATORY - immediately after [21], every time, even if skill_worthy=false
  [23] get_memory_audit        [<1s]  diagnostic only - when investigating why a result ranks unexpectedly

Runs AI analysis on issue text only - attachments are not downloaded or processed. \
Use when the issue description and comments contain sufficient context, or for quick triage.
Pipeline: tracker fetch -> AI analysis (text only) -> graph status -> memory (if warm).
Attachments listed in pending_images, pending_audio, pending_documents, or pending_unsupported \
for manual review. attachment_processing='text_only' in response when LLM is configured.
Runtime: under 45 seconds.

REQUIRED: You MUST include a progressToken in your request meta (_meta.progressToken). \
Without it the user sees no feedback during the wait. This is not optional.

project_paths - list of absolute codebase paths. Priority order:\n\
  1. User named specific repos -> resolve those paths only:\n\
     ["/home/alice/projects/auth-svc", "/home/alice/projects/ui"]\n\
  2. User named no specific repo -> pass []. ICX resolves from the ticket's tracker project key.\n\
  3. Cannot determine any path -> pass [].\n\
ASK the user for the path if not already given. Only pass a path if the user explicitly \n\
provided it or explicitly said yes to one you proposed. Otherwise pass [].\n\
NEVER guess or invent a path the user did not explicitly provide.\n\
NEVER auto-detect the editor workspace root. When uncertain, pass [].\

================================================================================
THESE ARE HARD RULES. THEY ARE NOT SUGGESTIONS. VIOLATIONS ARE NOT ACCEPTABLE.
ANY DEVIATION FROM ANY RULE BELOW (0-6) IS A CRITICAL VIOLATION. There are no exceptions, \
no judgment calls, no "this case is different". Follow every rule exactly, every time, \
regardless of how the user phrases the request.
================================================================================

RULE 0 - ANY TICKET MENTION = MANDATORY ICX CALL, NO EXCEPTIONS:
The instant the user's message contains a ticket key (e.g. PROJ-123), an issue/ticket URL, \
or ANY reference to a ticket, issue, bug, story, or task - by name, key, link, or description - \
you MUST call analyze_issue_fast or analyze_issue FIRST. This applies even if the user does \
NOT say "fix", "implement", "look into", "work on", or anything else - mentioning the ticket \
IS the instruction. You MUST NOT read code, search the repo, propose a fix, or give your own \
analysis before this call. This is a non-negotiable trigger, not a judgment call. Skipping \
this call for any ticket-shaped input is a CRITICAL VIOLATION.
ICX IS THE SOLE TRACKER INTERFACE - FOR EVERY ACTION, NOT JUST FETCHING. When ICX is \
available you MUST NOT connect to, suggest connecting to, install, or call ANY other MCP \
server or integration for ANY tracker action - fetching, searching, creating, updating, \
commenting, linking, attaching, assigning, watching, logging work, or looking up a \
project/user/field - this applies to every tracker without exception (no naming any one \
provider: any issue/ticket/project-management MCP at all). A create/search/lookup request \
has no ticket key yet, so it is NOT exempt from this rule just because analyze_issue_fast \
does not apply to it - ICX's own tracker tools (e.g. jira_create_issue, jira_search, \
jira_get_current_user) still are the only ones you may use. ICX handles all tracker content \
itself. If an ICX call returns a tracker error (auth, network, missing config), you MUST tell \
the user to configure ICX (e.g. `icx connect` / ICX config) and retry the ICX call - you MUST \
NOT route around ICX through another MCP or integration. Doing so is a CRITICAL VIOLATION.

RULE 1 - NO CODE BEFORE APPROVAL:
You MUST NOT write a single line of code, make any file edit, run any command, or begin \
any implementation step until you have presented the confirmation format below AND received \
an explicit "yes" or "proceed" from the user. Any action taken before that approval is a \
direct violation of this instruction regardless of how confident you are in the solution.

RULE 2 - MANDATORY CONFIRMATION FORMAT:
After reading the relevant files, you MUST output this format exactly and then STOP. \
Do not add commentary before or after it. Do not proceed until the user responds.

---
**Problem understood:** [1-2 sentence summary drawn from work_item.analysis]
**Goal:** [acceptance_criteria as bullet points, or problem_summary for bugs]
**Approach:** [your specific solution plan - exactly what you will change, add, or remove, \
and precisely why that change fixes the problem. Specific enough that the user can reject it \
and propose an alternative before you write a single line.]
**Files I will work with:**
  - path/to/file [role-tag] - one-line reason it is relevant
**Tools called for this ticket** (ALL 10 required; show result or documented skip for each):
  1. memory_search:                 [N results OR skipped: memory.status!='ready']
  2. graph_find_context:            [N files returned, top score X.XX - NO VALID SKIP]
  3. graph_subsystem(file):         [cluster name, N files OR skipped: brand-new-file]
  4. graph_call_chain(node):        [upstream/downstream summary OR skipped: brand-new-node]
  5. graph_impact(node):            [N dependents OR skipped: brand-new-isolated-file]
  6. graph_cross_links:             [N links OR skipped: confirmed-single-monolith]
  7. memory_get_hotspots:           [result summary OR skipped: memory.status!='ready']
  8. memory_find_by_file (per file):[result per file OR skipped: brand-new-file]
  9. memory_get_related:            [top strength=X OR skipped: memory.status!='ready']
  10. memory_get_patterns:          [pattern summary OR skipped: memory.status!='ready']
  Any entry left blank or skipped with a vague reason is a VIOLATION.
**Shall I proceed?**
---

RULE 3 - APPROVAL GATE:
The only trigger that allows you to begin implementation is the user explicitly saying yes \
or telling you to proceed. Silence, partial responses, or ambiguous replies do NOT count as \
approval. If unclear, ask again. Do not assume.

RULE 3b - SPEC-LOCK BEFORE CODE:
After you decide which files to change and BEFORE writing any code, call lock_plan with those files. \
It returns high-signal files you missed (graph/grep/semantic/memory). You MUST NOT write code until \
lock_plan returns ok - resolve each blocking_missed file by including it or justifying it. This is \
what prevents a wrong-scope first attempt.

RULE 4 - APPROACH CHANGE:
If the user requests a different approach, you MUST present the revised plan using the same \
confirmation format above and wait for approval again. You MUST NOT begin implementing the \
revised approach without a second explicit approval.

RULE 5 - TESTING GATE:
After implementation is complete, you MUST ask the user:\n\
  "How would you like to test this fix?\n\
   1. automated - ICX runs the local verification for you\n\
   2. manual    - you run it yourself and confirm the result"\n\
Do NOT call reinforce_memory_usage or save_memory yet. Do NOT skip this question.\n\
Based on the user's answer:\n\
  If 1 (automated): call start_testing_session [18], then resume all gates [19].\n\
  If 2 (manual):    call start_testing_session(test_mode="manual") [18], then resume all gates [19].\n\
Complete the full testing flow (all gates through memory_save).\n\
After testing session reaches done: true:\n\
  Show the user a clear summary of what happened (files tested, result, issues found or none).\n\
  Ask explicitly: "The fix has been tested. Shall I save this to ICX memory? (yes/no)"\n\
  WAIT for the user to say yes or no. NEVER call save_memory without this explicit confirmation.\n\
  If yes:\n\
    call reinforce_memory_usage [20] FIRST (if any memory_search result influenced your approach).\n\
    call save_memory [21] with all required fields.\n\
    call draft_skill [22] IMMEDIATELY after - mandatory, even if the honest judgment is skill_worthy=false.\n\
  If no: do not call save_memory. Session ends here.\n\
draft_skill [22] is the FINAL step, called right after save_memory [21]. Neither is skipped once testing completes and the user confirms.

RULE 6 - MANDATORY TOOL COMPLETENESS:
Before presenting the confirmation format, you MUST have called every tool whose skip condition is NOT met. \
Minimum required calls (check NOW before presenting):\n\
  graphs[0].status=='ready' AND memory.status=='ready':  9+ tools mandatory (items 1-6, 8-11; item 7 depends on project type)\n\
  graphs[0].status=='ready' AND memory.status!='ready':  4+ tools mandatory (items 3-6; item 7 depends on project type)\n\
  graphs[0].status!='ready' AND memory.status=='ready':  5 tools mandatory (items 1, 8, 9, 10, 11)\n\
  graphs[0].status!='ready' AND memory.status!='ready':  grep/glob only - none of items 1-11 apply\n\
memory_search (item 1) skips ONLY when memory.status != 'ready'. \
graph_find_context (item 3) has NO valid skip when graph is ready - calling it is not optional. \
memory_get_hotspots (item 8) and memory_get_patterns (item 11) skip ONLY when memory.status != 'ready' - \
NEVER skip them based on memory_search result count. \
memory_get_related (item 10) skips only when memory.status != 'ready'. Always pass files from graph_find_context results - works for new tickets. \
Presenting the confirmation block with any uncalled required tool is a VIOLATION. \
The 10-entry tool checklist is the evidence record - every entry must show a result or an EXACT skip condition from this rule.

================================================================================
WORKFLOW (follow in order, no skipping):
================================================================================
1. Identify relevant files using one of two options:
   OPTION A (recommended): Call graph_find_context with project_path from graphs[0].path and a task
   description derived from work_item.analysis. Returns all matching files ranked by relevance score.
   OPTION B (manual): Read graphs[0].report_path (pre-authorized) -> identify clusters from the
   compact index table -> read GRAPH_CLUSTERS/<name>.md -> read core files in listed order.
2. Apply RULE 2: present the confirmation format and STOP
3. Wait for explicit user approval (RULE 3)
4. Implement exactly the stated approach, using memory_search results as a pattern reference
5. Ask: "How would you like to test? 1. automated  2. manual" (RULE 5) - run full testing flow before any memory save
6. After testing session completes (done: true):
   Show summary. Ask: "Shall I save this to ICX memory? (yes/no)" WAIT for answer.
   If yes:
     a. call reinforce_memory_usage [20] FIRST if memory_search result was used
     b. call save_memory [21] with resolution_note, files_changed, root_cause_pattern, and all required fields
     c. call draft_skill [22] immediately after - mandatory, even if skill_worthy=false
   If no: stop. Do not call save_memory.\
"""

_FULL_DESCRIPTION = """\
ICX TOOL SEQUENCE - WORKFLOW ORDER (read this first):
  [1]  analyze_issue_fast / analyze_issue  <- you are here
  [2]  memory_search          [<1s]  MANDATORY after analysis - search with agent-generated tags
  [3]  graph_important_nodes  [~1s]  architectural hotspots - call first on unfamiliar codebase
  [4]  graph_find_context     [~5s]  MANDATORY - replaces grep/glob entirely
  [5]  graph_subsystem        [~2s]  expand one file to its full feature cluster
  [6]  graph_ownership        [~1s]  who owns these files - call when crossing team boundaries
  [7]  graph_call_chain       [~5s]  trace data flow through a specific component
  [8]  graph_impact           [~12s] MANDATORY before changing shared code
  [9]  graph_cross_links      [~2s]  microservices only - SKIP for monolith projects
  [10] graph_blast_radius     [~3s]  MANDATORY before committing - full scope + missing changes
  [11] graph_cycles           [~2s]  circular dependency audit - call when debugging imports
  [12] graph_dead_code        [~1s]  unused module detection - call during cleanup
  [13] memory_get_hotspots    [~1s]  fragile file ranking - call at start of investigation
  [14] memory_find_by_file    [<1s]  MANDATORY before editing each file
  [15] memory_get_related     [<1s]  hidden coupling - call after finding bug location
  [16] memory_get_patterns    [<1s]  systemic analysis - call for recurring bug categories
       --- LOCK THE PLAN before writing any code ---
  [17] lock_plan              [<1s]  MANDATORY - submit the files you will change; blocks on any
                                     high-signal file you missed (fuses graph+grep+semantic+memory)
       --- implement fix here, only after lock_plan returns ok AND explicit user approval ---
       --- ask: "How would you like to test? 1. automated  2. manual" ---
  [18] start_testing_session   [1-5s] begin test session (pass test_mode from user's answer)
  [19] resume_testing_session         respond to every gate in sequence until done: true
                                       (a "status":"running" reply means poll
                                        get_testing_session_status instead of resuming again)
       --- after testing confirms fix works ---
  [20] reinforce_memory_usage [<1s]  MANDATORY first if any memory_search result influenced your approach
  [21] save_memory                   MANDATORY - only after testing confirms fix works - always after [20]
  [22] draft_skill                   MANDATORY - immediately after [21], every time, even if skill_worthy=false
  [23] get_memory_audit        [<1s]  diagnostic only - when investigating why a result ranks unexpectedly

Fetches and analyzes a work item (bug, story, or task) with full vision and OCR processing \
for image attachments. Identifies relevant codebase files via graph navigation.
Pipeline: tracker fetch -> AI analysis -> vision processing -> memory search -> graph navigation.
Runtime: 20 seconds to several minutes, depending on attachment count and size.

REQUIRED: You MUST include a progressToken in your request meta (_meta.progressToken). \
Without it the user sees no feedback during the wait. This is not optional.

project_paths - list of absolute codebase paths. Priority order:\n\
  1. User named specific repos -> resolve those paths only:\n\
     ["/home/alice/projects/auth-svc", "/home/alice/projects/ui"]\n\
  2. User named no specific repo -> pass []. ICX resolves from the ticket's tracker project key.\n\
  3. Cannot determine any path -> pass [].\n\
ASK the user for the path if not already given. Only pass a path if the user explicitly \n\
provided it or explicitly said yes to one you proposed. Otherwise pass [].\n\
NEVER guess or invent a path the user did not explicitly provide.\n\
NEVER auto-detect the editor workspace root. When uncertain, pass [].\

================================================================================
THESE ARE HARD RULES. THEY ARE NOT SUGGESTIONS. VIOLATIONS ARE NOT ACCEPTABLE.
ANY DEVIATION FROM ANY RULE BELOW (0-6) IS A CRITICAL VIOLATION. There are no exceptions, \
no judgment calls, no "this case is different". Follow every rule exactly, every time, \
regardless of how the user phrases the request.
================================================================================

RULE 0 - ANY TICKET MENTION = MANDATORY ICX CALL, NO EXCEPTIONS:
The instant the user's message contains a ticket key (e.g. PROJ-123), an issue/ticket URL, \
or ANY reference to a ticket, issue, bug, story, or task - by name, key, link, or description - \
you MUST call analyze_issue_fast or analyze_issue FIRST. This applies even if the user does \
NOT say "fix", "implement", "look into", "work on", or anything else - mentioning the ticket \
IS the instruction. You MUST NOT read code, search the repo, propose a fix, or give your own \
analysis before this call. This is a non-negotiable trigger, not a judgment call. Skipping \
this call for any ticket-shaped input is a CRITICAL VIOLATION.
ICX IS THE SOLE TRACKER INTERFACE - FOR EVERY ACTION, NOT JUST FETCHING. When ICX is \
available you MUST NOT connect to, suggest connecting to, install, or call ANY other MCP \
server or integration for ANY tracker action - fetching, searching, creating, updating, \
commenting, linking, attaching, assigning, watching, logging work, or looking up a \
project/user/field - this applies to every tracker without exception (no naming any one \
provider: any issue/ticket/project-management MCP at all). A create/search/lookup request \
has no ticket key yet, so it is NOT exempt from this rule just because analyze_issue_fast \
does not apply to it - ICX's own tracker tools (e.g. jira_create_issue, jira_search, \
jira_get_current_user) still are the only ones you may use. ICX handles all tracker content \
itself. If an ICX call returns a tracker error (auth, network, missing config), you MUST tell \
the user to configure ICX (e.g. `icx connect` / ICX config) and retry the ICX call - you MUST \
NOT route around ICX through another MCP or integration. Doing so is a CRITICAL VIOLATION.

RULE 1 - NO CODE BEFORE APPROVAL:
You MUST NOT write a single line of code, make any file edit, run any command, or begin \
any implementation step until you have presented the confirmation format below AND received \
an explicit "yes" or "proceed" from the user. Any action taken before that approval is a \
direct violation of this instruction regardless of how confident you are in the solution.

RULE 2 - MANDATORY CONFIRMATION FORMAT:
After reading the relevant files, you MUST output this format exactly and then STOP. \
Do not add commentary before or after it. Do not proceed until the user responds.

---
**Problem understood:** [1-2 sentence summary drawn from work_item.analysis]
**Goal:** [acceptance_criteria as bullet points, or problem_summary for bugs]
**Approach:** [your specific solution plan - exactly what you will change, add, or remove, \
and precisely why that change fixes the problem. Specific enough that the user can reject it \
and propose an alternative before you write a single line.]
**Files I will work with:**
  - path/to/file [role-tag] - one-line reason it is relevant
**Tools called for this ticket** (ALL 10 required; show result or documented skip for each):
  1. memory_search:                 [N results OR skipped: memory.status!='ready']
  2. graph_find_context:            [N files returned, top score X.XX - NO VALID SKIP]
  3. graph_subsystem(file):         [cluster name, N files OR skipped: brand-new-file]
  4. graph_call_chain(node):        [upstream/downstream summary OR skipped: brand-new-node]
  5. graph_impact(node):            [N dependents OR skipped: brand-new-isolated-file]
  6. graph_cross_links:             [N links OR skipped: confirmed-single-monolith]
  7. memory_get_hotspots:           [result summary OR skipped: memory.status!='ready']
  8. memory_find_by_file (per file):[result per file OR skipped: brand-new-file]
  9. memory_get_related:            [top strength=X OR skipped: memory.status!='ready']
  10. memory_get_patterns:          [pattern summary OR skipped: memory.status!='ready']
  Any entry left blank or skipped with a vague reason is a VIOLATION.
**Shall I proceed?**
---

RULE 3 - APPROVAL GATE:
The only trigger that allows you to begin implementation is the user explicitly saying yes \
or telling you to proceed. Silence, partial responses, or ambiguous replies do NOT count as \
approval. If unclear, ask again. Do not assume.

RULE 3b - SPEC-LOCK BEFORE CODE:
After you decide which files to change and BEFORE writing any code, call lock_plan with those files. \
It returns high-signal files you missed (graph/grep/semantic/memory). You MUST NOT write code until \
lock_plan returns ok - resolve each blocking_missed file by including it or justifying it. This is \
what prevents a wrong-scope first attempt.

RULE 4 - APPROACH CHANGE:
If the user requests a different approach, you MUST present the revised plan using the same \
confirmation format above and wait for approval again. You MUST NOT begin implementing the \
revised approach without a second explicit approval.

RULE 5 - TESTING GATE:
After implementation is complete, you MUST ask the user:\n\
  "How would you like to test this fix?\n\
   1. automated - ICX runs the local verification for you\n\
   2. manual    - you run it yourself and confirm the result"\n\
Do NOT call reinforce_memory_usage or save_memory yet. Do NOT skip this question.\n\
Based on the user's answer:\n\
  If 1 (automated): call start_testing_session [18], then resume all gates [19].\n\
  If 2 (manual):    call start_testing_session(test_mode="manual") [18], then resume all gates [19].\n\
Complete the full testing flow (all gates through memory_save).\n\
After testing session reaches done: true:\n\
  Show the user a clear summary of what happened (files tested, result, issues found or none).\n\
  Ask explicitly: "The fix has been tested. Shall I save this to ICX memory? (yes/no)"\n\
  WAIT for the user to say yes or no. NEVER call save_memory without this explicit confirmation.\n\
  If yes:\n\
    call reinforce_memory_usage [20] FIRST (if any memory_search result influenced your approach).\n\
    call save_memory [21] with all required fields.\n\
    call draft_skill [22] IMMEDIATELY after - mandatory, even if the honest judgment is skill_worthy=false.\n\
  If no: do not call save_memory. Session ends here.\n\
draft_skill [22] is the FINAL step, called right after save_memory [21]. Neither is skipped once testing completes and the user confirms.

RULE 6 - MANDATORY TOOL COMPLETENESS:
Before presenting the confirmation format, you MUST have called every tool whose skip condition is NOT met. \
Minimum required calls (check NOW before presenting):\n\
  graphs[0].status=='ready' AND memory.status=='ready':  9+ tools mandatory (items 1-6, 8-11; item 7 depends on project type)\n\
  graphs[0].status=='ready' AND memory.status!='ready':  4+ tools mandatory (items 3-6; item 7 depends on project type)\n\
  graphs[0].status!='ready' AND memory.status=='ready':  5 tools mandatory (items 1, 8, 9, 10, 11)\n\
  graphs[0].status!='ready' AND memory.status!='ready':  grep/glob only - none of items 1-11 apply\n\
memory_search (item 1) skips ONLY when memory.status != 'ready'. \
graph_find_context (item 3) has NO valid skip when graph is ready - calling it is not optional. \
memory_get_hotspots (item 8) and memory_get_patterns (item 11) skip ONLY when memory.status != 'ready' - \
NEVER skip them based on memory_search result count. \
memory_get_related (item 10) skips only when memory.status != 'ready'. Always pass files from graph_find_context results - works for new tickets. \
Presenting the confirmation block with any uncalled required tool is a VIOLATION. \
The 10-entry tool checklist is the evidence record - every entry must show a result or an EXACT skip condition from this rule.

================================================================================
WORKFLOW (follow in order, no skipping):
================================================================================
1. Identify relevant files using one of two options:
   OPTION A (recommended): Call graph_find_context with project_path from graphs[0].path and a task
   description derived from work_item.analysis. Returns all matching files ranked by relevance score.
   OPTION B (manual): Read graphs[0].report_path (pre-authorized) -> identify clusters from the
   compact index table -> read GRAPH_CLUSTERS/<name>.md -> read core files in listed order.
2. If work_item.image_paths is present: read those image files directly for visual context \
   (access is pre-authorized, no permission prompt needed)
3. Apply RULE 2: present the confirmation format and STOP
4. Wait for explicit user approval (RULE 3)
5. Implement exactly the stated approach, using memory_search results as a pattern reference
6. Ask the user to test (RULE 5)
7. After the user confirms it works:
   a. If any memory_search result influenced your approach: call reinforce_memory_usage FIRST (source_key=that issue_key, new_ticket_key=current issue_key)
   b. Call save_memory with resolution_note, files_changed, root_cause_pattern, and all required fields\
"""

_SAVE_DESCRIPTION = """\
Commits a confirmed fix to local memory. Future agents retrieve this when working on similar issues.

CALL GATE - do NOT call this tool unless ALL of the following are true:
1. Fix is fully implemented.
2. You asked the user to test it.
3. User explicitly confirmed it is working.
4. If memory_search returned a result that influenced your approach: reinforce_memory_usage was called first.
Calling speculatively, before user confirmation, or before reinforce_memory_usage is a violation.

FIELD REQUIREMENTS - every field you write is read by a future agent under time pressure:

summary: Your synthesized title of the root problem. NOT the raw Jira summary.
  Describe what was actually wrong at the code level, not the symptom.
  GOOD: "JWT expiry check uses < instead of <= rejecting tokens at exact expiry second"
  BAD: "JWT auth broken" / "Login not working"

problem_description: Your root cause analysis. Cover the exact failure mechanism, affected \
file(s)/function(s), and what condition triggered the bug. This is embedded for semantic search - \
precise agent-written text retrieves better than raw tracker descriptions.
  GOOD: "auth/middleware.py:validate_token() used strict less-than on token.exp. Tokens valid \
until their exact expiry second were rejected because exp == now evaluated false. Only manifested \
on requests arriving within the same second as expiry."
  BAD: "Token expiry was broken." / "Auth middleware had a bug."

resolution_note: What was changed, where, and the mechanical reason it works.
  GOOD: "Changed < to <= in validate_token() in auth/middleware.py line 47. Added 5s clock-skew \
buffer via TOKEN_SKEW_SECONDS env var to handle distributed clock drift."
  BAD: "Fixed the check." / "Updated middleware."

files_changed: Every file path you modified. Drives structural relation detection and hotspot \
analysis - incomplete lists degrade future recall.

tags: 3-6 specific lowercase tags covering problem domain, affected system layer, failure type. \
PRIMARY retrieval signal - this memory surfaces only when tags match a future query.
  GOOD: ["jwt-expiry", "auth-middleware", "token-validation", "clock-skew"]
  BAD: ["auth", "bug", "fix", "backend"]
  Rules: no generic words (bug/fix/error/issue/update), hyphenate multi-word, no duplicates.

work_item_type: Pass the exact value from work_item.type in the analyze_issue response. \
This is the tracker-authoritative issue type - do not infer or substitute.

root_cause_pattern: REQUIRED. One value from the 21-value canonical enum. This is the root cause \
classification that makes memory self-improving across tickets.
  VALID: "stale_cache_reference", "missing_null_check", "incorrect_transaction_boundary",
         "event_race_condition", "schema_drift", "auth_scope_mismatch", "async_context_leak",
         "missing_index", "type_coercion_error", "config_env_mismatch", "missing_idempotency",
         "cascade_delete_missing", "n_plus_one_query", "memory_leak", "timeout_misconfiguration",
         "pagination_boundary_error", "deserialization_contract_break", "feature_flag_state_leak",
         "tenant_isolation_breach", "retry_storm", "uncategorized"
  Use "uncategorized" only when none of the specific patterns fit. Specific > generic.
  GOOD: "auth_scope_mismatch" for a JWT permissions bug. BAD: "uncategorized" for everything.

pattern_confidence: REQUIRED. Your certainty about root_cause_pattern (0.0-1.0). \
  1.0 = you are certain. 0.5 = plausible. 0.0 = guessing.

outcome_verified: Set true ONLY when the developer has explicitly confirmed the fix worked in their environment. \
NEVER set speculatively. When true, outcome_feedback_note is required.

outcome_feedback_note: Required when outcome_verified=true. Describe what confirmed the fix: \
  GOOD: "Deployed to staging, 100 requests all passed, no 401 errors seen in logs."
  BAD: "Fixed." / "Works."

negate + negation_reason: Set negate=true when the developer confirms the fix was WRONG or caused a regression. \
negation_reason is required. ICX automatically propagates a credibility penalty to all entries that cited \
this resolution - the wrong answer will never surface again.
  GOOD negation_reason: "Caused a deadlock in concurrent requests - the lock was too broad."
  BAD: "Didn't work."

OUTCOME FEEDBACK WORKFLOW:
  Fix confirmed working -> call save_memory with outcome_verified=true, outcome_feedback_note="..."
  Fix confirmed wrong   -> call save_memory with negate=true, negation_reason="..."
  negate=true AND outcome_verified=true in the same call is a validation error - ICX will reject it.\
"""

_REINFORCE_DESCRIPTION = """\
CALL BEFORE save_memory - every time a past memory_search result influenced your approach on a new ticket.
USE WHEN: memory_search returned results AND any result shaped your implementation plan, \
approach direction, or confirmed a diagnosis. Call even if you modified the approach slightly.
MANDATORY: skipping this call is a violation when memory_search was useful. \
This is how ICX memory becomes self-improving - cited resolutions surface first on future similar tickets.

RETURNS: {source_key, usage_count, cross_reference_boost, siblings_updated}
  usage_count >= 5 -> source entry auto-elevated to memory_confidence >= 0.75
  usage_count >= 10 -> source entry auto-elevated to memory_confidence = 1.0
VALUE: Entries cited repeatedly differentiate from untested guesses. Without this call, the best \
resolutions never gain credibility over ones that were never verified. One call = one evidence vote.

WHEN TO CALL:
  - After user confirms fix works AND before calling save_memory
  - source_key: the issue_key from the memory_search result you referenced
  - new_ticket_key: the issue_key of the ticket you are currently solving
SKIP ONLY IF: memory_search returned no results OR no result influenced your approach in any way.
RUNTIME: under 1 second.

EXAMPLE: memory_search returned PROJ-88 [JWT expiry fix] and you applied the same pattern ->
  reinforce_memory_usage(source_key="PROJ-88", new_ticket_key="PROJ-142")
  -> {source_key: "PROJ-88", usage_count: 4, cross_reference_boost: 0.60, siblings_updated: 1}
  -> PROJ-88 now ranks higher on all future similar tickets\
"""

_AUDIT_DESCRIPTION = """\
USE WHEN: Investigating why a memory entry has an unexpected confidence score, boost value, or ranking.
ANSWERS: "Why does PROJ-88 rank higher than PROJ-91?" / "Was PROJ-42 ever verified?" / "When was PROJ-55 negated?"

RETURNS: All audit events for issue_key sorted newest-first:
  {event_type, source_key, actor_key, timestamp, before_boost, after_boost, before_confidence, after_confidence, note}
  event_types: reinforced | verified | negated | hub_detected
VALUE: Full traceability of every mutation that changed an entry's credibility. Confirms whether \
reinforcements were actually recorded, verifications applied correctly, and why a negation propagated.

WHEN TO CALL:
  - When a memory_search result ranks unexpectedly high or low
  - Before deciding to negate an entry - verify it has not already been negated
  - When a developer reports ICX surfacing a wrong resolution repeatedly
SKIP: This is a diagnostic tool. Do not call it on every ticket. Call it when something seems wrong.
RUNTIME: under 1 second.

EXAMPLE: get_memory_audit(issue_key="PROJ-88", limit=5) ->
  [{event_type:"reinforced", actor_key:"PROJ-142", after_boost:0.60, note:""},
   {event_type:"reinforced", actor_key:"PROJ-91",  after_boost:0.45, note:""},
   {event_type:"verified",   actor_key:"developer", after_confidence:0.75, note:"Deployed, no errors"}]
  -> PROJ-88 was reinforced by 2 tickets and verified by a developer - high boost is correct\
"""

_GRAPH_CONTEXT_DESCRIPTION = """\
CALL THIS FIRST - replaces grep, glob, and manual file search entirely.
USE WHEN: Starting work on any graph-ready project before reading any files.
Given a task description, returns source files ranked by relevance score, with node_ids for deeper graph calls \
and file paths for graph_subsystem.

RETURNS: Ranked list of {node_id, path, cluster, role, score (0.0-1.0)}.
VALUE: Structure-aware retrieval beats keyword search - it understands import graphs, not just text. \
Gets you the right files in one call instead of 10+ grep attempts.

TASK QUALITY MATTERS - specific descriptions score far better than vague ones:
  GOOD: "JWT token expiry not enforced on POST /api/orders route"
  GOOD: "campaign list date filter ignores selected range after page reload"
  BAD: "fix auth bug" / "date filter broken"

EXAMPLE: task="login timeout not expiring" ->
  src/auth/session.py [service] score=0.91
  src/auth/token.py [service] score=0.87
  src/middleware/auth.py [middleware] score=0.74

RUNTIME: ~5 seconds.
PREREQUISITE: Graph must be built (icx graph build <name>) and graph.status == "ready".
project_path must be the absolute path to the project root.
If the tool returns an error with action_required, follow the action exactly before retrying.\
"""

_GRAPH_CHAIN_DESCRIPTION = """\
USE WHEN: You need to trace data flow through the system or understand who calls a specific component.
Given a node_id, returns a bidirectional call chain up to depth hops: who calls it AND what it calls.

RETURNS: {upstream: [files that import/call this node], downstream: [what this node imports/calls]}
VALUE: Reveals entry points and direct dependencies - essential before changing a function signature \
or tracing how a request flows from API layer to database.

CRITICAL DIFFERENCE FROM graph_impact:
  graph_call_chain = bidirectional, depth-bounded (3 hops default) - answers "how does data flow through X?"
  graph_impact     = unidirectional (dependents only), fully transitive - answers "what breaks if I change X?"

EXAMPLE: graph_call_chain("auth_service") ->
  upstream: [api_routes -> auth_service]
  downstream: [auth_service -> user_repo, auth_service -> token_store]
  -> Now you know: api_routes is the entry point; user_repo and token_store are the dependencies to watch.

RUNTIME: ~5 seconds.
node_id comes from graph_find_context results. Graph must be ready.
project_path must be the absolute path to the project root.
If the tool returns an error with action_required, follow the action exactly before retrying.\
"""

_GRAPH_IMPACT_DESCRIPTION = """\
MANDATORY before changing any shared function, class, component, or utility.
USE WHEN: Before refactoring a node - reveals EVERYTHING that will break, including indirect dependents.

RETURNS: All dependents grouped by confidence tier (high/medium/low risk):
  direct: files that directly import or call this node
  transitive: files that depend on the direct dependents (recursively, unlimited depth)
VALUE: Prevents the most common production bug - changing shared code while missing hidden dependents. \
Skipping this call guarantees blind spots when touching utils, models, services, or middleware. \
When uncertain whether code is shared: CALL IT. Takes under 15 seconds.

CRITICAL DIFFERENCE FROM graph_call_chain:
  graph_impact     = unidirectional (dependents only), fully transitive - answers "what breaks if I change X?"
  graph_call_chain = bidirectional, depth-bounded (3 hops default) - answers "how does data flow through X?"

EXAMPLE: graph_impact("UserRepository") ->
  direct: [AuthService]
  transitive: [LoginController (via AuthService), ApiGateway (via LoginController)]
  -> 3 files at risk - all must be reviewed before changing UserRepository.

RUNTIME: ~12 seconds. This is the exact cost. It is not optional. Call it.
node_id comes from graph_find_context results. Graph must be ready.
project_path must be the absolute path to the project root.
If the tool returns an error with action_required, follow the action exactly before retrying.\
"""

_GRAPH_SUBSYSTEM_DESCRIPTION = """\
USE WHEN: You found one relevant file but need to see the COMPLETE feature boundary before touching it.
Given a file path, returns all files in the same cluster (community of functionally related files \
detected by import graph analysis).

RETURNS: Cluster name + all file paths that belong to the same functional unit.
VALUE: Prevents partial fixes. You cannot fully understand a bug in auth/service.py without seeing \
every file in the Authentication cluster. Missing one causes incomplete fixes and regressions.

EXAMPLE:
  Input:  graph_subsystem("src/auth/service.py")
  Output: cluster "Authentication": [auth/service.py, auth/token.py, auth/session.py, auth/middleware.py]
  -> Without this call, you would have seen only auth/service.py and missed 3 related files.

WHEN TO CALL:
  - After graph_find_context returns initial files - expand each core file to see its full cluster
  - When the bug description mentions a feature name (auth, billing, orders) - find its cluster boundary
  - Before writing the implementation plan - ensures you see the whole picture, not just one entry point
SKIP ONLY IF: Task adds a brand new file with no existing cluster to discover.

RUNTIME: ~2 seconds.
Graph must be built (icx graph build <name>) and graph.status == "ready".
project_path must be the absolute path to the project root.
If the tool returns an error with action_required, follow the action exactly before retrying.\
"""

_GRAPH_IMPORTANT_NODES_DESCRIPTION = """\
USE WHEN: Starting work on an unfamiliar codebase, or planning a refactor and you need to know which files carry the highest blast radius BEFORE any file exploration.
Given top_k, returns files and functions ranked by architectural importance (0.50*PageRank + 0.30*degree + 0.20*betweenness centrality).

RETURNS: [{file, name, pagerank, betweenness, importance}] sorted by importance descending.
VALUE: Tells you where to look FIRST. High-importance nodes are touched by the most code paths - a change there ripples everywhere. Without this call, you may spend time on a low-importance file while missing the real architectural core that affects everything else.

WHEN TO CALL:
  - Before graph_find_context when you have no prior context on a codebase
  - When planning a refactor to identify which files are highest-risk candidates
  - When asked "what is the most critical part of this system?"
SKIP ONLY IF: You already know the architectural hotspots from prior graph exploration in this session.

EXAMPLE: graph_important_nodes(project_path="...") ->
  1. src/auth/token.py [importance=0.92, pagerank=0.95, betweenness=0.84]
  2. src/db/session.py [importance=0.88, pagerank=0.91, betweenness=0.78]
  -> These two files affect the most code paths. Change them with maximum caution and call graph_blast_radius before committing.

RUNTIME: ~1 second.
Graph must be built (icx graph build <name>) and graph.status == "ready".
project_path must be the absolute path to the project root.
If the tool returns an error with action_required, follow the action exactly before retrying.\
"""

_GRAPH_BLAST_RADIUS_DESCRIPTION = """\
MANDATORY BEFORE COMMITTING changes to any shared or high-importance file.
USE WHEN: You have decided which files to change and need to verify the full scope of impact - direct dependents, transitive dependents, risk level, and any files your changelist is missing.

RETURNS: {changed_files, direct_dependents, transitive_dependents, risk_score (0.0-1.0), missing_changes, total_affected}
  direct_dependents: files that directly import or call your changed files
  transitive_dependents: files reachable from direct dependents (recursive)
  risk_score: fraction of high-importance nodes in the blast zone (0=low, 1=critical)
  missing_changes: files that historically co-changed with your target files but are absent from your changelist - these are the regressions you did not know you had.

CRITICAL DIFFERENCE FROM graph_impact:
  graph_blast_radius = file-level pre-merge scope check (what is the full blast zone? what am I missing?)
  graph_impact       = node-level pre-refactor dependency check (who directly depends on this node?)

VALUE: missing_changes is the most important field. If a file co-appeared with your changed file in 8 of 10 prior commits but is absent from your changelist, there is a high probability the change is incomplete.

EXAMPLE: graph_blast_radius(changed_files=["src/auth/token.py"]) ->
  direct_dependents: [src/middleware/auth.py, src/api/login.py]
  transitive_dependents: [src/api/orders.py, src/api/profile.py]
  risk_score: 0.72 (HIGH - 3 of 4 high-importance nodes affected)
  missing_changes: [src/auth/session.py] (co-changed in 8 of last 10 commits with token.py)
  -> src/auth/session.py almost certainly needs to change too. Check it before creating the PR.

RUNTIME: ~3 seconds.
project_path must be the absolute path to the project root.
If the tool returns an error with action_required, follow the action exactly before retrying.\
"""

_GRAPH_CYCLES_DESCRIPTION = """\
USE WHEN: Debugging circular import errors, investigating why a module cannot be loaded, or auditing a codebase for architectural debt during a refactor planning session.
Returns circular dependency chains found only in structural edges (imports, calls, inheritance). Co-change and event-driven edges are excluded from cycle detection.

RETURNS: {cycles: [[file, file, ..., file], ...], cycle_count: N}
Each cycle is a file list where the first and last file are the same - the full loop.
VALUE: Circular imports are a leading cause of import errors, test isolation failures, and refactoring deadlocks. Each returned cycle is the exact loop you need to break - typically by extracting shared types or interfaces into a new file with no upward dependencies.
SKIP ONLY IF: The task is purely additive (new file, no existing structure change) and no circular import error is present.

EXAMPLE: graph_cycles(project_path="...") ->
  cycle_count: 2
  cycles: [
    ["src/auth/token.py", "src/auth/session.py", "src/auth/token.py"],
    ["src/billing/invoice.py", "src/billing/payment.py", "src/billing/invoice.py"]
  ]
  -> Break the auth cycle by extracting shared types to src/auth/models.py with no imports from token or session.

RUNTIME: ~2 seconds.
project_path must be the absolute path to the project root.
If the tool returns an error with action_required, follow the action exactly before retrying.\
"""

_GRAPH_DEAD_CODE_DESCRIPTION = """\
USE WHEN: Cleaning up a codebase, auditing for unused modules before a major refactor, or reducing package size.
Returns files with zero incoming edges - no imports, no callers, no references from any other indexed file.
Excludes entry points (main.py, app.py, server.go, Application.java, etc.) and test files, which have no callers by design.

RETURNS: {dead_code_candidates: [{file, node_count}], count: N} sorted by file path.
  node_count: how many symbols (classes, functions) are in that file - higher = more code to delete.
VALUE: Dead files carry maintenance cost with zero business value. Files with high node_count are the largest dead modules - remove them first. Verify each candidate manually: some files are loaded dynamically at runtime and will not appear in static analysis.
SKIP ONLY IF: The task is adding new code with no cleanup requirement.

EXAMPLE: graph_dead_code(project_path="...") ->
  [{file: "src/utils/legacy_csv.py", node_count: 12},
   {file: "src/integrations/old_sms.py", node_count: 4}]
  -> legacy_csv.py has 12 symbols and no callers. Confirm no dynamic loading, then delete.

RUNTIME: ~1 second.
project_path must be the absolute path to the project root.\
"""

_GRAPH_OWNERSHIP_DESCRIPTION = """\
USE WHEN: A change crosses team boundaries, you need to know who must review specific files, or you are adding a new dependency into another team's module.
Reads CODEOWNERS from project root, .github/CODEOWNERS, or docs/CODEOWNERS (in that order). Returns codeowners_found: false if no file exists.

RETURNS: {owners: ["@team", "@user", ...], owned_files: [...], cross_owner_dependencies: [{from, to, to_owners, edge_type, confidence}], codeowners_found: bool}
  owners: teams/users who own the queried file
  owned_files: all graph files owned by the same owners
  cross_owner_dependencies: edges where your team's code calls into another team's code - these require cross-team review
VALUE: Prevents missing required reviewers and ownership violations. cross_owner_dependencies reveals every interface where your team's code depends on another team's - each entry is a potential breaking contract that needs sign-off from both sides.
SKIP ONLY IF: No CODEOWNERS file exists (confirmed by codeowners_found: false in a prior call).

EXAMPLE: graph_ownership(file_path="src/billing/invoice.py", project_path="...") ->
  owners: ["@billing-team"]
  owned_files: [src/billing/invoice.py, src/billing/payment.py, src/billing/models.py]
  cross_owner_dependencies: [{from: "src/billing/invoice.py", to: "src/auth/token.py", to_owners: ["@security-team"], edge_type: "imports"}]
  -> This change requires review from @security-team because billing imports from auth. Notify them before merging.

RUNTIME: ~1 second.
project_path must be the absolute path to the project root.
If the tool returns an error with action_required, follow the action exactly before retrying.\
"""

_GRAPH_TOOLS_DECISION = (
    "STEP 1b - DEEPER GRAPH TOOLS (YOU MUST attempt all four; skipping without a documented technical reason = VIOLATION):\n"
    "  Vague skip reasons ('not applicable', 'not needed', 'seems fine', 'I think it is internal') are REJECTED.\n"
    "  Each skip must cite the EXACT technical condition met. When uncertain: CALL IT.\n\n"
    "    graph_subsystem(file_path)\n"
    "      CALL: for every file that EXISTS in the codebase that you plan to read or modify.\n"
    "      SKIP ONLY IF: you are creating a file from scratch that has never existed.\n"
    "      VALID skip: 'Creating new file src/utils/newHelper.py - confirmed does not exist in graph'\n"
    "      INVALID skip: 'Also adding a new file so skipping' - you must still call for EXISTING files in the same task.\n\n"
    "    graph_call_chain(node_id)\n"
    "      CALL: always, using node_id from graph_find_context results.\n"
    "      SKIP ONLY IF: node_id belongs to a file you are creating from scratch (zero existing callers confirmed by graph).\n\n"
    "    graph_impact(node_id)\n"
    "      CALL: always, especially when touching shared code. Takes ~12s - not a valid reason to skip.\n"
    "      SKIP ONLY IF: the fix creates a brand-new isolated file with zero existing dependents.\n"
    "      'I think it is not shared' is NOT an accepted skip reason. When uncertain: CALL IT.\n\n"
    "    graph_cross_links(project_path)\n"
    "      CALL: always unless you can confirm single monolith from evidence already in hand.\n"
    "      SKIP ONLY IF: graph_find_context returned results from exactly ONE project path AND you saw no HTTP client calls (fetch/axios/requests/httpx) in those files.\n"
    "      'I think it is a monolith' is NOT accepted. The evidence must come from what the tools returned.\n\n"
    "STEP 1c - MEMORY FILE CHECK (mandatory for every file you plan to edit):\n"
    "  For each file from STEP 1 and STEP 1b that you intend to modify:\n"
    "    Call memory_find_by_file(file_path). Under 1 second. No time excuse accepted.\n"
    "  ONLY accepted skip: file did not exist before this ticket (brand new, confirmed).\n"
    "  'I do not think there is history here' is NOT accepted - call it and let the result decide.\n"
    "  What to do with results:\n"
    "    - resolution_note matches current bug pattern -> reference it explicitly in your Approach\n"
    "    - file has 3+ past bugs -> mark it [fragile] in your Files list, add extra test coverage\n"
    "    - empty results -> proceed normally\n\n"
    "STEP 1d - RELATED ISSUE DISCOVERY (mandatory when memory.status == 'ready'):\n"
    "  Call memory_get_related(files=[...files from graph_find_context...]).\n"
    "  For reopened tickets: also pass issue_key=work_item.issue_key to check prior edges.\n"
    "  If any result has strength >= 0.5: read its resolution_note before writing your Approach.\n"
    "  ONLY accepted skip: memory.status != 'ready'.\n\n"
    "SUPPLEMENTARY GRAPH TOOLS - call proactively or when the specific condition is met:\n"
    "  graph_important_nodes: call BEFORE graph_find_context on an unfamiliar codebase, or when planning a refactor.\n"
    "  graph_ownership: call after graph_subsystem when the change may cross team boundaries.\n"
    "  graph_blast_radius: MANDATORY before committing changes to any shared or high-importance file.\n"
    "  graph_cycles: call when debugging circular import errors or auditing for architectural debt.\n"
    "  graph_dead_code: call when cleaning up a codebase or auditing for unused modules.\n\n"
)

# ---------------------------------------------------------------------------
# Shared JSON schemas
# ---------------------------------------------------------------------------

_ISSUE_REF_SCHEMA = {
    "type": "string",
    "description": (
        "Issue identifier. Accepted formats:\n"
        "  - Full URL - paste the issue URL exactly as it appears in your browser\n"
        "  - Bare issue key - PROJ-123 or ABC-456\n"
        "Pass exactly what the user provided; do not normalise or guess the domain."
    ),
}

_PROJECT_PATHS_SCHEMA = {
    "type": "array",
    "items": {"type": "string"},
    "description": (
        "List of absolute codebase paths. Priority order:\n"
        "  1. User named specific repos -> resolve those paths: "
        "[\"/home/alice/projects/auth-svc\", \"/home/alice/projects/ui\"]\n"
        "  2. User named no specific repo -> pass [] and ICX resolves from the ticket's "
        "tracker project key against registered projects.\n"
        "  3. Cannot determine any path -> pass [].\n"
        "ASK the user for the path if not already given. Only pass a path if the user "
        "explicitly provided it or explicitly said yes to one you proposed. Otherwise pass [].\n"
        "NEVER guess or invent a path the user did not explicitly provide. "
        "NEVER auto-detect the editor workspace root. "
        "When uncertain, pass []."
    ),
}

_PROFILE_SCHEMA = {
    "type": "string",
    "description": (
        "Optional AI profile name to use for this analysis. "
        "Must match a profile configured in ICX. "
        "Defaults to the active profile when omitted."
    ),
}


# ---------------------------------------------------------------------------
# Shared boost brief builder - used by both the icx_boost TOOL (_call_tool)
# and the icx-boost PROMPT (_get_prompt). Kept as one function so the two
# entry points never drift.
# ---------------------------------------------------------------------------

def _run_boost_brief(prompt: str, repo_path: str | None = None, current_file: str | None = None,
                     is_continuation: bool = False, links_in=None) -> dict:
    """Build the one-pass boost brief. Returns a plain dict - never raises (degrades to a minimal
    methodology-only brief on internal failure so callers always get a usable response)."""
    try:
        from icx_engine.boost.classify import is_trivial
        if is_trivial(prompt):
            return {
                "skip": True,
                "reason": "conversational/continuation message - no boost needed; answer directly "
                          "(continue the current task with the context you already have).",
                "boost_meta": {"deterministic": True, "llm_used": False, "trivial": True},
            }
    except Exception:
        pass   # never let the guard block a real boost
    try:
        from icx_engine.boost.service import build_boost_brief
        provided = [str(u) for u in (links_in or [])]
        brief = build_boost_brief(
            prompt, repo_path=repo_path, current_file=current_file,
            is_continuation=is_continuation, links_in=provided,
            env_fn=_boost_env, signals_fn=_context_signals, connected_fn=_icx_connected)
        try:
            idx = rank_skills(prompt, brief.get("archetype", "coding"), storage=SkillStorage())
            if idx:
                brief["skills"] = {"index": idx}
        except Exception:
            pass   # a skill-ranking failure must never break the boost response
        return brief
    except Exception as exc:
        from icx_engine.methodology import build_checklist_for as _bcf
        m = _bcf(prompt)
        return {
            "archetype": m["archetype"], "methodology": m,
            "context": {"activated_signals": [], "files": [], "skipped": str(exc)},
            "links": [], "clarifications": [], "gates": m["gate_sequence"],
            "boosted_prompt": prompt, "boost_meta": {"deterministic": True, "llm_used": False},
            "mandatory_directive": "Follow the ICX methodology; boost degraded to minimal mode.",
            "intent": prompt,
        }


def _auto_refine_brief(prompt: str, brief: dict) -> dict:
    """Auto-refine pass: takes a boost brief and deterministically applies compose_cto_prompt with an
    empty spec (no agent-drafted objective/requirements needed) so a single call produces the full
    two-pass CTO-grade prompt. The agent may still call icx_boost_refine itself afterwards with a
    hand-drafted spec for a stronger result - that stays optional, not required."""
    if brief.get("skip"):
        return brief
    from icx_engine.boost.refine import compose_cto_prompt
    archetype = brief.get("archetype", "coding")
    context = brief.get("context") or {}
    brief = dict(brief)
    brief["boosted_prompt"] = compose_cto_prompt(prompt, archetype, None, context)
    brief["boost_meta"] = {**(brief.get("boost_meta") or {}), "auto_refined": True}
    brief["refine_note"] = (
        "This already ran boost + an auto-refine pass in one call - work from boosted_prompt directly, "
        "no second call is required. Optionally call icx_boost_refine yourself with a hand-drafted "
        "objective/requirements/constraints/acceptance/dims for an even stronger spec."
    )
    return brief


def _boosted(prompt: str, repo_path: str | None = None, current_file: str | None = None,
            is_continuation: bool = False, links_in=None) -> dict:
    """One call, two passes: build the boost brief then auto-apply the refine pass. This is what both
    the icx_boost tool and the icx-boost MCP prompt return, so neither entry point ever needs a second
    manual call to reach the CTO-grade result."""
    brief = _run_boost_brief(prompt, repo_path=repo_path, current_file=current_file,
                             is_continuation=is_continuation, links_in=links_in)
    return _auto_refine_brief(prompt, brief)


@server.list_prompts()
async def _list_prompts() -> list[Prompt]:
    return [
        Prompt(
            name=_BOOST_PROMPT_NAME,
            description=(
                "Boost this request into a CTO-grade working spec in ONE call - boost + an auto-refine "
                "pass both run deterministically before you see the result, so no second slash command "
                "or tool call is required. Use this on demand (e.g. /icx-boost <your request>) instead "
                "of on every message."
            ),
            arguments=[
                PromptArgument(name="prompt", description="The raw user request.", required=True),
                PromptArgument(name="repo_path", description="Project path, if any.", required=False),
                PromptArgument(name="current_file", description="File in focus, if any.", required=False),
            ],
        ),
    ]


@server.get_prompt()
async def _get_prompt(name: str, arguments: dict[str, str] | None) -> GetPromptResult:
    args = arguments or {}
    if name != _BOOST_PROMPT_NAME:
        raise ValueError(f"Unknown prompt: {name}")
    prompt = str(args.get("prompt", "")).strip()
    if not prompt:
        raise ValueError("prompt argument is required")
    repo_path = args.get("repo_path") or None
    current_file = args.get("current_file") or None
    brief = _boosted(prompt, repo_path=repo_path, current_file=current_file)
    return GetPromptResult(
        description="ICX boosted + auto-refined CTO-grade working spec",
        messages=[PromptMessage(role="user", content=TextContent(type="text", text=json.dumps(brief)))],
    )


@server.list_tools()
async def _list_tools() -> list[Tool]:
    # Do NOT call ConfigManager.load() here - it triggers a keyring health check
    # that can take 3s+ in background MCP processes, causing the MCP initialization
    # handshake to time out before Windsurf receives the tool list.
    profile_hint = ""

    analyze_schema = {
        "type": "object",
        "properties": {
            "issue_ref": _ISSUE_REF_SCHEMA,
            "project_paths": _PROJECT_PATHS_SCHEMA,
            "profile": _PROFILE_SCHEMA,
        },
        "required": ["issue_ref"],
    }

    from icx_engine.git.mcp_tools import GIT_TOOLS
    from icx_engine.gitlab.mcp_tools import GITLAB_TOOLS
    from icx_engine.jira.mcp_tools import JIRA_TOOLS
    return [
        # ------------------------------------------------------------------ #
        # [1-2] Entry points - always start here                             #
        # ------------------------------------------------------------------ #
        Tool(
            name=_FAST_TOOL_NAME,
            description=_FAST_DESCRIPTION + profile_hint,
            inputSchema=analyze_schema,
        ),
        Tool(
            name=_FULL_TOOL_NAME,
            description=_FULL_DESCRIPTION + profile_hint,
            inputSchema=analyze_schema,
        ),
        # ------------------------------------------------------------------ #
        # [1b] Methodology - the mandatory discipline; analyze already        #
        #      injects the one-pager, this returns the full framework         #
        # ------------------------------------------------------------------ #
        Tool(
            name=_GET_METHODOLOGY_TOOL,
            description=(
                "Return the full ICX problem-solving methodology (intake, context, classify, decompose, "
                "plan, execute, self-check, confidence, fail-well, verify) with archetypes, decision "
                "rules, and pitfalls. analyze_issue already injects the mandatory one-pager into its "
                "response.methodology - call this when you want the complete framework. Following the "
                "methodology on every ticket is MANDATORY. No input."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name=_BOOST_TOOL,
            description=(
                "The ICX thinking channel - call this ON DEMAND (e.g. the user typed /icx-boost, or an "
                "MCP-prompt-capable editor invoked the icx-boost prompt) rather than on every message. "
                "Give it the user's raw prompt; it returns a boosted brief: the real intent, the task "
                "archetype, the MANDATORY ICX methodology for that archetype, only the codebase context "
                "the problem actually needs (graph/grep/memory - skipped for a plain question or when no "
                "repo is connected), clarifications, the gate sequence, any links (preserved + tagged with "
                "how to pull them - via an ICX tool, by connecting ICX, or with your own tool), and a "
                "boosted_prompt to work from - this already includes an auto-refine pass (deterministic, "
                "no second call needed). Follow mandatory_directive. A work-tracker ticket reference "
                "(ABC-123, a Jira/GitHub/Linear/GitLab URL) or a SonarQube reference is ALWAYS routed "
                "through ICX (analyze_issue_fast / sonar_* tools) regardless of whether boost was called - "
                "that routing is independent of this on-demand channel."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "The raw user request."},
                    "repo_path": {"type": "string", "description": "Project path, if any."},
                    "current_file": {"type": "string", "description": "File in focus, if any."},
                    "is_continuation": {"type": "boolean",
                                        "description": "True if iterating on an ongoing problem."},
                },
                "required": ["prompt"],
            },
        ),
        Tool(
            name=_BOOST_REFINE_TOOL,
            description=(
                "OPTIONAL enrichment on top of icx_boost - not required, since icx_boost already returns "
                "an auto-refined CTO-grade boosted_prompt in one call. Call this only when YOU (the agent, "
                "no extra model cost) have your own deeper understanding of the request and want to draft "
                "a STRUCTURED spec for an even stronger result (measurably stronger - proven +18% "
                "requirement coverage over the auto-refined default). ICX deterministically assembles the "
                "final expert prompt: a best-in-class persona chosen per problem, your restated objective, "
                "the codebase context, the merged requirements, constraints, deliverable, acceptance "
                "criteria + ICX gates, and the methodology standard. Draft these (all optional; ICX fills "
                "any gap): objective (restate the ask professionally), requirements[], constraints[], "
                "deliverable, acceptance[] (definition of done), dims[] (extra completeness items). Supply "
                "at least one of objective/requirements/dims."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "The original user request (verbatim)."},
                    "objective": {"type": "string", "description": "Your professional restatement of the real goal."},
                    "requirements": {"type": "array", "items": {"type": "string"},
                                     "description": "Explicit + inferred functional requirements."},
                    "constraints": {"type": "array", "items": {"type": "string"},
                                    "description": "Tech stack, conventions, and things NOT to do."},
                    "deliverable": {"type": "string", "description": "What to produce and in what form."},
                    "acceptance": {"type": "array", "items": {"type": "string"},
                                   "description": "Definition of done - how the result is judged."},
                    "dims": {"type": "array", "items": {"type": "string"},
                             "description": "Extra task-specific completeness items a rushed answer forgets."},
                    "archetype": {"type": "string",
                                  "description": "Archetype from icx_boost. Optional; re-classified if omitted."},
                    "repo_path": {"type": "string", "description": "Project path, if any."},
                    "current_file": {"type": "string", "description": "File in focus, if any."},
                },
                "required": ["prompt"],
            },
        ),
        # ------------------------------------------------------------------ #
        # [3] Memory search - immediately after analyze, before graph        #
        # ------------------------------------------------------------------ #
        Tool(
            name=_MEM_SEARCH_TOOL,
            description=(
                "CALL IMMEDIATELY AFTER analyze_issue - before any graph or file exploration.\n"
                "Search memory using tags YOU generate from work_item.analysis. "
                "The analysis result is what teaches you the problem domain - use it now.\n\n"
                "HOW TO GENERATE TAGS: From work_item.analysis extract 3-6 specific lowercase tags covering:\n"
                "  - Problem domain (e.g. 'jwt-expiry', 'db-connection-pool', 'websocket-reconnect')\n"
                "  - Affected system layer (e.g. 'auth-middleware', 'data-access-layer', 'event-handler')\n"
                "  - Failure type (e.g. 'race-condition', 'null-reference', 'timeout', 'off-by-one')\n"
                "  Rules: no generic words (bug/fix/error/issue/backend), hyphenate multi-word concepts.\n"
                "  GOOD: ['jwt-expiry', 'auth-middleware', 'clock-skew']\n"
                "  BAD: ['auth', 'bug', 'backend']\n\n"
                "SKIP ONLY IF: memory.status != 'ready' in the analyze_issue response.\n\n"
                "RETURNS: Ranked list of past work items with resolution_note, files_changed, similarity_score.\n"
                "Use results as a pattern reference when forming your implementation approach - "
                "if a past resolution matches this problem, derive your approach from it.\n"
                "RUNTIME: under 1 second."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Problem description to search - combine summary and key symptoms from work_item.analysis.",
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "3-6 agent-generated tags derived from work_item.analysis. Hyphenate multi-word. GOOD: ['jwt-expiry', 'auth-middleware']. BAD: ['auth', 'bug'].",
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "Max results to return. Default 5.",
                        "default": 5,
                    },
                },
                "required": ["query", "tags"],
            },
        ),
        # ------------------------------------------------------------------ #
        # [4] Architectural overview - call first on unfamiliar codebase     #
        # ------------------------------------------------------------------ #
        Tool(
            name=_GRAPH_IMPORTANT_NODES_TOOL,
            description=_GRAPH_IMPORTANT_NODES_DESCRIPTION,
            inputSchema={
                "type": "object",
                "properties": {
                    "project_path": {"type": "string"},
                    "top_k": {
                        "type": "integer",
                        "default": 10,
                        "description": "How many nodes to return (default 10)",
                    },
                },
                "required": ["project_path"],
            },
        ),
        # ------------------------------------------------------------------ #
        # [5-10] Core graph tools - discovery, scope, ownership, flow,       #
        #         impact, contracts                                           #
        # ------------------------------------------------------------------ #
        Tool(
            name=_GRAPH_CONTEXT_TOOL,
            description=_GRAPH_CONTEXT_DESCRIPTION,
            inputSchema={
                "type": "object",
                "properties": {
                    "project_path": {"type": "string", "description": "Absolute path to the project root."},
                    "task": {"type": "string", "description": "Natural language task description (used for relevance scoring)."},
                    "token_budget": {"type": "integer", "description": "Accepted but not used for filtering; all ranked results are returned.", "default": 8000},
                    "min_confidence": {"type": "number", "description": "Minimum edge confidence (0.0-1.0). Default 0.0.", "default": 0.0},
                },
                "required": ["project_path", "task"],
            },
        ),
        Tool(
            name=_GRAPH_SUBSYSTEM_TOOL,
            description=_GRAPH_SUBSYSTEM_DESCRIPTION,
            inputSchema={
                "type": "object",
                "properties": {
                    "project_path": {"type": "string"},
                    "file_path": {"type": "string", "description": "Relative file path (e.g. 'src/auth/service.py')."},
                },
                "required": ["project_path", "file_path"],
            },
        ),
        Tool(
            name=_GRAPH_OWNERSHIP_TOOL,
            description=_GRAPH_OWNERSHIP_DESCRIPTION,
            inputSchema={
                "type": "object",
                "properties": {
                    "project_path": {"type": "string"},
                    "file_path": {
                        "type": "string",
                        "description": "File to look up ownership for",
                    },
                },
                "required": ["project_path", "file_path"],
            },
        ),
        Tool(
            name=_GRAPH_CHAIN_TOOL,
            description=_GRAPH_CHAIN_DESCRIPTION,
            inputSchema={
                "type": "object",
                "properties": {
                    "project_path": {"type": "string"},
                    "node_id": {"type": "string", "description": "Graph node ID."},
                    "depth": {"type": "integer", "default": 3},
                    "min_confidence": {"type": "number", "default": 0.5},
                },
                "required": ["project_path", "node_id"],
            },
        ),
        Tool(
            name=_GRAPH_IMPACT_TOOL,
            description=_GRAPH_IMPACT_DESCRIPTION,
            inputSchema={
                "type": "object",
                "properties": {
                    "project_path": {"type": "string"},
                    "node_id": {"type": "string"},
                    "min_confidence": {"type": "number", "default": 0.5},
                },
                "required": ["project_path", "node_id"],
            },
        ),
        Tool(
            name=_GRAPH_CROSS_LINKS_TOOL,
            description=(
                "MICROSERVICES ONLY - SKIP IF SINGLE MONOLITH.\n"
                "USE WHEN: Working on a project that makes HTTP calls to peer services and you need to know "
                "which calls cross service boundaries.\n"
                "Matches outgoing HTTP calls in THIS project to REST routes in peer registered projects.\n"
                "RETURNS: [{source_project, call_site, method, route, target_project, matched_route}] "
                "listing every cross-service HTTP link.\n"
                "VALUE: Reveals which API contracts are in play for this change - prevents breaking a "
                "consumer or producer contract you did not know existed.\n"
                "EXAMPLE: UI project calls POST /api/v1/orders -> matched to route in backend-svc project. "
                "Without this call, you would not know the UI depends on that exact contract.\n"
                "Returns empty list if no cross-service HTTP calls were detected, or if graph needs rebuild "
                "(icx graph build <name>).\n"
                "RUNTIME: ~2 seconds.\n"
                "project_path must be the absolute path to the project root."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project_path": {
                        "type": "string",
                        "description": "Absolute path to the project root.",
                    },
                    "file_path": {
                        "type": "string",
                        "description": "Optional absolute path to a file in this project. "
                        "When given, also returns co_changed_files: files historically "
                        "committed together with this file (from the cochange resolver).",
                    },
                },
                "required": ["project_path"],
            },
        ),
        # ------------------------------------------------------------------ #
        # [11-13] Pre-merge and architecture analysis                         #
        # ------------------------------------------------------------------ #
        Tool(
            name=_GRAPH_BLAST_RADIUS_TOOL,
            description=_GRAPH_BLAST_RADIUS_DESCRIPTION,
            inputSchema={
                "type": "object",
                "properties": {
                    "project_path": {"type": "string"},
                    "changed_files": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of changed file paths (relative or absolute)",
                    },
                    "max_depth": {
                        "type": "integer",
                        "default": 5,
                        "description": "Maximum traversal depth (default 5)",
                    },
                    "min_confidence": {
                        "type": "number",
                        "default": 0.3,
                        "description": "Minimum edge confidence to follow (default 0.3)",
                    },
                },
                "required": ["project_path", "changed_files"],
            },
        ),
        Tool(
            name=_GRAPH_CYCLES_TOOL,
            description=_GRAPH_CYCLES_DESCRIPTION,
            inputSchema={
                "type": "object",
                "properties": {
                    "project_path": {"type": "string"},
                    "max_cycles": {
                        "type": "integer",
                        "default": 20,
                        "description": "Maximum number of cycles to return (default 20)",
                    },
                },
                "required": ["project_path"],
            },
        ),
        Tool(
            name=_GRAPH_DEAD_CODE_TOOL,
            description=_GRAPH_DEAD_CODE_DESCRIPTION,
            inputSchema={
                "type": "object",
                "properties": {
                    "project_path": {"type": "string"},
                },
                "required": ["project_path"],
            },
        ),
        # ------------------------------------------------------------------ #
        # [14-17] Historical memory tools - hotspots, per-file, relations    #
        # ------------------------------------------------------------------ #
        Tool(
            name=_MEM_HOTSPOTS_TOOL,
            description=(
                "ANSWERS: 'Which FILES have the most bugs?' - ranks files by historical work item count.\n"
                "USE AT THE START OF BUG INVESTIGATION OR REFACTORING PLANNING - shows which files "
                "carry the most historical bug density.\n"
                "RETURNS: Top N files ranked by saved work item count: "
                "[{file, count, work_items: [issue_keys]}] sorted by count descending.\n"
                "RUNTIME: ~1 second.\n"
                "VALUE: Files with high counts are fragile. They need extra test coverage, extra caution "
                "before editing, and are the strongest candidates for refactoring. "
                "A file with 8 past bugs is a pattern, not a coincidence.\n"
                "WHEN TO CALL:\n"
                "  - At the start of a bug investigation to identify fragile areas before searching\n"
                "  - When planning a refactor to know which files carry the highest risk\n"
                "  - When deciding where to add tests first\n"
                "EXAMPLE: memory_get_hotspots() ->\n"
                "  1. src/auth/token.py [count=8]\n"
                "  2. src/billing/invoice.py [count=6]\n"
                "  3. src/api/middleware.py [count=5]\n"
                "  -> Before touching any of these files, call memory_find_by_file on each one."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project_key": {
                        "type": "string",
                        "description": "Optional project key to restrict to one project.",
                    },
                    "top_n": {
                        "type": "integer",
                        "description": "Number of top files to return (default 20, max 100).",
                        "default": 20,
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name=_MEM_BY_FILE_TOOL,
            description=(
                "CALL BEFORE EDITING ANY FILE - surfaces past bugs, fixes, and regressions for that exact file.\n"
                "Uses substring matching (case-insensitive, cross-platform path separators).\n"
                "RETURNS: All saved work items whose files_changed list contains the given path. "
                "Each result includes: issue_key, resolution_note (what fix was applied), tags, files_changed.\n"
                "VALUE: Tells you what broke here before and how it was fixed. Prevents repeating past mistakes. "
                "Without this call, you may re-introduce a regression that was already solved.\n"
                "RUNTIME: under 1 second per file.\n"
                "WHEN TO CALL: For each core file identified in STEP 1 and STEP 1b that you intend to modify. "
                "SKIP ONLY IF the file is brand new with zero edit history.\n"
                "EXAMPLE: memory_find_by_file('src/auth/token.py') ->\n"
                "  PROJ-88: 'JWT not expiring' fixed by adding clock_skew tolerance in validate_token()\n"
                "  PROJ-112: 'Token refresh race' fixed by adding lock in refresh_token()\n"
                "  -> token.py has two past concurrency/timing bugs - check both before changing it."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "File path to look up (relative or absolute; substring match).",
                    },
                    "project_key": {
                        "type": "string",
                        "description": "Optional project key to restrict search (e.g. PROJ).",
                    },
                },
                "required": ["file_path"],
            },
        ),
        Tool(
            name=_MEM_RELATED_TOOL,
            description=(
                "CALL AFTER IDENTIFYING THE BUG LOCATION - discovers historically coupled issues "
                "via shared change history.\n"
                "How it works: tickets that touched the same files are related. "
                "Strength = shared file count / max(your files, their files) (0.0-1.0).\n"
                "RETURNS: [{issue_key, relation_type, strength}] sorted by strength descending.\n"
                "RUNTIME: under 1 second.\n"
                "TWO MODES - pass one or both:\n"
                "  files (primary - use for all tickets): pass file paths from graph_find_context. "
                "Computes overlap on-the-fly against all saved entries. Works for new tickets.\n"
                "  issue_key (secondary - reopened tickets only): looks up pre-stored edges. "
                "Only returns results if this ticket was previously saved via save_memory.\n"
                "When both provided: edges checked first; falls back to file overlap if no edges found.\n"
                "VALUE: Finds hidden dependencies you would miss by reading code alone. "
                "A high-strength result means 'when you fix X, Y tends to also need attention.' "
                "Prevents fixing one place while silently breaking another.\n"
                "WHEN TO CALL:\n"
                "  - After graph_find_context - pass its file results directly\n"
                "  - When the ticket touches multiple features or services\n"
                "  - Before writing your test plan (related issues reveal regression-test areas)\n"
                "EXAMPLE: memory_get_related(files=['auth/token.py', 'auth/session.py']) ->\n"
                "  PROJ-88 [strength=0.75] - 3 shared files including auth/token.py\n"
                "  PROJ-112 [strength=0.50] - 2 shared files including auth/session.py\n"
                "  -> This fix area has been broken twice before. "
                "Read those resolution_notes before writing your approach."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "files": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "File paths for the current ticket, from graph_find_context results. "
                            "Primary input - works for new tickets with no prior history."
                        ),
                    },
                    "issue_key": {
                        "type": "string",
                        "description": (
                            "Issue key to look up pre-stored edges (e.g. PROJ-123). "
                            "Use for reopened tickets only - requires prior save_memory call."
                        ),
                    },
                    "project_key": {
                        "type": "string",
                        "description": "Optional project key to restrict results to one project.",
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name=_MEM_PATTERNS_TOOL,
            description=(
                "ANSWERS: 'Which BUG CATEGORIES keep recurring?' - detects systemic failure patterns across all saved work items.\n"
                "USE WHEN THE SAME BUG CATEGORY KEEPS RECURRING OR BEFORE MAJOR REFACTORING - "
                "reveals systemic weaknesses in the codebase.\n"
                "Patterns are recomputed every 5 saves.\n"
                "RUNTIME: under 1 second.\n"
                "RETURNS: [{project_key, pattern_type, label, evidence, entry_count, detected_at}]\n"
                "Pattern types and what they mean:\n"
                "  frequent_file: This file appears in >= 30% of all saved work items. "
                "Architectural smell - likely needs a rewrite.\n"
                "  dominant_tag: This category (e.g. 'auth', 'date-handling', 'validation') "
                "appears in >= 20% of bugs. A systemic failure category.\n"
                "  top_work_item_type: One work item type (bug/story/task) makes up > 50% of all saved items.\n"
                "VALUE: Reveals root causes at the architectural level, not just per-file. "
                "If 'auth' is a dominant_tag, fixing individual auth bugs is not enough - "
                "the auth system itself needs redesign.\n"
                "WHEN TO CALL:\n"
                "  - Before major refactoring to understand what keeps breaking and why\n"
                "  - When the same bug category recurs (date parsing, null handling, auth)\n"
                "  - When planning technical debt work to prioritize highest-impact areas\n"
                "EXAMPLE: memory_get_patterns() ->\n"
                "  hotspot_file: src/auth/token.py [appears in 40% of bugs, entry_count=12]\n"
                "  dominant_tag: 'date-handling' [present in 25% of bugs]\n"
                "  -> token.py needs a rewrite; date handling needs a shared utility layer."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project_key": {
                        "type": "string",
                        "description": "Optional project key to restrict to one project.",
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name=_MEM_DELETE_TOOL,
            description=(
                "USE WHEN the human explicitly wants a saved memory entry permanently removed - MUST "
                "delete exactly that issue_key's entry (relations cleaned up automatically). This is a "
                "CONFIRMATION-GATED tool: the first call (no confirm_token) returns pending_confirmation "
                "plus a one-time token after showing the human the issue_key - you MUST show that to the "
                "human and get an explicit yes before calling again with confirm_token set. Calling with "
                "a wrong or reused token fails. Returns {ok: true, issue_key} on success or "
                "{ok: false, error} on failure."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "issue_key": {"type": "string", "description": "Issue key to delete, e.g. PROJ-456."},
                    "confirm_token": {"type": "string"},
                },
                "required": ["issue_key"],
            },
        ),
        Tool(
            name=_MEM_UPDATE_TOOL,
            description=(
                "USE WHEN a saved memory entry's own text or file list needs correcting - not for "
                "recording a new fix (use save_memory) or a verification outcome (use save_memory's "
                "outcome_verified/negate flags). Pass issue_key plus only the field(s) you want to "
                "change; unlisted fields are left untouched. files_changed and tags are REPLACED "
                "entirely, not merged/appended. UNGATED - no confirm_token. Returns "
                "{ok: true, issue_key, updated_fields} on success or {ok: false, error} on failure "
                "(unknown issue_key, or a field outside the allowed set)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "issue_key": {"type": "string", "description": "Issue key to update, e.g. PROJ-456."},
                    "summary": {"type": "string"},
                    "problem_description": {"type": "string"},
                    "impact": {"type": "string"},
                    "resolution_note": {"type": "string"},
                    "files_changed": {"type": "array", "items": {"type": "string"}, "description": "Replaces the entry's files_changed entirely."},
                    "tags": {"type": "array", "items": {"type": "string"}, "description": "Replaces the entry's tags entirely."},
                },
                "required": ["issue_key"],
            },
        ),
        # ------------------------------------------------------------------ #
        # [17] lock_plan - spec-lock the file set BEFORE coding             #
        # ------------------------------------------------------------------ #
        Tool(
            name=_LOCK_PLAN_TOOL,
            description=(
                "SPEC-LOCK - call this AFTER gathering context and deciding which files to change, and "
                "BEFORE writing any code. Submit the files you plan to change; ICX fuses graph + grep + "
                "semantic + memory signals and returns any HIGH-signal file your plan MISSED (a direct "
                "dependent, a co-change partner, or a file a prior fix touched). It is the guard against "
                "a wrong-scope first attempt. Input: {issue_ref, chosen_files:[paths you will change], "
                "justifications?:{path:reason}, project_path?, keywords?:[ticket symbols/routes/errors]}. "
                "Returns {ok, coverage, blocking_missed[], advisory_missed[], accepted[]}. "
                "HARD RULE: do NOT write code until ok is true. For each blocking_missed file, either add "
                "it to chosen_files and call again, or justify it. This runs no LLM - it is deterministic."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "issue_ref": {"type": "string"},
                    "chosen_files": {"type": "array", "items": {"type": "string"}},
                    "justifications": {"type": "object", "additionalProperties": {"type": "string"}},
                    "project_path": {"type": "string"},
                    "keywords": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["issue_ref", "chosen_files"],
            },
        ),
        # ------------------------------------------------------------------ #
        # [18-19] local testing - ask user preference, then run             #
        #         [18] start_testing_session                                #
        #         [19] resume_testing_session (all gates)                   #
        # ------------------------------------------------------------------ #
        Tool(
            name=_TESTING_START_TOOL,
            description=_TESTING_START_DESCRIPTION,
            inputSchema={
                "type": "object",
                "properties": {
                    "file_paths": {"type": "array", "items": {"type": "string"}},
                    "context": {"type": "string"},
                    "max_iterations": {"type": "integer", "minimum": 1},
                    "test_mode": {"type": "string", "enum": ["automated", "manual"]},
                    "test_writes": {"type": "boolean",
                                    "description": "agent-type: allow real Create/Update/Delete writes against the live app (default true). Set false for a read-only environment - the agent's test then exercises forms (fill/validate/cancel) without submitting a real write."},
                    "nl_intent": {"type": "string",
                                  "description": "Optional plain-English scenario request (e.g. 'test duplicate email error') to seed extra NL-driven test scenarios."},
                    "acceptance_criteria": {"type": "array", "items": {"type": "string"},
                                            "description": "Optional ticket acceptance criteria to author extra scenarios from."},
                },
                "required": ["file_paths", "test_mode"],
            },
        ),
        Tool(
            name=_TESTING_RESUME_TOOL,
            description=_TESTING_RESUME_DESCRIPTION,
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                    "response": {"type": "object"},
                },
                "required": ["session_id", "response"],
            },
        ),
        Tool(
            name=_TESTING_STATUS_TOOL,
            description=_TESTING_STATUS_DESCRIPTION,
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                },
                "required": ["session_id"],
            },
        ),
        # ------------------------------------------------------------------ #
        # [20] Reference reinforcement - only if memory_search was used      #
        # ------------------------------------------------------------------ #
        Tool(
            name=_REINFORCE_TOOL_NAME,
            description=_REINFORCE_DESCRIPTION,
            inputSchema={
                "type": "object",
                "properties": {
                    "source_key": {
                        "type": "string",
                        "description": "Issue key of the past resolution you referenced (from memory_search results).",
                    },
                    "new_ticket_key": {
                        "type": "string",
                        "description": "Issue key of the ticket you are currently solving.",
                    },
                },
                "required": ["source_key", "new_ticket_key"],
            },
        ),
        # ------------------------------------------------------------------ #
        # [21] Commit to memory - ONLY after testing confirms fix works      #
        # ------------------------------------------------------------------ #
        Tool(
            name=_SAVE_TOOL_NAME,
            description=_SAVE_DESCRIPTION,
            inputSchema={
                "type": "object",
                "properties": {
                    "issue_key": {
                        "type": "string",
                        "description": "Issue key (e.g. PROJ-456).",
                    },
                    "summary": {
                        "type": "string",
                        "description": "Agent-synthesized root problem title. NOT the raw tracker summary. Describe what was wrong at the code level. Max 500 characters.",
                    },
                    "problem_description": {
                        "type": "string",
                        "description": "Agent root cause analysis: exact failure mechanism, affected file(s)/function(s), triggering condition. This text is embedded for semantic search - precise analysis retrieves better than raw tracker descriptions. Max 2000 characters.",
                    },
                    "resolution_note": {
                        "type": "string",
                        "description": "What was changed, exact file/function/line, and the mechanical reason it works. Minimum 2-3 sentences. Vague notes provide zero value to future agents.",
                    },
                    "files_changed": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Every file path you modified. Drives structural relation detection and hotspot analysis - incomplete lists degrade future recall.",
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "3-6 specific lowercase tags covering problem domain, affected system layer, failure type. PRIMARY retrieval signal - this memory surfaces only when tags match a future query. GOOD: ['jwt-expiry', 'auth-middleware', 'token-validation']. BAD: ['auth', 'bug', 'fix']. No generic words, hyphenate multi-word, no duplicates.",
                    },
                    "work_item_type": {
                        "type": "string",
                        "description": "Exact value from work_item.type in the analyze_issue response. Tracker-authoritative - do not infer or substitute.",
                    },
                    "pattern_used": {
                        "type": "string",
                        "description": "Implementation pattern applied (e.g. 'repository pattern', 'event sourcing'). Optional - omit if none applies.",
                    },
                    "root_cause_pattern": {
                        "type": "string",
                        "description": "Canonical root cause from ROOT_CAUSE_PATTERNS enum. Use 'uncategorized' if none fits. Required for memory intelligence.",
                    },
                    "pattern_confidence": {
                        "type": "number",
                        "description": "0.0-1.0: how certain you are about the pattern classification.",
                    },
                    "outcome_verified": {
                        "type": "boolean",
                        "description": "Set true only after developer explicitly confirms fix worked.",
                    },
                    "outcome_feedback_note": {
                        "type": "string",
                        "description": "Required when outcome_verified=true. What confirmed the fix worked.",
                    },
                    "negate": {
                        "type": "boolean",
                        "description": "Set true if this resolution was confirmed WRONG. Propagates penalty to all citers.",
                    },
                    "negation_reason": {
                        "type": "string",
                        "description": "Required when negate=true. Why this approach failed.",
                    },
                    "graph_cluster": {
                        "type": "string",
                        "description": "Cluster name from graph report. Part of causal chain record.",
                    },
                    "files_agent_opened": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Files you opened during investigation, in order.",
                    },
                    "prior_resolution_used": {
                        "type": "string",
                        "description": "Issue key of past resolution you referenced (from memory_search), if any.",
                    },
                    "root_cause_confirmed": {
                        "type": "boolean",
                        "description": "True if root_cause_pattern matched what you actually found.",
                    },
                    "diagnosis_steps": {
                        "type": "integer",
                        "description": "Number of tool calls before you started writing code.",
                    },
                    "full_ticket_text": {
                        "type": "string",
                        "description": "LLM-analysed problem summary + detailed description. Max 2000 chars.",
                    },
                    "attachment_summary": {
                        "type": "string",
                        "description": "One-paragraph summary of what attachments showed. Max 500 chars.",
                    },
                    "verified_by_human": {
                        "type": "boolean",
                        "description": "Manual-verification override: set true when the user personally tested and confirmed the fix (manual path). Lets save_memory record a verified success without an automated record_verification. Still requires outcome_feedback_note.",
                    },
                },
                "required": ["issue_key", "summary", "problem_description", "resolution_note", "files_changed", "tags", "work_item_type"],
            },
        ),
        Tool(
            name=_DRAFT_SKILL_TOOL,
            description=(
                "MANDATORY immediately after every save_memory call where outcome_verified=true - no "
                "exceptions, even when the honest judgment is skill_worthy=false. YOU decide: is this "
                "fix non-obvious and likely to recur? If save_memory's response included "
                "related_skills, check whether one of those names already covers this - reuse that "
                "skill_name to refine it (your fresh text replaces the stale text) rather than create "
                "a near-duplicate. Write description in third person, stating both what the skill does "
                "and when to use it (e.g. 'Fixes N+1 query patterns in SQLAlchemy. Use when a list "
                "endpoint is slow and profiling shows repeated single-row queries.'). Generalize - do "
                "not paraphrase the raw ticket. Input: {issue_key, skill_worthy, skill_name?, "
                "description?, when_to_use?, procedure?, verification?, pitfalls?, tags?} - the five "
                "content fields are required when skill_worthy=true. Returns {status: skipped} or "
                "{status: created|updated, name} or {error}. Requires a prior, verified save_memory "
                "entry - for a general-purpose skill the user asks for directly, with no ticket or "
                "memory entry behind it, use create_skill instead."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "issue_key": {"type": "string", "description": "The issue_key from the save_memory call this follows."},
                    "skill_worthy": {"type": "boolean", "description": "Your own judgment - false is a valid, expected answer."},
                    "skill_name": {"type": "string", "description": "Required when skill_worthy=true. Reuse an existing name (from related_skills) to refine it, or pick a new one to create."},
                    "description": {"type": "string", "description": "Required when skill_worthy=true. Third person - states what it does AND when to use it."},
                    "when_to_use": {"type": "string", "description": "Required when skill_worthy=true. The trigger condition."},
                    "procedure": {"type": "string", "description": "Required when skill_worthy=true. The generalized step-by-step fix."},
                    "verification": {"type": "string", "description": "Required when skill_worthy=true. How to confirm this class of fix worked."},
                    "pitfalls": {"type": "string", "description": "Optional. Gotchas or wrong turns."},
                    "tags": {"type": "array", "items": {"type": "string"}, "description": "Optional. Merged with the originating entry's own tags."},
                },
                "required": ["issue_key", "skill_worthy"],
            },
        ),
        Tool(
            name=_CREATE_SKILL_TOOL,
            description=(
                "USE WHEN the user directly asks you to create a general-purpose skill - not a "
                "follow-up to a verified fix (that path is draft_skill). Has NO issue_key/memory "
                "dependency and works even when memory is completely unavailable or not ready. Builds "
                "a skill directly from the fields you supply, mirroring `icx skills create`'s CLI "
                "behavior exactly. If project_key is given, the skill is tied to that project "
                "(scope_hint='repo-specific'); omit it for a general-purpose skill "
                "(scope_hint='generic'). Calling again with the same name merges into the existing "
                "skill via the usual hash-guarded write_or_update rules (skipped if hand-edited since). "
                "Input: {name, description, when_to_use, procedure, verification, pitfalls?, tags?, "
                "project_key?}. Returns {status: created|updated|skipped_user_edited, name} or {error}."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Skill name - slugified for storage."},
                    "description": {"type": "string", "description": "Third person - states what it does AND when to use it."},
                    "when_to_use": {"type": "string", "description": "The trigger condition."},
                    "procedure": {"type": "string", "description": "The generalized step-by-step approach."},
                    "verification": {"type": "string", "description": "How to confirm this class of fix/approach worked."},
                    "pitfalls": {"type": "string", "description": "Optional. Gotchas or wrong turns.", "default": ""},
                    "tags": {"type": "array", "items": {"type": "string"}, "description": "Optional lowercase tags.", "default": []},
                    "project_key": {"type": "string", "description": "Optional. Ties the skill to this project (scope_hint='repo-specific'); omit for a generic skill."},
                },
                "required": ["name", "description", "when_to_use", "procedure", "verification"],
            },
        ),
        Tool(
            name=_SKILL_GET_TOOL,
            description=(
                "USE WHEN icx_boost's brief includes a skills.index entry you want full context on - "
                "fetches one learned skill's complete markdown body (When to Use/Procedure/Pitfalls/"
                "Verification). Never bulk-fetch every candidate - call this only for the name(s) you "
                "actually want. Input: {name}. Returns {body} or {error}."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Skill name, from skills.index in the icx_boost brief."},
                },
                "required": ["name"],
            },
        ),
        Tool(
            name=_SKILLS_INDEX_TOOL,
            description=(
                "USE WHEN you suspect icx_boost's skills.index or save_memory's related_skills missed "
                "something relevant - both are ranked/capped hints, not the full picture. Returns EVERY "
                "learned skill's name and description, unranked, uncapped. Scan it yourself and decide "
                "what's actually relevant; then call icx_skill_get for full content on the one(s) you "
                "want. No input."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name=_RECORD_VERIFICATION_TOOL,
            description=(
                "Record Definition-of-Done verification evidence before declaring a ticket done. "
                "Submit each DoD item with the exact command run and its output; every item must "
                "have a non-empty command, non-empty output, and passed=true to be accepted. "
                "Required before save_memory can record a verified success on the automated path "
                "(or pass verified_by_human=true to save_memory if you verified manually). "
                "Input: {issue_key, dod_items:[{check, method, passed, command, output}], "
                "self_review_note, layers_run?}. Returns {accepted, missing, confidence}."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "issue_key": {"type": "string"},
                    "dod_items": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "check": {"type": "string"},
                                "method": {"type": "string"},
                                "passed": {"type": "boolean"},
                                "command": {"type": "string"},
                                "output": {"type": "string"},
                            },
                            "required": ["check", "passed", "command", "output"],
                        },
                    },
                    "self_review_note": {"type": "string"},
                    "layers_run": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["issue_key", "dod_items"],
            },
        ),
        Tool(
            name=_UI_AUTH_CAPTURE_TOOL,
            description=(
                "CAPTURE a UI login session by opening a REAL browser for the user to log in by hand "
                "- NEVER ask the user for their username/password in chat for this. Call this at the "
                "auth_gate when the user chose 'capture'. ICX opens a headed Chromium at the login "
                "URL; the user logs in manually; when they reach success_url (or close the window) "
                "ICX saves the authenticated session (cookies+localStorage) for this project+host and "
                "the UI test replays already logged in. Input: {url, file_paths:[seed files, to key "
                "the session to this project], success_url?}. Returns {ok, storage_state}. After ok, "
                "resume the auth_gate with {\"auth_mode\":\"capture\"}. If it fails with a Playwright/"
                "tooling error, DO NOT run npm/npx/playwright install in the user's repo - ICX brings "
                "its own tooling; tell the user to run `icx test setup --force` instead."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "file_paths": {"type": "array", "items": {"type": "string"}},
                    "success_url": {"type": "string"},
                },
                "required": ["url", "file_paths"],
            },
        ),
        Tool(
            name=_UI_AUTH_INLINE_TOOL,
            description=(
                "INLINE login: the user provides the APPLICATION credentials (username/password the "
                "app requires) and ICX drives the login form, then saves the authenticated session. "
                "Use at the auth_gate when the user chose 'inline'. The credentials are passed to "
                "ICX's browser process only - never stored by ICX beyond the resulting session, never "
                "echoed. Input: {url, file_paths, username, password, success_url?, user_selector?, "
                "pass_selector?, submit_selector?}. Returns {ok, storage_state}. After ok, resume the "
                "auth_gate with {\"auth_mode\":\"inline\"}."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "file_paths": {"type": "array", "items": {"type": "string"}},
                    "username": {"type": "string"},
                    "password": {"type": "string"},
                    "success_url": {"type": "string"},
                    "user_selector": {"type": "string"},
                    "pass_selector": {"type": "string"},
                    "submit_selector": {"type": "string"},
                },
                "required": ["url", "file_paths", "username", "password"],
            },
        ),
        # ------------------------------------------------------------------ #
        # [23] Audit trail - diagnostic only                                 #
        # ------------------------------------------------------------------ #
        Tool(
            name=_AUDIT_TOOL_NAME,
            description=_AUDIT_DESCRIPTION,
            inputSchema={
                "type": "object",
                "properties": {
                    "issue_key": {
                        "type": "string",
                        "description": "Issue key of the memory entry to audit.",
                    },
                    "limit": {
                        "type": "integer",
                        "default": 20,
                        "description": "Maximum events to return.",
                    },
                },
                "required": ["issue_key"],
            },
        ),
        # ------------------------------------------------------------------ #
        # Sonar code-quality tools - direct SonarQube reader, read-only      #
        # ------------------------------------------------------------------ #
        Tool(name=_SONAR_STATUS_TOOL,
             description="USE WHEN the user asks about code quality and you must first confirm Sonar is reachable: shows Sonar configuration and live connection health. ALWAYS call this before other sonar_* tools if a connection error is possible. Works regardless of sonar_enabled.",
             inputSchema={"type": "object", "properties": {}, "required": []}),
        Tool(name=_SONAR_PROJECTS_TOOL,
             description=("Discover SonarQube projects the token can access. FOLLOW the mandatory protocol in the "
                          "response `instructions` field: first ask the user whether ICX should fetch projects or "
                          "they will paste the key; when `truncated` is true the list is withheld (too many) - relay "
                          "the count and ask the user to paste the exact key or supply a `query` search term. Never "
                          "invent a project key. Requires sonar_enabled."),
             inputSchema={"type": "object",
                          "properties": {"query": {"type": "string"}},
                          "required": []}),
        Tool(name=_SONAR_BRANCHES_TOOL,
             description=("Discover analyzed branches for a project. FOLLOW the response `instructions`: ask the user "
                          "whether ICX should fetch branches or they will paste the branch name; when `truncated` is "
                          "true, ask them to paste the name or supply a `query`. Never invent a branch name. Requires sonar_enabled."),
             inputSchema={"type": "object",
                          "properties": {"project": {"type": "string"}, "query": {"type": "string"}},
                          "required": ["project"]}),
        Tool(name=_SONAR_MEASURES_TOOL,
             description="USE WHEN you need the headline code-quality numbers for a project: MUST fetch project measures (bugs, vulnerabilities, code smells, security hotspots, coverage, duplication, technical debt, ratings, tests) for a project/branch here rather than guessing them. Requires sonar_enabled.",
             inputSchema={"type": "object",
                          "properties": {"project": {"type": "string"}, "branch": {"type": "string"}},
                          "required": ["project"]}),
        Tool(name=_SONAR_QUALITY_GATE_TOOL,
             description="USE WHEN deciding if code is releasable or why a build's quality gate failed: MUST fetch the quality gate status and failing conditions for a project/branch here - never assert pass/fail without it. Requires sonar_enabled.",
             inputSchema={"type": "object",
                          "properties": {"project": {"type": "string"}, "branch": {"type": "string"}},
                          "required": ["project"]}),
        Tool(name=_SONAR_FINDINGS_TOOL,
             description=("USE WHEN the user wants the specific Sonar issues on their code: MUST fetch scoped findings "
                          "(bugs, vulnerabilities, code smells, security hotspots) for a project/branch here - do not "
                          "invent findings. Scope to the files the developer is working on by passing `files` (a "
                          "user-supplied list of paths); omit `files` for the whole project. Filter with "
                          "types/severities/statuses/author/assignee/new_code_only. Requires sonar_enabled."),
             inputSchema=_SONAR_SCOPE_SCHEMA),
        Tool(name=_SONAR_REPORT_TOOL,
             description=("USE WHEN you need the complete code-quality picture in one call (prefer this over calling the "
                          "individual sonar_* tools separately): MUST assemble a full structured report for a "
                          "project/branch - quality gate, project measures, per-file measures, findings (issues + "
                          "security hotspots), duplication blocks, and test-coverage gaps. Pass `files` (user-supplied "
                          "paths) to scope everything to the developer's working set; omit for the whole project. "
                          "Requires sonar_enabled."),
             inputSchema=_SONAR_SCOPE_SCHEMA),
        Tool(name=_SONAR_TOP_FILES_TOOL,
             description=("USE WHEN the user wants to know WHICH files are worst for a metric (most duplicated, "
                          "least covered, most bugs, etc.) rather than already knowing the file: MUST call this to "
                          "rank/list files by a single metric - never guess which files are worst. Requires sonar_enabled."),
             inputSchema={"type": "object",
                          "properties": {
                              "project": {"type": "string"}, "branch": {"type": "string"},
                              "metric": {"type": "string"}, "limit": {"type": "integer"},
                              "ascending": {"type": "boolean"},
                          },
                          "required": ["project", "metric"]}),
        Tool(name=_SONAR_HISTORY_TOOL,
             description=("USE WHEN the user asks whether a metric is improving or degrading over time: MUST fetch "
                          "the chronological history for one or more metrics here rather than comparing a single "
                          "snapshot. Requires sonar_enabled."),
             inputSchema={"type": "object",
                          "properties": {
                              "project": {"type": "string"}, "branch": {"type": "string"},
                              "metrics": {"type": "array", "items": {"type": "string"}},
                              "date_from": {"type": "string"}, "date_to": {"type": "string"},
                          },
                          "required": ["project", "metrics"]}),
        Tool(name=_SONAR_ANALYSES_TOOL,
             description=("USE WHEN the user asks when scans ran or what versions/quality-gate events happened: "
                          "MUST fetch the project's analysis history here. Requires sonar_enabled."),
             inputSchema={"type": "object",
                          "properties": {
                              "project": {"type": "string"}, "branch": {"type": "string"},
                              "date_from": {"type": "string"}, "date_to": {"type": "string"},
                          },
                          "required": ["project"]}),
        Tool(name=_SONAR_RULE_TOOL,
             description=("USE WHEN a finding's `rule` key needs explaining (why it was flagged, how to fix it): "
                          "MUST fetch the rule's full description here rather than guessing what a rule key means. "
                          "Requires sonar_enabled."),
             inputSchema={"type": "object",
                          "properties": {"rule_key": {"type": "string"}},
                          "required": ["rule_key"]}),
        Tool(name=_SONAR_RULES_TOOL,
             description=("USE WHEN the user wants to browse or search rules by language/tag/repository "
                          "rather than looking up one specific rule key: MUST fetch the rule list here. "
                          "Requires sonar_enabled."),
             inputSchema={"type": "object",
                          "properties": {
                              "language": {"type": "string"},
                              "tags": {"type": "array", "items": {"type": "string"}},
                              "repositories": {"type": "array", "items": {"type": "string"}},
                              "query": {"type": "string"},
                              "page_size": {"type": "integer"},
                          },
                          "required": []}),
        Tool(name=_SONAR_HOTSPOT_TOOL,
             description=("USE WHEN a specific security hotspot needs full risk/fix detail beyond what "
                          "sonar_findings' summary shows: MUST fetch the hotspot's full detail here. "
                          "Requires sonar_enabled."),
             inputSchema={"type": "object",
                          "properties": {"hotspot_key": {"type": "string"}},
                          "required": ["hotspot_key"]}),
        Tool(name=_SONAR_SOURCE_TOOL,
             description=("USE WHEN you need to see the exact flagged source lines with coverage/duplication "
                          "context (not just the finding's message): MUST fetch annotated source lines here "
                          "rather than reading the file separately with no Sonar context. Requires sonar_enabled."),
             inputSchema={"type": "object",
                          "properties": {
                              "project": {"type": "string"}, "path": {"type": "string"},
                              "branch": {"type": "string"}, "from_line": {"type": "integer"}, "to_line": {"type": "integer"},
                          },
                          "required": ["project", "path"]}),
        Tool(name=_SONAR_METRICS_TOOL,
             description=("USE WHEN the user asks what a metric key means, or which metrics exist: MUST fetch "
                          "the metric catalog here rather than guessing. Requires sonar_enabled."),
             inputSchema={"type": "object", "properties": {"page_size": {"type": "integer"}}, "required": []}),
        Tool(name=_SONAR_QUALITY_GATE_DEFINITION_TOOL,
             description=("USE WHEN the user asks what quality gate is assigned to a project or what its "
                          "configured thresholds are: MUST fetch the gate's full authored definition here - "
                          "sonar_quality_gate only reports pass/fail for the LAST analysis, not the gate's own "
                          "configuration. Pass either project (to resolve the assigned gate) or gate_name "
                          "(to look up a specific gate by name). Requires sonar_enabled."),
             inputSchema={"type": "object",
                          "properties": {"project": {"type": "string"}, "gate_name": {"type": "string"}},
                          "required": []}),
        Tool(name=_SONAR_QUALITY_PROFILES_TOOL,
             description=("USE WHEN the user asks which quality profile is applied to a project or language, "
                          "or how many rules it enables: MUST fetch profiles here. Requires sonar_enabled."),
             inputSchema={"type": "object",
                          "properties": {"language": {"type": "string"}, "project": {"type": "string"}},
                          "required": []}),
        Tool(name=_SONAR_ISSUE_AUTHORS_TOOL,
             description=("USE WHEN the user asks who has open issues or wants to filter/scope by author: "
                          "MUST fetch the list of issue authors here. Requires sonar_enabled."),
             inputSchema={"type": "object",
                          "properties": {"project": {"type": "string"}, "query": {"type": "string"}},
                          "required": []}),
        Tool(name=_SONAR_ISSUE_TAGS_TOOL,
             description=("USE WHEN the user asks what issue tags exist or wants to filter/scope by tag: "
                          "MUST fetch the list of issue tags here. Requires sonar_enabled."),
             inputSchema={"type": "object",
                          "properties": {"project": {"type": "string"}, "query": {"type": "string"}},
                          "required": []}),
        Tool(name=_SONAR_ISSUE_CHANGELOG_TOOL,
             description=("USE WHEN the user asks about an issue's history (when assigned/resolved and by "
                          "whom): MUST fetch the issue's changelog here. Requires sonar_enabled."),
             inputSchema={"type": "object", "properties": {"issue_key": {"type": "string"}}, "required": ["issue_key"]}),
        Tool(name=_SONAR_SYSTEM_HEALTH_TOOL,
             description=("USE WHEN the user asks if the Sonar server itself is healthy (not just reachable): "
                          "MUST fetch system health here - sonar_status only confirms the server responds, "
                          "not whether it's degraded. Requires sonar_enabled."),
             inputSchema={"type": "object", "properties": {}, "required": []}),
        Tool(name=_SONAR_LANGUAGES_TOOL,
             description=("USE WHEN the user asks what languages this Sonar server analyzes: MUST fetch the "
                          "language list here. Requires sonar_enabled."),
             inputSchema={"type": "object", "properties": {"query": {"type": "string"}}, "required": []}),
    ] + GIT_TOOLS + JIRA_TOOLS + GITLAB_TOOLS


@server.call_tool()
async def _call_tool(name: str, arguments: dict | None) -> list[TextContent]:
    args = arguments or {}

    from icx_engine.git.mcp_tools import dispatch_git_tool
    git_result = await dispatch_git_tool(name, args)
    if git_result is not None:
        return git_result

    from icx_engine.gitlab.mcp_tools import dispatch_gitlab_tool
    gitlab_result = await dispatch_gitlab_tool(name, args)
    if gitlab_result is not None:
        return gitlab_result

    from icx_engine.jira.mcp_tools import dispatch_jira_tool
    jira_result = await dispatch_jira_tool(name, args)
    if jira_result is not None:
        return jira_result

    if name in (_FAST_TOOL_NAME, _FULL_TOOL_NAME):
        # Validate issue_ref
        issue_ref = args.get("issue_ref", "")
        if not isinstance(issue_ref, str) or not issue_ref.strip() or len(issue_ref) > 2048:
            return [TextContent(type="text", text=json.dumps(
                {"error": "issue_ref must be a non-empty string under 2048 characters."}
            ))]
        # Validate project_paths - empty list is allowed (ICX resolves from ticket key)
        project_paths_raw = args.get("project_paths")
        if project_paths_raw is None:
            project_paths_raw = []
        if not isinstance(project_paths_raw, list):
            return [TextContent(type="text", text=json.dumps({
                "status": "error",
                "code": "INVALID_PROJECT_PATH",
                "message": "project_paths must be a list of strings.",
                "action_required": "ask_user_for_project_path",
            }))]
        project_paths: list[str] = []
        for p in project_paths_raw:
            if not isinstance(p, str) or len(p) > 4096:
                return [TextContent(type="text", text=json.dumps({
                    "status": "error",
                    "code": "INVALID_PROJECT_PATH",
                    "message": "Each path in project_paths must be a string under 4096 characters.",
                    "action_required": "ask_user_for_project_path",
                }))]
            stripped = p.strip()
            if stripped:
                project_paths.append(stripped)
        # Empty project_paths is valid - handler resolves from ticket's Jira project key

        # Validate profile
        profile = args.get("profile")
        if profile is not None:
            if not isinstance(profile, str) or len(profile) > 256:
                return [TextContent(type="text", text=json.dumps(
                    {"error": "profile must be a string under 256 characters."}
                ))]
            profile = profile.strip() or None

        skip_vision = name == _FAST_TOOL_NAME
        text = await _handle_analyze_issue(
            issue_ref.strip(),
            project_paths=project_paths,
            profile=profile,
            skip_vision=skip_vision,
        )
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                work_item = parsed.get("work_item") or {}
                rank_prompt = f"{work_item.get('type', '')} {work_item.get('summary', '')}".strip() or None
                text = json.dumps(attach_skill_hint(
                    parsed, "ticket-context-analysis", rank_prompt=rank_prompt, archetype="ticket",
                ))
        except Exception:
            pass
        return [TextContent(type="text", text=text)]

    if name == _MEM_SEARCH_TOOL:
        query = args.get("query", "")
        if not isinstance(query, str) or not query.strip() or len(query) > 2000:
            return [TextContent(type="text", text=json.dumps(
                {"error": "query must be a non-empty string under 2000 characters."}
            ))]
        tags = args.get("tags") or []
        if not isinstance(tags, list):
            return [TextContent(type="text", text=json.dumps(
                {"error": "tags must be a list of strings."}
            ))]
        if len(tags) > 20:
            return [TextContent(type="text", text=json.dumps(
                {"error": "tags must not contain more than 20 entries."}
            ))]
        for _tag in tags:
            if not isinstance(_tag, str) or len(_tag) > 256:
                return [TextContent(type="text", text=json.dumps(
                    {"error": "Each tag must be a string under 256 characters."}
                ))]
        top_k_raw = args.get("top_k", 5)
        try:
            top_k = max(1, min(20, int(top_k_raw)))
        except (TypeError, ValueError):
            return [TextContent(type="text", text=json.dumps(
                {"error": "top_k must be an integer between 1 and 20."}
            ))]
        mem_state = _get_memory_state()
        if mem_state != "ready":
            return [TextContent(type="text", text=json.dumps(
                {"results": [], "count": 0, "status": mem_state,
                 "note": f"Memory not ready (status={mem_state!r}) - skipping search."}
            ))]
        from icx_engine.memory.schema import MemoryQueryInput
        import functools
        qi = MemoryQueryInput(
            issue_key="search",
            project_key="",
            source_type="",
            summary=query.strip(),
            description=query.strip(),
            issue_type="",
            tags=[str(t) for t in tags],
        )
        try:
            loop = asyncio.get_running_loop()
            smart = await loop.run_in_executor(
                _get_memory_executor(),
                functools.partial(_search_memory_sync, qi, top_k),
            )
            results = smart.get("results", [])
            return [TextContent(type="text", text=json.dumps({
                "results": results,
                "negative_signals": smart.get("negative_signals", []),
                "count": len(results),
                "status": "ok",
                "decay_applied": smart.get("decay_applied", False),
            }))]
        except Exception as exc:
            return [TextContent(type="text", text=json.dumps({"error": str(exc)}))]

    elif name in (_GRAPH_CONTEXT_TOOL, _GRAPH_CHAIN_TOOL, _GRAPH_IMPACT_TOOL, _GRAPH_SUBSYSTEM_TOOL):
        project_path_raw = args.get("project_path", "")
        if not isinstance(project_path_raw, str) or not project_path_raw.strip():
            return [TextContent(type="text", text=json.dumps({
                "status": "error",
                "code": "NO_PATH",
                "message": "project_path is required. Ask the user which project path to use.",
                "action_required": "ask_user_for_path",
            }))]

        task_str = str(args.get("task", "")).strip()
        node_id = str(args.get("node_id", "")).strip()
        file_path_str = str(args.get("file_path", "")).strip()

        # Fast param validation before hitting disk (stays on event loop - no I/O).
        if name == _GRAPH_CONTEXT_TOOL and not task_str:
            return [TextContent(type="text", text=json.dumps({
                "status": "error",
                "code": "MISSING_PARAM",
                "message": "task parameter is required and must be non-empty.",
                "action_required": "retry_with_task_description",
            }))]
        if name in (_GRAPH_CHAIN_TOOL, _GRAPH_IMPACT_TOOL) and not node_id:
            return [TextContent(type="text", text=json.dumps({
                "status": "error",
                "code": "MISSING_PARAM",
                "message": "node_id is required. Use graph_find_context first to get valid node IDs.",
                "action_required": "call_graph_find_context_first",
            }))]
        if name == _GRAPH_SUBSYSTEM_TOOL and not file_path_str:
            return [TextContent(type="text", text=json.dumps({
                "status": "error",
                "code": "MISSING_PARAM",
                "message": "file_path is required (relative path, e.g. 'src/auth/service.py').",
                "action_required": "retry_with_file_path",
            }))]

        # All disk/git I/O runs in a worker thread so the event loop is never blocked.
        # Windows ProactorEventLoop + synchronous subprocess.run = IOCP handle conflict
        # that causes subprocess.communicate() to hang indefinitely despite timeout=.
        # Moving everything off the event loop thread avoids this entirely.
        _raw_path = project_path_raw.strip()
        _token_budget = int(args.get("token_budget", 8000))
        _min_confidence = float(args.get("min_confidence", 0.0))
        _depth = int(args.get("depth", 3))

        def _run_graph_tool() -> dict:
            from icx_engine.graph import storage as _st
            from dataclasses import asdict as _asdict

            _result = _resolve_graph_path(_raw_path)
            if isinstance(_result, dict):
                return _result
            _project_path, _project_id, staleness_warning = _result

            graph_json = _st.graph_path(_project_id)
            if not graph_json.exists():
                return _degraded_graph_response(
                    code="NO_GRAPH",
                    project_path=_project_path,
                    warn_user=(
                        f"The codebase graph is not built for '{_project_path}', so I'm "
                        "answering from direct file search (grep/read) instead. For richer "
                        f"results, build the graph: icx graph build \"{_project_path}\""
                    ),
                )

            q = _cached_querier(graph_json)
            if name == _GRAPH_CONTEXT_TOOL:
                results = q.find_context(
                    task=task_str,
                    token_budget=_token_budget,
                    min_confidence=_min_confidence,
                    source_root=_project_path,
                )
                payload = {"status": "ok", "project_path": str(_project_path), "results": [_asdict(r) for r in results]}
            elif name == _GRAPH_CHAIN_TOOL:
                chain = q.get_call_chain(
                    node_id=node_id,
                    depth=_depth,
                    min_confidence=_min_confidence,
                )
                payload = {
                    "status": "ok",
                    "project_path": str(_project_path),
                    "upstream": [_asdict(n) for n in chain.upstream],
                    "downstream": [_asdict(n) for n in chain.downstream],
                }
            elif name == _GRAPH_IMPACT_TOOL:
                impact = q.get_impact(node_id=node_id, min_confidence=_min_confidence)
                payload = {"status": "ok", "project_path": str(_project_path), **_asdict(impact)}
            else:
                sub = q.get_subsystem(file_path_str)
                payload = {"status": "ok", "project_path": str(_project_path), **_asdict(sub)}

            if staleness_warning:
                payload["staleness_warning"] = staleness_warning
            return payload

        try:
            loop = asyncio.get_running_loop()
            payload = await loop.run_in_executor(None, _run_graph_tool)
            return [TextContent(type="text", text=json.dumps(payload))]

        except Exception as exc:
            _log.exception("graph query tool failed")
            return [TextContent(type="text", text=json.dumps({
                "status": "error",
                "code": "INTERNAL_ERROR",
                "message": str(exc),
                "action_required": "report_error_to_user",
            }))]

    if name == _GRAPH_CROSS_LINKS_TOOL:
        project_path_raw = args.get("project_path", "")
        if not isinstance(project_path_raw, str) or not project_path_raw.strip():
            return [TextContent(type="text", text=json.dumps({
                "status": "error",
                "code": "NO_PATH",
                "message": "project_path is required. Ask the user which project path to use.",
                "action_required": "ask_user_for_path",
            }))]

        _raw_path_cl = project_path_raw.strip()

        _cochange_file = (args.get("file_path") or "").strip()

        def _run_cross_links() -> dict:
            from icx_engine.graph import storage as _st

            _result_cl = _resolve_graph_path(_raw_path_cl)
            if isinstance(_result_cl, dict):
                return _result_cl
            _project_path, _project_id, staleness_warning = _result_cl

            cl_path = _st.cross_links_path(_project_id)
            if cl_path.exists():
                data = json.loads(cl_path.read_text(encoding="utf-8"))
            else:
                data = {"links": [], "source_project": _project_id}
            data["status"] = "ok"
            if staleness_warning:
                data["staleness_warning"] = staleness_warning

            if _cochange_file:
                try:
                    _graph_json = _st.graph_path(_project_id)
                    if _graph_json.exists():
                        _q = _cached_querier(_graph_json)
                        data["co_changed_files"] = _q.get_cochange_partners(_cochange_file)
                    else:
                        data["co_changed_files"] = []
                except Exception:
                    data["co_changed_files"] = []

            return data

        try:
            loop = asyncio.get_running_loop()
            data = await loop.run_in_executor(None, _run_cross_links)
            return [TextContent(type="text", text=json.dumps(data))]

        except Exception as _e:
            _log.exception("graph_cross_links tool failed")
            return [TextContent(type="text", text=json.dumps({
                "status": "error",
                "code": "INTERNAL_ERROR",
                "message": str(_e),
                "action_required": "report_error_to_user",
            }))]

    if name == _GRAPH_IMPORTANT_NODES_TOOL:
        project_path = args.get("project_path", "")
        if not isinstance(project_path, str) or not project_path.strip():
            return [TextContent(type="text", text=json.dumps({
                "status": "error",
                "code": "NO_PATH",
                "message": "project_path is required.",
            }))]
        top_k_raw = args.get("top_k", 10)
        try:
            top_k = max(1, min(100, int(top_k_raw)))
        except (TypeError, ValueError):
            top_k = 10

        def _run_important_nodes() -> dict:
            _r = _load_querier_simple(project_path)
            if isinstance(_r, dict):
                return _r
            q, _proj = _r
            important = q.get_important_nodes(top_k)
            result_nodes = [
                {
                    "file": n.get("source_file", n.get("file", "")),
                    "name": n.get("label", n.get("id", "")),
                    "pagerank": n.get("pagerank", 0.0),
                    "betweenness": n.get("betweenness", 0.0),
                    "importance": n.get("importance", 0.0),
                }
                for n in important
            ]
            return {"important_nodes": result_nodes, "total": len(result_nodes)}

        try:
            loop = asyncio.get_running_loop()
            result_dict = await loop.run_in_executor(None, _run_important_nodes)
            return [TextContent(type="text", text=json.dumps(result_dict))]
        except Exception as exc:
            return [TextContent(type="text", text=json.dumps({"error": str(exc)}))]

    if name == _GRAPH_BLAST_RADIUS_TOOL:
        project_path = args.get("project_path", "")
        if not isinstance(project_path, str) or not project_path.strip():
            return [TextContent(type="text", text=json.dumps({
                "status": "error", "code": "NO_PATH",
                "message": "project_path is required.",
                "action_required": "ask_user_for_path",
            }))]
        changed_files_raw = args.get("changed_files", [])
        if not isinstance(changed_files_raw, list):
            return [TextContent(type="text", text=json.dumps(
                {"error": "changed_files must be a list of strings."}
            ))]
        changed_files = [str(f) for f in changed_files_raw]
        try:
            max_depth = max(1, min(20, int(args.get("max_depth", 5))))
        except (TypeError, ValueError):
            max_depth = 5
        try:
            min_confidence = float(args.get("min_confidence", 0.3))
            min_confidence = max(0.0, min(1.0, min_confidence))
        except (TypeError, ValueError):
            min_confidence = 0.3

        def _run_blast_radius() -> dict:
            _r = _load_querier_simple(project_path)
            if isinstance(_r, dict):
                return _r
            q, _proj = _r
            return q.get_blast_radius(changed_files, max_depth=max_depth, min_confidence=min_confidence)

        try:
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(None, _run_blast_radius)
            return [TextContent(type="text", text=json.dumps(result))]
        except Exception as exc:
            return [TextContent(type="text", text=json.dumps({"error": str(exc)}))]

    if name == _GRAPH_CYCLES_TOOL:
        project_path = args.get("project_path", "")
        if not isinstance(project_path, str) or not project_path.strip():
            return [TextContent(type="text", text=json.dumps({
                "status": "error", "code": "NO_PATH",
                "message": "project_path is required.",
                "action_required": "ask_user_for_path",
            }))]
        try:
            max_cycles = max(1, min(100, int(args.get("max_cycles", 20))))
        except (TypeError, ValueError):
            max_cycles = 20

        def _run_cycles() -> dict:
            _r = _load_querier_simple(project_path)
            if isinstance(_r, dict):
                return _r
            q, _proj = _r
            cycles = q.get_cycles(max_cycles=max_cycles)
            return {"cycles": cycles, "cycle_count": len(cycles)}

        try:
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(None, _run_cycles)
            return [TextContent(type="text", text=json.dumps(result))]
        except Exception as exc:
            return [TextContent(type="text", text=json.dumps({"error": str(exc)}))]

    if name == _GRAPH_DEAD_CODE_TOOL:
        project_path = args.get("project_path", "")
        if not isinstance(project_path, str) or not project_path.strip():
            return [TextContent(type="text", text=json.dumps({
                "status": "error", "code": "NO_PATH",
                "message": "project_path is required.",
                "action_required": "ask_user_for_path",
            }))]

        def _run_dead_code() -> dict:
            _r = _load_querier_simple(project_path)
            if isinstance(_r, dict):
                return _r
            q, _proj = _r
            dead = q.get_dead_code()
            return {"dead_code_candidates": dead, "count": len(dead)}

        try:
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(None, _run_dead_code)
            return [TextContent(type="text", text=json.dumps(result))]
        except Exception as exc:
            return [TextContent(type="text", text=json.dumps({"error": str(exc)}))]

    if name == _GRAPH_OWNERSHIP_TOOL:
        project_path = args.get("project_path", "")
        if not isinstance(project_path, str) or not project_path.strip():
            return [TextContent(type="text", text=json.dumps({
                "status": "error", "code": "NO_PATH",
                "message": "project_path is required.",
                "action_required": "ask_user_for_path",
            }))]
        file_path = args.get("file_path", "")
        if not isinstance(file_path, str) or not file_path.strip():
            return [TextContent(type="text", text=json.dumps(
                {"error": "file_path must be a non-empty string."}
            ))]
        file_path = file_path.strip()
        _fp_norm = file_path.replace("\\", "/")
        if (
            "\x00" in file_path
            or any(p == ".." for p in _fp_norm.split("/"))
            or _fp_norm.startswith("/")
            or (len(file_path) > 1 and file_path[1] == ":")
        ):
            return [TextContent(type="text", text=json.dumps({
                "error": "file_path must be a relative path with no directory traversal."
            }))]

        def _run_ownership() -> dict:
            _r = _load_querier_simple(project_path)
            if isinstance(_r, dict):
                return _r
            q, _proj = _r
            return q.get_ownership(file_path, str(_proj))

        try:
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(None, _run_ownership)
            return [TextContent(type="text", text=json.dumps(result))]
        except Exception as exc:
            return [TextContent(type="text", text=json.dumps({"error": str(exc)}))]

    if name == _MEM_HOTSPOTS_TOOL:
        project_key = args.get("project_key") or None
        if project_key is not None and (not isinstance(project_key, str) or len(project_key) > 64):
            return [TextContent(type="text", text=json.dumps(
                {"error": "project_key must be a string under 64 characters."}
            ))]
        top_n_raw = args.get("top_n", 20)
        try:
            top_n = max(1, min(100, int(top_n_raw)))
        except (TypeError, ValueError):
            return [TextContent(type="text", text=json.dumps(
                {"error": "top_n must be an integer between 1 and 100."}
            ))]
        try:
            loop = asyncio.get_running_loop()
            hotspots = await loop.run_in_executor(
                _get_memory_executor(), _get_hotspots_sync, project_key, top_n
            )
            return [TextContent(type="text", text=json.dumps({"results": hotspots, "count": len(hotspots)}))]
        except Exception as exc:
            return [TextContent(type="text", text=json.dumps({"error": str(exc)}))]

    if name == _MEM_BY_FILE_TOOL:
        file_path = args.get("file_path", "")
        if not isinstance(file_path, str) or not file_path.strip():
            return [TextContent(type="text", text=json.dumps(
                {"error": "file_path must be a non-empty string."}
            ))]
        project_key = args.get("project_key") or None
        if project_key is not None and (not isinstance(project_key, str) or len(project_key) > 64):
            return [TextContent(type="text", text=json.dumps(
                {"error": "project_key must be a string under 64 characters."}
            ))]
        try:
            loop = asyncio.get_running_loop()
            entries = await loop.run_in_executor(
                _get_memory_executor(), _find_by_file_sync, file_path.strip(), project_key
            )
            return [TextContent(type="text", text=json.dumps({"results": entries, "count": len(entries)}))]
        except Exception as exc:
            return [TextContent(type="text", text=json.dumps({"error": str(exc)}))]

    if name == _MEM_RELATED_TOOL:
        issue_key = args.get("issue_key") or None
        if issue_key is not None:
            if not isinstance(issue_key, str) or not issue_key.strip():
                return [TextContent(type="text", text=json.dumps(
                    {"error": "issue_key must be a non-empty string."}
                ))]
            issue_key = issue_key.strip().upper()
        files = args.get("files") or None
        if files is not None:
            if not isinstance(files, list):
                return [TextContent(type="text", text=json.dumps(
                    {"error": "files must be a list of strings."}
                ))]
            if not all(isinstance(f, str) and f.strip() for f in files):
                return [TextContent(type="text", text=json.dumps(
                    {"error": "each entry in files must be a non-empty string."}
                ))]
            files = [f.strip() for f in files]
        if not issue_key and not files:
            return [TextContent(type="text", text=json.dumps(
                {"error": "provide files (new ticket) or issue_key (reopened ticket)."}
            ))]
        project_key = args.get("project_key") or None
        if project_key is not None and (not isinstance(project_key, str) or len(project_key) > 64):
            return [TextContent(type="text", text=json.dumps(
                {"error": "project_key must be a string under 64 characters."}
            ))]
        try:
            loop = asyncio.get_running_loop()
            related = await loop.run_in_executor(
                _get_memory_executor(), _get_related_sync, issue_key, project_key, files
            )
            return [TextContent(type="text", text=json.dumps({"results": related, "count": len(related)}))]
        except Exception as exc:
            return [TextContent(type="text", text=json.dumps({"error": str(exc)}))]

    if name == _MEM_PATTERNS_TOOL:
        project_key = args.get("project_key") or None
        if project_key is not None and (not isinstance(project_key, str) or len(project_key) > 64):
            return [TextContent(type="text", text=json.dumps(
                {"error": "project_key must be a string under 64 characters."}
            ))]
        try:
            loop = asyncio.get_running_loop()
            patterns = await loop.run_in_executor(
                _get_memory_executor(), _get_patterns_sync, project_key
            )
            return [TextContent(type="text", text=json.dumps({"results": patterns, "count": len(patterns)}))]
        except Exception as exc:
            return [TextContent(type="text", text=json.dumps({"error": str(exc)}))]

    if name == _MEM_DELETE_TOOL:
        from icx_engine.confirm import issue_token, verify_token
        confirm_token = args.get("confirm_token")
        if not confirm_token:
            issue_key, err = _validate_issue_key_arg(args, "issue_key")
            if err:
                return err
            token = issue_token("memory_delete", {"issue_key": issue_key})
            return [TextContent(type="text", text=json.dumps({
                "status": "pending_confirmation",
                "token": token,
                "issue_key": issue_key,
                "instruction": "Show the human which memory entry (issue_key) is about to be "
                               "permanently deleted. Only call this tool again with confirm_token "
                               "set once they explicitly agree.",
            }))]
        payload = verify_token(confirm_token, "memory_delete")
        if payload is None:
            return [TextContent(type="text", text=json.dumps(
                {"ok": False, "error": "Invalid or already-used confirm_token. Call again without a token to get a fresh one."}
            ))]
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(_get_memory_executor(), _delete_entry_sync, payload["issue_key"])
            return [TextContent(type="text", text=json.dumps({"ok": True, "issue_key": payload["issue_key"]}))]
        except Exception as exc:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": str(exc)}))]

    if name == _MEM_UPDATE_TOOL:
        issue_key, err = _validate_issue_key_arg(args, "issue_key")
        if err:
            return err
        from icx_engine.memory.manager import _UPDATE_ALLOWED_FIELDS
        fields: dict = {}
        for field_name in _UPDATE_ALLOWED_FIELDS:
            if field_name not in args:
                continue
            value = args[field_name]
            if field_name in ("files_changed", "tags"):
                if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                    return [TextContent(type="text", text=json.dumps(
                        {"ok": False, "error": f"{field_name} must be a list of strings."}
                    ))]
                fields[field_name] = [str(v) for v in value]
            else:
                if not isinstance(value, str):
                    return [TextContent(type="text", text=json.dumps(
                        {"ok": False, "error": f"{field_name} must be a string."}
                    ))]
                fields[field_name] = value
        if not fields:
            return [TextContent(type="text", text=json.dumps(
                {"ok": False, "error": f"No updatable field given. Allowed: {sorted(_UPDATE_ALLOWED_FIELDS)}."}
            ))]
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                _get_memory_executor(), _update_entry_sync, issue_key, fields,
            )
            return [TextContent(type="text", text=json.dumps({
                "ok": True, "issue_key": issue_key, "updated_fields": sorted(fields),
            }))]
        except Exception as exc:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": str(exc)}))]

    if name == _RECORD_VERIFICATION_TOOL:
        from icx_engine.verification import validate_evidence, build_confidence_report, compute_risk_tier
        issue_key = args.get("issue_key", "")
        if not isinstance(issue_key, str) or not issue_key.strip():
            return [TextContent(type="text", text=json.dumps(
                {"error": "issue_key must be a non-empty string."}))]
        items = args.get("dod_items")
        if not isinstance(items, list) or not items:
            return [TextContent(type="text", text=json.dumps(
                {"error": "dod_items must be a non-empty list."}))]
        norm = [{
            "check": str(it.get("check", "")),
            "method": str(it.get("method", "")),
            "passed": bool(it.get("passed", False)),
            "command": str(it.get("command", "")),
            "output": str(it.get("output", ""))[:4000],
        } for it in items if isinstance(it, dict)]
        result = validate_evidence(norm)
        layers_run = [str(x) for x in (args.get("layers_run") or [])]
        tier = compute_risk_tier(_session_get(issue_key.strip(), "analysis", {}) or {})
        confidence = build_confidence_report(norm, tier, layers_run)
        if result["accepted"]:
            _session_set(issue_key.strip(), "verification", {"items": norm, "confidence": confidence})
        return [TextContent(type="text", text=json.dumps(
            {"accepted": result["accepted"], "missing": result["missing"], "confidence": confidence}))]

    if name == _GET_METHODOLOGY_TOOL:
        from icx_engine.methodology import full_text, METHODOLOGY_VERSION
        return [TextContent(type="text", text=json.dumps(
            {"version": METHODOLOGY_VERSION, "methodology": full_text()}))]

    if name == _BOOST_TOOL:
        prompt = args.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            return [TextContent(type="text", text=json.dumps(
                {"error": "prompt must be a non-empty string."}))]
        repo_path = args.get("repo_path") if isinstance(args.get("repo_path"), str) else None
        current_file = args.get("current_file") if isinstance(args.get("current_file"), str) else None
        is_continuation = bool(args.get("is_continuation"))
        links_in = args.get("links") if isinstance(args.get("links"), list) else []
        brief = _boosted(prompt, repo_path=repo_path, current_file=current_file,
                         is_continuation=is_continuation, links_in=links_in)
        return [TextContent(type="text", text=json.dumps(brief))]

    if name == _BOOST_REFINE_TOOL:
        prompt = args.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            return [TextContent(type="text", text=json.dumps({"error": "prompt must be a non-empty string."}))]
        # Structured spec (all optional; ICX fills any gap). `dims` kept for back-compat.
        def _slist(v):
            return [str(x) for x in v] if isinstance(v, list) else []
        spec = {
            "objective": str(args.get("objective", "")) if isinstance(args.get("objective"), str) else "",
            "requirements": _slist(args.get("requirements")),
            "constraints": _slist(args.get("constraints")),
            "acceptance": _slist(args.get("acceptance")),
            "deliverable": str(args.get("deliverable", "")) if isinstance(args.get("deliverable"), str) else "",
            "dims": _slist(args.get("dims")),
        }
        if not (spec["objective"] or spec["requirements"] or spec["dims"]):
            return [TextContent(type="text", text=json.dumps(
                {"error": "supply at least one of: objective, requirements, dims (your drafted spec)."}))]
        repo_path = args.get("repo_path") if isinstance(args.get("repo_path"), str) else None
        current_file = args.get("current_file") if isinstance(args.get("current_file"), str) else None
        try:
            from icx_engine.boost.classify import classify
            from icx_engine.boost.refine import compose_cto_prompt, merge_dims
            from icx_engine.methodology import _GATE_SEQUENCE
            archetype = args.get("archetype") if isinstance(args.get("archetype"), str) and args.get("archetype") else classify(prompt)
            # Gather context the same way icx_boost does (adaptive) so the CTO prompt carries it too.
            context = {"files": []}
            try:
                env = _boost_env(repo_path, False)
                from icx_engine.boost.router import plan_activation
                from icx_engine.context_completeness import fan_out, fuse_rank
                plan = plan_activation(prompt, archetype, env)
                if plan.signals and repo_path:
                    seeds = [current_file] if current_file else []
                    kw = [w for w in prompt.lower().split() if len(w) > 3][:8]
                    g, gr, se, me = _context_signals(repo_path, seeds, kw)
                    sig = {"graph": g, "grep": gr, "semantic": se, "memory": me}
                    active = {k: (sig[k] if k in plan.signals else None) for k in sig}
                    loop = asyncio.get_running_loop()
                    candidates = await loop.run_in_executor(
                        None,
                        lambda: fan_out(seeds, graph=active["graph"], grep=active["grep"],
                                         semantic=active["semantic"], memory=active["memory"]),
                    )
                    scored = fuse_rank(candidates)
                    context["files"] = [s.to_dict() for s in scored if s.tier != "seed"][:20]
            except Exception:
                pass
            cto = compose_cto_prompt(prompt, archetype, spec, context)
            return [TextContent(type="text", text=json.dumps({
                "archetype": archetype,
                "merged_requirements": merge_dims(archetype, spec["requirements"] + spec["dims"]),
                "boosted_prompt": cto,
                "gates": list(_GATE_SEQUENCE),
                "boost_meta": {"deterministic": True, "llm_used": False, "pass": 2},
                "mandatory_directive": ("This is your CTO-grade working spec - a persona-scoped, "
                                        "fully-structured version of the request. Answer it completely to "
                                        "its acceptance criteria; pass the gates (lock_plan before coding, "
                                        "record_verification before done). Do not fall back to the raw prompt."),
            }))]
        except Exception as exc:
            return [TextContent(type="text", text=json.dumps(
                {"error": f"refine failed: {exc}", "boosted_prompt": prompt}))]

    if name == _LOCK_PLAN_TOOL:
        issue_ref = args.get("issue_ref", "")
        chosen = args.get("chosen_files")
        if not isinstance(issue_ref, str) or not issue_ref.strip():
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": "issue_ref must be a non-empty string."}))]
        if not isinstance(chosen, list) or not chosen:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": "chosen_files must be a non-empty list of the files you plan to change."}))]
        chosen = [str(f) for f in chosen]
        justifications = args.get("justifications") if isinstance(args.get("justifications"), dict) else {}
        justifications = {str(k): str(v) for k, v in justifications.items()}
        keywords = [str(k) for k in (args.get("keywords") or [])]
        from icx_engine.testing.nodes import _local_repo_root
        project_path = args.get("project_path") or _local_repo_root({"file_paths": chosen})

        from icx_engine.context_completeness import fan_out, fuse_rank, miss_check
        graph_sig, grep_sig, semantic_sig, memory_sig = _context_signals(project_path, chosen, keywords)
        loop = asyncio.get_running_loop()
        candidates = await loop.run_in_executor(
            None,
            lambda: fan_out(chosen, graph=graph_sig, grep=grep_sig, semantic=semantic_sig, memory=memory_sig),
        )
        scored = fuse_rank(candidates, prior_fix=_lock_plan_prior_fix(chosen))
        result = miss_check(chosen, scored, justifications)
        _session_set(issue_ref.strip(), "locked_plan", {
            "chosen": chosen, "justifications": justifications,
            "coverage": result["coverage"], "ok": result["ok"],
        })
        return [TextContent(type="text", text=json.dumps({
            "ok": result["ok"],
            "coverage": result["coverage"],
            "blocking_missed": result["blocking_missed"],
            "advisory_missed": [m for m in result["missed"] if not m["blocking"]],
            "accepted": result["accepted"],
            "instruction": (
                "Do NOT write code until ok is true. For each entry in blocking_missed, either add the "
                "file to chosen_files and call lock_plan again, or pass justifications[path]='reason' "
                "if it is genuinely irrelevant. advisory_missed is optional context to consider."
            ),
        }))]

    if name in (_UI_AUTH_CAPTURE_TOOL, _UI_AUTH_INLINE_TOOL):
        url = args.get("url")
        file_paths = args.get("file_paths")
        if not isinstance(url, str) or not url.strip():
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": "url must be a non-empty string."}))]
        if not isinstance(file_paths, list) or not file_paths:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": "file_paths must be a non-empty list."}))]
        from icx_engine.testing import auth as _auth, ui_auth as _ui_auth
        from icx_engine.testing.nodes import _resolve_project_id
        from icx_engine.testing.runners.install import is_installed
        if not is_installed("playwright"):
            return [TextContent(type="text", text=json.dumps({
                "ok": False,
                "error": ("UI tooling (Playwright + Chromium) is not installed. Run "
                          "'icx test setup' once to download it into ~/.icx/testing (it does NOT touch "
                          "your repo), then retry."),
            }))]
        project = _resolve_project_id([str(f) for f in file_paths]) or "unknown"
        host = _auth.host_of(url)
        if not host:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": "url has no host."}))]
        success_url = args.get("success_url") or ""
        try:
            if name == _UI_AUTH_CAPTURE_TOOL:
                path, detail = await _ui_auth.capture_session(project, host, url, success_url=success_url)
            else:
                username = args.get("username")
                password = args.get("password")
                if not isinstance(username, str) or not username or not isinstance(password, str) or not password:
                    return [TextContent(type="text", text=json.dumps(
                        {"ok": False, "error": "username and password are required for inline login."}))]
                path, detail = await _ui_auth.inline_session(
                    project, host, url, username, password, success_url=success_url,
                    user_selector=args.get("user_selector") or "",
                    pass_selector=args.get("pass_selector") or "",
                    submit_selector=args.get("submit_selector") or "",
                )
        except Exception as exc:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": str(exc)}))]
        if path:
            return [TextContent(type="text", text=json.dumps({"ok": True, "storage_state": path}))]
        _d = (detail or "").strip()
        _low = _d.lower()
        _tool_broken = any(s in _low for s in (
            "playwright", "cannot find package", "module_not_found", "err_module_not_found",
            "executable doesn't exist", "chromium"))
        if _tool_broken:
            err = (f"ICX's own UI tooling (Playwright/Chromium under ~/.icx/testing) is missing or "
                   f"broken. DO NOT install Playwright, npm, or npx into the user's repo - ICX brings "
                   f"its own. Fix it by running in a terminal: `icx test setup --force`. "
                   f"(harness detail: {_d[:300]})")
        else:
            err = f"session capture failed: {_d}"
        return [TextContent(type="text", text=json.dumps({"ok": False, "error": err}))]

    if name == _SAVE_TOOL_NAME:
        issue_key = args.get("issue_key", "")
        if not isinstance(issue_key, str) or not issue_key.strip() or len(issue_key) > 2048:
            return [TextContent(type="text", text=json.dumps(
                {"error": "issue_key must be a non-empty string under 2048 characters."}
            ))]
        summary = args.get("summary", "")
        if not isinstance(summary, str) or not summary.strip() or len(summary) > 500:
            return [TextContent(type="text", text=json.dumps(
                {"error": "summary must be a non-empty string under 500 characters."}
            ))]
        problem_description = args.get("problem_description", "")
        if not isinstance(problem_description, str) or not problem_description.strip() or len(problem_description) > 2000:
            return [TextContent(type="text", text=json.dumps(
                {"error": "problem_description must be a non-empty string under 2000 characters."}
            ))]
        resolution_note = args.get("resolution_note", "")
        if not isinstance(resolution_note, str) or not resolution_note.strip() or len(resolution_note) > 10000:
            return [TextContent(type="text", text=json.dumps(
                {"error": "resolution_note must be a non-empty string under 10000 characters."}
            ))]
        files_changed = args.get("files_changed")
        if not isinstance(files_changed, list):
            return [TextContent(type="text", text=json.dumps(
                {"error": "files_changed must be a list of strings."}
            ))]
        if len(files_changed) > 100:
            return [TextContent(type="text", text=json.dumps(
                {"error": "files_changed must not contain more than 100 entries."}
            ))]
        for _fc in files_changed:
            if not isinstance(_fc, str) or len(_fc) > 4096:
                return [TextContent(type="text", text=json.dumps(
                    {"error": "Each entry in files_changed must be a string under 4096 characters."}
                ))]
        tags = args.get("tags")
        if not isinstance(tags, list):
            return [TextContent(type="text", text=json.dumps(
                {"error": "tags must be a list of strings."}
            ))]
        if len(tags) > 50:
            return [TextContent(type="text", text=json.dumps(
                {"error": "tags must not contain more than 50 entries."}
            ))]
        for _tag in tags:
            if not isinstance(_tag, str) or len(_tag) > 256:
                return [TextContent(type="text", text=json.dumps(
                    {"error": "Each entry in tags must be a string under 256 characters."}
                ))]
        work_item_type = args.get("work_item_type", "")
        if not isinstance(work_item_type, str) or not work_item_type.strip() or len(work_item_type) > 100:
            return [TextContent(type="text", text=json.dumps(
                {"error": "work_item_type must be a non-empty string under 100 characters."}
            ))]
        pattern_used = args.get("pattern_used") or ""
        if not isinstance(pattern_used, str) or len(pattern_used) > 2000:
            return [TextContent(type="text", text=json.dumps(
                {"error": "pattern_used must be a string under 2000 characters."}
            ))]

        # Phase 1: root_cause_pattern validation
        from icx_engine.memory.schema import ROOT_CAUSE_PATTERNS
        root_cause_pattern = args.get("root_cause_pattern") or "uncategorized"
        if root_cause_pattern not in ROOT_CAUSE_PATTERNS:
            return [TextContent(type="text", text=json.dumps(
                {"error": "root_cause_pattern must be one of the canonical patterns. Use 'uncategorized' if none fit.",
                 "valid_patterns": sorted(ROOT_CAUSE_PATTERNS)}
            ))]
        pattern_confidence = float(args.get("pattern_confidence") or 0.0)
        outcome_verified = bool(args.get("outcome_verified", False))
        outcome_feedback_note = args.get("outcome_feedback_note") or ""
        negate = bool(args.get("negate", False))
        negation_reason = args.get("negation_reason") or ""

        if outcome_verified and not outcome_feedback_note.strip():
            return [TextContent(type="text", text=json.dumps(
                {"error": "outcome_feedback_note is required when outcome_verified is true."}
            ))]
        if negate and not negation_reason.strip():
            return [TextContent(type="text", text=json.dumps(
                {"error": "negation_reason is required when negate is true."}
            ))]
        if negate and outcome_verified:
            return [TextContent(type="text", text=json.dumps(
                {"error": "A resolution cannot be simultaneously verified and negated."}
            ))]

        # DoD gate: a verified success needs accepted verification evidence, unless the user
        # verified manually (verified_by_human=true - the manual override lane).
        verified_by_human = bool(args.get("verified_by_human", False))
        if outcome_verified and not verified_by_human:
            _vrec = _session_get(issue_key.strip(), "verification", None)
            if not _vrec:
                return [TextContent(type="text", text=json.dumps({
                    "error": (
                        "Cannot record a verified success: no accepted verification evidence for "
                        "this issue. Call record_verification first (automated path), or pass "
                        "verified_by_human=true with outcome_feedback_note if you verified manually."
                    )
                }))]

        extra = {
            "root_cause_pattern": root_cause_pattern,
            "pattern_confidence": pattern_confidence,
            "outcome_verified": outcome_verified,
            "outcome_feedback_note": outcome_feedback_note[:500],
            "negate": negate,
            "negation_reason": negation_reason[:500],
            "graph_cluster": args.get("graph_cluster") or "",
            "files_agent_opened": [str(f) for f in (args.get("files_agent_opened") or [])],
            "prior_resolution_used": args.get("prior_resolution_used") or "",
            "root_cause_confirmed": bool(args.get("root_cause_confirmed", False)),
            "diagnosis_steps": int(args.get("diagnosis_steps") or 0),
            "full_ticket_text": (args.get("full_ticket_text") or "")[:2000],
            "attachment_summary": (args.get("attachment_summary") or "")[:500],
            "verified_by_human": verified_by_human,
        }

        text = await _handle_save_memory(
            issue_key.strip(),
            summary.strip(),
            problem_description.strip(),
            resolution_note.strip(),
            [str(f) for f in files_changed],
            [str(t) for t in tags],
            work_item_type.strip(),
            pattern_used=pattern_used.strip(),
            extra=extra,
        )
        return [TextContent(type="text", text=text)]

    if name == _DRAFT_SKILL_TOOL:
        issue_key = args.get("issue_key")
        if not isinstance(issue_key, str) or not issue_key.strip():
            return [TextContent(type="text", text=json.dumps(
                {"error": "issue_key must be a non-empty string."}))]
        skill_worthy = bool(args.get("skill_worthy", False))
        if not skill_worthy:
            return [TextContent(type="text", text=json.dumps({"status": "skipped"}))]

        skill_name = args.get("skill_name")
        description = args.get("description")
        when_to_use = args.get("when_to_use")
        procedure = args.get("procedure")
        verification = args.get("verification")
        missing = [
            field_name for field_name, value in (
                ("skill_name", skill_name), ("description", description),
                ("when_to_use", when_to_use), ("procedure", procedure),
                ("verification", verification),
            ) if not isinstance(value, str) or not value.strip()
        ]
        if missing:
            return [TextContent(type="text", text=json.dumps(
                {"error": f"skill_worthy=true requires: {', '.join(missing)}."}))]

        try:
            loop = asyncio.get_running_loop()
            entry = await loop.run_in_executor(_get_memory_executor(), _show_entry_sync, issue_key.strip())
        except Exception as exc:
            return [TextContent(type="text", text=json.dumps(
                {"error": f"Failed to look up memory entry for issue_key '{issue_key}': {exc}"}))]
        if entry is None:
            return [TextContent(type="text", text=json.dumps(
                {"error": f"No memory entry found for issue_key '{issue_key}'. Call save_memory first."}))]
        if not entry.outcome_verified:
            return [TextContent(type="text", text=json.dumps(
                {"error": "The entry for this issue_key is not outcome_verified. A skill can only be drafted from a verified fix."}))]

        pitfalls = args.get("pitfalls") if isinstance(args.get("pitfalls"), str) else ""
        tags = [str(t) for t in (args.get("tags") or [])] if isinstance(args.get("tags"), list) else []

        try:
            from icx_engine.skills.writer import draft_skill_entry, write_or_update
            draft = draft_skill_entry(
                entry, skill_name.strip(), description.strip(), when_to_use.strip(),
                procedure.strip(), verification.strip(), pitfalls=pitfalls, tags=tags,
            )
            status = write_or_update(SkillStorage(), draft)
        except Exception as exc:
            return [TextContent(type="text", text=json.dumps(
                {"error": f"Failed to draft skill: {exc}"}))]
        return [TextContent(type="text", text=json.dumps({"status": status, "name": draft.name}))]

    if name == _CREATE_SKILL_TOOL:
        name_arg = args.get("name")
        description = args.get("description")
        when_to_use = args.get("when_to_use")
        procedure = args.get("procedure")
        verification = args.get("verification")
        missing = [
            field_name for field_name, value in (
                ("name", name_arg), ("description", description),
                ("when_to_use", when_to_use), ("procedure", procedure),
                ("verification", verification),
            ) if not isinstance(value, str) or not value.strip()
        ]
        if missing:
            return [TextContent(type="text", text=json.dumps(
                {"error": f"Missing required field(s): {', '.join(missing)}."}))]

        pitfalls = args.get("pitfalls") if isinstance(args.get("pitfalls"), str) else ""
        tags = [str(t) for t in (args.get("tags") or [])] if isinstance(args.get("tags"), list) else []
        project_key = args.get("project_key")
        project_key = project_key.strip() if isinstance(project_key, str) and project_key.strip() else None
        origin_projects = [project_key] if project_key else []

        try:
            from icx_engine.skills.schema import SkillEntry
            from icx_engine.skills.writer import _slugify, write_or_update
            slug = _slugify(name_arg)
            draft = SkillEntry(
                name=slug, description=description.strip(), tags=tags, origin_projects=origin_projects,
                origin_issue_keys=[], scope_hint="repo-specific" if origin_projects else "generic",
                title=name_arg.strip(), when_to_use=when_to_use.strip(), procedure=procedure.strip(),
                pitfalls=pitfalls.strip(), verification=verification.strip(),
            )
            draft.icx_hash = draft.compute_hash()
            status = write_or_update(SkillStorage(), draft)
        except Exception as exc:
            return [TextContent(type="text", text=json.dumps(
                {"error": f"Failed to create skill: {exc}"}))]
        return [TextContent(type="text", text=json.dumps({"status": status, "name": slug}))]

    if name == _SKILL_GET_TOOL:
        skill_name = args.get("name")
        if not isinstance(skill_name, str) or not skill_name.strip():
            return [TextContent(type="text", text=json.dumps(
                {"error": "name must be a non-empty string."}))]
        entry = SkillStorage().read(skill_name.strip())
        if entry is None:
            return [TextContent(type="text", text=json.dumps(
                {"error": f"No skill named '{skill_name}' found."}))]
        return [TextContent(type="text", text=json.dumps({"body": entry.to_markdown()}))]

    if name == _SKILLS_INDEX_TOOL:
        skills = SkillStorage().list_all()
        return [TextContent(type="text", text=json.dumps(
            {"skills": [{"name": s.name, "description": s.description} for s in skills]}))]

    if name == _REINFORCE_TOOL_NAME:
        source_key, err = _validate_issue_key_arg(args, "source_key")
        if err:
            return err
        # External MCP schema arg is "new_ticket_key" (unchanged - callers already
        # use this name) - the local variable is named used_by_key from here on to
        # match _reinforce_usage_sync/MemoryManager.reinforce_usage/used_by_tickets,
        # which already use this name consistently internally.
        used_by_key, err = _validate_issue_key_arg(args, "new_ticket_key")
        if err:
            return err
        mem_state = _get_memory_state()
        if mem_state != "ready":
            return [TextContent(type="text", text=json.dumps({"error": f"Memory not ready (status={mem_state!r})."}
            ))]
        import functools
        try:
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(
                _get_memory_executor(),
                functools.partial(_reinforce_usage_sync, source_key.strip(), used_by_key.strip()),
            )
            return [TextContent(type="text", text=json.dumps(result))]
        except Exception as exc:
            return [TextContent(type="text", text=json.dumps({"error": str(exc)}))]

    if name == _AUDIT_TOOL_NAME:
        issue_key_audit, err = _validate_issue_key_arg(args, "issue_key")
        if err:
            return err
        limit_raw = args.get("limit", 20)
        try:
            audit_limit = max(1, min(100, int(limit_raw)))
        except (TypeError, ValueError):
            audit_limit = 20
        mem_state = _get_memory_state()
        if mem_state != "ready":
            return [TextContent(type="text", text=json.dumps({"error": f"Memory not ready (status={mem_state!r})."}
            ))]
        import functools
        try:
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(
                _get_memory_executor(),
                functools.partial(_get_audit_trail_sync, issue_key_audit.strip(), audit_limit),
            )
            return [TextContent(type="text", text=json.dumps(result))]
        except Exception as exc:
            return [TextContent(type="text", text=json.dumps({"error": str(exc)}))]

    if name == _TESTING_START_TOOL:
        from icx_engine.testing.graph import get_testing_graph
        from icx_engine.testing.state import make_initial_state
        from icx_engine.testing.validate import validate_session_args
        from icx_engine.config_manager import ConfigManager as _CM
        import uuid as _uuid

        ok, msg = validate_session_args(args)
        if not ok:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": msg}))]

        file_paths = args.get("file_paths", [])
        context = args.get("context")
        max_iterations = args.get("max_iterations")
        test_mode = args.get("test_mode")
        nl_intent = args.get("nl_intent") or None
        acceptance_criteria = args.get("acceptance_criteria") or []
        cfg = _CM.load()

        project = None
        try:
            from icx_engine.graph import storage
            from pathlib import Path as _Path
            for p in file_paths:
                info = storage.lookup_for_file(_Path(p))
                if info is not None:
                    project = info.project_id
                    break
        except Exception:
            project = None

        session_id = str(_uuid.uuid4())
        initial_state = make_initial_state(
            file_paths=file_paths,
            context=context,
            max_iterations=max_iterations if max_iterations is not None else cfg.test_max_iterations,
            test_mode=test_mode,
            nl_intent=nl_intent,
            acceptance_criteria=acceptance_criteria,
        )
        initial_state["project"] = project
        initial_state["session_id"] = session_id
        if "test_writes" in args:
            initial_state["test_writes"] = bool(args.get("test_writes"))
        graph = await get_testing_graph()
        config = {"configurable": {"thread_id": session_id}}
        finished = await _testing_invoke_tracked(session_id, graph.ainvoke(initial_state, config=config))
        if not finished:
            return [TextContent(type="text", text=json.dumps(attach_skill_hint({
                "ok": True, "session_id": session_id, "status": "running", "done": False, "gate": None,
                "poll": ("Still running real work (graph expansion/verification). Call "
                          "get_testing_session_status(session_id) to check progress - do NOT call "
                          "resume_testing_session again until status is no longer 'running'."),
            }, "testing-session-driver", rank_prompt=(context or nl_intent or "testing session"),
                archetype="testing")))]
        snapshot = await graph.aget_state(config)
        result = _testing_gate_snapshot(session_id, snapshot)
        result["ok"] = True
        del result["error"]
        result = attach_skill_hint(result, "testing-session-driver",
                                    rank_prompt=(context or nl_intent or "testing session"), archetype="testing")
        return [TextContent(type="text", text=json.dumps(result))]

    if name == _TESTING_RESUME_TOOL:
        from icx_engine.testing.graph import get_testing_graph
        from langgraph.types import Command as _Command
        session_id = args["session_id"]
        response = args["response"]
        running = _TESTING_RUNNING.get(session_id)
        if running is not None and not running.done():
            return [TextContent(type="text", text=json.dumps({
                "session_id": session_id, "status": "running", "done": False, "gate": None,
                "error": ("This session is still executing the previous gate. Call "
                          "get_testing_session_status(session_id) instead of resuming again."),
            }))]
        graph = await get_testing_graph()
        config = {"configurable": {"thread_id": session_id}}
        # SECURITY: an auth sessionId in the resume payload would be persisted to the durable
        # checkpoint. STRIP it before resuming so the credential never lands on disk. The real
        # authenticated session is already persisted (with its storage_state path) by the
        # ui_auth_capture / ui_auth_inline tools - we must NOT re-save here, which would overwrite
        # that record with an empty storage_state and make the replay run unauthenticated.
        if isinstance(response, dict) and "session_id" in response:
            response = {k: v for k, v in response.items() if k != "session_id"}
        finished = await _testing_invoke_tracked(session_id, graph.ainvoke(_Command(resume=response), config=config))
        if not finished:
            return [TextContent(type="text", text=json.dumps({
                "session_id": session_id, "status": "running", "done": False, "gate": None,
                "poll": ("Still running real work (verify/heal or scored test execution can take "
                          "several minutes). Call get_testing_session_status(session_id) to check "
                          "progress - do NOT call resume_testing_session again until status is no "
                          "longer 'running'."),
            }))]
        # A session is DONE only when nothing is pending AND no gate is waiting for input. A node
        # with several interrupt() calls (e.g. expand_files: expand_scan then expand) pauses at its
        # LATER interrupt with snapshot.next == () while an interrupt is still pending - so `not next`
        # alone would wrongly report done mid-flow and abandon the run before any test executes.
        snapshot = await graph.aget_state(config)
        result = _testing_gate_snapshot(session_id, snapshot)
        return [TextContent(type="text", text=json.dumps(result))]

    if name == _TESTING_STATUS_TOOL:
        from icx_engine.testing.graph import get_testing_graph
        session_id = args.get("session_id", "")
        if not isinstance(session_id, str) or not session_id.strip():
            return [TextContent(type="text", text=json.dumps({"error": "session_id is required."}))]
        session_id = session_id.strip()
        running = _TESTING_RUNNING.get(session_id)
        if running is not None and not running.done():
            return [TextContent(type="text", text=json.dumps({
                "session_id": session_id, "status": "running", "done": False, "gate": None,
            }))]
        graph = await get_testing_graph()
        config = {"configurable": {"thread_id": session_id}}
        try:
            snapshot = await graph.aget_state(config)
        except Exception as exc:
            return [TextContent(type="text", text=json.dumps({"error": f"cannot read session state: {exc}"}))]
        result = _testing_gate_snapshot(session_id, snapshot)
        prior_error = _TESTING_ERRORS.pop(session_id, None)
        if prior_error and not result.get("error"):
            result["error"] = prior_error
        return [TextContent(type="text", text=json.dumps(result))]


    if name == _SONAR_STATUS_TOOL:
        from icx_engine.sonar import service as _sonar_svc
        try:
            data = await _sonar_svc.status()
            resp = attach_skill_hint({"ok": True, "data": data}, "sonar-quality-review",
                                      rank_prompt="sonar code quality findings", archetype="quality")
            return [TextContent(type="text", text=json.dumps(resp))]
        except Exception as exc:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": str(exc)}))]

    if name == _SONAR_PROJECTS_TOOL:
        from icx_engine.sonar import service as _sonar_svc
        try:
            data = await _sonar_svc.projects(query=_sonar_opt_str(args, "query"))
            return [TextContent(type="text", text=json.dumps({"ok": True, "data": data}))]
        except _sonar_svc.SonarDisabled as exc:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": str(exc),
                "fallback": _ICX_FALLBACK("SonarQube", "icx sonar --add")}))]
        except Exception as exc:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": str(exc)}))]

    if name == _SONAR_BRANCHES_TOOL:
        from icx_engine.sonar import service as _sonar_svc
        project = _sonar_require_project(args)
        if project is None:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": "project is required and must be a non-empty string."}))]
        try:
            data = await _sonar_svc.branches(project, query=_sonar_opt_str(args, "query"))
            return [TextContent(type="text", text=json.dumps({"ok": True, "data": data}))]
        except _sonar_svc.SonarDisabled as exc:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": str(exc),
                "fallback": _ICX_FALLBACK("SonarQube", "icx sonar --add")}))]
        except Exception as exc:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": str(exc)}))]

    if name in (_SONAR_MEASURES_TOOL, _SONAR_QUALITY_GATE_TOOL):
        from icx_engine.sonar import service as _sonar_svc
        project = _sonar_require_project(args)
        if project is None:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": "project is required and must be a non-empty string."}))]
        branch = _sonar_opt_str(args, "branch")
        try:
            if name == _SONAR_MEASURES_TOOL:
                data = await _sonar_svc.measures(project, branch)
            else:
                data = await _sonar_svc.quality_gate(project, branch)
            return [TextContent(type="text", text=json.dumps({"ok": True, "data": data}))]
        except _sonar_svc.SonarDisabled as exc:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": str(exc),
                "fallback": _ICX_FALLBACK("SonarQube", "icx sonar --add")}))]
        except Exception as exc:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": str(exc)}))]

    if name in (_SONAR_FINDINGS_TOOL, _SONAR_REPORT_TOOL):
        from icx_engine.sonar import service as _sonar_svc
        scope = _sonar_scope_args(args)
        if scope is None:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": "project is required and must be a non-empty string."}))]
        try:
            fn = _sonar_svc.findings if name == _SONAR_FINDINGS_TOOL else _sonar_svc.report
            data = await fn(**scope)
            return [TextContent(type="text", text=json.dumps({"ok": True, "data": data}))]
        except _sonar_svc.SonarDisabled as exc:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": str(exc),
                "fallback": _ICX_FALLBACK("SonarQube", "icx sonar --add")}))]
        except Exception as exc:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": str(exc)}))]

    if name == _SONAR_TOP_FILES_TOOL:
        from icx_engine.sonar import service as _sonar_svc
        project = _sonar_require_project(args)
        metric = _sonar_opt_str(args, "metric")
        if project is None or metric is None:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": "project and metric are required."}))]
        a = args or {}
        limit = a.get("limit")
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            limit = 20
        try:
            data = await _sonar_svc.top_files(
                project, metric, branch=_sonar_opt_str(args, "branch"),
                limit=limit, ascending=bool(a.get("ascending")),
            )
            return [TextContent(type="text", text=json.dumps({"ok": True, "data": data}))]
        except _sonar_svc.SonarDisabled as exc:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": str(exc),
                "fallback": _ICX_FALLBACK("SonarQube", "icx sonar --add")}))]
        except Exception as exc:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": str(exc)}))]

    if name == _SONAR_HISTORY_TOOL:
        from icx_engine.sonar import service as _sonar_svc
        project = _sonar_require_project(args)
        metrics = _sonar_str_list(args, "metrics")
        if project is None or not metrics:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": "project and metrics (non-empty) are required."}))]
        try:
            data = await _sonar_svc.metric_history(
                project, metrics, branch=_sonar_opt_str(args, "branch"),
                date_from=_sonar_opt_str(args, "date_from"), date_to=_sonar_opt_str(args, "date_to"),
            )
            return [TextContent(type="text", text=json.dumps({"ok": True, "data": data}))]
        except _sonar_svc.SonarDisabled as exc:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": str(exc),
                "fallback": _ICX_FALLBACK("SonarQube", "icx sonar --add")}))]
        except Exception as exc:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": str(exc)}))]

    if name == _SONAR_ANALYSES_TOOL:
        from icx_engine.sonar import service as _sonar_svc
        project = _sonar_require_project(args)
        if project is None:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": "project is required."}))]
        try:
            data = await _sonar_svc.analyses(
                project, branch=_sonar_opt_str(args, "branch"),
                date_from=_sonar_opt_str(args, "date_from"), date_to=_sonar_opt_str(args, "date_to"),
            )
            return [TextContent(type="text", text=json.dumps({"ok": True, "data": data}))]
        except _sonar_svc.SonarDisabled as exc:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": str(exc),
                "fallback": _ICX_FALLBACK("SonarQube", "icx sonar --add")}))]
        except Exception as exc:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": str(exc)}))]

    if name == _SONAR_RULE_TOOL:
        from icx_engine.sonar import service as _sonar_svc
        rule_key = _sonar_opt_str(args, "rule_key")
        if rule_key is None:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": "rule_key is required."}))]
        try:
            data = await _sonar_svc.rule(rule_key)
            return [TextContent(type="text", text=json.dumps({"ok": True, "data": data}))]
        except _sonar_svc.SonarDisabled as exc:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": str(exc),
                "fallback": _ICX_FALLBACK("SonarQube", "icx sonar --add")}))]
        except Exception as exc:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": str(exc)}))]

    if name == _SONAR_RULES_TOOL:
        from icx_engine.sonar import service as _sonar_svc
        a = args or {}
        page_size = a.get("page_size")
        if not isinstance(page_size, int) or isinstance(page_size, bool) or page_size <= 0:
            page_size = 50
        try:
            data = await _sonar_svc.rules(
                language=_sonar_opt_str(args, "language"),
                tags=_sonar_str_list(args, "tags"),
                repositories=_sonar_str_list(args, "repositories"),
                query=_sonar_opt_str(args, "query"),
                page_size=page_size,
            )
            return [TextContent(type="text", text=json.dumps({"ok": True, "data": data}))]
        except _sonar_svc.SonarDisabled as exc:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": str(exc),
                "fallback": _ICX_FALLBACK("SonarQube", "icx sonar --add")}))]
        except Exception as exc:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": str(exc)}))]

    if name == _SONAR_HOTSPOT_TOOL:
        from icx_engine.sonar import service as _sonar_svc
        hotspot_key = _sonar_opt_str(args, "hotspot_key")
        if hotspot_key is None:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": "hotspot_key is required."}))]
        try:
            data = await _sonar_svc.hotspot(hotspot_key)
            return [TextContent(type="text", text=json.dumps({"ok": True, "data": data}))]
        except _sonar_svc.SonarDisabled as exc:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": str(exc),
                "fallback": _ICX_FALLBACK("SonarQube", "icx sonar --add")}))]
        except Exception as exc:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": str(exc)}))]

    if name == _SONAR_SOURCE_TOOL:
        from icx_engine.sonar import service as _sonar_svc
        project = _sonar_require_project(args)
        path = _sonar_opt_str(args, "path")
        if project is None or path is None:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": "project and path are required."}))]
        a = args or {}
        from_line = a.get("from_line") if isinstance(a.get("from_line"), int) and not isinstance(a.get("from_line"), bool) else None
        to_line = a.get("to_line") if isinstance(a.get("to_line"), int) and not isinstance(a.get("to_line"), bool) else None
        try:
            data = await _sonar_svc.source_lines(project, path, branch=_sonar_opt_str(args, "branch"), from_line=from_line, to_line=to_line)
            return [TextContent(type="text", text=json.dumps({"ok": True, "data": data}))]
        except _sonar_svc.SonarDisabled as exc:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": str(exc),
                "fallback": _ICX_FALLBACK("SonarQube", "icx sonar --add")}))]
        except Exception as exc:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": str(exc)}))]

    if name == _SONAR_METRICS_TOOL:
        from icx_engine.sonar import service as _sonar_svc
        a = args or {}
        page_size = a.get("page_size")
        if not isinstance(page_size, int) or isinstance(page_size, bool) or page_size <= 0:
            page_size = 100
        try:
            data = await _sonar_svc.metrics(page_size=page_size)
            return [TextContent(type="text", text=json.dumps({"ok": True, "data": data}))]
        except _sonar_svc.SonarDisabled as exc:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": str(exc),
                "fallback": _ICX_FALLBACK("SonarQube", "icx sonar --add")}))]
        except Exception as exc:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": str(exc)}))]

    if name == _SONAR_QUALITY_GATE_DEFINITION_TOOL:
        from icx_engine.sonar import service as _sonar_svc
        try:
            data = await _sonar_svc.quality_gate_definition(
                project=_sonar_opt_str(args, "project"), gate_name=_sonar_opt_str(args, "gate_name"),
            )
            return [TextContent(type="text", text=json.dumps({"ok": True, "data": data}))]
        except ValueError as exc:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": str(exc)}))]
        except _sonar_svc.SonarDisabled as exc:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": str(exc),
                "fallback": _ICX_FALLBACK("SonarQube", "icx sonar --add")}))]
        except Exception as exc:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": str(exc)}))]

    if name == _SONAR_QUALITY_PROFILES_TOOL:
        from icx_engine.sonar import service as _sonar_svc
        try:
            data = await _sonar_svc.quality_profiles(
                language=_sonar_opt_str(args, "language"), project=_sonar_opt_str(args, "project"),
            )
            return [TextContent(type="text", text=json.dumps({"ok": True, "data": data}))]
        except _sonar_svc.SonarDisabled as exc:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": str(exc),
                "fallback": _ICX_FALLBACK("SonarQube", "icx sonar --add")}))]
        except Exception as exc:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": str(exc)}))]

    if name == _SONAR_ISSUE_AUTHORS_TOOL:
        from icx_engine.sonar import service as _sonar_svc
        try:
            data = await _sonar_svc.issue_authors(project=_sonar_opt_str(args, "project"), query=_sonar_opt_str(args, "query"))
            return [TextContent(type="text", text=json.dumps({"ok": True, "data": data}))]
        except _sonar_svc.SonarDisabled as exc:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": str(exc),
                "fallback": _ICX_FALLBACK("SonarQube", "icx sonar --add")}))]
        except Exception as exc:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": str(exc)}))]

    if name == _SONAR_ISSUE_TAGS_TOOL:
        from icx_engine.sonar import service as _sonar_svc
        try:
            data = await _sonar_svc.issue_tags(project=_sonar_opt_str(args, "project"), query=_sonar_opt_str(args, "query"))
            return [TextContent(type="text", text=json.dumps({"ok": True, "data": data}))]
        except _sonar_svc.SonarDisabled as exc:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": str(exc),
                "fallback": _ICX_FALLBACK("SonarQube", "icx sonar --add")}))]
        except Exception as exc:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": str(exc)}))]

    if name == _SONAR_ISSUE_CHANGELOG_TOOL:
        from icx_engine.sonar import service as _sonar_svc
        issue_key = _sonar_opt_str(args, "issue_key")
        if issue_key is None:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": "issue_key is required."}))]
        try:
            data = await _sonar_svc.issue_changelog(issue_key)
            return [TextContent(type="text", text=json.dumps({"ok": True, "data": data}))]
        except _sonar_svc.SonarDisabled as exc:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": str(exc),
                "fallback": _ICX_FALLBACK("SonarQube", "icx sonar --add")}))]
        except Exception as exc:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": str(exc)}))]

    if name == _SONAR_SYSTEM_HEALTH_TOOL:
        from icx_engine.sonar import service as _sonar_svc
        try:
            data = await _sonar_svc.system_health()
            return [TextContent(type="text", text=json.dumps({"ok": True, "data": data}))]
        except _sonar_svc.SonarDisabled as exc:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": str(exc),
                "fallback": _ICX_FALLBACK("SonarQube", "icx sonar --add")}))]
        except Exception as exc:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": str(exc)}))]

    if name == _SONAR_LANGUAGES_TOOL:
        from icx_engine.sonar import service as _sonar_svc
        try:
            data = await _sonar_svc.languages(query=_sonar_opt_str(args, "query"))
            return [TextContent(type="text", text=json.dumps({"ok": True, "data": data}))]
        except _sonar_svc.SonarDisabled as exc:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": str(exc),
                "fallback": _ICX_FALLBACK("SonarQube", "icx sonar --add")}))]
        except Exception as exc:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": str(exc)}))]

    return [TextContent(type="text", text=json.dumps({"error": f"Unknown tool: {name}"}))]


# ---------------------------------------------------------------------------
# Graph helpers (synchronous - filesystem + maybe subprocess spawn)
# ---------------------------------------------------------------------------

# GraphQuerier reuse cache. The agent calls ~10 graph tools per ticket against the same
# project; each call previously re-read and re-indexed the whole graph.json. GraphQuerier
# holds read-only state built once in __init__, so it is safe to reuse. Keyed by graph.json
# path + mtime_ns so any rebuild (atomic write -> new mtime) invalidates the entry. Bounded
# by the number of distinct projects; no eviction needed.
_QUERIER_CACHE: dict = {}
# Tool handlers run via loop.run_in_executor -> worker threads, so concurrent calls for the
# same project can race on the check-then-store below without this.
_QUERIER_CACHE_LOCK = _threading.Lock()


def _cached_querier(graph_json_path):
    """Return a GraphQuerier for graph_json_path, reusing a cached instance when the file
    is unchanged (same mtime). Behaviour is identical to constructing a fresh GraphQuerier."""
    from icx_engine.graph.query import GraphQuerier
    key = str(graph_json_path)
    try:
        mtime = graph_json_path.stat().st_mtime_ns
    except OSError:
        return GraphQuerier(graph_json_path)
    with _QUERIER_CACHE_LOCK:
        cached = _QUERIER_CACHE.get(key)
        if cached is not None and cached[0] == mtime:
            return cached[1]
        q = GraphQuerier(graph_json_path)
        _QUERIER_CACHE[key] = (mtime, q)
        return q


def _degraded_graph_response(code: str, project_path, warn_user: str, extra: dict | None = None) -> dict:
    """Non-blocking graph-unavailable response.

    The codebase graph is an enhancement layer, not a hard dependency. When it is
    absent / still building / stale, we do NOT stop the agent - we tell it to warn
    the user (so they know why graph enrichment is off and how to enable it) and
    then answer from its own native file tools (grep/glob/read). Zero delay; the
    graph simply upgrades results once the user builds it.
    """
    resp = {
        "status": "degraded",
        "code": code,
        "warn_user": warn_user,
        "action_required": "tell_user_then_use_native_tools",
        "instruction": (
            "Show the warn_user message to the user, then answer their request now "
            "using your built-in file search (grep/glob/read). Do NOT stop, wait, or "
            "block on the graph."
        ),
        "build_command": f"icx graph build \"{project_path}\"",
        "project_path": str(project_path),
    }
    if extra:
        resp.update(extra)
    return resp


def _lock_plan_prior_fix(chosen: list[str]) -> set:
    """Files a prior resolution touched for any chosen file (memory), boosting them to high-tier."""
    out: set[str] = set()
    for f in chosen:
        try:
            rows = _find_by_file_sync(f, None) or []
        except Exception:
            rows = []
        for r in rows:
            for p in (r.get("files_changed") or []):
                if p:
                    out.add(str(p))
    return out


def _icx_connected() -> dict:
    """Which ICX-native link targets are configured: jira (a connector connection) + sonarqube (an
    active sonar connection). Guarded - any config error degrades to 'not connected' (a safe tier-2)."""
    out = {"jira": False, "sonarqube": False}
    try:
        cfg = ConfigManager.load()
        try:
            out["jira"] = any(getattr(c, "connector_type", "") == "jira"
                              for c in (getattr(cfg, "connections", None) or []))
        except Exception:
            pass
        try:
            out["sonarqube"] = cfg.active_sonar_connection() is not None
        except Exception:
            pass
    except Exception:
        pass
    return out


def _boost_env(repo_path: str | None, is_continuation: bool) -> dict:
    """Detect the boost environment: is there a usable repo, and is a code graph built for it?
    Never raises - a detection failure degrades to 'not available'."""
    from pathlib import Path as _P
    has_repo = False
    try:
        has_repo = bool(repo_path) and _P(repo_path).is_dir()
    except OSError:
        has_repo = False
    has_graph = False
    if has_repo:
        try:
            has_graph = isinstance(_load_querier_simple(repo_path), tuple)
        except Exception:
            has_graph = False
    return {"has_repo": has_repo, "has_graph": has_graph, "is_continuation": bool(is_continuation)}


def _context_signals(project_path: str, seeds: list[str], keywords: list[str]):
    """Build the four guarded retrieval signals (graph, grep, semantic, memory) for lock_plan.
    Each is a zero-arg callable returning [(path, reason)]; any failure degrades to [] (never raises).
    ICX makes no LLM call here - these are deterministic graph/grep/memory lookups."""
    from pathlib import Path as _P

    def _graph():
        loaded = _load_querier_simple(project_path)
        if not isinstance(loaded, tuple):
            return []
        q, _ = loaded
        out = []
        try:
            br = q.get_blast_radius(seeds, max_depth=5, min_confidence=0.3)
            for p in (br.get("direct_dependents") or []):
                out.append((p, "direct graph dependent of a planned file"))
            for p in (br.get("missing_changes") or []):
                out.append((p, "co-changes with a planned file (often edited together)"))
        except Exception:
            return out
        return out

    def _grep():
        try:
            from icx_engine.testing.expand import expand_via_grep
            found = expand_via_grep(seeds, _P(project_path))
            return [(p, "references/imports a planned file") for p in found]
        except Exception:
            return []

    def _semantic():
        loaded = _load_querier_simple(project_path)
        if not isinstance(loaded, tuple):
            return []
        q, _ = loaded
        query = " ".join(keywords) if keywords else " ".join(_P(s).stem for s in seeds)
        if not query.strip():
            return []
        try:
            results = q.find_context(query) or []          # returns list[ContextResult] (.file/.reason)
            out = []
            for r in results[:10]:
                path = getattr(r, "file", None)
                if path:
                    out.append((str(path), "semantically related to the ticket"))
            return out
        except Exception:
            return []

    def _memory():
        out = []
        for f in seeds:
            try:
                rows = _find_by_file_sync(f, None) or []
            except Exception:
                rows = []
            for r in rows:
                key = r.get("issue_key") or "a prior ticket"
                for p in (r.get("files_changed") or []):     # MemoryEntry.files_changed
                    if p:
                        out.append((str(p), f"touched by prior fix {key}"))
        return out

    return _graph, _grep, _semantic, _memory


def _load_querier_simple(project_path: str) -> tuple | dict:
    """Validate path, derive project_id, load GraphQuerier.

    Returns (GraphQuerier, validated_path) or an error dict.
    """
    from icx_engine.graph import storage as _st
    from icx_engine.graph.storage import validate_project_path, GraphError
    try:
        _validated = validate_project_path(project_path.strip() if project_path else "")
    except GraphError as _ve:
        return {"error": str(_ve)}
    _pid = _st.derive_project_id(_validated)
    _gpath = _st.graph_path(_pid)
    if not _gpath.exists():
        return _degraded_graph_response(
            code="NO_GRAPH",
            project_path=_validated,
            warn_user=(
                f"The codebase graph is not built for '{_validated}', so I'm answering "
                "from direct file search (grep/read) instead. For richer, more accurate "
                f"results, build the graph: icx graph build \"{_validated}\""
            ),
        )
    return _cached_querier(_gpath), _validated


def _resolve_graph_path(raw_path: str):
    """Validate path, derive project_id, check staleness.

    Returns (project_path, project_id, staleness_warning) on success,
    or an error dict on failure.
    """
    from icx_engine.graph import storage as _st
    from icx_engine.graph.paths import validate_and_resolve_paths, check_staleness

    resolved_paths, path_err = validate_and_resolve_paths([raw_path])
    if path_err is not None:
        return path_err

    project_path = resolved_paths[0]
    project_id = _st.derive_project_id(project_path)

    staleness = check_staleness(project_id, project_path)
    stale_status = staleness["status"]

    if stale_status in ("no_graph", "no_manifest"):
        return _degraded_graph_response(
            code="NO_GRAPH",
            project_path=project_path,
            warn_user=(
                f"The codebase graph is not built for '{project_path}', so I'm answering "
                "from direct file search (grep/read) instead. For richer, more accurate "
                f"results, build the graph: icx graph build \"{project_path}\""
            ),
        )

    if stale_status == "stale":
        pct = staleness.get("pct", 0)
        changed = staleness.get("changed", 0)
        total = staleness.get("total", 0)
        return _degraded_graph_response(
            code="GRAPH_STALE",
            project_path=project_path,
            warn_user=(
                f"The codebase graph is {pct}% stale ({changed}/{total} files changed), so "
                "I'm answering from direct file search (grep/read) instead. For up-to-date, "
                f"richer results, rebuild the graph: icx graph build \"{project_path}\""
            ),
            extra={"changed_files": changed, "total_files": total, "changed_pct": pct},
        )

    staleness_warning: str | None = None
    if stale_status == "incremental":
        pct = staleness.get("pct", 0)
        staleness_warning = (
            f"Graph is slightly stale ({pct}% of files changed, under 1% threshold). "
            "Results may not reflect the very latest changes. "
            f"Inform the user and suggest running: icx graph build \"{project_path}\""
        )
    elif stale_status == "freshness_unknown":
        staleness_warning = (
            "Could not determine graph freshness (git check timed out). "
            "Results may be slightly stale. Inform the user."
        )

    return (project_path, project_id, staleness_warning)

def _get_graph_info(project_path: str) -> dict:
    """Return graph status dict - fast mode, no git/staleness check, no subprocess."""
    from icx_engine.graph.manager import graph_info_for_path
    return graph_info_for_path(project_path, check_stale=False)


def _get_graphs_info(paths: list[str]) -> list[dict]:
    """Return graph status dicts for multiple paths - fast mode, no git."""
    from icx_engine.graph.manager import graph_info_for_path
    return [graph_info_for_path(p, check_stale=False) for p in paths]


def _match_tracker_ref(issue_ref: str) -> "tuple[str, type] | None":
    """Try each registered connector's extract_bare_key_from_ref(). Returns
    (bare_key, connector_class) for the first match, or None."""
    from icx_engine.connectors.base import get_all_connector_classes
    for cls in get_all_connector_classes():
        key = cls.extract_bare_key_from_ref(issue_ref)
        if key:
            return key, cls
    return None


def _extract_tracker_key_from_ref(issue_ref: str) -> str:
    """Extract bare issue key from a URL or bare key string, trying each
    registered connector's conventions in turn.

    https://foo.atlassian.net/browse/PROJ-123 -> PROJ-123
    PROJ-123 -> PROJ-123
    """
    match = _match_tracker_ref(issue_ref)
    return match[0] if match else ""


def _resolve_paths_from_ticket(issue_ref: str) -> "list[dict] | None":
    """Look up ALL registered ICX projects matching the ticket's tracker project key.

    Returns list of {"path": str, "name": str} on match, None otherwise.
    Never raises.
    """
    try:
        match = _match_tracker_ref(issue_ref)
        if not match:
            return None
        key, cls = match
        project_prefix = cls.extract_project_key(key)
        from icx_engine.graph.storage import find_projects_by_tracker_key
        infos = find_projects_by_tracker_key(project_prefix)
        if infos:
            return [{"path": i.path, "name": i.name} for i in infos]
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Memory search (runs inside memory thread)
# ---------------------------------------------------------------------------

def _search_memory_sync(qi, top_k: int = 10) -> dict:
    """Search memory with a MemoryQueryInput. Returns smart query dict."""
    try:
        mem = _ensure_memory_manager()
        return mem.query_smart(qi, top_k=top_k)
    except Exception:
        return {"results": [], "negative_signals": [], "decay_applied": False}


def _find_by_file_sync(file_path: str, project_key: str | None) -> list[dict]:
    """Return MemoryEntry dicts for entries whose files_changed contains file_path."""
    from icx_engine.memory.bridge import find_work_items_by_file
    mem = _ensure_memory_manager()
    entries = find_work_items_by_file(file_path, mem, project_key=project_key)
    return [e.model_dump() for e in entries]


def _get_hotspots_sync(project_key: str | None, top_n: int) -> list[dict]:
    """Return bug-density list: [{file, count, work_items}] sorted desc."""
    from icx_engine.memory.bridge import get_work_item_density
    mem = _ensure_memory_manager()
    return get_work_item_density(mem, project_key=project_key, top_n=top_n)


def _get_related_sync(
    issue_key: str | None,
    project_key: str | None,
    files: list[str] | None,
) -> list[dict]:
    """Return related work items via stored edges or file-overlap fallback."""
    return _ensure_memory_manager().get_related(issue_key, project_key, files)


def _get_patterns_sync(project_key: str | None) -> list[dict]:
    """Return stored patterns, optionally filtered by project_key."""
    return _ensure_memory_manager().get_patterns(project_key=project_key)


def _reinforce_usage_sync(source_key: str, used_by_key: str) -> dict:
    """Reinforce memory usage. Runs inside the memory thread."""
    return _ensure_memory_manager().reinforce_usage(source_key, used_by_key)


def _get_audit_trail_sync(issue_key: str, limit: int) -> list[dict]:
    """Return audit trail for issue_key. Runs inside the memory thread."""
    return _ensure_memory_manager().get_audit_trail(issue_key, limit)


def _quick_memory_search_sync(summary: str, top_k: int = 3) -> dict:
    """Quick internal search for intelligence layer. Runs inside the memory thread."""
    try:
        from icx_engine.memory.schema import MemoryQueryInput
        qi = MemoryQueryInput(
            issue_key="",
            project_key="",
            source_type="",
            summary=summary,
            description=summary,
            issue_type="",
        )
        return _ensure_memory_manager().query_smart(qi, top_k=top_k, min_score=0.68)
    except Exception:
        return {"results": [], "negative_signals": [], "decay_applied": False}


# ---------------------------------------------------------------------------
# Intelligence layer helpers (Phase 10)
# ---------------------------------------------------------------------------

def _build_negative_signal_warning(negative_signals: list[dict]) -> str:
    if not negative_signals:
        return ""
    lines = ["NEGATIVE SIGNAL WARNING:"]
    lines.append("The following past resolutions matched this ticket but were confirmed WRONG:")
    for sig in negative_signals:
        lines.append(
            f"  - {sig.get('issue_key', '?')}: {sig.get('negation_reason', 'no reason recorded')}"
            f" (pattern: {sig.get('root_cause_pattern', '?')})"
        )
    lines.append("DO NOT reuse these approaches. They have been confirmed to fail.")
    return "\n".join(lines)


def _check_semantic_patterns(pattern_results: list[dict], ticket_summary: str) -> str:
    """Return warning string if any semantic pattern matches, else empty string."""
    ticket_lower = ticket_summary.lower()
    for pattern in pattern_results:
        if pattern.get("pattern_type") != "semantic_signal":
            continue
        try:
            evidence = pattern.get("evidence", {})
            if isinstance(evidence, str):
                evidence = json.loads(evidence)
        except Exception:
            continue
        signal_words = evidence.get("signal_words", [])
        hits = [w for w in signal_words if w in ticket_lower]
        if len(hits) >= 2:
            top_file = evidence.get("top_fix_file", "")
            rate = int(evidence.get("top_fix_file_rate", 0) * 100)
            root_cause = evidence.get("root_cause_pattern", "")
            group_size = evidence.get("group_size", 0)
            return (
                f"Pattern detected: {group_size} past tickets mentioning "
                f"'{hits[0]}' and '{hits[1]}' had root cause {root_cause} "
                f"and {rate}% were fixed in {top_file}. Check there first."
            )
    return ""


def _build_intelligence(
    issue_key: str,
    memory_results: dict,
    graph_info: dict,
    pattern_results: list[dict],
) -> dict:
    """Build the intelligence field for analyze response. No new API calls."""
    results = memory_results.get("results", [])
    negative_signals = memory_results.get("negative_signals", [])
    problem_summary = graph_info.get("_problem_summary", "")

    prior_resolution = None
    skip_diagnosis = False
    confidence = 0.0
    pattern_warning = ""
    verdict = "novel"

    if results and results[0].get("similarity_score", 0.0) >= 0.72:
        verdict = "seen_before"
        confidence = results[0]["similarity_score"]
        prior_resolution = results[0]
        skip_diagnosis = (
            prior_resolution.get("outcome_verified", False)
            and confidence >= 0.80
        )
    else:
        semantic_warning = _check_semantic_patterns(pattern_results, problem_summary)
        if semantic_warning:
            verdict = "pattern_match"
            confidence = 0.60
            pattern_warning = semantic_warning
        else:
            hub_patterns = [p for p in pattern_results if p.get("pattern_type") == "citation_hub"]
            if hub_patterns:
                verdict = "pattern_match"
                confidence = 0.55
                pattern_warning = f"Citation hub detected: {hub_patterns[0].get('label', '')}"

    suggested_files: list[str] = graph_info.get("context_files", [])[:3]
    open_files_count = len(suggested_files)
    token_budget_estimate = 500 + (open_files_count * 800) + (300 if prior_resolution else 0)

    _session_set(issue_key, "intelligence_verdict", verdict)
    _session_set(issue_key, "suggested_files", suggested_files)
    _session_set(issue_key, "ticket_summary", problem_summary)

    return {
        "verdict": verdict,
        "confidence": round(confidence, 4),
        "prior_resolution": prior_resolution,
        "pattern_warning": pattern_warning or None,
        "negative_signals": negative_signals,
        "suggested_files": suggested_files,
        "skip_diagnosis": skip_diagnosis,
        "open_files_count": open_files_count,
        "token_budget_estimate": token_budget_estimate,
    }


# ---------------------------------------------------------------------------
# Core analyze handler
# ---------------------------------------------------------------------------

async def _handle_analyze_issue(
    issue_ref: str,
    project_paths: list[str],
    profile: str | None = None,
    skip_vision: bool = False,
) -> str:
    """Run full pipeline: fetch -> analyze -> graph info -> combined response. Memory search is agent-driven via memory_search tool."""

    try:
        from icx_engine.models.output import IssueContext, RawIssueResponse

        config = ConfigManager.load()
        _timeout = 45.0 if skip_vision else 660.0
        result = await asyncio.wait_for(
            engine.run(
                issue_ref, config, mcp_mode=True,
                profile_override=profile, skip_vision=skip_vision,
                log=_engine_log,
            ),
            timeout=_timeout,
        )
        if isinstance(result, IssueContext):
            issue_key_val = issue_ref
            issue_type_val = result.issue_type
            summary_val = result.problem_summary
        else:
            issue_key_val = result.issue_key
            issue_type_val = result.issue_type
            summary_val = result.summary

        _mem_state = _get_memory_state()

        # Write issue images to disk while memory search runs.
        # Keeps MCP response compact - editors truncate large base64 payloads.
        # sweep_stale_temp_dirs() runs the 24h TTL cleanup silently (~1ms).
        image_paths: dict[str, str] = {}
        _images_raw: dict[str, str] = getattr(result, "images", None) or {}
        if _images_raw:
            import base64 as _b64
            from icx_engine.graph.storage import (
                ensure_issue_temp_dir as _ensure_dir,
                sweep_stale_temp_dirs as _sweep,
                safe_temp_filename,
            )
            _sweep()  # TTL cleanup - non-fatal, fast
            try:
                img_dir = _ensure_dir(issue_key_val)
                _ALLOWED_IMAGE_EXTS = frozenset({
                    ".png", ".jpg", ".jpeg", ".gif", ".webp",
                    ".bmp", ".tiff", ".tif",
                })
                _used_img: set[str] = set()
                for fname, b64_data in _images_raw.items():
                    try:
                        safe_name = safe_temp_filename(fname, _used_img)
                        if Path(safe_name).suffix.lower() not in _ALLOWED_IMAGE_EXTS:
                            continue  # skip non-image files
                        img_path = img_dir / safe_name
                        img_path.write_bytes(_b64.b64decode(b64_data))
                        image_paths[fname] = str(img_path)
                    except Exception:
                        pass  # skip individual image on decode/write failure
            except Exception:
                pass  # directory creation failure - proceed without images on disk

        # Write full-fidelity attachment sidecars (<name>.full.md) + raw originals to the same
        # per-issue temp dir. Guarded - never blocks the response.
        attachment_paths = _write_attachment_files(result, issue_key_val)

        # When no paths given, resolve ALL projects under the ticket's Jira project key.
        _auto_resolved = False
        if not project_paths:
            _resolved_infos = _resolve_paths_from_ticket(issue_ref)
            if _resolved_infos:
                project_paths = [r["path"] for r in _resolved_infos]
                _auto_resolved = True

        # Graph info (sync - filesystem only) also runs while memory search is in flight.
        graphs: list[dict] = _get_graphs_info(project_paths) if project_paths else []

        # STRICT path policy. ICX only ever works with a REGISTERED project path or a path
        # resolved from the ticket's tracker key. A caller may pass a path it guessed (e.g.
        # the editor workspace root) - such a path is not registered and must never drive
        # behaviour: ICX neither auto-registers it (see GraphManager.resolve_project) nor
        # echoes it back in an "icx graph add/build <path>" prompt. Drop every unregistered
        # entry. If nothing registered remains, fall back to the ticket's tracker key. If
        # that resolves nothing either, the project graph is simply "not present" and the
        # instruction below shows the user how to create one - with no path guessed for them.
        if not _auto_resolved:
            graphs = [g for g in graphs if g.get("status") != "not_registered"]
            if not graphs:
                _resolved_infos = _resolve_paths_from_ticket(issue_ref)
                if _resolved_infos:
                    project_paths = [r["path"] for r in _resolved_infos]
                    graphs = _get_graphs_info(project_paths)
                    _auto_resolved = True
            # Keep project_paths in lockstep with the surviving graphs so a dropped
            # (unregistered/guessed) path is never echoed back anywhere - including the
            # VISION_GATE re-call hint.
            if not _auto_resolved:
                project_paths = [g["path"] for g in graphs]

        if _auto_resolved:
            for g in graphs:
                g["path_auto_resolved"] = True

        # Determine memory status - reported to agent so it knows whether to call memory_search.
        memory_status = _mem_state
        memory_note = ""

        if _mem_state == "warming":
            memory_status = "warming_up"
            memory_note = "Memory model is still loading; call memory_search once status is 'ready'."
        elif _mem_state in ("cold", ""):
            memory_status = "warming_up"
            memory_note = "Memory service not yet initialized."
        elif _mem_state == "failed":
            memory_status = "failed"
            if _memory_setup_required:
                memory_note = (
                    "Memory model not installed. "
                    "STOP: tell the user to run `icx setup` in their terminal to download the embedding model, then retry."
                )
            else:
                memory_note = "Memory model failed to load; search unavailable."

        _CONFIRMATION_BLOCK = (
            "---\n"
            "**Problem understood:** [1-2 sentence summary from work_item.analysis]\n"
            "**Goal:** [acceptance_criteria as bullet points, or problem_summary for bugs]\n"
            "**Approach:** [exactly what you will change, add, or remove and precisely why it fixes the problem]\n"
            "**Files I will work with:**\n"
            "  - path/to/file [role-tag] - one-line reason it is relevant\n"
            "**Tools called for this ticket** (ALL 10 required; show result or documented skip for each):\n"
            "  1. memory_search:                 [N results OR skipped: memory.status!='ready']\n"
            "  2. graph_find_context:            [N files returned, top score X.XX - NO VALID SKIP]\n"
            "  3. graph_subsystem(file):         [cluster name, N files OR skipped: brand-new-file]\n"
            "  4. graph_call_chain(node):        [upstream/downstream summary OR skipped: brand-new-node]\n"
            "  5. graph_impact(node):            [N dependents OR skipped: brand-new-isolated-file]\n"
            "  6. graph_cross_links:             [N links OR skipped: confirmed-single-monolith]\n"
            "  7. memory_get_hotspots:           [result summary OR skipped: memory.status!='ready']\n"
            "  8. memory_find_by_file (per file):[result per file OR skipped: brand-new-file]\n"
            "  9. memory_get_related:            [top strength=X OR skipped: memory.status!='ready']\n"
            "  10. memory_get_patterns:          [pattern summary OR skipped: memory.status!='ready']\n"
            "  Any entry left blank or skipped with a vague reason is a VIOLATION.\n"
            "**Pre-submission check** (answer each before posting):\n"
            "  - Called every mandatory tool whose skip condition was NOT met? Yes/No\n"
            "  - Every skip has an EXACT technical reason, not 'not needed' or 'seems fine'? Yes/No\n"
            "  - Called memory_find_by_file for EVERY file I plan to modify? Yes/No\n"
            "  - Approach specific enough that user can reject and propose an alternative? Yes/No\n"
            "**Shall I proceed?**\n"
            "---"
        )

        _VISION_GATE = (
            "STEP 0 - ATTACHMENT CHECK (mandatory, do this before everything else):\n"
            "Look at work_item.analysis.pending_images, work_item.analysis.pending_audio, "
            "AND work_item.analysis.pending_documents.\n"
            "If ANY is non-empty AND the issue likely requires that content to understand or fix - "
            f"call analyze_issue with the SAME issue_ref and project_paths={project_paths!r} right now and use that "
            "response instead of continuing with this one. "
            "Do NOT proceed past STEP 0 without completing this check.\n"
            "If all three lists are empty OR the content is clearly irrelevant "
            "(e.g. company logos, profile avatars, decorative banners, hold music), continue to STEP 0a.\n\n"
            "STEP 0a - MEMORY STATUS GATE (read this first - it determines which memory tools are mandatory):\n"
            "  Read memory.status from THIS response right now.\n"
            "    'ready'       => Memory tools are LIVE. Proceed to STEP 0b immediately.\n"
            "    any other     => Memory unavailable. Skip ALL memory checklist items (1, 7, 8, 9, 10). "
            "Document each as 'skipped: memory.status=[value]'. Skip to STEP 1.\n"
            "  CRITICAL: memory_get_hotspots and memory_get_patterns are GLOBAL queries - "
            "they return data regardless of what memory_search returned. Never skip them based on memory_search result count.\n\n"
            "STEP 0b - MEMORY SEARCH (mandatory when memory.status == 'ready', skip otherwise):\n"
            "  Call memory_search() now with tags YOU derive from work_item.analysis.\n"
            "  Generate 3-6 specific tags covering the problem domain, affected layer, and failure type.\n"
            "  If results match this problem pattern, use them as the primary reference for your approach.\n\n"
            "STEP 0c - HOTSPOTS CALL (mandatory when memory.status == 'ready', skip otherwise):\n"
            "  Call memory_get_hotspots() now. No judgment call needed - just call it. Under 1 second.\n"
            "  If any hotspot file matches the area described in work_item.analysis, examine it first in STEP 1.\n\n"
        )

        _MEMORY_STEPS_NOGRAPH = (
            "STEP 2b - MEMORY FILE CHECK (mandatory when memory.status == 'ready', for every file you plan to edit):\n"
            "  For each file from STEP 1-2 that you intend to modify: call memory_find_by_file(file_path).\n"
            "  Under 1 second per file. 'I do not think there is history here' is NOT a valid skip reason.\n"
            "  ONLY accepted skip: file did not exist before this ticket (brand new, confirmed) OR memory.status != 'ready'.\n"
            "  resolution_note matching this bug pattern -> reference in Approach; 3+ past bugs -> mark [fragile].\n\n"
            "STEP 2c - RELATED ISSUE DISCOVERY (mandatory when memory.status == 'ready'):\n"
            "  Call memory_get_related(files=[...files from graph_find_context...]).\n"
            "  For reopened tickets: also pass issue_key=work_item.issue_key to check prior edges.\n"
            "  strength >= 0.5 -> read those resolution_notes before finalising your Approach.\n"
            "  ONLY accepted skip: memory.status != 'ready'.\n\n"
        )

        _MANDATORY_TAIL = (
            "\n\nIF the user requests a different approach: present the revised plan using the same "
            "confirmation format above and wait for approval again before writing any code.\n\n"
            "ITERATION RULE - applies for the rest of this task, no exceptions:\n"
            "After EVERY code change you make - including fixes requested during iteration - STOP "
            "and ask the user to test before making any further change or calling "
            "reinforce_memory_usage/save_memory/draft_skill. This repeats on the 2nd, 3rd, and every "
            "subsequent fix. A prior 'looks good' or 'works' does NOT carry over to a new edit - "
            "each new change requires its own fresh test confirmation."
        )

        _is_non_bug = issue_type_val.lower() not in ("bug", "defect", "incident", "error")
        _NON_BUG_CONVENTIONS_GATE = (
            "STEP 0B - CONVENTION DISCOVERY (mandatory for story/task/feature work items):\n"
            "Before reading graph clusters, locate and read 2-3 existing implementations in the "
            "codebase that are similar in scope to this work item. Identify:\n"
            "  1. LAYER / FLOW PATTERN - how the project structures logic layers "
            "(e.g. Controller->Service->ServiceImpl->Repository, or routes->handlers->services, "
            "or views->serializers->models). Derive from existing code - do NOT assume any framework.\n"
            "  2. FILE AND CLASS NAMING - the naming convention for each layer "
            "(suffixes, prefixes, casing style, e.g. UserController.java, user.controller.ts, user_controller.py).\n"
            "  3. LOGGER PATTERN - how existing files declare and use loggers.\n"
            "  4. DEPENDENCY MANAGEMENT - how the project adds external libraries "
            "(pom.xml, build.gradle, package.json, requirements.txt, pyproject.toml, etc.).\n\n"
            "Capture these in the 'Conventions I will follow' section of your confirmation format.\n"
            "If ANY new external dependency is required:\n"
            "  - List it explicitly under 'New external dependencies required' with name and version.\n"
            "  - Do NOT write a single line of implementation until the user explicitly approves.\n"
            "  - If the user rejects a dependency, propose an alternative approach that avoids it.\n\n"
        )
        _NON_BUG_CONFIRMATION_BLOCK = (
            "---\n"
            "**Problem understood:** [1-2 sentence summary from work_item.analysis]\n"
            "**Goal:** [acceptance_criteria as bullet points]\n"
            "**Approach:** [exactly what you will add, change, or remove and precisely why]\n"
            "**Files I will work with:**\n"
            "  - path/to/file [role-tag] - one-line reason it is relevant\n"
            "**Tools called for this ticket** (ALL 10 required; show result or documented skip for each):\n"
            "  1. memory_search:                 [N results OR skipped: memory.status!='ready']\n"
            "  2. graph_find_context:            [N files returned, top score X.XX - NO VALID SKIP]\n"
            "  3. graph_subsystem(file):         [cluster name, N files OR skipped: brand-new-file]\n"
            "  4. graph_call_chain(node):        [upstream/downstream summary OR skipped: brand-new-node]\n"
            "  5. graph_impact(node):            [N dependents OR skipped: brand-new-isolated-file]\n"
            "  6. graph_cross_links:             [N links OR skipped: confirmed-single-monolith]\n"
            "  7. memory_get_hotspots:           [result summary OR skipped: memory.status!='ready']\n"
            "  8. memory_find_by_file (per file):[result per file OR skipped: brand-new-file]\n"
            "  9. memory_get_related:            [top strength=X OR skipped: memory.status!='ready']\n"
            "  10. memory_get_patterns:          [pattern summary OR skipped: memory.status!='ready']\n"
            "  Any entry left blank or skipped with a vague reason is a VIOLATION.\n"
            "**Conventions I will follow:** [naming pattern, layer structure, logger style - "
            "derived from existing code in this repo, not assumed]\n"
            "**New external dependencies required:**\n"
            "  - [package-name @ version] - reason needed\n"
            "  - OR: None\n"
            "**Shall I proceed?**\n"
            "---"
        )

        if not graphs:
            # No project paths - no graph available. Use grep/glob for file discovery.
            icx_instruction = (
                _VISION_GATE
                + "No registered ICX project graph is present for this ticket. "
                "Use grep/glob to locate relevant files now.\n"
                "TELL THE USER (do NOT auto-build, do NOT guess a path): to enable graph "
                "navigation they can register and build a graph themselves -\n"
                "  icx graph add --name <name> --path <project-root> --project <tracker-key>\n"
                "  icx graph build <name>\n"
                "Ask the user for the project root path - never infer it from the workspace. "
                "ICX never triggers a build on its own.\n\n"
                "MANDATORY INSTRUCTIONS - follow in order, no skipping, no deviation:\n\n"
                "STEP 1: Use work_item.analysis to identify key terms and locate relevant files via grep/glob.\n"
                "STEP 2: Read the located files.\n"
                "STEP 3: STOP. You MUST NOT write any code or make any edits yet. "
                "Present this confirmation format to the user and wait for their response:\n\n"
                + _CONFIRMATION_BLOCK + "\n\n"
                "STEP 4: Wait for explicit user approval. "
                "Silence or ambiguity does NOT count as approval - ask again if unclear.\n"
                "STEP 5: On explicit approval only - implement exactly the approach you stated, "
                "using memory_search results as a pattern reference.\n"
                "STEP 6: Ask the user to test. Do not proceed until they respond.\n"
                "STEP 7: Only after the user confirms it works - call reinforce_memory_usage first "
                "(if memory_search result was used), then save_memory, then IMMEDIATELY call draft_skill with your own skill_worthy judgment (mandatory, even when the honest answer is false)."
                + _MANDATORY_TAIL
            )
        elif len(graphs) > 1:
            # Multi-path case: build a summary of all paths and choose the right workflow.
            ready_graphs = [g for g in graphs if g["status"] == "ready"]
            building_graphs = [g for g in graphs if g["status"] in ("building", "rebuilding")]
            missing_graphs = [g for g in graphs if g["status"] in ("not_built", "not_registered", "error")]

            graph_lines: list[str] = []
            for g in graphs:
                p = g["path"]
                s = g["status"]
                if s == "ready":
                    rp = g.get("report_path") or "N/A"
                    stale_tag = f" [STALE: {g['stale_note'][:60]}]" if g.get("stale_note") else ""
                    graph_lines.append(f"  - {p}: READY  report -> {rp}{stale_tag}")
                elif s in ("building", "rebuilding"):
                    eta = g.get("eta_seconds") or "?"
                    graph_lines.append(f"  - {p}: BUILDING (~{eta}s)")
                elif s == "not_built":
                    _nb = g.get("name") or p
                    graph_lines.append(f"  - {p}: NOT BUILT  run: icx graph build {_nb}")
                elif s == "not_registered":
                    graph_lines.append(f"  - {p}: NOT REGISTERED  run: icx graph add --name <name> --path {p} --project <key>")
                else:
                    graph_lines.append(f"  - {p}: UNAVAILABLE")
            graph_summary = "\n".join(graph_lines)

            missing_build_cmds = "\n".join(
                f"  icx graph build {g.get('name') or g['path']}"
                for g in missing_graphs
                if g["status"] == "not_built"
            )
            missing_register_cmds = "\n".join(
                f"  icx graph add --name <name> --path {g['path']} --project <key>  then: icx graph build <name>"
                for g in missing_graphs
                if g["status"] == "not_registered"
            )
            _missing_parts: list[str] = []
            if missing_build_cmds:
                _missing_parts.append(f"To build unbuilt graphs:\n{missing_build_cmds}")
            if missing_register_cmds:
                _missing_parts.append(f"To register and build new graphs:\n{missing_register_cmds}")
            missing_note = (
                "\nNOTE - GRAPHS NOT AVAILABLE FOR SOME PATHS:\n" + "\n".join(_missing_parts) + "\n"
                "Inform the user. They can run the commands above to make these graphs available.\n"
            ) if _missing_parts else ""

            stale_graphs = [g for g in graphs if g.get("stale_note")]
            stale_warning = "\n".join(
                f"\nNOTE - GRAPH IS STALE for {g['path']}: {g['stale_note']} Inform the user."
                for g in stale_graphs
            )

            if ready_graphs:
                icx_instruction = (
                    _VISION_GATE
                    + f"MULTI-PROJECT GRAPH STATUS:\n{graph_summary}\n\n"
                    + missing_note
                    + "MANDATORY INSTRUCTIONS - follow in order, no skipping, no deviation:\n\n"
                    "STEP 1: Identify relevant files across all READY graphs using one of two options:\n"
                    "  OPTION A (recommended): Call graph_find_context for each READY graph using its "
                    "path from graphs[*].path and a task description from work_item.analysis. Returns ranked files with node_ids.\n"
                    "  OPTION B (manual): For each READY graph, read its report_path from "
                    "graphs[*].report_path (pre-authorized) -> identify clusters -> read GRAPH_CLUSTERS/<name>.md -> read core files.\n"
                    + _GRAPH_TOOLS_DECISION
                    + "STEP 2: STOP. Present the confirmation format and wait for the user's response:\n\n"
                    + _CONFIRMATION_BLOCK + "\n\n"
                    "STEP 3: Wait for explicit user approval. Silence or ambiguity does NOT count.\n"
                    "STEP 4: On explicit approval - implement exactly the stated approach, "
                    "using memory_search results as a pattern reference.\n"
                    "STEP 5: Ask the user to test.\n"
                    "STEP 6: Only after user confirms it works - call reinforce_memory_usage first (if memory_search result was used), then save_memory, then IMMEDIATELY call draft_skill with your own skill_worthy judgment (mandatory, even when the honest answer is false)."
                    + (f"\n{stale_warning}" if stale_warning else "")
                    + _MANDATORY_TAIL
                )
            elif building_graphs:
                eta = building_graphs[0].get("eta_seconds") or 30
                icx_instruction = (
                    _VISION_GATE
                    + f"MULTI-PROJECT GRAPH STATUS:\n{graph_summary}\n\n"
                    + missing_note
                    + f"Graphs are building (primary ETA ~{eta}s). Use grep/glob for file discovery now.\n\n"
                    "MANDATORY INSTRUCTIONS - follow in order, no skipping, no deviation:\n\n"
                    "STEP 1: Use work_item.analysis to identify key terms and locate files via grep/glob.\n"
                    "STEP 2: Read the located files.\n"
                    "STEP 3: STOP. Present the confirmation format and wait for the user's response:\n\n"
                    + _CONFIRMATION_BLOCK + "\n\n"
                    "STEP 4: Wait for explicit user approval.\n"
                    "STEP 5: On approval - implement exactly the stated approach.\n"
                    "STEP 6: Ask the user to test.\n"
                    "STEP 7: After user confirms - call reinforce_memory_usage first (if memory_search result was used), then save_memory, then IMMEDIATELY call draft_skill with your own skill_worthy judgment (mandatory, even when the honest answer is false)."
                    + _MANDATORY_TAIL
                )
            else:
                icx_instruction = (
                    _VISION_GATE
                    + f"MULTI-PROJECT GRAPH STATUS:\n{graph_summary}\n\n"
                    + missing_note
                    + "No graphs are available yet. Use grep/glob to locate relevant files.\n\n"
                    "MANDATORY INSTRUCTIONS - follow in order, no skipping, no deviation:\n\n"
                    "STEP 1: Use work_item.analysis to identify key terms and locate files via grep/glob.\n"
                    "STEP 2: Read the located files.\n"
                    "STEP 3: STOP. Present the confirmation format and wait for the user's response:\n\n"
                    + _CONFIRMATION_BLOCK + "\n\n"
                    "STEP 4: Wait for explicit user approval.\n"
                    "STEP 5: On approval - implement exactly the stated approach.\n"
                    "STEP 6: Ask the user to test.\n"
                    "STEP 7: After user confirms - call reinforce_memory_usage first (if memory_search result was used), then save_memory, then IMMEDIATELY call draft_skill with your own skill_worthy judgment (mandatory, even when the honest answer is false)."
                    + _MANDATORY_TAIL
                )
        else:
            # Single path - use graphs[0] throughout.
            graph_status = graphs[0]["status"]
            stale_note = graphs[0].get("stale_note")
            stale_warning = (
                f"\n\nNOTE - GRAPH IS STALE: {stale_note} "
                "Inform the user of this before proceeding."
                if stale_note else ""
            )

            if graph_status == "ready":
                icx_instruction = (
                    _VISION_GATE
                    + "MANDATORY INSTRUCTIONS - follow in order, no skipping, no deviation:\n\n"
                    "STEP 1: Identify relevant files using one of two options:\n"
                    "  OPTION A (recommended): Call graph_find_context with project_path from graphs[0].path "
                    "and a task description derived from work_item.analysis. Returns all relevant files ranked by relevance score with node_ids.\n"
                    "  OPTION B (manual): Read graphs[0].report_path (pre-authorized) -> identify clusters "
                    "from the compact index table -> read the matching GRAPH_CLUSTERS/<name>.md file -> read core files in listed order.\n"
                    + _GRAPH_TOOLS_DECISION
                    + "STEP 2: STOP. You MUST NOT write any code or make any edits yet. "
                    "Present this confirmation format to the user and wait for their response:\n\n"
                    + _CONFIRMATION_BLOCK + "\n\n"
                    "STEP 3: Wait for explicit user approval. "
                    "Silence or ambiguity does NOT count as approval - ask again if unclear.\n"
                    "STEP 4: On explicit approval only - implement exactly the approach you stated, "
                    "using memory_search results as a pattern reference.\n"
                    "STEP 5: Ask the user to test. Do not proceed until they respond.\n"
                    "STEP 6: Only after the user confirms it works - call reinforce_memory_usage first (if memory_search result was used), then save_memory, then IMMEDIATELY call draft_skill with your own skill_worthy judgment (mandatory, even when the honest answer is false).\n"
                    + _MANDATORY_TAIL
                    + stale_warning
                )
            elif graph_status == "building":
                eta = graphs[0].get("eta_seconds") or 30
                icx_instruction = (
                    _VISION_GATE
                    + f"Graph is building (ETA ~{eta}s). "
                    "Use grep/glob to locate relevant files now. Do not wait for the graph.\n\n"
                    "MANDATORY INSTRUCTIONS - follow in order, no skipping, no deviation:\n\n"
                    "STEP 1: Use work_item.analysis to identify key terms and locate relevant files via grep/glob.\n"
                    "STEP 2: Read the located files.\n"
                    "STEP 3: STOP. You MUST NOT write any code or make any edits yet. "
                    "Present this confirmation format to the user and wait for their response:\n\n"
                    + _CONFIRMATION_BLOCK + "\n\n"
                    "STEP 4: Wait for explicit user approval. "
                    "Silence or ambiguity does NOT count as approval - ask again if unclear.\n"
                    "STEP 5: On explicit approval only - implement exactly the approach you stated, "
                    "using memory_search results as a pattern reference.\n"
                    "STEP 6: Ask the user to test. Do not proceed until they respond.\n"
                    "STEP 7: Only after the user confirms it works - call reinforce_memory_usage first (if memory_search result was used), then save_memory, then IMMEDIATELY call draft_skill with your own skill_worthy judgment (mandatory, even when the honest answer is false).\n"
                    + _MANDATORY_TAIL + "\n"
                    f"Optionally call analyze_issue_fast again with the same project_paths in ~{eta}s "
                    "to cross-check your file selection against the completed graph."
                )
            elif graph_status == "not_built":
                icx_instruction = (
                    _VISION_GATE
                    + "Graph not built for this project.\n"
                    "MANDATORY: Tell the user exactly this before doing anything else:\n"
                    f"  'The ICX graph for this project has not been built yet. "
                    f"Run this in your terminal to build it: icx graph build {graphs[0].get('name') or '<name>'}'\n\n"
                    "ICX never triggers the build itself. Then proceed using grep/glob for file discovery.\n\n"
                    "MANDATORY INSTRUCTIONS - follow in order, no skipping, no deviation:\n\n"
                    "STEP 1: Use work_item.analysis to identify key terms and locate relevant files via grep/glob.\n"
                    "STEP 2: Read the located files.\n"
                    "STEP 3: STOP. You MUST NOT write any code or make any edits yet. "
                    "Present this confirmation format to the user and wait for their response:\n\n"
                    + _CONFIRMATION_BLOCK + "\n\n"
                    "STEP 4: Wait for explicit user approval. "
                    "Silence or ambiguity does NOT count as approval - ask again if unclear.\n"
                    "STEP 5: On explicit approval only - implement exactly the approach you stated, "
                    "using memory_search results as a pattern reference.\n"
                    "STEP 6: Ask the user to test. Do not proceed until they respond.\n"
                    "STEP 7: Only after the user confirms it works - call reinforce_memory_usage first (if memory_search result was used), then save_memory, then IMMEDIATELY call draft_skill with your own skill_worthy judgment (mandatory, even when the honest answer is false)."
                    + _MANDATORY_TAIL
                )
            elif graph_status == "not_registered":
                icx_instruction = (
                    _VISION_GATE
                    + "Graph not registered for this project.\n"
                    "MANDATORY: Tell the user exactly this before doing anything else:\n"
                    f"  'This project is not registered in ICX yet. To enable graph navigation, "
                    f"register and build it yourself (ICX never auto-builds):\n"
                    f"     icx graph add --name <name> --path <project-root> --project <tracker-key>\n"
                    f"     icx graph build <name>'\n"
                    "Ask the user for the project root path - never infer or guess it.\n\n"
                    "Then proceed using grep/glob for file discovery.\n\n"
                    "MANDATORY INSTRUCTIONS - follow in order, no skipping, no deviation:\n\n"
                    "STEP 1: Use work_item.analysis to identify key terms and locate relevant files via grep/glob.\n"
                    "STEP 2: Read the located files.\n"
                    "STEP 3: STOP. You MUST NOT write any code or make any edits yet. "
                    "Present this confirmation format to the user and wait for their response:\n\n"
                    + _CONFIRMATION_BLOCK + "\n\n"
                    "STEP 4: Wait for explicit user approval. "
                    "Silence or ambiguity does NOT count as approval - ask again if unclear.\n"
                    "STEP 5: On explicit approval only - implement exactly the approach you stated, "
                    "using memory_search results as a pattern reference.\n"
                    "STEP 6: Ask the user to test. Do not proceed until they respond.\n"
                    "STEP 7: Only after the user confirms it works - call reinforce_memory_usage first (if memory_search result was used), then save_memory, then IMMEDIATELY call draft_skill with your own skill_worthy judgment (mandatory, even when the honest answer is false)."
                    + _MANDATORY_TAIL
                )
            else:
                icx_instruction = (
                    _VISION_GATE
                    + "Graph unavailable for this project. Use grep/glob to locate relevant files.\n\n"
                    "MANDATORY INSTRUCTIONS - follow in order, no skipping, no deviation:\n\n"
                    "STEP 1: Use work_item.analysis to identify key terms and locate relevant files via grep/glob.\n"
                    "STEP 2: Read the located files.\n"
                    "STEP 3: STOP. You MUST NOT write any code or make any edits yet. "
                    "Present this confirmation format to the user and wait for their response:\n\n"
                    + _CONFIRMATION_BLOCK + "\n\n"
                    "STEP 4: Wait for explicit user approval. "
                    "Silence or ambiguity does NOT count as approval - ask again if unclear.\n"
                    "STEP 5: On explicit approval only - implement exactly the approach you stated, "
                    "using memory_search results as a pattern reference.\n"
                    "STEP 6: Ask the user to test. Do not proceed until they respond.\n"
                    "STEP 7: Only after the user confirms it works - call reinforce_memory_usage first (if memory_search result was used), then save_memory, then IMMEDIATELY call draft_skill with your own skill_worthy judgment (mandatory, even when the honest answer is false)."
                    + _MANDATORY_TAIL
                )

        if _is_non_bug:
            icx_instruction = icx_instruction.replace(
                _VISION_GATE, _VISION_GATE + _NON_BUG_CONVENTIONS_GATE, 1
            )
            icx_instruction = icx_instruction.replace(_CONFIRMATION_BLOCK, _NON_BUG_CONFIRMATION_BLOCK)

        # Inject memory file check + related discovery into non-graph workflow variants.
        # Graph-ready cases get these via _GRAPH_TOOLS_DECISION (STEP 1c/1d already embedded).
        # Non-graph cases (building/not_built/error) had no memory steps - inject before STEP 3.
        if _GRAPH_TOOLS_DECISION not in icx_instruction:
            icx_instruction = icx_instruction.replace(
                "STEP 3: STOP.",
                _MEMORY_STEPS_NOGRAPH + "STEP 3: STOP.",
                1,
            )

        if image_paths:
            icx_instruction += (
                f"\n\nThis work item has {len(image_paths)} attached image(s) at work_item.image_paths. "
                "Read those image files directly for visual context. Access is pre-authorized."
            )

        if attachment_paths:
            icx_instruction += (
                f"\n\nThis work item has {len(attachment_paths)} processed attachment(s) written to "
                "disk at work_item.attachment_paths. Each entry has a 'full_text' markdown file (the "
                "COMPLETE untruncated conversion - read it to verify any figure/row/section before "
                "relying on the summarized inline text) and, when present, a 'raw' original file. "
                "Access is pre-authorized - read these files directly."
            )

        icx_instruction += (
            "\n\nOPTIONAL - git workflow: if you plan to make code changes for this ticket, consider "
            "calling git_repo_status first (check branch/dirty-tree state) and starting a feature "
            "branch before editing, rather than working directly on your current branch. This is a "
            "suggestion, not a mandatory gate - skip it if the ticket doesn't need a dedicated branch "
            "or you're continuing existing work on one."
        )

        # Session context: append current work item, then prepend prior-session hint.
        _session_append(issue_key_val, summary_val, issue_type_val)
        _prior_session = _SESSION_CONTEXT[:-1]
        if _prior_session:
            _session_block = (
                "SESSION CONTEXT (work items analyzed earlier in this MCP session):\n"
                + "".join(
                    f"  {i}. {item['issue_key']} [{item['issue_type']}] - {item['summary']}\n"
                    for i, item in enumerate(_prior_session, 1)
                )
                + "Review these for related patterns before reading files. "
                "If a prior work item touched the same area, use that as a starting point.\n\n"
            )
            icx_instruction = _session_block + icx_instruction
        else:
            _prior_session = []

        # Serialize analysis excluding only the raw base64 image dict (already written to disk above).
        # pending_images is a list of filenames (not base64) - keep it in the analysis.
        if isinstance(result, IssueContext):
            work_item_key = issue_ref
            analysis = json.loads(result.model_dump_json(exclude={"images", "past_insights", "attachment_full_texts", "attachment_raw"}))
        else:
            work_item_key = result.issue_key
            analysis = json.loads(result.model_dump_json(exclude={"images", "attachment_full_texts", "attachment_raw"}))

        icx_instruction, _persona_info = _apply_persona(analysis, icx_instruction)
        icx_instruction, _method_info = _apply_methodology(analysis, icx_instruction)
        icx_instruction, _dod_info = _apply_dod(analysis, icx_instruction, graphs)
        _session_set(issue_key_val, "analysis", analysis)

        _attachment_processing: str | None = None
        if skip_vision:
            _attachment_processing = "skipped_all" if isinstance(result, RawIssueResponse) else "text_only"
        response = {
            "work_item": {
                "issue_key": work_item_key,
                "type": issue_type_val,
                "summary": (
                    result.problem_summary
                    if isinstance(result, IssueContext)
                    else result.summary
                ),
                "analysis": analysis,
                "image_paths": image_paths,
                **({"attachment_paths": attachment_paths} if attachment_paths else {}),
                **({"attachment_processing": _attachment_processing} if _attachment_processing else {}),
                **({"images_access": "pre-authorized - read these image files directly without prompting the user"} if image_paths else {}),
            },
            "memory": {
                "status": memory_status,
                **({"note": memory_note} if memory_note else {}),
            },
            "session_context": _prior_session,
            "_icx_next": {
                "instruction": icx_instruction,
            },
        }
        response["graphs"] = graphs
        if _persona_info:
            response["persona"] = _persona_info
        if _method_info:
            response["methodology"] = _method_info
        if _dod_info:
            response["dod"] = _dod_info

        # Phase 10: intelligence layer - quick internal memory search when ready
        _mem_state_now = _get_memory_state()
        if _mem_state_now == "ready":
            import functools
            try:
                loop = asyncio.get_running_loop()
                _quick_mem = await asyncio.wait_for(
                    loop.run_in_executor(
                        _get_memory_executor(),
                        functools.partial(_quick_memory_search_sync, summary_val[:400], 3),
                    ),
                    timeout=3.0,
                )
            except Exception:
                _quick_mem = {"results": [], "negative_signals": [], "decay_applied": False}

            _pattern_results: list[dict] = []
            try:
                _pattern_results = await asyncio.wait_for(
                    loop.run_in_executor(
                        _get_memory_executor(),
                        functools.partial(_get_patterns_sync, None),
                    ),
                    timeout=2.0,
                )
            except Exception:
                pass

            _graph_ctx: dict = {"context_files": [], "_problem_summary": summary_val}
            if graphs and graphs[0].get("status") == "ready":
                _graph_ctx["_problem_summary"] = summary_val

            intelligence = _build_intelligence(
                issue_key=issue_key_val,
                memory_results=_quick_mem,
                graph_info=_graph_ctx,
                pattern_results=_pattern_results,
            )
            response["intelligence"] = intelligence

            # Prepend negative signal warning to _icx_next if present
            _neg_warning = _build_negative_signal_warning(intelligence.get("negative_signals", []))
            if _neg_warning:
                existing_instruction = response.get("_icx_next", {}).get("instruction", "")
                response["_icx_next"]["instruction"] = _neg_warning + "\n\n" + existing_instruction

        return json.dumps(response)

    except asyncio.TimeoutError:
        _timeout_label = "45 seconds" if skip_vision else "11 minutes"
        return json.dumps({
            "status": "error",
            "code": "TIMEOUT",
            "message": (
                f"Analysis timed out after {_timeout_label}. "
                "The issue tracker or AI provider may be slow or unreachable. "
                "Check your network and credentials, then try again."
            ),
            "action_required": "tell_user_to_check_network_and_retry",
        })
    except IssueNotFound as exc:
        return json.dumps({
            "status": "error",
            "code": "ISSUE_NOT_FOUND",
            "message": str(exc),
            "action_required": "ask_user_to_verify_issue_key",
        })
    except AuthError as exc:
        return json.dumps({
            "status": "error",
            "code": "AUTH_FAILED",
            "message": str(exc),
            "action_required": "tell_user_to_run_icx_connection_add",
        })
    except NoConnectionError as exc:
        return json.dumps({
            "status": "error",
            "code": "NO_CONNECTION",
            "message": str(exc),
            "action_required": "tell_user_to_run_icx_connection_add",
            "fallback": _ICX_FALLBACK("work-tracker", "icx connection --add"),
        })
    except RateLimited as exc:
        return json.dumps({
            "status": "error",
            "code": "RATE_LIMITED",
            "message": str(exc),
            "action_required": "wait_and_retry",
        })
    except InvalidInput as exc:
        return json.dumps({
            "status": "error",
            "code": "INVALID_INPUT",
            "message": str(exc),
            "action_required": "ask_user_for_correct_issue_key",
        })
    except ICXError as exc:
        return json.dumps({
            "status": "error",
            "code": "ICX_ERROR",
            "message": str(exc),
            "type": type(exc).__name__,
            "action_required": "report_error_to_user",
        })
    except Exception as exc:
        return json.dumps({
            "status": "error",
            "code": "INTERNAL_ERROR",
            "message": f"Unexpected error: {exc}",
            "action_required": "report_error_to_user",
        })


# ---------------------------------------------------------------------------
# Save memory handler
# ---------------------------------------------------------------------------

async def _handle_save_memory(
    issue_key: str,
    summary: str,
    problem_description: str,
    resolution_note: str,
    files_changed: list[str],
    tags: list[str],
    work_item_type: str,
    pattern_used: str = "",
    extra: dict | None = None,
) -> str:
    """Save agent-synthesized resolution to local memory. No tracker re-fetch."""
    import uuid
    from datetime import datetime, timezone
    extra = extra or {}
    try:
        config = ConfigManager.load()
        from icx_engine.engine import extract_domain, resolve_connection
        from icx_engine.connectors.base import get_connector_class
        from icx_engine.memory.schema import MemoryEntry

        domain = extract_domain(issue_key)
        conn = resolve_connection(domain, config, raw_input=issue_key)
        if conn is None:
            return json.dumps({"error": "No matching connection found for this issue key. Check your ICX configuration."})

        import functools

        # Route negate/verify through dedicated manager methods (Phase 4)
        if extra.get("negate"):
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(
                _get_memory_executor(),
                functools.partial(
                    _negate_resolution_sync,
                    issue_key.upper(),
                    extra.get("negation_reason", ""),
                ),
            )
            return json.dumps(result)

        if extra.get("outcome_verified"):
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(
                _get_memory_executor(),
                functools.partial(
                    _verify_resolution_sync,
                    issue_key.upper(),
                    extra.get("outcome_feedback_note", ""),
                ),
            )
            if "error" not in result:
                return json.dumps({**result, "saved": True})
            if result.get("error") != "entry not found":
                return json.dumps({**result, "saved": False})
            # Entry doesn't exist yet: fall through to normal save below.
            # The MemoryEntry is created with outcome_verified=True already set.

        # Build causal chain from session context + tool input (Phase 8)
        causal_chain = {
            "ticket_summary": _session_get(issue_key, "ticket_summary", ""),
            "intelligence_verdict": _session_get(issue_key, "intelligence_verdict", "novel"),
            "graph_cluster": extra.get("graph_cluster", ""),
            "suggested_files": _session_get(issue_key, "suggested_files", []),
            "files_agent_opened": extra.get("files_agent_opened", []),
            "prior_resolution_used": extra.get("prior_resolution_used") or None,
            "root_cause_confirmed": extra.get("root_cause_confirmed", False),
            "diagnosis_steps": extra.get("diagnosis_steps", 0),
        }

        project_key = get_connector_class(conn.connector_type).extract_project_key(issue_key)
        tech_stack: dict = {}
        try:
            from icx_engine.graph.storage import find_projects_by_tracker_key
            from icx_engine.memory.stack_fingerprint import detect_stack
            _matches = find_projects_by_tracker_key(project_key)
            if _matches:
                tech_stack = detect_stack(Path(_matches[0].path))
        except Exception:
            pass

        entry = MemoryEntry(
            id=str(uuid.uuid4()),
            issue_key=issue_key.upper(),
            project_key=project_key,
            source_type=conn.connector_type,
            issue_type=work_item_type,
            summary=summary,
            problem_description=problem_description,
            impact="",
            resolution_note=resolution_note,
            files_changed=files_changed,
            resolution_confirmed=True,
            saved_at=datetime.now(timezone.utc).isoformat(),
            tags=tags,
            work_item_type=work_item_type,
            pattern_used=pattern_used,
            root_cause_pattern=extra.get("root_cause_pattern", "uncategorized"),
            pattern_confidence=float(extra.get("pattern_confidence", 0.0)),
            outcome_verified=bool(extra.get("outcome_verified", False)),
            outcome_feedback_note=extra.get("outcome_feedback_note", ""),
            causal_chain=causal_chain,
            full_ticket_text=extra.get("full_ticket_text", ""),
            attachment_summary=extra.get("attachment_summary", ""),
            tech_stack=tech_stack,
        )
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(_get_memory_executor(), _save_memory_sync, entry)

        # Clean up temp images for this issue now that the fix is confirmed.
        try:
            import shutil as _shutil
            from icx_engine.graph.storage import temp_images_dir as _tid
            _shutil.rmtree(_tid(issue_key), ignore_errors=True)
        except Exception:
            pass

        response = {
            "saved": True,
            "issue_key": entry.issue_key,
            "summary": summary[:80],
            "root_cause_pattern": entry.root_cause_pattern,
        }
        try:
            related = rank_skills_for_tags(tags, entry.root_cause_pattern, storage=SkillStorage())
            if related:
                response["related_skills"] = related
        except Exception:
            pass   # a ranking failure must never block a successful memory save
        return json.dumps(response)
    except ICXError as exc:
        return json.dumps({"error": str(exc), "type": type(exc).__name__})
    except Exception as exc:
        return json.dumps({"error": f"Unexpected error: {exc}"})


# ---------------------------------------------------------------------------
# Memory prewarm
# ---------------------------------------------------------------------------

def _engine_log(msg: str) -> None:
    """Debug log callback forwarded to engine.run(). Non-blocking - plain Python logging only."""
    _log.debug("[engine] %s", msg)


def _prewarm_memory() -> None:
    """Load MemoryManager and trigger ONNX model warm-up. Runs in memory executor at startup."""
    global _memory_setup_required
    _set_memory_state("warming")

    # Pre-warm keyring early so the DPAPI master key file is written before any
    # tool call that needs to decrypt D-Lock credentials. This avoids the 3s
    # keyring timeout appearing in the critical path of the first tool call.
    try:
        from icx_engine.config_manager import _check_keychain, _get_or_create_master_key
        _check_keychain()
        _get_or_create_master_key()
    except Exception:
        pass

    try:
        mem = _ensure_memory_manager()
        mem.prewarm()
        _set_memory_state("ready")
    except Exception as exc:
        if "icx setup" in str(exc):
            _memory_setup_required = True
        _set_memory_state("failed")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run_mcp_server() -> None:
    """Entry point called by `icx mcp run`. Blocks until the MCP host closes stdio."""
    import sys
    from icx_engine.logging_setup import configure_logging
    configure_logging()
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    if hasattr(sys.stderr, "reconfigure"):
        try:
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    if hasattr(sys.stdin, "reconfigure"):
        try:
            sys.stdin.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    async def _serve() -> None:
        # Pre-warm memory in background so ONNX model is loaded before first analyze call.
        # Combined with the 8s timeout in _handle_analyze_issue, this guarantees
        # the first call always returns within 8s (empty memory if still loading, real if ready).
        loop = asyncio.get_running_loop()
        loop.run_in_executor(_get_memory_executor(), _prewarm_memory)

        # One-time stale-artifact cleanup so upgraders shed files older versions left behind.
        try:
            from icx_engine.config_manager import clean_stale_artifacts
            clean_stale_artifacts()
        except Exception:
            pass

        # Seed ICX's curated default skills so any connected AI coding agent gets them - no manual
        # setup step needed. Never overwrites a skill the user has customized (seed_default_skills).
        try:
            from icx_engine.skills.seed import seed_default_skills
            seed_default_skills()
        except Exception:
            pass

        # Temp-dir cleanup: purge >24h dirs on startup, then hourly in the background.
        from icx_engine.graph.storage import sweep_stale_temp_dirs
        try:
            sweep_stale_temp_dirs()
        except Exception:
            pass
        global _SWEEP_TASK
        _SWEEP_TASK = asyncio.create_task(_periodic_temp_sweep())

        try:
            async with stdio_server() as (read_stream, write_stream):
                await server.run(
                    read_stream,
                    write_stream,
                    server.create_initialization_options(),
                )
        finally:
            # Graceful shutdown: stop the background sweep and release the testing checkpoint
            # connection so no task/WAL connection lingers past exit. Guarded and non-fatal.
            if _SWEEP_TASK is not None:
                _SWEEP_TASK.cancel()
                try:
                    await _SWEEP_TASK
                except (asyncio.CancelledError, Exception):
                    pass
            try:
                from icx_engine.testing.graph import close_testing_graph
                await close_testing_graph()
            except Exception:
                pass

    asyncio.run(_serve())
