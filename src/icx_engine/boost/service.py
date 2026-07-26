"""Shared boost orchestration - the single source of truth used by the icx_boost MCP tool, the
`icx boost brief` CLI, and any editor hook. Given a prompt + injected providers (environment detection,
retrieval signals, connection status), it classifies, adaptively gathers context, enriches links, and
returns the boosted brief. Decoupled: the real graph/grep/memory/config providers are injected, so this
is unit-testable with fakes and never imports the MCP or graph layers itself."""
from __future__ import annotations

from icx_engine.boost.brief import build_brief
from icx_engine.boost.classify import classify
from icx_engine.boost.links import build_link_plan, extract_links
from icx_engine.boost.router import plan_activation
from icx_engine.context_completeness import fan_out, fuse_rank
from icx_engine.methodology import compact_checklist


def build_boost_brief(prompt: str, repo_path: str | None = None, current_file: str | None = None,
                      is_continuation: bool = False, links_in=None, *,
                      env_fn, signals_fn, connected_fn) -> dict:
    """Assemble the boosted brief.

    env_fn(repo_path, is_continuation) -> {has_repo, has_graph, is_continuation}
    signals_fn(repo_path, seeds, keywords) -> (graph, grep, semantic, memory) zero-arg callables
    connected_fn() -> {"jira": bool, "sonarqube": bool}
    All three are injected so this stays decoupled + testable. Pure aside from the injected providers."""
    env = env_fn(repo_path, is_continuation)
    archetype = classify(prompt, env)
    plan = plan_activation(prompt, archetype, env)

    provided = [str(u) for u in (links_in or []) if u]
    urls = list(dict.fromkeys(provided + extract_links(prompt)))
    link_plan = build_link_plan(urls, connected_fn())

    context = {"activated_signals": sorted(plan.signals), "files": [], "skipped": plan.skipped}
    if plan.signals and repo_path:
        seeds = [current_file] if current_file else []
        keywords = [w for w in prompt.lower().split() if len(w) > 3][:8]
        g, gr, se, me = signals_fn(repo_path, seeds, keywords)
        active = {"graph": g, "grep": gr, "semantic": se, "memory": me}
        for k in list(active):
            if k not in plan.signals:
                active[k] = None
        cands = fan_out(seeds, graph=active["graph"], grep=active["grep"],
                        semantic=active["semantic"], memory=active["memory"])
        scored = fuse_rank(cands)
        context["files"] = [s.to_dict() for s in scored if s.tier != "seed"][:20]

    methodology = compact_checklist(prompt, archetype, env)
    return build_brief(prompt, archetype, methodology, context, plan, [], link_plan)
