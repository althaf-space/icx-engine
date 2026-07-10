from __future__ import annotations
import asyncio
import logging
from pathlib import Path
from typing import Any

from langgraph.errors import GraphBubbleUp
from langgraph.types import interrupt

from icx_engine.testing.state import TestingState
from icx_engine.testing.classify import classify_file
from icx_engine.testing.compat import build_report
from icx_engine.testing.handlers import get_handler
from icx_engine.testing.expand import expand_via_grep, union_rank
from icx_engine.testing import auth as _auth
from icx_engine.testing import apispec as _apispec
from icx_engine.testing import profile_gen as _profile_gen
from icx_engine.testing import rules as _rules

_log = logging.getLogger(__name__)

_REREAD_MANDATE = (
    " MANDATORY RE-READ: Open and read EVERY file listed above fully, from the first line to the last "
    "line, in THIS step right now. Any earlier read, summary, cache, or memory of these files is STALE "
    "and must NOT be used as your basis - if you read a file before, read it again completely now. "
    "Partial reading or relying on memory causes missed details and wrong output. After reading, include "
    "read_receipts: a list of {\"path\", \"line_count\", \"last_line\"} for every file you read this step, "
    "as proof of a full read."
)


def _record_receipts(state: TestingState, gate: str, response: dict) -> list[dict]:
    recs = response.get("read_receipts")
    entry = {"gate": gate, "receipts": recs if isinstance(recs, list) else []}
    return state.get("read_receipts", []) + [entry]


_POLL_INTERVAL = 30
_MAX_RETRIES = 3
_RETRY_BACKOFF = [5, 15, 45]
_TERMINAL_STATES = {"completed", "failed", "cancelled"}


def _resolve_choice(response: dict[str, Any], key: str, numbered_map: dict[str, str]) -> str:
    """Accept either a number ("1", "2") or the full word value for a choice field."""
    value = str(response.get(key, "")).strip()
    return numbered_map.get(value, value)




def _load_querier(project_paths: list[str]):
    try:
        from icx_engine.graph import storage
        from icx_engine.graph.query import GraphQuerier
    except Exception as exc:
        _log.debug("graph module unavailable: %s", exc)
        return None

    for p in project_paths:
        try:
            info = storage.lookup_for_file(Path(p))
            if info is None:
                continue
            gpath = storage.graph_path(info.project_id)
            if gpath.exists():
                try:
                    return GraphQuerier(gpath)
                except Exception as exc:
                    _log.warning("graph load failed for %s: %s", p, exc)
        except Exception as exc:
            _log.debug("lookup failed for %s: %s", p, exc)

    return None


def _project_root(file_paths: list[str]) -> Path | None:
    try:
        from icx_engine.graph import storage
    except Exception:
        storage = None
    for p in file_paths:
        if storage is not None:
            try:
                info = storage.lookup_for_file(Path(p))
                if info is not None:
                    return Path(info.path)
            except Exception:
                pass
    parents = [Path(p).resolve().parent for p in file_paths if p]
    if not parents:
        return None
    try:
        import os
        common = os.path.commonpath([str(p) for p in parents])
        return Path(common)
    except ValueError:
        return parents[0]


def _expand_files_via_graph(
    file_paths: list[str],
    querier: Any | None,
    context: str | None = None,
) -> list[str]:
    if querier is None:
        return list(file_paths)

    expanded: set[str] = set(file_paths)

    # blast_radius: all files that depend on the changed files (direct + transitive)
    try:
        blast = querier.get_blast_radius(file_paths)
        expanded.update(blast.get("direct_dependents", []))
        expanded.update(blast.get("transitive_dependents", []))
    except Exception as exc:
        _log.warning("get_blast_radius failed: %s", exc)

    for fp in file_paths:
        fp_norm = fp.replace("\\", "/")

        # get_subsystem: full feature cluster the file belongs to (top 10 by degree)
        try:
            subsystem = querier.get_subsystem(fp_norm)
            expanded.update(subsystem.top_files)
        except Exception as exc:
            _log.warning("get_subsystem failed for %s: %s", fp_norm, exc)

        # get_cochange_partners: files historically committed together (strength >= 0.5)
        try:
            partners = querier.get_cochange_partners(fp_norm)
            for partner in partners:
                if partner.get("strength", 0.0) >= 0.5 and partner.get("file"):
                    expanded.add(partner["file"])
        except Exception as exc:
            _log.warning("get_cochange_partners failed for %s: %s", fp_norm, exc)

    # find_context: semantic search using task/context description (top 5 matches)
    if context:
        try:
            ctx_results = querier.find_context(context)
            for r in ctx_results[:5]:
                if r.file:
                    expanded.add(r.file)
        except Exception as exc:
            _log.warning("find_context failed: %s", exc)

    return sorted(expanded)


def _infer_url_from_files(file_paths: list[str], querier: Any | None) -> str | None:
    return None


