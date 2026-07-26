from __future__ import annotations
import asyncio
import logging
import sys
from pathlib import Path
from typing import Any

from langgraph.types import interrupt

from icx_engine.testing.state import TestingState
from icx_engine.testing.classify import classify_file
from icx_engine.testing.compat import build_report
from icx_engine.testing.handlers import get_handler
from icx_engine.testing.expand import expand_via_grep, union_rank
from icx_engine.testing import auth as _auth
from icx_engine.testing import apispec as _apispec
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
_URL_GATE_MAX_REASK = 3   # gate 3: re-ask a missing ui/agent URL this many times before erroring
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
    # The ONE place the test type is chosen. Gate 3 later only confirms the URL - it never re-asks
    # the type.
    _TYPE_MAP = {"1": "agent", "2": "api", "3": "unit"}
    response = interrupt({
        "gate": "pick_type",
        "message": (
            "Pick the test type (chosen ONCE - gate 3 will only confirm the URL, not ask this again).\n\n"
            "  1. agent - you (the connected agent) write a real Playwright test covering the screen's "
            "Element Census, run it yourself, and fix your own script until it passes (frontend, "
            "needs a URL).\n"
            "  2. api   - REST endpoint test (backend, needs a URL).\n"
            "  3. unit  - run the repo's unit tests (no URL, no running app)."
        ),
        "options": ["1. agent", "2. api", "3. unit"],
    })
    test_type = _resolve_choice(response, "test_type", _TYPE_MAP)
    if test_type not in ("agent", "api", "unit"):
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
    relevant = get_handler(mode).relevant_layers() if mode in ("agent", "api") else None

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
        "message": (
            "Confirm files for testing. Off-type files are excluded by default. Resume with "
            "{\"confirmed_files\": [<paths>]} listing EXACTLY the files you want tested for the "
            "rest of this session - anything you omit here is excluded for every later gate "
            "(analyze_screen, compat_scan, etc.), not just this one. If you resume without "
            "confirmed_files, ICX keeps the full selected_files list (nothing gets excluded)."
        ),
        "test_type": mode,
        "selected_files": selected,
        "excluded_off_type": off_type,
        "file_sources": file_sources,
        "graph_available": querier is not None,
    })
    final = confirmed.get("confirmed_files", selected)
    return {
        "file_paths": final,
        "all_candidate_files": selected,
        "file_sources": file_sources,
        "classified": [c for c in classified if c["path"] in set(final)],
        "url": confirmed.get("url") or state.get("url"),
        "read_receipts": _record_receipts(state, "expand_scan", scan),
    }


# -- node_known_screen_check: known-screen fast path (skip expand/census/compat) -----
#
# Safety bar (deliberately conservative - a wrong fast-path skip means a real change goes
# untested): fast_path is offered to the user ONLY when the cache is PROVABLY fresh - every
# cached confirmed file's content is byte-identical to what was cached, AND a cheap, deterministic
# re-discovery (ICX's own graph + grep - no agent call) over the original seeds finds no candidate
# file outside what was seen last time. Either check failing means no gate is even shown; the
# session falls straight through to the normal expand_files pipeline, exactly like a cache miss.

def _deterministic_candidates(seeds: list[str]) -> set[str]:
    """The same graph+grep candidate set node_expand_files falls back to - ICX-local, no agent call,
    so it can run silently as a staleness probe before ever asking the user anything."""
    querier = _load_querier(seeds)
    graph_expanded = _expand_files_via_graph(seeds, querier)
    root = _project_root(seeds)
    grepped: list[str] = []
    if root is not None:
        try:
            grepped = expand_via_grep(seeds, root)
        except Exception as exc:
            _log.warning("known_screen_check: grep expand failed: %s", exc)
    ranked = union_rank(seeds, graph_expanded, grepped)
    return {p for p, _src in ranked}


async def node_known_screen_check(state: TestingState) -> dict:
    """USER-DECISION gate: before the expand -> census -> compat pipeline runs, check whether this
    exact screen (project + original seed files) was already cleared in a prior session. A cache hit
    that is NOT provably fresh (see module docstring) is treated exactly like a cache miss - falls
    through silently, no gate, no choice, no override. Only a provably fresh hit is ever offered to
    the user as an optional fast path."""
    from icx_engine.testing import screen_cache as _cache

    seeds = state.get("original_seeds") or state.get("file_paths") or []
    project = _resolve_project_id(seeds)
    if project is None:
        return {"known_screen_available": False}

    entry = _cache.load_screen(project, seeds)
    if entry is None or entry.test_type != (state.get("test_type") or ""):
        return {"known_screen_available": False}

    hash_fresh, changed = _cache.freshness(entry)
    if not hash_fresh:
        return {"known_screen_available": False}
    try:
        discovered_now = _deterministic_candidates(seeds)
    except Exception as exc:
        _log.warning("known_screen_check: re-discovery failed, treating as stale: %s", exc)
        return {"known_screen_available": False}
    new_files = sorted(discovered_now - set(entry.all_candidates))
    if new_files:
        return {"known_screen_available": False}

    n_funcs = len((entry.screen_model or {}).get("functionalities") or [])
    response = interrupt({
        "gate": "known_screen",
        "cached_at": entry.cached_at,
        "confirmed_files": entry.confirmed_files,
        "functionality_count": n_funcs,
        "census_coverage": entry.census_coverage,
        "message": (
            f"This exact screen was cleared before, on {entry.cached_at} "
            f"({len(entry.confirmed_files)} files, {n_funcs} functionalities, "
            f"coverage {entry.census_coverage:.0%}). Every cached file is byte-identical to then, "
            "and a fresh check found no new related file - safe to reuse. "
            "Reply {\"decision\": \"fast_path\"} to reuse the cached scope/census/compat clearance "
            "and skip straight to URL/layer confirmation, or {\"decision\": \"rescan\"} to redo file "
            "discovery, census, and compat scan from scratch anyway."
        ),
    })
    decision = str((response or {}).get("decision", "")).strip().lower() if isinstance(response, dict) else ""
    if decision != "fast_path":
        return {"known_screen_available": False}

    out: dict[str, Any] = {
        "known_screen_available": True,
        "file_paths": list(entry.confirmed_files),
        "all_candidate_files": list(entry.all_candidates),
        "screen_model": entry.screen_model,
        "census_coverage": entry.census_coverage,
        "analyzer_id": entry.analyzer_id,
        "analyzer_family": entry.analyzer_family,
        "compat_resolution": dict(entry.compat_resolution),
    }
    if entry.url and not state.get("url"):
        out["url"] = entry.url
    return out


