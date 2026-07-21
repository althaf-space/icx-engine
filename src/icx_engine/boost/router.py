"""Adaptive context router - decide WHICH retrieval signals to fire from the prompt + environment.
Methodology is always applied by the brief; this only gates the (potentially expensive) codebase
lookups so a doubt or a no-repo task never triggers a graph walk. Deterministic; no I/O."""
from __future__ import annotations

from dataclasses import dataclass, field

from icx_engine.boost.classify import CODE_ARCHETYPES


@dataclass
class ActivationPlan:
    signals: set = field(default_factory=set)      # subset of graph|grep|semantic|memory
    reasons: dict = field(default_factory=dict)    # signal -> why it was activated
    skipped: str = ""                              # honest reason nothing (or less) ran


def plan_activation(prompt: str, archetype: str, env: dict | None = None) -> ActivationPlan:
    env = env or {}
    has_repo = bool(env.get("has_repo"))
    has_graph = bool(env.get("has_graph"))
    is_continuation = bool(env.get("is_continuation"))
    plan = ActivationPlan()

    if archetype not in CODE_ARCHETYPES:
        # doubt / research / design / writing / planning - answer from methodology + knowledge, not a
        # codebase walk. Memory only if the user is continuing a prior thread.
        if is_continuation:
            plan.signals.add("memory")
            plan.reasons["memory"] = "continuation - prior work may be relevant"
        plan.skipped = f"'{archetype}' is a knowledge/design task - no codebase lookup needed"
        return plan

    if not has_repo:
        if is_continuation:
            plan.signals.add("memory")
            plan.reasons["memory"] = "continuation - prior work may be relevant"
        plan.skipped = "no repository connected - skipping graph/grep context"
        return plan

    # code task with a repo: grep always applies; graph + semantic only when a graph is built.
    plan.signals.add("grep")
    plan.reasons["grep"] = "find files that reference the change target"
    if has_graph:
        plan.signals.add("graph")
        plan.reasons["graph"] = "structural dependents + co-change files from the code graph"
        plan.signals.add("semantic")
        plan.reasons["semantic"] = "files semantically related to the request"
    else:
        plan.skipped = "no code graph built for this repo - grep only (run 'icx graph build' for more)"
    if is_continuation:
        plan.signals.add("memory")
        plan.reasons["memory"] = "continuation - files touched by prior fixes"
    return plan