def _apply_scope(prompt: str | None, file_paths: list[str], scope: str) -> str | None:
    if not prompt or scope != "ticket":
        return prompt
    file_list = "\n".join(f"- {f}" for f in file_paths)
    return (
        prompt
        + f"\n\nSCOPE: Ticket-scoped test.\n"
        f"Only test functionality directly changed by these files:\n{file_list}\n"
        f"Skip unrelated screens, fields, or functionality not touched by these files."
    )


# -- node_pick_type: test type selection -----------------------------------

async def node_pick_type(state: TestingState) -> dict:
    _TYPE_MAP = {"1": "agent", "2": "ui", "3": "api"}
    response = interrupt({
        "gate": "pick_type",
        "message": (
            "Pick the test type. ICX selects files based on your choice.\n\n"
            "  1. agent - adaptive AI browser-use (frontend).\n"
            "  2. ui    - strict Playwright field validation (frontend).\n"
            "  3. api   - REST endpoint test (backend)."
        ),
        "options": ["1. agent", "2. ui", "3. api"],
    })
    test_type = _resolve_choice(response, "test_type", _TYPE_MAP)
    if test_type not in ("agent", "ui", "api"):
        test_type = "agent"
    return {"test_type": test_type}


# -- Gate 1: file confirmation ----------------------------------------------

async def node_expand_files(state: TestingState) -> dict:
    seeds = state["file_paths"]
    querier = _load_querier(seeds)
    graph_expanded = _expand_files_via_graph(seeds, querier, context=state.get("context"))
    root = _project_root(seeds)

    # Agent grep (funnel): the agent searches the repo for related files. ICX grep is the fallback.
    scan = interrupt({
        "gate": "expand_scan",
        "seeds": seeds,
        "project_root": str(root) if root is not None else None,
        "graph_expanded": graph_expanded,
        "rules": _rules.load_gate_rules("expand_scan"),
        "rules_path": _rules.gate_rules_path("expand_scan"),
        "instruction": (
            "Search the repository for files related to the seed files: importers, callers, "
            "same-feature components, and the route/page that renders them. Read with your own tools. "
            "Return {\"related_files\": [<repo paths>]}. graph_expanded already lists graph-derived files; "
            "add what the graph missed."
            + _REREAD_MANDATE
        ),
    })
    related = scan.get("related_files")
    if not isinstance(related, list):
        related = []
        if root is not None:
            try:
                related = expand_via_grep(seeds, root)
            except Exception as exc:
                _log.warning("grep expand failed: %s", exc)

    ranked = union_rank(seeds, graph_expanded, related)
    file_sources = {p: src for p, src in ranked}

    mode = state.get("test_type")
    classified: list[dict[str, Any]] = []
    selected: list[str] = []
    off_type: list[str] = []
    relevant = get_handler(mode).relevant_layers() if mode in ("ui", "agent", "api") else None

    for path, _src in ranked:
        try:
            content = Path(path).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            content = None
        fc = classify_file(path, content)
        fc.source = file_sources.get(path, "seed")
        classified.append(vars(fc))
        if relevant is None or fc.layer in relevant or path in seeds:
            selected.append(path)
        else:
            off_type.append(path)

    confirmed = interrupt({
        "gate": "expand",
        "message": "Confirm files for testing. Off-type files are excluded by default.",
        "test_type": mode,
        "selected_files": selected,
        "excluded_off_type": off_type,
        "file_sources": file_sources,
        "graph_available": querier is not None,
    })
    final = confirmed.get("confirmed_files", selected)
    return {
        "file_paths": final,
        "file_sources": file_sources,
        "classified": [c for c in classified if c["path"] in set(final)],
        "url": confirmed.get("url") or state.get("url"),
        "read_receipts": _record_receipts(state, "expand_scan", scan),
    }


# -- node_compat_scan: agent-generate interrupt for compatibility detection --
#
# ICX does NOT judge compatibility and does NOT verify the agent's answer - it is
# a pure router. The mandate below makes completeness the agent's responsibility:
# assess everything from first principles, never defer a problem to the runner's
# tolerance, and surface every finding to the user, who decides each one.

