"""Curated default skills ICX seeds into every user's ~/.icx/skills/ store (see seed.py). Content
is written in ICX's own words - no third-party skill markdown is copied verbatim. Where a skill is
inspired by an external source (UI/UX rules, test-taxonomy frameworks, Karpathy's public commentary
on minimal-diff discipline), that attribution is stated in the skill's own text, never presented as
the source's own words.

Each entry mirrors icx_engine.skills.schema.SkillEntry's constructor fields, minus icx_hash/
created_at/updated_at/origin_projects/origin_issue_keys, which seed.py fills in."""
from __future__ import annotations

DEFAULT_SKILLS: list[dict] = [
    {
        "name": "systematic-debugging",
        "description": "Use when a bug or unexpected failure needs fixing - do not guess-patch.",
        "tags": ["debugging", "bugfix", "root-cause"],
        "title": "Systematic Debugging",
        "when_to_use": (
            "Any time something is broken, throws, or behaves unexpectedly - before writing any fix."
        ),
        "procedure": (
            "1. Reproduce the failure with the smallest input that triggers it.\n"
            "2. Isolate: bisect the code path (add logging/prints or a debugger, not guesses) until "
            "the exact line or condition that causes the wrong behavior is identified.\n"
            "3. Form one concrete hypothesis for the root cause - not a vague guess.\n"
            "4. Test the hypothesis directly (read the code path, or add a minimal check) before "
            "writing the fix.\n"
            "5. Fix the root cause, not the symptom.\n"
            "6. Re-run the original reproduction to verify the failure is gone.\n"
            "7. Add a regression test that would have failed before the fix."
        ),
        "pitfalls": (
            "Patching the symptom (wrapping the failing line in try/except) instead of the cause. "
            "Changing multiple things at once and losing track of which change fixed it. "
            "Declaring it fixed without re-running the original reproduction."
        ),
        "verification": (
            "The original reproduction no longer fails, the new regression test passes, and the fix "
            "can be explained as 'X was wrong because Y' - not 'I changed things until it worked'."
        ),
    },
    {
        "name": "test-driven-development",
        "description": "Use when adding new behavior or fixing a bug that needs a regression test.",
        "tags": ["testing", "tdd", "bugfix"],
        "title": "Test-Driven Development",
        "when_to_use": "Before implementing any new function, class, or bugfix that has a testable outcome.",
        "procedure": (
            "1. Write a test that describes the desired behavior and asserts the expected result.\n"
            "2. Run it and confirm it FAILS (proves the test actually exercises the missing behavior).\n"
            "3. Write the minimal implementation that makes the test pass.\n"
            "4. Run the test again and confirm it PASSES.\n"
            "5. Refactor if needed, re-running the test after each change."
        ),
        "pitfalls": (
            "Writing the implementation first and the test after - the test then only proves the code "
            "does what it does, not what it should do. Skipping the red (failing) step, which hides "
            "tests that pass regardless of the implementation."
        ),
        "verification": "The test failed before the implementation and passes after, with no other tests broken.",
    },
    {
        "name": "plan-before-code",
        "description": "Use when a request is non-trivial and the approach is not already obvious.",
        "tags": ["planning", "requirements"],
        "title": "Plan Before Code",
        "when_to_use": "Any request that touches more than one file, or whose intent could be read more than one way.",
        "procedure": (
            "1. Restate the request in your own words - what is being asked and why.\n"
            "2. Identify every file/module the change will touch and what changes there.\n"
            "3. Note anything ambiguous and resolve it (ask, or state the reasonable default you're taking).\n"
            "4. List the test cases that will prove the change works.\n"
            "5. Get confirmation before writing code, when the project's own workflow requires it."
        ),
        "pitfalls": (
            "Starting to edit files before the scope is clear, then discovering the request meant "
            "something else after half the change is already made."
        ),
        "verification": "The plan names every file that will change and the tests that will prove it, before any edit is made.",
    },
    {
        "name": "minimal-diff-discipline",
        "description": "Use for every code change - keep the diff no larger than the task requires.",
        "tags": ["code-review", "scope", "diff"],
        "title": "Minimal Diff Discipline",
        "when_to_use": "Every code change, always - this is a default constraint, not a special case.",
        "procedure": (
            "1. Before editing, decide exactly what the task requires - nothing more.\n"
            "2. Do not refactor, rename, reformat, or restructure code the task did not ask about.\n"
            "3. Do not add abstractions, config flags, or error handling for cases that cannot occur.\n"
            "4. Every changed line should trace back to something the request actually asked for.\n"
            "5. Before finishing, re-read the diff and remove anything that snuck in beyond the task."
        ),
        "pitfalls": (
            "Turning a one-line bugfix into a file-wide cleanup pass. Adding 'while I'm here' changes "
            "that make the diff harder to review and increase regression risk. This principle is "
            "distilled from patterns attributed to Andrej Karpathy's public commentary on AI-assisted "
            "coding - it is not his own published text."
        ),
        "verification": "Every line in the diff maps to a specific part of the request; nothing extra was touched.",
    },
    {
        "name": "verification-before-completion",
        "description": "Use before declaring any task, fix, or feature done.",
        "tags": ["verification", "completion", "quality-gate"],
        "title": "Verification Before Completion",
        "when_to_use": "Before saying a task is complete, always - never declare done on inspection alone.",
        "procedure": (
            "1. Identify an objective check for the change: a test run, a build, or an actual "
            "execution of the changed path.\n"
            "2. Run it and capture the real output - do not assume it would pass.\n"
            "3. If the check fails, fix and re-run - do not report success with a known failure.\n"
            "4. State what was actually run and what it showed, not just 'it should work now'."
        ),
        "pitfalls": (
            "Declaring a fix works because the code 'looks right'. Running a narrow test that doesn't "
            "actually cover the changed behavior and treating it as proof."
        ),
        "verification": "A real command was run after the change, its output is known, and it confirms the behavior.",
    },
    {
        "name": "code-review-before-merge",
        "description": "Use after implementing a change, before calling it finished.",
        "tags": ["code-review", "self-review"],
        "title": "Code Review Before Merge",
        "when_to_use": "After the implementation is complete and tests pass, before reporting the task done.",
        "procedure": (
            "1. Re-read the full diff as if reviewing someone else's PR.\n"
            "2. Check it against the original request - is everything asked for present, and nothing extra?\n"
            "3. Check for the failure modes a reviewer would flag: unhandled edge cases, inconsistent "
            "naming, dead code left behind, missing test coverage for a new branch.\n"
            "4. Fix anything found before reporting completion."
        ),
        "pitfalls": "Treating the first working version as final without a second pass against the request.",
        "verification": "The diff was re-read end to end against the request, and any issues found were fixed.",
    },
    {
        "name": "ui-ux-accessibility-baseline",
        "description": "Use when writing or editing any UI component, screen, or page.",
        "tags": ["ui", "ux", "accessibility", "frontend", "wcag"],
        "scope_hint": "generic",
        "title": "UI/UX Accessibility Baseline",
        "when_to_use": "Any time UI markup or a frontend component is created or modified.",
        "procedure": (
            "1. Every interactive element (button, link, control) is keyboard-reachable and shows a "
            "visible focus state - never mouse-only.\n"
            "2. Every form input has a real associated label (`<label>` or `aria-label`) - never a "
            "placeholder used as the only label.\n"
            "3. Touch/click targets are at least 44x44px.\n"
            "4. Color is never the only signal conveying meaning - pair it with an icon or text; text "
            "contrast meets WCAG 2.2 AA (4.5:1 normal text, 3:1 large text/UI components).\n"
            "5. Use semantic HTML first (`<button>`, `<nav>`, `<table>`) - reach for `<div onClick>` "
            "only when no semantic element fits.\n"
            "6. Respect `prefers-reduced-motion` for any animation.\n"
            "7. Heading hierarchy is sequential, one `<h1>` per view - never skip levels.\n"
            "8. Loading, empty, and error states are explicit, visible UI states - never a silent gap.\n"
            "9. New components match the existing design system's spacing and type scale rather than "
            "inventing new values."
        ),
        "pitfalls": (
            "Building a control that only works with a mouse. Using color alone (e.g. red text) to "
            "signal an error with no icon or text. Skipping heading levels for visual sizing instead "
            "of semantic order. These rules are adapted from widely-used cross-agent UI guideline "
            "sets (e.g. Vercel's Web Design Guidelines) and the WCAG 2.2 AA standard - not copied "
            "verbatim from either."
        ),
        "verification": (
            "Every interactive element is keyboard-operable with visible focus, every input has a "
            "real label, and contrast/target-size rules above are met."
        ),
    },
    {
        "name": "comprehensive-test-authoring",
        "description": "Use when asked to write tests for a class, component, or endpoint and full coverage is wanted.",
        "tags": ["testing", "security", "api-contract", "quality", "architecture"],
        "scope_hint": "generic",
        "title": "Comprehensive Test Authoring",
        "when_to_use": (
            "When the request is to generate thorough test coverage for a unit of code - not just a "
            "single happy-path check."
        ),
        "procedure": (
            "Cover each of these dimensions that applies to the artifact under test:\n"
            "1. Functional: happy path, every branch/condition, boundary values (min/max/empty/null).\n"
            "2. Error handling: invalid input, dependency failure, timeout, partial failure.\n"
            "3. Security (OWASP ASVS-aligned): authentication/authorization checks, input "
            "validation/injection, sensitive-data exposure, rate limiting where applicable.\n"
            "4. Data validation: schema/type enforcement, required-field checks, malformed-payload "
            "rejection.\n"
            "5. API contract: request/response shape verified against its schema, independent of the "
            "implementation's internals.\n"
            "6. Architecture conformance (ISO/IEC 25010-mapped): the change respects existing module "
            "boundaries and dependency direction.\n"
            "7. Non-functional spot checks: one performance/load-shape test where the artifact is "
            "performance-sensitive; one accessibility check where it renders UI.\n"
            "8. Regression guard: a test that would have failed before the fix/feature and passes now.\n"
            "Pick the coverage shape by artifact type: pyramid-shaped (many unit, some integration, "
            "few end-to-end) for backend/service classes with deep logic; trophy-shaped "
            "(integration-heavy) for UI components where the unit/integration boundary is thin."
        ),
        "pitfalls": (
            "Writing only happy-path tests and calling it 'full coverage'. Testing implementation "
            "details instead of the API contract, which breaks tests on every internal refactor. "
            "Applying pyramid shape to UI code or trophy shape to a pure backend service - the shape "
            "should match the artifact, not be applied uniformly everywhere."
        ),
        "verification": (
            "Each applicable dimension above has at least one test, and at least one test would have "
            "failed against the pre-fix/pre-feature code."
        ),
    },
    {
        "name": "sonar-quality-review",
        "description": "Use when reviewing or acting on SonarQube findings for a project.",
        "tags": ["sonar", "code-quality", "static-analysis"],
        "title": "SonarQube Quality Review",
        "when_to_use": "Any time SonarQube findings, quality gate status, or code smells are being reviewed or fixed.",
        "procedure": (
            "1. Start with the quality gate status and severity breakdown, not the raw issue list.\n"
            "2. Triage by severity and blocker/critical status first - do not fix low-severity items "
            "before higher ones are addressed.\n"
            "3. For each finding, fix the actual root cause the rule is checking for - never suppress "
            "or annotate away a real issue just to clear the count.\n"
            "4. After fixing, re-check the quality gate/measures to confirm the fix actually moved the "
            "metric, not just silenced the specific line."
        ),
        "pitfalls": (
            "Adding a suppression comment instead of fixing the underlying issue. Fixing issues in an "
            "arbitrary order instead of by severity. Not re-checking the gate after fixing, so a fix "
            "that didn't actually resolve the rule goes unnoticed."
        ),
        "verification": "The quality gate/measures were re-checked after the fix and show the issue resolved.",
    },
    {
        "name": "ticket-context-analysis",
        "description": "Use when starting work on a work-tracker ticket (Jira or similar).",
        "tags": ["jira", "tracker", "requirements"],
        "title": "Ticket Context Analysis",
        "when_to_use": "Before writing any code in response to a ticket key or tracker issue URL.",
        "procedure": (
            "1. Fetch the full ticket - description, comments, and every attachment - never work from "
            "the title alone.\n"
            "2. Read any linked issues or referenced tickets that add context to the requirement.\n"
            "3. Identify the actual acceptance criteria, not just the summary line.\n"
            "4. If the ticket is ambiguous about scope, resolve it before starting - do not guess."
        ),
        "pitfalls": (
            "Starting work from just the ticket title, missing a requirement buried in a comment or "
            "attachment. Ignoring a linked ticket that changes the scope of the current one."
        ),
        "verification": "The implementation addresses every acceptance criterion found in the ticket's full content, not just its title.",
    },
    {
        "name": "safe-git-workflow",
        "description": "Use for any git operation on a repository - status, commit, merge, or conflict resolution.",
        "tags": ["git", "workflow", "safety"],
        "title": "Safe Git Workflow",
        "when_to_use": "Before any git operation - status check, commit, merge, or conflict resolution.",
        "procedure": (
            "1. Check the repository's current state first (branch, dirty tree, leftover state from "
            "an interrupted prior run) before any other git action.\n"
            "2. Never force-push, and never discard uncommitted work without confirming it is not needed.\n"
            "3. When a merge conflict appears, resolve it - never discard one side to make it go away.\n"
            "4. Stage and commit exactly the intended files, never a wildcard that could catch "
            "unrelated changes.\n"
            "5. Confirm with the user before any destructive or hard-to-reverse step."
        ),
        "pitfalls": (
            "Force-pushing over work that hasn't been reviewed. Resolving a conflict by discarding "
            "changes instead of merging them. Staging everything with a wildcard and accidentally "
            "committing unrelated or sensitive files."
        ),
        "verification": "The repo's status was checked first, only the intended files were staged, and no destructive command ran without confirmation.",
    },
    {
        "name": "codebase-graph-navigation",
        "description": "Use before editing code that other parts of the codebase might depend on.",
        "tags": ["graph", "architecture", "impact-analysis"],
        "title": "Codebase Graph Navigation",
        "when_to_use": "Before modifying a function, class, or module that could have callers elsewhere in the codebase.",
        "procedure": (
            "1. Check the blast radius of the change - what else calls or depends on the code being "
            "edited.\n"
            "2. Check for cycles or ownership boundaries the change might cross.\n"
            "3. If the change touches widely-used or central code, review every caller's expectations "
            "before changing the signature or behavior.\n"
            "4. Never edit shared code blind, assuming it has no other callers."
        ),
        "pitfalls": (
            "Changing a widely-used function's behavior without checking who else calls it, breaking "
            "callers that were never looked at."
        ),
        "verification": "The blast radius was checked before the change, and every affected caller still behaves correctly after it.",
    },
    {
        "name": "testing-session-driver",
        "description": "Use when testing an app, screen, UI flow, or API - never hand-roll tests when a testing session tool is available.",
        "tags": ["testing", "qa", "automation"],
        "title": "Testing Session Driver",
        "when_to_use": "Any request to test a screen, UI flow, or API endpoint when a testing session tool is connected.",
        "procedure": (
            "1. Start a testing session rather than writing ad hoc test scripts by hand.\n"
            "2. Let the census/discovery step establish what actually exists on the screen or endpoint "
            "before authoring any test.\n"
            "3. Author the test flow from the discovered structure, not from assumptions about the UI.\n"
            "4. Run the verify/heal loop to confirm the authored test actually passes against the real "
            "target before considering it done."
        ),
        "pitfalls": (
            "Writing tests against assumed selectors or endpoints instead of what discovery actually "
            "found, producing tests that pass without truly exercising the target."
        ),
        "verification": "The test was authored from real discovered structure and passed the verify/heal loop against the live target.",
    },
    {
        "name": "memory-effective-usage",
        "description": "Use throughout any task where a memory/knowledge system is available.",
        "tags": ["memory", "knowledge", "learning"],
        "title": "Effective Memory Usage",
        "when_to_use": "At the start of a task (search for prior relevant work) and at the end (save what was learned).",
        "procedure": (
            "1. Search memory for related prior work before implementing, so past decisions and fixes "
            "aren't repeated blind.\n"
            "2. If a past memory entry is used, reinforce it so future ranking reflects it was useful.\n"
            "3. After the work is confirmed correct, save what was learned with the real outcome, not "
            "a guess at whether it worked."
        ),
        "pitfalls": (
            "Skipping the search step and re-solving a problem that was already solved before. Saving "
            "a memory entry before the fix is actually confirmed working."
        ),
        "verification": "Memory was searched before implementation, and any save reflects a confirmed, verified outcome.",
    },
]
