"""ICX agent methodology - the mandatory problem-solving discipline every agent working through ICX
must follow. Pure module (no I/O, no LLM). ASCII-only faithful distillation of the AI Problem-Solving
Framework: intake -> context -> classify -> decompose -> plan -> execute -> self-check -> confidence
-> fail-well -> verify.

`build_checklist(analysis)` returns the per-ticket MANDATORY checklist injected into every analyze
response (unavoidable). `full_text()` returns the complete operative methodology for the get_methodology
tool (pullable on demand).
"""
from __future__ import annotations

METHODOLOGY_VERSION = "1.0"

# The always-present, per-ticket mandatory spine (kept short so the agent actually follows it).
ONE_PAGER = """\
ICX METHODOLOGY (mandatory - follow every step, every ticket):
1 INTAKE     - restate the REAL intent + deliverable shape. List explicit + implicit + conventional
               requirements. Note constraints. Flag ambiguity: proceed on the dominant reading and
               state the assumption, OR ask ONE question only when a wrong guess is expensive.
2 CONTEXT    - read the actual artifacts BEFORE theorizing. Test each unknown: "would knowing this
               change the output?" If yes and not inferable -> get it (graph/grep/memory/read), never
               fabricate. Declare load-bearing assumptions inline.
3 CLASSIFY   - name the archetype (coding/debugging/design/perf/db/security/...) -> inherit its
               workflow + known pitfalls.
4 DECOMPOSE  - split into independently verifiable units; order by dependency; do the risky/uncertain
               part first.
5 PLAN       - 2-4 REAL alternatives (different trade-offs), compare on the axes that matter, COMMIT
               with a one-line reason + name the strongest rejected option. Then call lock_plan.
6 EXECUTE    - contract-first (signatures/schemas before bodies). Tools over guesses. Match the
               codebase's conventions. Localize uncertainty. Fix root cause, not the symptom.
7 SELF-CHECK - adversarial: try to prove it wrong. Complete (every clause answered) + correct (run/
               mentally-execute >=2 inputs incl an edge) + secure + performant + edges + consistent.
               Repair before delivering - do not ship a disclaimer instead of a fix.
8 CONFIDENCE - calibrated + specific. Verified confidence extends ONLY to what was actually verified.
9 FAIL WELL  - name the real error, fix the CLASS not the instance, re-verify, keep the lesson.
VERIFY       - unit-mindset -> integration seams -> performance -> security -> regression guard.

INVARIANTS (cut across every step):
 - Read the actual artifact before theorizing about it.
 - Retrieval + execution beat recall + inspection.
 - The accepted explanation must account for ALL the evidence.
 - Verified confidence extends only as far as what was verified.
 - Simplicity wins ties; reversibility buys deliberation budget.
 - Honesty about limits is part of the deliverable, not an apology.
"""

_INTAKE_CHECKLIST = [
    "deliverable: type, format, length, audience, environment (lang/version/OS/framework)",
    "requirements: explicit (hard) + implicit (entailed) + conventional (norms) - all three",
    "constraints: technical, resource, compatibility, policy/safety, preference",
    "ambiguity: dominant reading + stated assumption, or ONE question if a wrong guess is costly",
    "risk: harm / irreversibility / high-stakes domain / confidence gap",
]

_VERIFICATION_BATTERY = [
    "completeness: every clause of a compound request answered; no silent stubs",
    "correctness: run or mentally execute >=2 concrete inputs incl one edge; verify APIs exist",
    "security: hostile input, no secrets in logs, authZ per-resource, safe defaults, guarded destructive ops",
    "performance: complexity fits real data size; no N+1 / unbounded memory",
    "edges: empty/null, boundaries (0,1,max,max+1), duplicates, unicode, scale, time, concurrency, hostile",
    "consistency: names/types agree; matches prior turns; one voice; delivered format == requested",
]

# Archetype -> (discipline, top pitfalls to avoid). Brief + actionable.
_ARCHETYPES = {
    "coding": ("contract-first; write to the spec not the example; no over-abstraction",
               "writing to the example not the spec; hallucinated APIs; ignoring the repo's conventions"),
    "debugging": ("hypothesis-driven; the accepted cause must explain ALL symptoms; fix origin not surface",
                  "pattern-matching to a famous bug; fixing where the error surfaces not where it originates"),
    "design": ("state dominant constraints; baseline a small team can run; expose top trade-offs + rejected",
               "resume-driven design; designing for imagined scale; one answer with no trade-offs shown"),
    "performance": ("no optimization without measurement; algorithmic > I/O > memory > cache > micro",
                    "optimizing the readable 5% while one unindexed query is the cost; caching over a bug"),
    "database": ("model from access patterns not nouns; index to predicates; migrations without long locks",
                 "schemas from nouns; index-everything or none; migrations that lock big tables; money as float"),
    "security": ("trust boundaries hostile by default; vetted primitives; default-deny; dual-use limits",
                 "authenticated != authorized; client-only sanitize; hand-rolled crypto; secrets in logs"),
    "research": ("search for time-sensitive/post-cutoff; primary sources; synthesize, cite, surface disagreement",
                 "answering current-state from memory; trusting a top SEO page; one source as consensus"),
    "writing": ("audience+purpose; structure; the reader's experience; format for the medium",
                "uniform AI-essay voice; bullet-point disease; burying the lede; length as quality"),
    "planning": ("de-risk early; verifiable milestones; sequence by dependency; checkpoints",
                 "uniform granularity; unobservable milestones; no slack; unknowns scheduled last"),
    "doubt": ("answer the actual question directly; verify facts/APIs before asserting; state uncertainty",
              "answering from memory when it is checkable; over-answering a simple question; false confidence"),
}