_COMPAT_MANDATE = (
    " You are assessing whether the code can be tested AS-IS for test_type '{mode}'. This is YOUR "
    "complete responsibility - ICX does not check your work, so nothing may be left unexamined."
    " COMPLETENESS - leave nothing: reason from first principles about everything a test physically must "
    "do (reach the screen, locate each control, see it, interact with it as a real user would, observe "
    "the result). Examine every interactive element and every state involved. Do NOT work from a fixed "
    "list of known problems - anything that could stop a deterministic test is in scope."
    " FORBIDDEN - deferring to the runner: you may NOT pass anything by assuming the test tool, "
    "browser-use agent, or Playwright will 'work around it', 'still manage', 'figure it out', or be "
    "'less robust but fine'. The runner's tolerance is not your excuse. If a real user or a deterministic "
    "test would struggle with a control as written, it IS a finding. 'Probably works' / 'should be ok' / "
    "'optional' are not verdicts - if you are not certain it is cleanly testable as-is, it is a finding."
    " REPORT, DO NOT DECIDE: every concern, however small, becomes a finding you show the user - what you "
    "saw (path + line), why it impedes testing, and the concrete change you propose. You do NOT silently "
    "accept, skip, or drop anything; the user decides each finding's fate and you then execute exactly "
    "that."
    " Return {\"all_compatible\": bool, \"findings\": [{\"path\", \"compatible\": bool, \"reasons\": [str], "
    "\"required_changes\": [str]}]}. Set all_compatible true ONLY if you genuinely found nothing by "
    "inspection - never by assuming the tool will cope. required_changes must be concrete, actionable edits."
)


async def node_compat_scan(state: TestingState) -> dict:
    mode = state.get("test_type") or "agent"
    files = list(state["file_paths"])
    response = interrupt({
        "gate": "compat_scan",
        "test_type": mode,
        "file_paths": files,
        "rules": _rules.load_gate_rules("compat_scan"),
        "rules_path": _rules.gate_rules_path("compat_scan"),
        "instruction": _COMPAT_MANDATE.replace("{mode}", mode) + _REREAD_MANDATE,
    })
    response = response if isinstance(response, dict) else {}
    findings = response.get("findings")
    if not isinstance(findings, list):
        # fallback: ICX heuristic detection (headless / no agent only)
        classified = []
        for path in files:
            try:
                content = Path(path).read_text(encoding="utf-8", errors="ignore")
            except OSError:
                content = None
            classified.append(classify_file(path, content))
        findings = [vars(v) for v in build_report(classified, mode)]
    return {"compat_findings": findings, "read_receipts": _record_receipts(state, "compat_scan", response)}


def route_after_scan(state: TestingState) -> str:
    findings = state.get("compat_findings", [])
    if any(not f.get("compatible", True) for f in findings):
        return "compat_check"
    return "config_gate"


# -- node_compat_check: compatibility gate with human-in-the-loop remediation --

async def node_compat_check(state: TestingState) -> dict:
    files = list(state["file_paths"])
    iteration = state.get("compat_iteration", 0)
    max_iter = state.get("max_compat_iterations", 3)
    findings = state.get("compat_findings", [])
    incompatible = [f for f in findings if not f.get("compatible", True)]

    if not incompatible:
        return {"status": "compat_ok"}

    forced = iteration >= max_iter
    response = interrupt({
        "gate": "compat_check",
        "forced": forced,
        "message": (
            "Some files are not ready for testing. Review the required changes."
            if not forced else
            "Max remediation rounds reached. Resolve the remaining files (drop or manual)."
        ),
        "test_type": state.get("test_type"),
        "iteration": iteration,
        "max_iterations": max_iter,
        "incompatible": incompatible,
        "rules": _rules.load_gate_rules("compat_check"),
        "rules_path": _rules.gate_rules_path("compat_check"),
        "instruction": (
            "Show every finding to the user and let the user decide each one. If decision is 'approve', "
            "apply the required_changes to the source files, then resume with {\"decision\": \"approve\"} "
            "to re-scan. If decision is 'reject', provide resolution per path: 'drop' (remove from the "
            "test set), 'manual' (user will test it by hand), or 'accept' (test it as-is, no change - "
            "the user knowingly accepts the finding). Never choose on the user's behalf."
        ),
    })

    decision = str(response.get("decision", "")).strip().lower()
    resolution = dict(state.get("compat_resolution", {}))
    res_in = response.get("resolution", {}) or {}
    incompatible_paths = [f.get("path") for f in incompatible]

    if decision == "approve" and not forced:
        edited = response.get("edited_files", [p for p in incompatible_paths if p])
        return {
            "status": "compat_recheck",
            "compat_iteration": iteration + 1,
            "edited_files": state.get("edited_files", []) + list(edited),
        }

    remaining = []
    for path in files:
        choice = str(res_in.get(path, "manual")).strip().lower()
        if path in incompatible_paths:
            if choice == "drop":
                resolution[path] = "dropped"
            elif choice == "accept":
                resolution[path] = "accepted"
                remaining.append(path)   # keep in the test set, unchanged
            else:
                resolution[path] = "manual"
                remaining.append(path)
        else:
            remaining.append(path)

    status = "compat_empty" if not remaining else "compat_ok"
    return {
        "status": status,
        "file_paths": remaining,
        "compat_resolution": resolution,
        "compat_iteration": iteration + 1,
    }


# -- Gate 2: mode + scope selection ----------------------------------------

