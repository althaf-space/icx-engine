"""ICX Universal Boost Channel - classify a request, adaptively gather context, and return a
methodology-scaffolded boosted brief. Pure core (no I/O, no LLM); retrieval is injected by the MCP layer."""
from __future__ import annotations

from icx_engine.boost.brief import build_brief, compose_boosted_prompt
from icx_engine.boost.classify import CODE_ARCHETYPES, classify
from icx_engine.boost.links import build_link_plan, extract_links
from icx_engine.boost.router import ActivationPlan, plan_activation

__all__ = ["classify", "CODE_ARCHETYPES", "plan_activation", "ActivationPlan",
           "build_brief", "compose_boosted_prompt", "extract_links", "build_link_plan"]