def route_after_known_screen_check(state: TestingState) -> str:
    return "config_gate" if state.get("known_screen_available") else "expand_files"


# -- node_analyze_screen: per-framework Element Census (zero-miss backbone) ----

_CENSUS_MAX_REASK = 2   # re-ask the agent this many times when reconciliation counts do not add up


async def node_analyze_screen(state: TestingState) -> dict:
    """Run the framework-specific Element Census so authoring misses NOTHING.

    Selects the analyzer prompt for the detected framework, injects it + the confirmed files, and
    the agent returns the census/functionality model (strict JSON). ICX then runs the reconciliation
    gate (counts must add up) and re-asks (bounded) if the census was cut short. The resulting
    `screen_model` feeds `author_flow`, which converts it into comprehensive ordered steps.

    Fully guarded: no analyzer match, an unparseable model, or any error degrades cleanly to the
    existing free-authoring behavior - this node can never break a session.
    """
    from icx_engine.testing.analyzers import select_analyzer, prompt_text
    from icx_engine.testing.analyzers.schema import validate_census

    try:
        spec = select_analyzer(file_paths=state.get("file_paths") or [])
    except Exception:
        spec = None
    if spec is None:
        return {}   # unknown framework -> skip census, author freely

    try:
        prompt = prompt_text(spec)
    except Exception:
        prompt = ""
    if not prompt.strip():
        return {}

    model: dict | None = None
    coverage = 0.0
    last_receipt: dict = {}
    attempt = 0
    reask_note = ""
    census_warnings: list[str] = []
    while attempt <= _CENSUS_MAX_REASK:
        attempt += 1
        payload = {
            "gate": "analyze_screen",
            "analyzer_id": spec.id,
            "analyzer_family": spec.family,
            "file_paths": state.get("file_paths"),
            "message": (
                f"ELEMENT CENSUS ({spec.label}). Apply the analyzer prompt to the listed files and "
                f"return its STRICT JSON census as {{\"screen_model\": {{...}}}}. The census is the ONLY "
                f"input to test generation - if it is incomplete, the tests are incomplete. ICX LINTS "
                f"it structurally and RE-ASKS until it is complete; completeness is MANDATORY, not "
                f"best-effort. Before returning, CONFIRM every item:\n"
                f"  [ ] EVERY functionality on the screen (list, search, sort, pagination, refresh, "
                f"create, view, edit, delete, clone, activate, approve, DOWNLOAD/EXPORT, bulk, ...).\n"
                f"  [ ] EVERY field on each create/edit form, each with its domSelectors.\n"
                f"  [ ] Each field's real length/format read FROM THE CODE (maxLength/minLength/min/max/"
                f"pattern; type email/tel/url/number) - the save uses these.\n"
                f"  [ ] CREATE and EDIT/MODIFY submit buttons captured SEPARATELY (they differ - e.g. "
                f"Save vs Update); never copy one onto the other.\n"
                f"  [ ] If a create/edit form is a MULTI-STEP WIZARD (tabs / NEXT navigation), model it "
                f"as a `steps` array - each step with its fields + nextButton, last step submits. Do "
                f"NOT flatten a wizard into one field list; it will not run.\n"
                f"  [ ] DOWNLOAD/EXPORT controls captured as their own functionality (trigger + "
                f"type Download).\n"
                f"  [ ] Any app-level confirm popup (NO/YES / OK dialog) - note its confirm-button "
                f"selector.\n"
                f"A miss on ANY of these is a missed or broken test. Read every file FULLY first."
                + (f"\n\nPREVIOUS ATTEMPT REJECTED - FIX: {reask_note}" if reask_note else "")
                + _REREAD_MANDATE
            ),
        }
        # Send the (large) prompt text only on the FIRST attempt - the agent already has it on
        # re-ask, and re-embedding 15-33KB in every reask bloats the durable checkpoint.
        if attempt == 1:
            payload["analyzer_prompt"] = prompt
        resp = interrupt(payload)
        last_receipt = resp if isinstance(resp, dict) else {}
        raw = last_receipt.get("screen_model") if isinstance(last_receipt, dict) else None
        report = validate_census(spec.family, raw)
        # STRUCTURAL LINT (UI only): reconciliation checks counts add up; the lint checks the census is
        # actually BUILDABLE + correct - create/edit not sharing a submit, every mutating form has its
        # own submit + trigger, every field has a selector. This enforces census quality independent of
        # which agent produced it, so no agent's mistake (wrong edit-submit, missing selector) slips
        # through. Hard defects re-ask; soft warnings are recorded and proceed.
        lint = None
        if spec.family == "ui":
            from icx_engine.testing.analyzers.census_lint import lint_ui_census
            lint = lint_ui_census(raw if isinstance(raw, dict) else {})
        if report.ok and (lint is None or lint.ok):
            model = raw if isinstance(raw, dict) else None
            coverage = report.coverage_score
            census_warnings = list(lint.soft) if lint else []
            break
        notes = list(report.errors[:6]) if not report.ok else []
        if lint and lint.hard:
            notes = (lint.hard[:6] + notes)[:6]
        reask_note = "; ".join(notes) or "census did not reconcile"
        coverage = report.coverage_score
        model = raw if isinstance(raw, dict) else model  # keep best-effort even if not perfect
        census_warnings = list(lint.soft) if lint else []

    return {
        "analyzer_id": spec.id,
        "analyzer_family": spec.family,
        "screen_model": model,
        "census_coverage": coverage,
        "census_warnings": census_warnings,
        "read_receipts": _record_receipts(state, "analyze_screen", last_receipt),
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
    " FORBIDDEN - shallow undefined-identifier checks: before flagging ANY identifier as undefined/"
    "missing, you must check the WHOLE repo for its definition, not just this file's own imports/"
    "destructures - grep for it, and check index.html (and any public/ or static/ HTML) for a classic "
    "<script src=...> tag that defines it as a global (a legitimate pattern needing no import). Flagging "
    "a repo-wide-defined global as undefined because you only looked at one file's imports is the exact "
    "shallow-inspection failure this mandate exists to prevent - hold your own undefined-checks to the "
    "same rigor you are required to hold the rest of this scan to."
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
            "DETECTION MODE (how the UI layer generates the test spec):\n"
            "  1. auto_detect - the UI layer opens the URL live with Playwright, scans page fields.\n"
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


# -- gate 2b: rulebook-injected JSON spec generation -----------------------

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

def _save_known_screen(state: TestingState) -> None:
    """Refresh the known-screen cache whenever a session reaches config_gate with a settled census -
    on a full rescan this writes the new clearance; on a fast-path reuse it just bumps cached_at.
    Agent-type only (the cache models a UI screen's census/compat clearance); best-effort, never
    raises - a cache-write failure must never affect the run."""
    if state.get("test_type") != "agent" or not state.get("screen_model"):
        return
    try:
        from icx_engine.testing import screen_cache as _cache
        seeds = state.get("original_seeds") or state.get("file_paths") or []
        project = _resolve_project_id(seeds)
        if project is None:
            return
        _cache.save_screen(
            project, seeds,
            test_type="agent",
            url=state.get("url"),
            all_candidates=state.get("all_candidate_files") or state.get("file_paths") or [],
            confirmed_files=state.get("file_paths") or [],
            screen_model=state.get("screen_model"),
            census_coverage=state.get("census_coverage") or 0.0,
            analyzer_id=state.get("analyzer_id"),
            analyzer_family=state.get("analyzer_family"),
            compat_resolution=state.get("compat_resolution") or {},
        )
    except Exception as exc:
        _log.warning("known_screen_check: cache save failed: %s", exc)


async def node_config_gate(state: TestingState) -> dict:
    """Gate 3 (local engine): CONFIRM the URL and lock in the layer to run. The layer is ANCHORED on
    the test type already picked at pick_type (no re-asking the type). Risk-tier extras are offered as
    OPTIONAL add-ons only; the default is exactly the picked type.
    """
    _save_known_screen(state)
    from icx_engine.verification import compute_risk_tier, recommend_layers
    test_type = state.get("test_type") or "unit"
    analysis = {"problem_summary": state.get("context") or ""}
    tier = compute_risk_tier(analysis)
    # Anchor: the picked type IS the layer. Extras are only suggestions, never the default.
    recommended = [test_type]
    optional_extra = [l for l in recommend_layers(tier) if l != test_type]
    cur_url = state.get("url")
    needs_url = test_type in ("agent", "api")

    extra_line = (f"  Optional extra layers you could add: {', '.join(optional_extra)}\n"
                  if optional_extra else "")
    url_line = (f"TARGET URL: {cur_url or 'NOT SET - required for this type'}\n  Confirm this URL or provide a new one.\n"
                if needs_url else "TARGET URL: not needed for unit tests.\n")
    is_browser = test_type == "agent"
    visible_line = ("BROWSER: headless (hidden, faster) by default. Reply visible:true to WATCH the "
                    "test drive a real browser.\n"
                    "SLOWMO: when visible, each step is slowed + paused so you can follow it. Reply "
                    "slowmo:<ms> to set the pace (default 1000 = 1s when visible; 0 when headless). "
                    "Ask the user what slowmo they want before accepting.\n" if is_browser else "")

    response = interrupt({
        "gate": 3,
        "message": (
            f"You chose the '{test_type}' test - that layer will run. This gate only confirms the URL "
            f"(it does NOT re-ask the type).\n\n"
            f"{extra_line}"
            f"{url_line}"
            f"{visible_line}"
            "Reply 'accept' to run just your chosen type, or list layers to override."
        ),
        "test_type": test_type,
        "risk_tier": tier,
        "recommended_layers": recommended,
        "optional_layers": optional_extra,
        "current": {"url": cur_url, "visible": not state.get("headless", True),
                    "slowmo_default_when_visible": 1000,
                    "test_writes": bool(state.get("test_writes", True))},
    })

    layers = response.get("layers") if isinstance(response, dict) else None
    selected = [str(l) for l in layers] if isinstance(layers, list) and layers else recommended
    final_url = (response.get("url") if isinstance(response, dict) else None) or state.get("url")

    # agent needs a URL. If none was supplied, RE-ASK (a fresh interrupt) rather than raising -
    # raising would escape the unguarded graph.ainvoke in the resume handler and, because the
    # gate-3 resume value is already consumed, every retry would replay the url-less value and
    # re-raise, permanently stranding the session. A fresh interrupt lets the next resume deliver
    # the URL. Bounded so a client that never supplies one cannot loop forever.
    reask = 0
    while test_type == "agent" and not final_url and reask < _URL_GATE_MAX_REASK:
        reask += 1
        r2 = interrupt({
            "gate": 3,
            "needs_url": True,
            "test_type": test_type,
            "message": (f"A TARGET URL is REQUIRED for a '{test_type}' test and none is set. "
                        f"Reply with the URL to test, e.g. {{\"url\": \"http://...\"}}."),
        })
        if isinstance(r2, dict) and r2.get("url"):
            final_url = r2["url"]
    if test_type == "agent" and not final_url:
        raise ValueError(
            f"test_type '{test_type}' requires a URL but none was provided after {reask} attempts."
        )

    update: dict[str, Any] = {
        "selected_layers": selected,
        "risk_tier": tier,
        "status": "running",
    }
    # Visible browser option (agent only): visible:true -> the agent launches its own browser headed.
    resp = response if isinstance(response, dict) else {}
    if "visible" in resp:
        headless = not bool(resp.get("visible"))
    elif "headless" in resp:
        headless = bool(resp.get("headless"))
    else:
        headless = bool(state.get("headless", True))
    update["headless"] = headless
    # slowmo (agent only): 0 when headless; when visible, the user-chosen ms or 1s default so a
    # human can follow each step. Ignored for api/unit.
    if is_browser:
        if headless:
            slowmo = 0
        else:
            raw = resp.get("slowmo")
            try:
                slowmo = max(0, int(raw)) if raw is not None else 1000
            except (TypeError, ValueError):
                slowmo = 1000
        update["slowmo"] = slowmo
    # test_writes override (agent/api): the user can turn real Create/Update/Delete writes off at
    # this gate for a read-only environment, without restarting the session.
    if "test_writes" in resp:
        update["test_writes"] = bool(resp.get("test_writes"))
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

    # Dev-server port drift (Vite/CRA/webpack-dev-server auto-increments on a taken port): the
    # EXACT host has no valid session, but this same project has one at a different port on the
    # same hostname. Surface it explicitly rather than silently falling back to public - the user
    # decides, ICX never auto-matches a session across hosts on its own.
    other_sessions: list[dict[str, str]] = []
    if project and not _valid_stored_session(existing):
        hostname = _auth.hostname_of(url)
        for h, rec in (_auth.list_sessions_for_project(project) if project else []):
            if h == host or not _valid_stored_session(rec):
                continue
            if hostname and h.split(":")[0] == hostname:
                other_sessions.append({"host": h, "captured_at": rec.captured_at,
                                       "expires_at": rec.expires_at})

    other_line = ""
    if other_sessions:
        listing = "; ".join(f"{o['host']} (captured {o['captured_at']})" for o in other_sessions)
        other_line = (
            f"\n  NOTE: no session for {host}, but this project has one at a different port on the "
            f"same host: {listing}. This is likely just a dev-server port change (same app). Reply "
            f"{{\"auth_mode\": \"reuse\", \"reuse_host\": \"<one of the hosts above>\"}} to reuse it - "
            "cookie-based auth transfers across a port change, but localStorage/sessionStorage-based "
            "auth (common in SPAs) is ORIGIN-SCOPED INCLUDING PORT and will NOT restore correctly; if "
            "the app still looks logged out after reuse, capture fresh instead."
        )

    response = interrupt({
        "gate": "auth_gate",
        "test_type": state.get("test_type"),
        "host": host,
        "has_valid_session": existing is not None,
        "session_expires_at": existing.expires_at if existing else None,
        "other_host_sessions": other_sessions,
        "message": (
            "Choose how to authenticate this run.\n"
            "  public  - no login; the run does not attempt auth.\n"
            "  capture - ICX opens a REAL browser; the user logs in BY HAND; ICX saves the session.\n"
            "            (Do NOT ask the user for username/password in chat for this.)\n"
            "  reuse   - reuse the stored session for this project + host.\n"
            "  inline  - the user provides the APP credentials; ICX drives the login form + saves it."
            + other_line
        ),
        "options": ["public", "capture", "reuse", "inline"],
        "instruction": (
            "For capture: call the ui_auth_capture tool (url + file_paths) - it opens a browser for "
            "manual login and saves the session. For inline: call ui_auth_inline (url + file_paths + "
            "username + password) - only inline collects credentials, and they go to ICX's browser "
            "process, never into chat history. After either tool returns ok, resume with "
            "{\"auth_mode\": \"capture\"|\"inline\"}. For reuse, resume with {\"auth_mode\": \"reuse\"}, "
            "optionally with {\"reuse_host\": \"<host>\"} to reuse a session from gate.other_host_sessions "
            "instead of the current host. For public, resume with {\"auth_mode\": \"public\"}."
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
    elif mode == "reuse":
        reuse_host = str(response.get("reuse_host") or "").strip()
        if reuse_host and reuse_host != host and project:
            other_rec = _auth.load_session(project, reuse_host)
            if _valid_stored_session(other_rec):
                # Alias the other host's session under the CURRENT host key - _session_storage
                # re-derives host from state["url"] at use time, so this is what makes the reused
                # storageState actually get picked up for THIS run's target URL.
                _auth.save_session(project, host, other_rec.session_id,
                                   storage_state=other_rec.storage_state)
            else:
                update["auth_mode"] = "public"
                update["auto_auth_recover"] = False
        elif not _valid_stored_session(existing):
            # `existing` can be a non-None RECORD (TTL not expired) whose storage_state file is
            # missing, empty, or corrupt - reuse must not silently proceed unauthenticated against
            # that record. Same fallback as the never-had-a-session case: public, and drop the
            # auto-recover flag so a later step never claims "session restored" when nothing usable
            # was found.
            update["auth_mode"] = "public"
            update["auto_auth_recover"] = False
    return update


def _valid_stored_session(rec) -> bool:
    """A stored auth record is usable only if its storage_state file exists and parses as a
    Playwright storage state with actual cookies or localStorage origins - not just present."""
    if rec is None or not rec.storage_state:
        return False
    p = Path(rec.storage_state)
    if not p.exists():
        return False
    try:
        import json
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and bool(data.get("cookies") or data.get("origins"))


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


def _session_storage(state: TestingState) -> str | None:
    """Path to the captured/inline authenticated session (Playwright storageState) for this URL, or
    None. Shared by discovery, verify/heal, and the scored replay so all three run logged-in."""
    try:
        host = _auth.host_of(state.get("url") or "")
        project = state.get("project")
        rec = _auth.load_session(project, host) if project and host else None
        if rec and rec.storage_state and Path(rec.storage_state).exists():
            return rec.storage_state
    except Exception:
        pass
    return None


async def _combined_census(state: TestingState, source_model: dict) -> dict:
    """The ONE census path for UI/agent: fuse the runtime-DISCOVERED census (live DOM crawl) with the
    source census the agent produced. Discovery supplies real selectors, real control kinds, and the
    real wizard-step structure (it can never name a selector that does not exist); source supplies the
    JS-hidden constraints (maxLength/regex/format) and any fields the crawler could not read. Merged
    they beat either alone - this is why COMBINED is the only method, not a user-selectable mode.

    Degrades to the source census ONLY when the live app/session is physically unavailable (tooling
    absent, app down, empty crawl) - a fallback, never a choice. Never raises."""
    try:
        from icx_engine.testing.local_executor import run_ui_discovery
        from icx_engine.testing.analyzers.census_merge import merge_census
        repo = _local_repo_root(state)
        resolver = await _runtime_resolver(repo)
        discovered = await run_ui_discovery(
            repo, state.get("url") or "", storage_state=_session_storage(state),
            runtime_resolver=resolver, ui_headed=not state.get("headless", True))
        if isinstance(discovered, dict) and discovered.get("functionalities"):
            merged = merge_census(discovered, source_model)
            if isinstance(merged, dict) and merged.get("functionalities"):
                return merged
    except Exception:
        pass
    return source_model


def route_after_auth(state: TestingState) -> str:
    """agent authors its own Playwright test; unit WITH a census authors tests first; api/plain-unit run."""
    tt = state.get("test_type")
    if tt == "agent":
        return "author_flow"
    if tt == "unit" and isinstance(state.get("screen_model"), dict) and state.get("screen_model"):
        return "unit_author"
    return "local_run"


# per-language test-authoring guidance for the unit_author gate, keyed to the census analyzer family.
_UNIT_FRAMEWORK_HINT = {
    "cpp":  "C/C++: GoogleTest (TEST/TEST_F) or Catch2 (TEST_CASE), registered via CMake add_test so ctest runs them.",
    "sql":  "SQL routines: utPLSQL (--%test), tSQLt (test schema procs), or pgTAP - matching your ICX_SQL_TEST_CMD framework.",
    "grpc": "gRPC: a client that calls each rpc and asserts the response/status, runnable via your ICX_GRPC_TEST_CMD.",
    "iac":  "IaC: policy/validation assertions (checkov custom checks / tflint rules / terraform validate) per your ICX_IAC_TEST_CMD.",
    "backend": "the repo's unit framework (pytest / JUnit / jest / go test / cargo test / rspec / phpunit) - one test per unit/handler.",
    "ui":   "the repo's unit framework for these components.",
}


async def node_unit_author(state: TestingState) -> dict:
    """UNIT test authoring from the Element Census: instruct the agent to WRITE comprehensive tests
    covering EVERY unit/routine/function the census enumerated, using its own editor, so the language
    runner (pytest/ctest/utPLSQL/...) discovers and executes them on the next step. This is what makes
    the unit family comprehensive - without it the census would be informational only. Guarded: no
    census -> no-op (plain run of whatever tests already exist)."""
    model = state.get("screen_model")
    if not (isinstance(model, dict) and model):
        return {}
    fam = str(state.get("analyzer_family") or "backend")
    hint = _UNIT_FRAMEWORK_HINT.get(fam, _UNIT_FRAMEWORK_HINT["backend"])
    resp = interrupt({
        "gate": "unit_author",
        "analyzer_family": fam,
        "analyzer_id": state.get("analyzer_id"),
        "screen_model": model,
        "message": (
            "WRITE COMPREHENSIVE UNIT TESTS from the Element Census in gate.screen_model. Cover EVERY "
            "testable unit / routine / function / endpoint it lists - happy path AND edge/invalid/error "
            "cases and every validation. Use YOUR EDITOR to create the test files IN THE REPO so the "
            f"runner discovers them. Framework: {hint} Do not skip any censused unit - a missed unit is "
            "a missed test. When the files are written, confirm to proceed to the run."
        ),
    })
    return {"read_receipts": _record_receipts(state, "unit_author", resp if isinstance(resp, dict) else {})}


async def node_author_flow(state: TestingState) -> dict:
    """AGENT-GENERATE gate: the connected agent writes a REAL Playwright test file and runs it
    itself (its own Bash tool, against ICX's pinned Playwright install), reading Playwright's own
    failures and fixing its own script until the checklist rulebook is covered. ICX provides the
    census (what to cover, with live-DOM-verified selectors) and the pinned tool paths; ICX does not
    generate, execute, or interpret any test content itself - see rules_defaults/author_flow.md for
    the checklist the agent follows. The census is a floor, not a ceiling: the agent may test and
    report functionality it discovers by reading source that the census never listed (`discovered`).
    The agent's own self-fix loop is bounded by `max_iterations` (communicated in the gate message,
    not separately enforced by ICX). agent-type only."""
    auth_mode = str(state.get("auth_mode") or "public")
    if auth_mode in ("capture", "inline", "reuse"):
        # A session was captured/reused: ICX restores it (cookies + localStorage + sessionStorage)
        # BEFORE navigation, so the app boots already logged in. Do NOT re-author login steps.
        login_line = (
            "A saved login session WILL be restored automatically before the test runs (storage_state "
            "path in gate.storage_state), so do NOT author any login steps in your Playwright test - "
            "load that storage state into your browser context and go straight to the target URL."
        )
    else:
        login_line = (
            "This app has NO saved session - author real login steps for THIS app first (read the "
            "actual login form, do not assume a 2-field layout), then proceed."
        )
    model = state.get("screen_model")
    if isinstance(model, dict) and model and state.get("url"):
        # COMBINED CENSUS: fuse the live-DOM crawl with the agent's source census. Discovery supplies
        # real selectors/control kinds/wizard structure (it can never name a selector that does not
        # exist); source supplies JS-hidden constraints (maxLength/regex/format) the crawler cannot
        # read. Degrades to the source census only when the live app/session is unavailable.
        model = await _combined_census(state, model)

    writes_line = (
        "DATA WRITES ARE ENABLED: actually submit Create/Update and perform Delete against the live "
        "app. Use clearly-tagged, GENERIC test data (e.g. a 'Test'/'QA' prefix + a run-unique "
        "timestamp token) so created records are identifiable, and clean up what you created. NEVER "
        "embed a tool/vendor name (e.g. 'ICX') in any data value - it is internal tooling, not app data."
        if state.get("test_writes", True) else
        "DATA WRITES ARE DISABLED: exercise the forms (open/fill/reset/validate/cancel) and open "
        "view/edit, but do NOT click the final Save/Update or confirm a Delete."
    )
    max_iter = int(state.get("max_iterations") or 3)
    iteration_cap_line = (
        f"SELF-FIX BUDGET: you get at most {max_iter} write/run/read-failure/fix rounds in this pass. "
        "If the checklist is still not covered after that many rounds, STOP fixing - resume with "
        "whatever report/coverage you have and list what's left as findings. Do not loop indefinitely."
    )

    repo = _local_repo_root(state)
    storage_state = _session_storage(state)
    report_path = str(Path(repo) / ".icx-agent-junit.xml")
    playwright_env = _playwright_env()
    headless = bool(state.get("headless", True))
    slowmo = int(state.get("slowmo") or 0)
    browser_line = (
        "Launch your browser HEADLESS (default, fastest)." if headless else
        f"Launch your browser HEADED (visible:true was chosen) with slowMo:{slowmo} so a human can "
        "watch it - the user wants to see this run."
    )
    from icx_engine.testing.analyzers.scenarios import build_scenario_guidance
    scenario_guidance = build_scenario_guidance(state.get("nl_intent"), state.get("acceptance_criteria"))

    response = interrupt({
        "gate": "author_flow",
        "test_type": "agent",
        "screen_model": model if isinstance(model, dict) else None,
        "url": state.get("url"),
        "auth_mode": auth_mode,
        "storage_state": storage_state,
        "file_paths": state.get("file_paths"),
        "report_path": report_path,
        "rules": _rules.load_gate_rules("author_flow"),
        "rules_path": _rules.gate_rules_path("author_flow"),
        "playwright": playwright_env,
        "headless": headless,
        "slowmo": slowmo,
        "message": (
            f"{login_line}\n\n{writes_line}\n\n{browser_line}\n\n{iteration_cap_line}\n\n"
            "Follow gate.rules (the checklist) in full - it is binding, read it every time. "
            "gate.screen_model is a FLOOR, not a ceiling: you are the one actually reading this "
            "app's source - if you find a functionality, field, or tag (e.g. an upload/export/report "
            "action) that gate.screen_model never listed, TEST IT TOO and report it in \"discovered\" "
            "below; never skip something real just because ICX's census didn't name it. For a "
            "functionality with no create-step (export/upload/download/report), exercise it against "
            "whatever real data already exists - do not skip it for lack of a record to create. If "
            "the live app or source disagrees with what gate.screen_model says, trust what you "
            "actually see/read, adapt, and say why in \"findings\" or \"discovered\". "
            "Write a real Playwright test file in this repo covering gate.screen_model per the "
            "checklist, then run it YOURSELF (Bash) against ICX's pinned Playwright install - see "
            "gate.playwright for the node executable and env vars (NODE_PATH, "
            "PLAYWRIGHT_BROWSERS_PATH) to set, so you use ICX's own install, not a bare npx/global "
            "one. Point the run at gate.report_path with a JUnit reporter "
            "(`--reporter=junit --output=<gate.report_path>` for `playwright test`, or the "
            "equivalent for your test runner). Read failures Playwright itself reports, fix your "
            "OWN script, re-run - repeat until the checklist is covered or you have confirmed a "
            "genuine app bug (report it, do not force a false pass). "
            "Resume with {\"report_path\": \"<path you actually wrote the JUnit report to>\", "
            "\"test_file\": \"<path to the test file you wrote>\", \"covered\": [\"<census "
            "functionality names/ids you covered>\"], \"discovered\": [\"<functionality/tag names "
            "you found by reading code and tested, that gate.screen_model never listed>\"], "
            "\"findings\": [\"<genuine app bugs found, if any>\"]}."
            + scenario_guidance
        ),
    })

    out = {"read_receipts": _record_receipts(state, "author_flow", response if isinstance(response, dict) else {})}
    if isinstance(model, dict) and model:
        out["screen_model"] = model
    if isinstance(response, dict):
        out["agent_report_path"] = str(response.get("report_path") or report_path)
        out["agent_test_file"] = str(response.get("test_file") or "")
        out["agent_covered"] = list(response.get("covered") or [])
        out["agent_findings"] = list(response.get("findings") or [])
        out["agent_discovered"] = list(response.get("discovered") or [])
    return out


def _playwright_env() -> dict:
    """Node executable + env vars for ICX's own pinned Playwright install, so the agent runs its
    hand-written test against ICX's tooling (never a bare npx/global install). Best-effort - an
    absent install still returns a usable shape (empty env), the gate message tells the agent what
    each field means regardless."""
    try:
        from icx_engine.runtime_manager import resolve_harness_node
        from icx_engine.testing.runners.install import installed_path, browsers_dir
        node = resolve_harness_node() or "node"
        pw = installed_path("playwright")
        env = {}
        if pw:
            env["NODE_PATH"] = str(Path(pw) / "node_modules")
            env["PLAYWRIGHT_BROWSERS_PATH"] = str(browsers_dir(Path(pw)))
        return {"node": node, "env": env, "installed": bool(pw)}
    except Exception:
        return {"node": "node", "env": {}, "installed": False}


# lang aliases: runner.lang -> Runtime Manager language key
_RUNTIME_LANG_ALIAS = {"js-ts": "node", "javascript": "node", "typescript": "node"}


async def _runtime_resolver(repo: str):
    """Return an async resolver(lang)->path that uses the Runtime Manager to pick the repo-correct
    runtime, non-blocking. Falls back to None (runner uses PATH) when not resolvable.

    Memoized per (lang, repo) for the resolver's lifetime: resolving spawns a version-probe
    subprocess on a registry miss, and one run may query several same-language runners - the cache
    collapses those to a single resolution.
    """
    cache: dict[str, str | None] = {}

    async def _resolve(lang: str):
        from icx_engine.runtime_manager import resolve_runtime, resolve_harness_node
        key = _RUNTIME_LANG_ALIAS.get(lang, lang)
        if key in cache:
            return cache[key]
        try:
            if key in ("ui", "agent"):
                # The UI harness (Playwright) needs a MODERN node, decoupled from the
                # app's node - a node-14/16 project still gets UI testing on a discovered node-18+.
                path = await asyncio.to_thread(resolve_harness_node)
            else:
                res = await asyncio.to_thread(resolve_runtime, key, repo)
                path = res.path if getattr(res, "status", "") == "resolved" else None
        except Exception:
            path = None
        cache[key] = path
        return path
    return _resolve


def _agent_report_result(state: TestingState) -> dict:
    """Build the same {ok, test_type, reason, runners, summary, reports, cases, unavailable} shape
    run_local_verification returns, but from the JUnit report the AGENT's own Playwright run wrote
    (see node_author_flow) - ICX parses it, never executes anything itself for agent-type. Adds
    `coverage_gaps`: census functionalities neither `covered` nor `discovered` named (the census is
    a floor - anything the agent found by reading source and tested closes a gap same as a census
    item does), `discovered`: functionality/tags the agent found on its own, and `findings`: genuine
    app bugs the agent reported (distinct from test failures)."""
    from icx_engine.testing.runners.junit import parse_junit_xml

    report_path = str(state.get("agent_report_path") or "")
    if not report_path or not Path(report_path).exists():
        return {"ok": False, "test_type": "agent",
                "reason": f"no JUnit report found at {report_path or '(none given)'} - the agent's "
                          f"Playwright run may not have completed or was not pointed at this path",
                "runners": ["playwright"], "summary": {}, "reports": [], "cases": [], "unavailable": []}

    rep = parse_junit_xml(report_path)
    discovered = [str(d).strip() for d in (state.get("agent_discovered") or []) if str(d).strip()]
    covered = {str(c).strip().lower() for c in (state.get("agent_covered") or []) if str(c).strip()}
    covered |= {d.lower() for d in discovered}
    model = state.get("screen_model")
    gaps: list[str] = []
    if isinstance(model, dict) and covered:
        for f in (model.get("functionalities") or []):
            if not isinstance(f, dict):
                continue
            name = str(f.get("functionality") or f.get("id") or "").strip()
            if name and name.lower() not in covered:
                gaps.append(name)

    summary = {"total": rep.total, "passed": rep.passed, "failures": rep.failures,
              "errors": rep.errors, "skipped": rep.skipped}
    return {
        "ok": rep.ok and not gaps,
        "test_type": "agent",
        "reason": "" if rep.ok and not gaps else
                  ("tests failed or none ran" if not rep.ok else
                   f"census functionalities not covered by the agent's test: {', '.join(gaps)}"),
        "runners": ["playwright"],
        "summary": summary,
        "reports": [{"runner": "playwright", "report_path": report_path, "total": rep.total, "ok": rep.ok}],
        "cases": [(c.name, c.status, c.time) for c in rep.cases],
        "unavailable": [],
        "coverage_gaps": gaps,
        "discovered": discovered,
        "findings": list(state.get("agent_findings") or []),
    }


async def node_local_run(state: TestingState) -> dict:
    """Run the local (in-process, async) verification suite and feed the result to the review gate.

    Executes engine='local'. Fully async; never blocks the event loop. Uses the Runtime Manager to
    run each layer on the repo-correct runtime. Maps a failed suite to a single issue so the review
    gate handles it uniformly.

    agent-type does NOT invoke a runner here - the agent already ran its own Playwright test (see
    node_author_flow); this just reads the JUnit report it produced (`_agent_report_result`).
    """
    test_type = state.get("test_type") or "unit"
    repo = _local_repo_root(state)

    if test_type == "agent":
        res = _agent_report_result(state)
        if res.get("ok"):
            return {"status": "parsed", "issues": [], "full_report": res, "run_id": "local"}
        issues = [{
            "name": "verification_failed",
            "description": res.get("reason", "agent test run did not pass"),
            "severity": "high",
            "detail": res.get("summary", {}),
        }]
        return {"status": "parsed", "issues": issues, "full_report": res, "run_id": "local"}

    from icx_engine.testing.local_executor import run_local_verification
    if test_type == "api":
        # Materialize the backend census into openapi.json + *.hurl so schemathesis (schema fuzz) and
        # hurl (scripted per-endpoint status asserts) have something to run - the census IS the spec.
        model = state.get("screen_model")
        if isinstance(model, dict) and model and str(state.get("analyzer_family")) == "backend":
            try:
                from icx_engine.testing.analyzers.to_api_spec import materialize_api_spec
                materialize_api_spec(model, repo, state.get("url") or "")
            except Exception:
                pass
    try:
        resolver = await _runtime_resolver(repo)
        res = await run_local_verification(
            repo, test_type, target_url=state.get("url"), runtime_resolver=resolver,
        )
    except Exception as exc:
        # An execution error is a FAILURE, not a pass. Emit an issue so route_after_check_issues does
        # not treat empty issues as "all tests passed" (which would make memory_save record a false
        # green). The review gate then surfaces it to the user.
        return {"status": "error", "last_error": f"local verification failed: {exc}",
                "run_id": "local",
                "issues": [{"name": "verification_error",
                            "description": f"local verification failed to run: {exc}",
                            "severity": "high", "detail": {}}]}

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
        # Census coverage as a DoD dimension: when the Element Census ran, surface how completely the
        # authored tests cover the screen model (1.0 = every censused element reconciled). This makes
        # "nothing missed" a visible, scored part of Definition-of-Done, not just an internal check.
        cov = float(state.get("census_coverage") or 0.0)
        if state.get("screen_model") is not None:
            dod_items.append({
                "check": f"element-census coverage ({state.get('analyzer_id') or 'analyzer'})",
                "method": "census-reconciliation",
                "passed": cov >= 0.999,
                "command": "analyze_screen",
                "output": f"coverage={cov}",
            })
            res["census_coverage"] = cov
        res["confidence"] = build_confidence_report(dod_items, tier, layers)
        res["dod_items"] = dod_items
    except Exception:
        pass

    # STATIC SECURITY (always on): native, no-install scan of the repo source - leaked secrets, dangerous
    # code patterns (SAST-lite), and dependency/SCA - folded onto res['security']. Fully guarded; a scan
    # failure never affects the node result. Runtime DAST probes run separately inside the UI/API flow.
    try:
        from icx_engine.testing.security import fold_into_result
        fold_into_result(res, repo)
    except Exception:
        pass

    # TEST QUALITY (always on, honest): regression selection (relevant tests for the change), performance
    # regression (when before/after metrics are provided), and mutation scoring (opt-in via a report path).
    # Each reports real data or an honest 'not run: <reason>'. Fully guarded - never affects the result.
    try:
        from icx_engine.testing.quality_advisory import fold_quality
        fold_quality(res, repo)
    except Exception:
        pass

    # ANALYTICS (opt-in, off by default): record this run into the local history store. Fully guarded
    # - a recording failure never affects the node result.
    try:
        from icx_engine.testing.analytics.record import analytics_enabled, record_from_result
        if analytics_enabled():
            import time as _t
            record_from_result(res, app=str(state.get("project") or state.get("url") or "app"),
                               run_id=f"{state.get('project') or 'run'}-{int(_t.time())}", ts=_t.time())
    except Exception:
        pass

    # HUMAN REPORT (always on): write a browser-viewable HTML report of this run to
    # ~/.icx/testing/reports/ (or ICX_TEST_REPORTS_DIR) + refresh index.html. Fully guarded - a report
    # write never affects the node result. The MCP agent gets the structured result; this is the mirror
    # a human can open and read.
    try:
        import time as _rt
        from icx_engine.testing.reporting.session_report import write_session_report
        write_session_report(res, {
            "app": str(state.get("project") or state.get("url") or "app"),
            "url": str(state.get("url") or ""),
            "test_type": str(state.get("test_type") or res.get("test_type") or ""),
            "ts": int(_rt.time()),
            "run_id": f"{state.get('project') or 'run'}-{int(_rt.time())}",
        })
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
            "  1. automated - ICX runs the local verification suite, shows issues\n"
            "                 found, loops until clean, then asks\n"
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
    history_path.parent.mkdir(parents=True, exist_ok=True, **({"mode": 0o700} if sys.platform != "win32" else {}))
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