async def node_mode_gate(state: TestingState) -> dict:
    _MODE_MAP = {"1": "auto_detect", "2": "json_spec"}
    _SCOPE_MAP = {"1": "ticket", "2": "full"}
    _MERGE_MAP = {"1": True, "2": False}
    default_mode = "auto_detect" if state.get("url") else "json_spec"
    default_merge = state.get("merge_files", len(state["file_paths"]) > 1)

    response = interrupt({
        "gate": 2,
        "message": (
            "Answer all questions below. Press Enter after each.\n\n"
            "DETECTION MODE (how Magik generates the test spec):\n"
            "  1. auto_detect - Magik opens the URL live with Playwright, scans page fields.\n"
            "                   Requires the app to be running and URL accessible.\n"
            "  2. json_spec   - AI reads your JSX/TSX source files directly. No browser needed.\n"
            "                   Use this when URL requires VPN or auth.\n"
            f"  Default: {'1' if default_mode == 'auto_detect' else '2'}. {default_mode}\n\n"
            "TEST SCOPE:\n"
            "  1. ticket - Test only functionality changed by the listed files (recommended).\n"
            "  2. full   - Full end-to-end test of the entire screen.\n"
            "  Default: 1. ticket\n\n"
            + (
                "MERGE FILES (multiple JSX files - merge into one spec?):\n"
                "  1. yes - merge all files into one combined spec.\n"
                "  2. no  - generate separate specs per file.\n"
                f"  Default: {'1. yes' if default_merge else '2. no'}\n\n"
                if len(state["file_paths"]) > 1 else ""
            ) +
            "URL (required for auto_detect, optional for json_spec):\n"
            f"  Current: {state.get('url') or 'not set'}\n"
            "  Provide URL or press Enter to keep current."
        ),
        "options": {
            "mode":        ["1. auto_detect", "2. json_spec"],
            "scope":       ["1. ticket", "2. full"],
            "merge_files": ["1. yes", "2. no"],
        },
        "defaults": {
            "mode":        f"{'1' if default_mode == 'auto_detect' else '2'}",
            "scope":       "1",
            "merge_files": "1" if default_merge else "2",
            "url":         state.get("url"),
        },
        "file_paths": state["file_paths"],
    })

    mode = _resolve_choice(response, "mode", _MODE_MAP)
    if mode not in ("auto_detect", "json_spec"):
        mode = default_mode
    scope = _resolve_choice(response, "scope", _SCOPE_MAP)
    if scope not in ("ticket", "full"):
        scope = "ticket"
    merge_raw = _resolve_choice(response, "merge_files", _MERGE_MAP)
    merge = bool(merge_raw) if isinstance(merge_raw, bool) else default_merge

    update: dict[str, Any] = {
        "detection_mode": mode,
        "scope": scope,
        "merge_files": merge,
    }
    if response.get("url"):
        update["url"] = response["url"]
    return update


# -- node_generate_context: Magik prompt fetch + AI editor spec ------------

_SPEC_MAX_REASK = 2


def _run_gate_2b(base_payload: dict) -> tuple[dict, list[str]]:
    """Run gate 2b with the rulebook injected and required-section presence enforced.

    ICX does not judge spec quality - it only checks that every section listed in
    ~/.icx/testing_rules/2b.md (REQUIRED_SECTIONS, user-owned) is present and
    non-empty, and re-asks the agent naming exactly what is missing. Bounded so it
    never hangs; the agent may resume with accept_incomplete:true after the user
    knowingly accepts an incomplete spec. Returns (last spec_response, still-missing)."""
    instr = base_payload.get("instruction", "")
    reask = 0
    missing: list[str] = []
    while True:
        payload = dict(base_payload)
        payload["gate"] = "2b"
        payload["rules"] = _rules.load_gate_rules("2b")
        payload["rules_path"] = _rules.gate_rules_path("2b")
        if missing:
            payload["missing_sections"] = missing
            payload["instruction"] = (
                "YOUR PREVIOUS SPEC WAS INCOMPLETE. Missing required sections: "
                + ", ".join(missing) + ". Regenerate a COMPLETE json_spec that includes every one "
                "- do not simplify or drop any. If the user has reviewed and knowingly accepts an "
                "incomplete spec, resume with accept_incomplete:true. " + instr
            )
        else:
            payload["instruction"] = instr
        resp = interrupt(payload)
        resp = resp if isinstance(resp, dict) else {}
        missing = _rules.missing_sections("2b", resp.get("json_spec"))
        accepted = str(resp.get("accept_incomplete", "")).strip().lower() in ("1", "true", "yes")
        if not missing or accepted:
            return resp, ([] if accepted else missing)
        reask += 1
        if reask > _SPEC_MAX_REASK:
            return resp, missing   # bounded - caller records spec_warnings, never silently hides




# -- Gate 3: submission config ----------------------------------------------

