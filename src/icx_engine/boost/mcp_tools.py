"""MCP tool surface for the icx_boost thinking-channel tools. Owns its own Tool()
definitions and dispatch function - mcp_server.py's _list_tools()/_call_tool()
get a few additive lines only, no restructuring. The icx-boost MCP *prompt* stays
registered in mcp_server.py (protocol-level concern) but calls _boosted() from here."""

from __future__ import annotations

import asyncio
import json

from mcp.types import TextContent, Tool

from icx_engine.mcp_server import _boost_env, _boosted, _context_signals

_BOOST_TOOL = "icx_boost"
_BOOST_REFINE_TOOL = "icx_boost_refine"

BOOST_TOOLS: list[Tool] = [
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
            "through ICX (jira_analyze_issue_fast / sonar_* tools) regardless of whether boost was called - "
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
            annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
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
            annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False},
    ),
]


async def dispatch_boost_tool(name: str, arguments: dict) -> list[TextContent] | None:
    args = arguments or {}

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
                                        "its acceptance criteria; pass the gates (icx_lock_plan before coding, "
                                        "icx_record_verification before done). Do not fall back to the raw prompt."),
            }))]
        except Exception as exc:
            return [TextContent(type="text", text=json.dumps(
                {"error": f"refine failed: {exc}", "boosted_prompt": prompt}))]

    return None