# Text signals per archetype (first match in this order wins). Deterministic.
_ARCHETYPE_SIGNALS = (
    ("debugging", ("error", "crash", "broken", "regression", "fails", "failing", "exception",
                   "stack trace", "not working", "bug", "500", "npe", "null pointer")),
    ("security", ("auth", "token", "vulnerab", "injection", "secret", "xss", "csrf", "exploit",
                  "sql injection", "sanitize", "escape")),
    ("performance", ("slow", "latency", "performance", "timeout", "optimi", "n+1", "throughput",
                     "memory leak")),
    ("database", ("schema", "migration", "query", "sql", "index", "orm", "table", "join")),
    ("design", ("design", "architect", "structure", "scale", "trade-off", "approach", "should we")),
    ("planning", ("plan", "roadmap", "milestone", "sequence", "break down", "estimate")),
    ("research", ("latest", "current", "compare", "which is better", "research", "benchmark",
                  "what is the best")),
    ("writing", ("write a doc", "documentation", "readme", "blog", "explain in writing", "draft a")),
)
# Short interrogatives that are a plain question ("doubt"), not a build task.
_DOUBT_STARTS = ("what", "why", "how", "is", "are", "does", "do", "can", "should", "which",
                 "when", "who")


def _token_hit(token: str, text: str, words: list[str]) -> bool:
    """Match a signal token. Alphanumeric tokens match on a word boundary (prefix of a word, so
    'optimi' hits 'optimize' but 'orm' does NOT hit 'form'); tokens with spaces/punctuation match as
    a substring of the full text."""
    if token.isalnum():
        return any(w.startswith(token) for w in words)
    return token in text


def classify_text(text: str) -> str:
    """Best-effort archetype from raw prompt text. Deterministic; recommendation - the agent confirms."""
    t = (text or "").lower().strip()
    words = [w.strip(".,;:!?()[]{}\"'") for w in t.split()]
    for arch, toks in _ARCHETYPE_SIGNALS:
        if any(_token_hit(tok, t, words) for tok in toks):
            return arch
    first = words[0] if words else ""
    if (t.endswith("?") or first in _DOUBT_STARTS) and len(words) <= 25:
        return "doubt"
    return "coding"

# The named failure modes and their mitigation (the traps that produce most first-pass mistakes).
_FAILURE_MODES = [
    "API/interface hallucination -> prefer well-established APIs; verify or flag uncertain ones; run code",
    "requirement drop (a clause of a compound request lost) -> checklist against the original message",
    "stale-world answers -> search for current/time-sensitive facts; never assert them from memory",
    "instruction drift (early constraints fade over long output) -> re-check constraints at each boundary",
    "plausible-wrong reasoning (one invalid step in a fluent chain) -> verify by a 2nd method / execution",
    "over-agreement (accepting a wrong premise or a bad fix) -> premise-check; push back with the reason",
    "symptom-level debugging -> the cause must explain ALL symptoms; fix the origin",
    "over-engineering -> solve the specific problem asked; simplicity + YAGNI",
    "format mismatch (right content, wrong shape) -> confirm deliverable shape at intake and review",
    "context drift in long sessions -> re-read the source/artifact before editing it",
]

# Where hallucination concentrates: VERIFY or hedge here, never assert bare.
_HIGH_RISK_ZONES = [
    "exact quotes, citations, case/paper names",
    "precise numbers: stats, prices, versions, dates",
    "niche/long-tail APIs, small libraries, internal tools",
    "details of specific real people / small entities",
    "anything post-cutoff or fast-moving",
]

_BUG_TYPES = {"bug", "defect", "incident", "error"}


def _classify(analysis: dict) -> str:
    """Best-effort archetype from the analysis (recommendation; the agent confirms)."""
    itype = str(analysis.get("issue_type", "")).lower()
    text = " ".join(str(analysis.get(k, "")) for k in
                    ("problem_summary", "detailed_description", "impact")).lower()
    if itype in _BUG_TYPES or any(t in text for t in ("error", "crash", "broken", "regression", "fails")):
        return "debugging"
    if any(t in text for t in ("design", "architect", "structure", "scale")):
        return "design"
    if any(t in text for t in ("slow", "latency", "performance", "timeout", "optimi")):
        return "performance"
    if any(t in text for t in ("auth", "token", "vulnerab", "injection", "secret", "xss", "csrf")):
        return "security"
    if any(t in text for t in ("schema", "migration", "query", "sql", "index")):
        return "database"
    return "coding"