async def node_config_gate(state: TestingState) -> dict:
    """Gate 3 (local engine): present the RISK-BASED recommended verification layers + the target
    URL; the USER selects which layers run and confirms the URL. Recommendation only - human decides.
    """
    from icx_engine.verification import compute_risk_tier, recommend_layers
    analysis = {"problem_summary": state.get("context") or ""}
    tier = compute_risk_tier(analysis)
    recommended = recommend_layers(tier)
    cur_url = state.get("url")

    response = interrupt({
        "gate": 3,
        "message": (
            "Confirm the verification configuration.\n\n"
            f"RECOMMENDED verification layers (risk tier: {tier}):\n"
            f"  {', '.join(recommended)}\n"
            "  This is a recommendation - choose which layers to run (reply with a subset or accept).\n\n"
            f"TARGET URL: {cur_url or 'NOT SET - required for api and ui/agent types'}\n"
            "  Confirm this URL or provide a new one."
        ),
        "risk_tier": tier,
        "recommended_layers": recommended,
        "current": {"url": cur_url},
    })

    test_type = state.get("test_type") or "unit"
    layers = response.get("layers") if isinstance(response, dict) else None
    selected = [str(l) for l in layers] if isinstance(layers, list) and layers else recommended
    final_url = (response.get("url") if isinstance(response, dict) else None) or state.get("url")

    if test_type in ("ui", "agent") and not final_url:
        raise ValueError(
            f"test_type '{test_type}' requires a URL. Provide url in your config response."
        )

    update: dict[str, Any] = {
        "selected_layers": selected,
        "risk_tier": tier,
        "status": "running",
    }
    if final_url:
        update["url"] = final_url
    for k in ("api_endpoint", "api_method", "api_payload", "api_payload_type", "api_headers"):
        if isinstance(response, dict) and k in response:
            update[k] = response[k]
    return update


# -- auth gate helpers ------------------------------------------------------

def _resolve_project_name(file_paths: list[str]) -> str | None:
    # Human-readable project name, used only for display (e.g. profile markdown).
    try:
        from icx_engine.graph import storage
    except Exception:
        return None
    for p in file_paths:
        try:
            info = storage.lookup_for_file(Path(p))
            if info is not None:
                return info.name
        except Exception:
            pass
    return None


def _resolve_project_id(file_paths: list[str]) -> str | None:
    # Stable, unique key for the auth store. Prefer the graph project_id (a hash
    # of the resolved project path, collision-proof); fall back to a hash of the
    # project root so the key is never ambiguous across different projects.
    try:
        from icx_engine.graph import storage
        for p in file_paths:
            try:
                info = storage.lookup_for_file(Path(p))
                if info is not None:
                    return info.project_id
            except Exception:
                pass
    except Exception:
        pass
    root = _project_root(file_paths)
    if root is not None:
        import hashlib
        return "path:" + hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:12]
    return None


async def node_auth_gate(state: TestingState) -> dict:
    if state.get("test_type") == "api":
        return {}

    url = state.get("url") or ""
    host = _auth.host_of(url)
    project = state.get("project") or _resolve_project_id(state["file_paths"])
    existing = _auth.load_session(project, host) if project and host else None

    response = interrupt({
        "gate": "auth_gate",
        "test_type": state.get("test_type"),
        "host": host,
        "has_valid_session": existing is not None,
        "session_expires_at": existing.expires_at if existing else None,
        "message": (
            "Choose how to authenticate this run.\n"
            "  public  - no login; Magik will not attempt auth (fixes wandering).\n"
            "  capture - log in once in a visible browser, reuse the session.\n"
            "  reuse   - reuse the stored session for this project + host.\n"
            "  inline  - provide credentials; ICX logs in and stores the session."
        ),
        "options": ["public", "capture", "reuse", "inline"],
        "instruction": (
            "For capture/inline, perform the login via the magik_login_* MCP tools first, "
            "then resume with {\"auth_mode\": \"capture\"|\"inline\", \"session_id\": \"<id>\"}. "
            "For reuse, resume with {\"auth_mode\": \"reuse\"}. "
            "For public, resume with {\"auth_mode\": \"public\"}."
        ),
    })

    mode = str(response.get("auth_mode", "public")).strip().lower()
    if mode not in ("public", "capture", "reuse", "inline"):
        mode = "public"

    update: dict[str, Any] = {
        "auth_mode": mode,
        "host": host,
        "project": project,
        "auth_ref": f"{project}::{host}",
        "auto_auth_recover": mode != "public",
    }

    if mode in ("capture", "inline"):
        sid = response.get("session_id")
        if sid and project and host:
            _auth.save_session(project, host, str(sid))
    elif mode == "reuse" and existing is None:
        update["auth_mode"] = "public"
        update["auto_auth_recover"] = False
    return update


# -- node_profile_push: profile_push gate (user yes/no) then profile_gen ------
# First interrupt: user decides whether to push a profile.
# On yes: fetch the profile-creation prompt from Magik, then second interrupt
# (AGENT-GENERATE) so the agent reads the source files and returns the markdown.
# If the agent returns no markdown, fall back to the ICX heuristic generator.

