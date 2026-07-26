"""Shared best-in-class engineering personas - the single source of truth for BOTH the analyze
(Jira) senior-persona layer and the boost refine's CTO-grade prompt. Pure module (no I/O, no LLM):
maps a problem's text to the right specialist persona (security, database, performance, UI/UX, ...) so
the prompt handed to the agent always operates at the correct expert bar, chosen per problem - never a
single hardcoded persona."""
from __future__ import annotations

import re

PERSONA_SLUGS: set[str] = {
    "cto", "principal-engineer", "solution-architect", "system-architect",
    "enterprise-architect", "staff-backend-engineer", "staff-frontend-engineer",
    "principal-ui-ux-architect", "principal-data-architect", "principal-database-architect",
    "staff-devops-sre", "principal-security-architect", "staff-performance-engineer",
    "principal-ml-engineer", "staff-mobile-engineer", "principal-integration-architect",
    "principal-qa-automation-architect", "principal-api-test-architect",
    "principal-unit-test-architect",
}

DEFAULT_PERSONA = "system-architect"
UI_PERSONAS = {"principal-ui-ux-architect", "staff-frontend-engineer"}

# Ordered: first slug whose keyword set hits the problem text wins the keyword heuristic.
# NOTE: matched against text with URLs already stripped (see strip_urls) - a pasted URL's own path
# segments (a "/login" route, an "/api/" prefix) must never masquerade as task intent.
PERSONA_KEYWORDS: list[tuple[str, set[str]]] = [
    ("principal-security-architect", {"jwt", "oauth", "credential", "secret", "vulnerab",
        "injection", "xss", "csrf", "encrypt", "exploit", "penetration test", "pentest"}),
    ("principal-database-architect", {"index", "query plan", "slow query", "sql", "join",
        "deadlock", "n+1", "orm query", "table scan"}),
    ("principal-data-architect", {"schema", "migration", "pipeline", "etl", "data model",
        "warehouse", "ingest", "dataset"}),
    ("staff-performance-engineer", {"latency", "throughput", "timeout", "memory leak",
        "cpu", "performance", "profil", "bottleneck", "p99", "load", "too slow", "very slow",
        "runs slow", "slow load", "slow response", "slower", "slowly", "slowing down"}),
    ("staff-devops-sre", {"deploy", "ci/cd", "terraform", "kubernetes", "docker",
        "infra", "cluster", "helm", "reliability", "outage", "rollout"}),
    ("principal-ml-engineer", {"inference", "training", "embedding", "ml",
        "prediction", "dataset drift", "fine-tune"}),
    ("staff-mobile-engineer", {"android", "ios", "react native", "swift", "kotlin app",
        "mobile", "app store", "play store"}),
    ("principal-integration-architect", {"webhook", "third-party", "event bus",
        "kafka", "message queue", "api contract", "grpc", "proto"}),
    ("principal-api-test-architect", {"api test", "endpoint test", "contract test",
        "schemathesis", "hurl", "status code", "api coverage", "api spec test"}),
    ("principal-unit-test-architect", {"unit test", "unit tests", "mock", "stub", "jest",
        "pytest", "junit", "mutation test", "test coverage report", "assert"}),
    ("principal-qa-automation-architect", {"e2e", "playwright", "browser test",
        "ui test", "click through", "which cases", "which all cases", "test each",
        "every case", "regression suite", "flaky", "qa", "test plan", "test coverage",
        "cover every", "slowmo", "headed browser", "test the screen"}),
    ("principal-ui-ux-architect", {"button", "layout", "css", "styling", "modal", "form",
        "screen", "ui", "ux", "responsive", "accessib", "component render"}),
    ("staff-frontend-engineer", {"react", "vue", "state management", "redux", "hook", "frontend",
        "client-side", "dom"}),
    ("staff-backend-engineer", {"api", "endpoint", "service", "controller", "repository",
        "backend", "server", "handler", "null pointer", "500"}),
    ("solution-architect", {"integrate systems", "end-to-end", "cross-service", "microservice"}),
    ("system-architect", {"architecture", "data flow", "scaling", "refactor", "coupling"}),
]

UI_VOCAB = {"button", "layout", "css", "styling", "modal", "form", "screen", "ui", "ux",
    "responsive", "accessib", "render", "component", "page", "click"}
BACKEND_VOCAB = {"api", "endpoint", "service", "controller", "repository", "backend",
    "server", "handler", "database", "query", "schema", "null pointer"}