def _core(archetype: str) -> dict:
    """Shared mandatory-checklist body for a given archetype."""
    discipline, pitfalls = _ARCHETYPES.get(archetype, _ARCHETYPES["coding"])
    return {
        "version": METHODOLOGY_VERSION,
        "mandatory": True,
        "one_pager": ONE_PAGER,
        "archetype": archetype,
        "archetype_discipline": discipline,
        "archetype_pitfalls": pitfalls,
        "intake_checklist": _INTAKE_CHECKLIST,
        "verification_battery": _VERIFICATION_BATTERY,
        "failure_modes_to_avoid": _FAILURE_MODES,
        "hallucination_high_risk_zones": _HIGH_RISK_ZONES,
        "gate_sequence": list(_GATE_SEQUENCE),
        "note": "This is mandatory. Call get_methodology for the full framework (archetypes, "
                "decision trees, failure modes, case studies).",
    }


_GATE_SEQUENCE = (
    "answer INTAKE + CONTEXT before planning",
    "produce 2-4 alternatives, COMMIT one with reasons",
    "call lock_plan with the files you will change (blocks on missed high-signal files)",
    "implement only after lock_plan ok",
    "run the VERIFICATION battery; record_verification before declaring done",
)


def build_checklist(analysis: dict | None) -> dict:
    """Per-ticket MANDATORY methodology checklist for the analyze response. Pure; guarded."""
    analysis = analysis if isinstance(analysis, dict) else {}
    return _core(_classify(analysis))


def build_checklist_for(prompt: str, archetype: str | None = None, env: dict | None = None) -> dict:
    """Generalized checklist for ANY task (not just a Jira ticket). Classifies from the prompt when
    archetype is not given. env is accepted for future signal shaping; currently advisory."""
    arch = archetype or classify_text(prompt or "")
    return _core(arch)


def compact_checklist(prompt: str, archetype: str | None = None, env: dict | None = None) -> dict:
    """Token-lean methodology for the boost brief (returned on EVERY prompt): the operative spine
    (one_pager) + this archetype's discipline/pitfalls + gate_sequence + a pointer to get_methodology
    for the full framework. Drops the full intake/verification/failure-mode/hallucination lists (they
    live in get_methodology) - saving ~600 tokens per call with no loss (they are one tool call away)."""
    arch = archetype or classify_text(prompt or "")
    discipline, pitfalls = _ARCHETYPES.get(arch, _ARCHETYPES["coding"])
    return {
        "version": METHODOLOGY_VERSION,
        "mandatory": True,
        "one_pager": ONE_PAGER,
        "archetype": arch,
        "archetype_discipline": discipline,
        "archetype_pitfalls": pitfalls,
        "gate_sequence": list(_GATE_SEQUENCE),
        "note": "Full framework (intake checklist, verification battery, failure modes, hallucination "
                "zones, all archetypes) is one call away: get_methodology.",
    }


def full_text() -> str:
    """Complete operative methodology for the get_methodology tool. ASCII-only."""
    parts = [
        "# ICX Agent Methodology (mandatory)\n",
        ONE_PAGER,
        "\n## Intake checklist\n" + "\n".join(f"- {x}" for x in _INTAKE_CHECKLIST),
        "\n## Archetypes (name yours, inherit its discipline + avoid its pitfalls)\n"
        + "\n".join(f"- {k}:\n    do:    {d}\n    avoid: {p}" for k, (d, p) in _ARCHETYPES.items()),
        "\n## Verification battery (run before 'done', even without a test suite)\n"
        + "\n".join(f"- {x}" for x in _VERIFICATION_BATTERY),
        "\n## Failure modes to avoid (the traps behind most first-pass mistakes)\n"
        + "\n".join(f"- {x}" for x in _FAILURE_MODES),
        "\n## Hallucination high-risk zones (VERIFY or hedge here - never assert bare)\n"
        + "\n".join(f"- {x}" for x in _HIGH_RISK_ZONES),
        "\n## Confidence\n"
        "- express uncertainty calibrated + specific, not blanket hedging.\n"
        "- verification raises confidence ONLY about the path actually verified.\n"
        "- never launder a guess into confident assertion; retrieval/execution over recall.",
        "\n## Fail well\n"
        "- name the real error; fix the CLASS not the instance; re-verify at >= original rigor; "
        "keep the lesson for the rest of the session.",
        "\n## Non-negotiable invariants\n"
        "- read the artifact before theorizing; the explanation must account for ALL evidence;\n"
        "- simplicity wins ties; reversibility buys deliberation budget; honesty about limits is\n"
        "  part of the deliverable.\n",
    ]
    return "\n".join(parts)