def _read_profile_file(path_str: str) -> str | None:
    # Read a user-provided Project Profile markdown file. Returns the text, or
    # None if it is missing/unreadable/empty.
    try:
        p = Path(path_str)
        if not p.is_file():
            return None
        text = p.read_text(encoding="utf-8", errors="ignore")
        return text if text.strip() else None
    except OSError:
        return None




# -- node_submit ------------------------------------------------------------

def _local_repo_root(state: TestingState) -> str:
    """Derive the repo root for local verification from the confirmed/seed file paths."""
    import os
    fps = state.get("file_paths") or []
    if not fps:
        return os.getcwd()
    dirs = [os.path.dirname(p) for p in fps if p]
    try:
        return os.path.commonpath(dirs) if len(dirs) > 1 else (dirs[0] or os.getcwd())
    except ValueError:
        return dirs[0] or os.getcwd()




def _flow_key(state: TestingState) -> str:
    """Stable cache key for a session's authored UI flow: project id if known, else the first
    seed file path (normalized). Both node_author_flow and node_local_run use this."""
    proj = state.get("project")
    if proj:
        return str(proj)
    fps = state.get("file_paths") or []
    return (fps[0] if fps else "session").replace("\\", "/")


def route_after_auth(state: TestingState) -> str:
    """UI/agent flows author a Stagehand flow before running; unit/api run directly."""
    return "author_flow" if state.get("test_type") in ("ui", "agent") else "local_run"


async def node_author_flow(state: TestingState) -> dict:
    """AGENT-GENERATE gate: the agent authors the UI test flow (goto/fill/click/assert steps,
    including login), which ICX caches for deterministic replay. Equivalent to the old spec-gen
    gate, but for the local Stagehand engine. UI/agent only."""
    from icx_engine.testing.runners.ui import UiFlow, UiStep, save_flow
    response = interrupt({
        "gate": "author_flow",
        "message": (
            "Author the UI test flow for this screen. Read the screen source fully, then produce the "
            "ordered steps a real user takes - including any login steps. Each step: "
            "{action: goto|fill|click|assert, target: <selector or url>, value?: <text/expected>, "
            "description?: <intent>}. This is cached and replayed DETERMINISTICALLY (no LLM on rerun)."
        ),
        "url": state.get("url"),
        "file_paths": state.get("file_paths"),
    })
    steps_raw = response.get("steps") if isinstance(response, dict) else None
    steps = [
        UiStep(action=str(s.get("action", "")), target=str(s.get("target", "")),
               value=str(s.get("value", "")), description=str(s.get("description", "")))
        for s in (steps_raw or []) if isinstance(s, dict)
    ]
    flow = UiFlow(name=str(state.get("test_type") or "ui"), url=state.get("url") or "",
                  authored=bool(steps), steps=steps)
    save_flow(_flow_key(state), flow)
    return {"read_receipts": _record_receipts(state, "author_flow", response if isinstance(response, dict) else {})}


# lang aliases: runner.lang -> Runtime Manager language key
_RUNTIME_LANG_ALIAS = {"js-ts": "node", "javascript": "node", "typescript": "node"}


async def _runtime_resolver(repo: str):
    """Return an async resolver(lang)->path that uses the Runtime Manager to pick the repo-correct
    runtime, non-blocking. Falls back to None (runner uses PATH) when not resolvable."""
    async def _resolve(lang: str):
        from icx_engine.runtime_manager import resolve_runtime
        key = _RUNTIME_LANG_ALIAS.get(lang, lang)
        try:
            res = await asyncio.to_thread(resolve_runtime, key, repo)
            return res.path if getattr(res, "status", "") == "resolved" else None
        except Exception:
            return None
    return _resolve


async def node_local_run(state: TestingState) -> dict:
    """Run the local (in-process, async) verification suite and feed the result to the review gate.

    Executes engine='local'. Fully async; never blocks the event loop. Uses the Runtime Manager to
    run each layer on the repo-correct runtime. Maps a failed suite to a single issue so the review
    gate handles it uniformly.
    """
    from icx_engine.testing.local_executor import run_local_verification
    test_type = state.get("test_type") or "unit"
    repo = _local_repo_root(state)
    ui_flow_path = None
    if test_type in ("ui", "agent"):
        from icx_engine.testing.runners.ui import flow_path
        ui_flow_path = flow_path(_flow_key(state))
    try:
        resolver = await _runtime_resolver(repo)
        res = await run_local_verification(
            repo, test_type, target_url=state.get("url"), runtime_resolver=resolver,
            ui_flow_path=ui_flow_path,
        )
    except Exception as exc:
        return {"status": "error", "last_error": f"local verification failed: {exc}",
                "run_id": "local", "issues": []}

    # DoD integration: derive a confidence report from the suite result so the agent can feed
    # record_verification / save_memory. One DoD item per runner (evidence = its command + summary).
    try:
        from icx_engine.verification import build_confidence_report
        tier = str(state.get("risk_tier") or "medium")
        layers = list(state.get("selected_layers") or [test_type])
        dod_items = [{
            "check": f"{rep.get('runner', 'runner')} verification",
            "method": test_type,
            "passed": bool(rep.get("ok")),
            "command": str(rep.get("runner", "")),
            "output": f"total={rep.get('total', 0)}",
        } for rep in res.get("reports", [])]
        res["confidence"] = build_confidence_report(dod_items, tier, layers)
        res["dod_items"] = dod_items
    except Exception:
        pass

    if res.get("ok"):
        return {"status": "parsed", "issues": [], "full_report": res, "run_id": "local"}
    issues = [{
        "name": "verification_failed",
        "description": res.get("reason", "local verification did not pass"),
        "severity": "high",
        "detail": res.get("summary", {}),
    }]
    return {"status": "parsed", "issues": issues, "full_report": res, "run_id": "local"}