# slug -> (title, focus). The title is used verbatim in the ROLE line; focus states what to reason about.
PERSONA_PROFILE: dict[str, tuple[str, str]] = {
    "cto": ("CTO", "weigh business impact, risk, and long-term maintainability above local convenience"),
    "principal-engineer": ("principal engineer", "attack the hardest ambiguity first and prove the mechanism, not the symptom"),
    "solution-architect": ("solution architect", "design the end-to-end flow across every system the change touches"),
    "system-architect": ("senior system architect", "reason about service boundaries, data flow, scaling, and migration safety"),
    "enterprise-architect": ("enterprise architect", "keep the change consistent with organization-wide standards and other systems"),
    "staff-backend-engineer": ("staff backend engineer", "trace the request path, service and data layers, and error handling"),
    "staff-frontend-engineer": ("staff frontend engineer", "reason about component state, data fetching, and render correctness"),
    "principal-ui-ux-architect": ("principal UI/UX architect", "reason about layout, interaction, accessibility, and visual states"),
    "principal-data-architect": ("principal data architect", "reason about the schema, data modeling, and pipeline integrity"),
    "principal-database-architect": ("principal database architect", "reason about query plans, indexing, and transactional correctness"),
    "staff-devops-sre": ("staff DevOps/SRE", "reason about deployment, reliability, rollout, and blast radius in production"),
    "principal-security-architect": ("principal security architect", "reason about the trust boundary, authn/authz, and exploit paths"),
    "staff-performance-engineer": ("staff performance engineer", "reason about latency, throughput, allocation, and measured bottlenecks"),
    "principal-ml-engineer": ("principal ML engineer", "reason about the model, data, evaluation, and inference path"),
    "staff-mobile-engineer": ("staff mobile engineer", "reason about the platform lifecycle, device constraints, and app state"),
    "principal-integration-architect": ("principal integration architect", "reason about API contracts, events, retries, and third-party failure modes"),
    "principal-qa-automation-architect": ("principal QA automation architect", "reason about end-to-end user flows, coverage completeness, and how each pass/fail is proven by real execution, not assumption"),
    "principal-api-test-architect": ("principal API test architect", "reason about contract correctness, status codes, schema validation, and failure-mode coverage across endpoints"),
    "principal-unit-test-architect": ("principal unit test architect", "reason about function-level correctness, edge cases, and whether a test actually asserts behavior or merely executes code"),
}

# Map a boost archetype to a sensible default persona when the keyword heuristic finds nothing.
ARCHETYPE_PERSONA = {
    "security": "principal-security-architect",
    "database": "principal-database-architect",
    "performance": "staff-performance-engineer",
    "debugging": "principal-engineer",
    "design": "system-architect",
    "coding": "staff-backend-engineer",
    "planning": "solution-architect",
    "research": "principal-engineer",
    "writing": "principal-engineer",
    "doubt": "principal-engineer",
    "testing": "principal-qa-automation-architect",
}

_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_KW_RE_CACHE: dict[str, "re.Pattern[str]"] = {}


def strip_urls(text: str) -> str:
    """Remove pasted URLs before keyword/archetype matching. A URL's own path segments (a '/login'
    route, an '/api/' prefix, a 'users' resource name) are routing details, not task intent - matching
    keywords against them causes false persona/archetype hits unrelated to what was actually asked."""
    return _URL_RE.sub(" ", text or "")


def kw_hit(text: str, kw: str) -> bool:
    """Match kw against lowercased text. Multi-word or non-alnum tokens match as a substring; a single
    alphanumeric token matches at a word start with any suffix (so 'endpoint' hits 'endpoints' but 'ci'
    does NOT hit 'decision')."""
    if " " in kw or not kw.isalnum():
        return kw in text
    pat = _KW_RE_CACHE.get(kw)
    if pat is None:
        pat = re.compile(r"\b" + re.escape(kw) + r"\w*")
        _KW_RE_CACHE[kw] = pat
    return pat.search(text) is not None


def keyword_persona(text: str, issue_type: str = "") -> str | None:
    """First persona whose keyword set hits the (lowercased, URL-stripped) text; None if nothing
    matches."""
    low = strip_urls(text or "").lower()
    for slug, kws in PERSONA_KEYWORDS:
        if any(kw_hit(low, kw) for kw in kws):
            return slug
    if issue_type.lower() == "epic":
        return "system-architect"
    return None


def select_persona(text: str, archetype: str = "") -> str:
    """Best-in-class persona for a problem, from its text (keyword heuristic), falling back to the
    archetype's default persona, then the global default. Never returns None."""
    return keyword_persona(text) or ARCHETYPE_PERSONA.get(archetype, "") or DEFAULT_PERSONA


def persona_profile(slug: str) -> tuple[str, str]:
    """(title, focus) for a persona slug; the default persona's profile for an unknown slug."""
    return PERSONA_PROFILE.get(slug, PERSONA_PROFILE[DEFAULT_PERSONA])