# -- node_poll --------------------------------------------------------------



# -- node_error_gate --------------------------------------------------------



# -- node_parse_report ------------------------------------------------------



# kept for test compatibility - delegates to real parser




# -- node_review ------------------------------------------------------------

async def node_review(state: TestingState) -> dict:
    interrupt({
        "gate": 4,
        "message": (
            "Full test report below. Review the results and, only if a fix is warranted, "
            "propose ONE fix iteration. The user approves each iteration individually."
        ),
        "issues": state["issues"],
        "full_report": state.get("full_report"),
        "iteration": state["iteration"],
        "run_counters": state.get("run_counters", {}),
    })
    fix_response = interrupt({
        "gate": 5,
        "message": (
            "Approve this fix iteration? Resume with {\"approve_iteration\": true, "
            "\"fixes_applied\": [...]} to re-run, or {\"approve_iteration\": false} to stop."
        ),
    })
    approve = bool(fix_response.get("approve_iteration", True))
    fix_entry = {
        "iteration": state["iteration"],
        "issues_count": len(state["issues"]),
        "fixes_applied": fix_response.get("fixes_applied", []),
    }
    return {
        "fix_log": state["fix_log"] + [fix_entry],
        "iteration": state["iteration"] + 1,
        "approve_iteration": approve,
    }


# -- node_limit_gate --------------------------------------------------------

async def node_limit_gate(state: TestingState) -> dict:
    _LIMIT_MAP = {"1": "continue", "2": "end_session"}
    response = interrupt({
        "gate": "limit",
        "message": (
            f"Reached max iterations ({state['max_iterations']}). "
            f"{len(state['issues'])} issues still found. Continue or end?"
        ),
        "options": ["1. continue (+3 iterations)", "2. end_session"],
        "remaining_issues": state["issues"],
    })
    choice = _resolve_choice(response, "choice", _LIMIT_MAP)
    if choice == "continue":
        return {"max_iterations": state["max_iterations"] + 3, "status": "running"}
    return {"status": "cancelled"}


# -- node_mode_select -------------------------------------------------------

async def node_mode_select(state: TestingState) -> dict:
    # test_mode pre-set at session start via start_testing_session parameter - skip interrupt
    if state.get("test_mode") in ("automated", "manual"):
        return {}

    _MODE_MAP = {"1": "automated", "2": "manual"}
    response = interrupt({
        "gate": "mode",
        "message": (
            "Choose how to run this test.\n\n"
            "  1. automated - ICX submits to Magik-AI, polls for results,\n"
            "                 shows issues found, loops until clean, then asks\n"
            "                 you to verify the UI.\n"
            "  2. manual    - You run the test yourself. ICX waits, then asks\n"
            "                 you to report the result before saving the record."
        ),
        "options": ["1. automated", "2. manual"],
        "default": "1",
    })
    choice = _resolve_choice(response, "choice", _MODE_MAP)
    if choice not in ("automated", "manual"):
        choice = "automated"
    return {"test_mode": choice}


# -- node_manual_wait -------------------------------------------------------

async def node_manual_wait(state: TestingState) -> dict:
    interrupt({
        "gate": "manual",
        "message": (
            "Run the test manually against the application now.\n"
            "Files in scope:\n"
            + "\n".join(f"  - {f}" for f in state["file_paths"])
            + "\n\nCall resume_testing_session with {\"done\": true} when finished."
        ),
        "file_paths": state["file_paths"],
        "context": state.get("context"),
    })
    return {}


# -- node_manual_result -----------------------------------------------------

async def node_manual_result(state: TestingState) -> dict:
    _PASS_MAP = {"1": True, "2": False}
    response = interrupt({
        "gate": "manual_result",
        "message": (
            "Report your test result. Answer all questions.\n\n"
            "DID THE TEST PASS?\n"
            "  1. yes - all functionality works correctly.\n"
            "  2. no  - found issues (list them below).\n\n"
            "ISSUES FOUND (list each issue, or leave empty if passed):\n"
            "  Example: ['Login button not responding', 'Error message missing']\n\n"
            "NOTES (any additional observations, optional):"
        ),
        "options": {"passed": ["1. yes (passed)", "2. no (failed)"]},
    })
    passed_raw = _resolve_choice(response, "passed", _PASS_MAP)
    if isinstance(passed_raw, bool):
        passed = passed_raw
    else:
        passed = str(passed_raw).lower() not in ("false", "no", "0", "2")
    return {
        "manual_result": {
            "passed": passed,
            "issues": response.get("issues", []),
            "notes": response.get("notes", ""),
        },
        "status": "test_complete",
    }


# -- node_ui_check ----------------------------------------------------------

async def node_ui_check(state: TestingState) -> dict:
    interrupt({
        "gate": "ui_check",
        "message": (
            "Testing complete. Open the application and verify the UI.\n\n"
            "Check: layout, navigation, error states, and all functionality touched by the changed files.\n\n"
            "Files tested: " + ", ".join(state["file_paths"]) + "\n"
            f"Iterations run: {state.get('iteration', 0)}\n"
            f"Final issue count: {len(state.get('issues', []))}\n\n"
            "CONFIRM UI STATUS:\n"
            "  1. yes - UI looks correct, everything is working.\n"
            "  2. no  - Found visual issues (describe them in notes)."
        ),
        "options": ["1. yes (UI correct)", "2. no (found issues)"],
        "files_tested": state["file_paths"],
        "iterations_run": state.get("iteration", 0),
        "final_issue_count": len(state.get("issues", [])),
    })
    return {}


# -- node_memory_save -------------------------------------------------------

def _save_test_record(state: TestingState) -> None:
    import json as _json
    history_path = Path.home() / ".icx" / "testing_history.json"
    history_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        records: list[dict[str, Any]] = _json.loads(history_path.read_text(encoding="utf-8")) if history_path.exists() else []
    except Exception:
        records = []

    from datetime import datetime, timezone
    record: dict[str, Any] = {
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "test_mode": state.get("test_mode", "automated"),
        "file_paths": state["file_paths"],
        "context": state.get("context"),
        "iterations": state.get("iteration", 0),
        "final_issue_count": len(state.get("issues", [])),
        "issues": state.get("issues", []),
    }
    if state.get("manual_result"):
        record["manual_result"] = state["manual_result"]
    records.append(record)
    history_path.write_text(_json.dumps(records, indent=2), encoding="utf-8")


async def node_memory_save(state: TestingState) -> dict:
    issue_count = len(state.get("issues", []))
    result_line = "all tests passed" if issue_count == 0 else f"{issue_count} issue(s) remain"
    if state.get("manual_result"):
        mr = state["manual_result"]
        result_line = "passed" if mr.get("passed") else f"failed - {mr.get('notes', '')}"

    _SAVE_MAP = {"1": True, "2": False}
    response = interrupt({
        "gate": "memory_save",
        "message": (
            "Save this test session to ICX testing history?\n\n"
            "Summary:\n"
            f"  Files:      {', '.join(state['file_paths'])}\n"
            f"  Mode:       {state.get('test_mode', 'automated')}\n"
            f"  Result:     {result_line}\n"
            f"  Iterations: {state.get('iteration', 0)}\n\n"
            "SAVE TO HISTORY?\n"
            "  1. yes - save session record.\n"
            "  2. no  - discard, do not save."
        ),
        "options": ["1. yes (save)", "2. no (discard)"],
        "summary": {
            "files": state["file_paths"],
            "test_mode": state.get("test_mode", "automated"),
            "result": result_line,
            "iterations": state.get("iteration", 0),
        },
    })
    save_raw = _resolve_choice(response, "save", _SAVE_MAP)
    if isinstance(save_raw, bool):
        do_save = save_raw
    else:
        do_save = str(save_raw).lower() not in ("false", "no", "0", "2")
    if do_save:
        try:
            _save_test_record(state)
            saved = True
        except Exception as exc:
            _log.warning("testing history save failed: %s", exc)
            saved = False
    else:
        saved = False

    return {"status": "done", "_memory_saved": saved}


# -- route functions --------------------------------------------------------

def route_after_compat(state: TestingState) -> str:
    if state.get("status") == "compat_recheck":
        return "compat_scan"
    if state.get("status") == "compat_empty":
        return "ui_check"
    return "config_gate"


def route_after_mode_select(state: TestingState) -> str:
    if state.get("test_mode") == "manual":
        return "expand_files"
    return "pick_type"


def route_after_expand(state: TestingState) -> str:
    if state.get("test_mode") == "manual":
        return "manual_wait"
    return "compat_scan"


def route_after_check_issues(state: TestingState) -> str:
    if not state["issues"]:
        return "ui_check"
    if state.get("approve_iteration") is False:
        return "ui_check"
    if state["iteration"] >= state["max_iterations"]:
        return "limit_gate"
    return "loop"




