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
        "when_to_use": "Before writing any code in response to a ticket key or tracker issue URL - fetch this in full (icx_skill_get) right after calling jira_analyze_issue/jira_analyze_issue_fast, before presenting anything to the user.",
        "procedure": (
            "1. Fetch the full ticket - description, comments, and every attachment - never work from "
            "the title alone (jira_analyze_issue_fast for text-only/quick triage, jira_analyze_issue for "
            "full vision/OCR on image attachments - both share this workflow). Read any linked issues "
            "that add context. Identify the actual acceptance criteria, not just the summary line. If "
            "ambiguous about scope, resolve it before starting - do not guess.\n\n"
            "FULL TOOL SEQUENCE (in order):\n"
            "  [1]  jira_analyze_issue_fast / jira_analyze_issue  <- entry point\n"
            "  [2]  memory_search          MANDATORY after analysis - search with agent-generated tags\n"
            "  [3]  graph_important_nodes  architectural hotspots - call first on unfamiliar codebase\n"
            "  [4]  graph_find_context     MANDATORY - replaces grep/glob entirely\n"
            "  [5]  graph_subsystem        expand one file to its full feature cluster\n"
            "  [6]  graph_ownership        who owns these files - call when crossing team boundaries\n"
            "  [7]  graph_call_chain       trace data flow through a specific component\n"
            "  [8]  graph_impact           MANDATORY before changing shared code\n"
            "  [9]  graph_cross_links      microservices only - SKIP for monolith projects\n"
            "  [10] graph_blast_radius     MANDATORY before committing - full scope + missing changes\n"
            "  [11] graph_cycles           circular dependency audit - call when debugging imports\n"
            "  [12] graph_dead_code        unused module detection - call during cleanup\n"
            "  [13] memory_get_hotspots    fragile file ranking - call at start of investigation\n"
            "  [14] memory_find_by_file    MANDATORY before editing each file\n"
            "  [15] memory_get_related     hidden coupling - call after finding bug location\n"
            "  [16] memory_get_patterns    systemic analysis - call for recurring bug categories\n"
            "       --- LOCK THE PLAN before writing any code ---\n"
            "  [17] icx_lock_plan          MANDATORY - submit the files you will change; blocks on any "
            "high-signal file you missed (fuses graph+grep+semantic+memory)\n"
            "       --- implement fix here, only after icx_lock_plan returns ok AND explicit user approval ---\n"
            "       --- ask: \"How would you like to test? 1. automated  2. manual\" ---\n"
            "  [18] testing_start_session  begin test session (pass test_mode from user's answer)\n"
            "  [19] testing_resume_session respond to every gate in sequence until done: true (a "
            "\"status\":\"running\" reply means poll testing_get_session_status instead of resuming again "
            "- see the testing-session-driver skill for the full gate protocol)\n"
            "       --- after testing confirms fix works ---\n"
            "  [20] reinforce_memory_usage MANDATORY first if any memory_search result influenced your approach\n"
            "  [21] save_memory            MANDATORY - only after testing confirms fix works - always after [20]\n"
            "  [22] icx_draft_skill        MANDATORY - immediately after [21], every time, even if skill_worthy=false\n"
            "  [23] get_memory_audit       diagnostic only - when investigating why a result ranks unexpectedly\n\n"
            "RULE 1 - NO CODE BEFORE APPROVAL: do not write a single line of code, edit a file, run a "
            "command, or begin implementation until you have presented the confirmation format below AND "
            "received an explicit \"yes\"/\"proceed\" from the user.\n\n"
            "RULE 2 - MANDATORY CONFIRMATION FORMAT - after reading the relevant files, output this "
            "format exactly and then STOP (no commentary before or after):\n"
            "---\n"
            "**Problem understood:** [1-2 sentence summary drawn from work_item.analysis]\n"
            "**Goal:** [acceptance_criteria as bullet points, or problem_summary for bugs]\n"
            "**Approach:** [your specific solution plan - exactly what you will change, add, or remove, "
            "and precisely why; specific enough that the user can reject it and propose an alternative "
            "before you write a single line]\n"
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
            "  Any entry left blank or skipped with a vague reason is a violation.\n"
            "**Shall I proceed?**\n"
            "---\n\n"
            "RULE 3 - APPROVAL GATE: only an explicit yes/proceed from the user allows implementation to "
            "begin. Silence, partial responses, or ambiguous replies do not count. If unclear, ask again.\n\n"
            "RULE 3b - SPEC-LOCK BEFORE CODE: after deciding which files to change and BEFORE writing any "
            "code, call icx_lock_plan with those files - it returns high-signal files you missed "
            "(graph/grep/semantic/memory). Do not write code until it returns ok; resolve each "
            "blocking_missed file by including it or justifying it.\n\n"
            "RULE 4 - APPROACH CHANGE: if the user requests a different approach, present the revised "
            "plan using the same confirmation format and wait for approval again before implementing it.\n\n"
            "RULE 5 - TESTING GATE: after implementation, ask \"How would you like to test this fix? "
            "1. automated - ICX runs the local verification for you  2. manual - you run it yourself and "
            "confirm the result.\" Do not call reinforce_memory_usage or save_memory yet. Call "
            "testing_start_session [18] with the chosen test_mode, then resume all gates [19] through "
            "done: true. After the session completes: show a summary, ask \"Shall I save this to ICX "
            "memory? (yes/no)\" and WAIT. If yes: call reinforce_memory_usage [20] first (if a "
            "memory_search result influenced the approach), then save_memory [21] with all required "
            "fields, then icx_draft_skill [22] immediately after (mandatory even if skill_worthy=false). "
            "If no: stop, do not call save_memory.\n\n"
            "RULE 6 - MANDATORY TOOL COMPLETENESS: before presenting the confirmation format, every tool "
            "whose skip condition is NOT met must already have been called:\n"
            "  graph ready AND memory ready:     9+ tools mandatory (items 1-6, 8-11; item 7 by project type)\n"
            "  graph ready AND memory not ready: 4+ tools mandatory (items 3-6; item 7 by project type)\n"
            "  graph not ready AND memory ready: 5 tools mandatory (items 1, 8, 9, 10, 11)\n"
            "  neither ready:                    grep/glob only, none of items 1-11 apply\n"
            "memory_search (1) skips only when memory not ready. graph_find_context (3) has NO valid "
            "skip when graph is ready. memory_get_hotspots (8)/memory_get_patterns (11) skip only when "
            "memory not ready - never based on memory_search's result count. memory_get_related (10) "
            "skips only when memory not ready; pass files from graph_find_context (works for new "
            "tickets too). The 10-entry checklist is the evidence record - every entry needs a real "
            "result or an exact skip condition from this rule."
        ),
        "pitfalls": (
            "Starting work from just the ticket title, missing a requirement buried in a comment or "
            "attachment. Ignoring a linked ticket that changes the scope of the current one. Writing "
            "code or making file edits before the user has explicitly approved the confirmation format. "
            "Skipping a graph/memory tool whose skip condition (RULE 6) was not actually met. Calling "
            "save_memory before the testing flow reaches done: true and the user explicitly confirms. "
            "Presenting a vague skip reason (\"not applicable\", \"seems fine\") instead of the exact "
            "technical condition."
        ),
        "verification": (
            "The implementation addresses every acceptance criterion found in the ticket's full content, "
            "not just its title; the confirmation format was shown and explicitly approved before any "
            "code was written; the 10-entry tool checklist shows a real result or an exact skip condition "
            "for every entry; save_memory (and icx_draft_skill immediately after) only happened after "
            "testing reached done: true and the user explicitly confirmed."
        ),
    },
    {
        "name": "safe-git-workflow",
        "description": (
            "Drives any git/GitLab operation on a repository through ICX's own tools end-to-end - "
            "status, branching, staging/committing, stashing, fetch/pull/sync, reverse-merge, "
            "conflict inspection/resolution, MR creation, tagging, branch deletion, or "
            "dependency-pin diagnosis. Use for the full workflow, not just one step."
        ),
        "tags": ["git", "workflow", "safety", "conflict", "dependency-pins", "gitlab"],
        "title": "Safe Git Workflow",
        "when_to_use": (
            "Before any git or GitLab operation - status check, branch creation, commit, stash, "
            "sync, reverse-merge, conflict resolution, MR, tag, branch deletion, file restore, or "
            "dependency-pin diagnosis."
        ),
        "procedure": (
            "0. Every git_*/gitlab_* tool below except git_repo_status itself is reached via "
            "icx_call_tool(tool_name, arguments) now, not called directly by name - tools/list "
            "only advertises a small core set. Use icx_find_tools(module='git') or "
            "icx_find_tools(module='gitlab') first to get each tool's real name/schema, then call "
            "it through icx_call_tool - everything below still applies exactly as written, just "
            "reached that way.\n"
            "1. Always call git_repo_status first, before any other git_* tool and before running "
            "any raw git command - never skip this even if you think you know the state. It reports "
            "current branch, staged/unstaged/untracked/deleted/renamed/conflicted files, "
            "ahead/behind/upstream, and leftover state (scratch branches, ICX stashes, "
            "merge-in-progress) from an interrupted prior run.\n"
            "1a. Two DISTINCT confirmation mechanisms exist - never conflate them. TYPE A (target-"
            "branch consensus): status='confirm_remembered'/'needs_confirmation'/'needs_manual_pick' "
            "on git_start_branch/git_reverse_merge/git_create_mr/git_finish_ticket whenever "
            "parent_branch is omitted - asks the human EVERY call, even if a value was confirmed for "
            "this repo before (confirm_remembered offers it back as a one-tap proposed_default, it "
            "is never silently reused). TYPE B (do-it-or-not, confirm_token): the two-call pattern on "
            "every destructive tool (stage_and_commit, push, create_mr, create_tag/retag/delete_tag, "
            "delete_branch, finish_ticket, restore_files, stash_drop, repin_dependency, every "
            "conflict-resolution tool) - first call with no token shows the human exactly what will "
            "happen and returns a one-time token, second call with that token executes. "
            "git_create_mr and git_finish_ticket carry BOTH types layered; git_reverse_merge carries "
            "ONLY type A - it executes the merge (or quarantines to a scratch branch on conflict) as "
            "soon as parent_branch resolves, with no separate confirm_token step; its backup-branch "
            "safety net is what stands in for that.\n"
            "2. Starting work on a NEW branch: git_start_branch (never `git checkout -b`) - pass "
            "ticket_key, or null plus project_code for a ticketless branch (project_code has no "
            "remembered/derived default - if omitted, returns status='needs_project_code' and you "
            "must ask the human, never invent one). Produces feature/<slug>-<ticket_key> or "
            "feature/<slug>-<PROJECT_CODE>-0000 - the branch NAME ITSELF is always agent-proposed, "
            "human-approved, never silently invented. If the repo has "
            "require_ticket_in_branch_name enabled (git_check_branch_name_policy/"
            "git_set_branch_policy), a ticketless name (including the -0000 placeholder, which never "
            "counts as a real ticket) is refused before anything is created locally.\n"
            "2a. Switching to a branch that ALREADY EXISTS by its EXACT name: git_checkout_branch, "
            "not git_start_branch - git_start_branch always derives/slugifies/prefixes the name it's "
            "given (e.g. summary_or_preferred_name='development' becomes 'feature/development', a "
            "NEW unwanted branch, not a checkout of the real 'development'). git_checkout_branch "
            "switches verbatim - local if it exists, or fetches and tracks it if it exists only on "
            "the remote - and auto-stashes a dirty tree rather than refusing or losing anything (the "
            "stash is left in place, retrieve it afterward with git_stash_apply/git_stash_pop, never "
            "auto-popped onto the target branch).\n"
            "3. Dirty tree before syncing: git_stash_create sets changes aside (never raw `git "
            "stash`); git_stash_list/git_stash_apply/git_stash_pop retrieve them; git_stash_drop "
            "(confirmation-gated) permanently discards one.\n"
            "4. Updating from remote: git_fetch (download only, never touches the working tree), "
            "git_pull (integrates the CURRENT branch's own remote counterpart, strategy='ff-only' "
            "safe default or 'merge' for a real conflict-capable merge), or git_sync (one-shot: "
            "fetch+stash+merge+restore, for a bare 'just sync me' request - always uses "
            "strategy='merge' internally). Use git_reverse_merge instead when bringing in a "
            "DIFFERENT parent/target branch into a feature branch.\n"
            "5. Staging/committing: git_stage_and_commit with an explicit file list, never a "
            "wildcard. ticket_key is nullable everywhere it appears - pass null rather than "
            "inventing one to satisfy a schema. On success, the continuous backup-latest/<key> "
            "pointer moves to the new commit automatically (see step 8's backup-tier note).\n"
            "6. Conflicts are inspected and resolved the same way regardless of what caused them - "
            "ICX's own reverse-merge/pull/sync, a manual merge/pull, a rebase, or a cherry-pick: "
            "git_get_conflict_details for base/ours/theirs plus per-hunk line numbers, then "
            "git_conflict_take_ours/git_conflict_take_theirs/git_conflict_apply_resolution to fix "
            "one file, git_conflict_mark_resolved to stage (hard-blocks if any marker remains or "
            "the file isn't actually conflicted), then git_stage_and_commit to commit - each step "
            "stays separate and gated, never combined into one call. git_conflict_abort backs out "
            "of a merge/cherry-pick/rebase entirely when the human wants to abandon it.\n"
            "7. Pushing/MR: git_push to share progress without an MR, git_create_mr to open (or "
            "reuse) one and attempt an immediate merge - a refusal right after creation is polled "
            "since GitLab computes mergeability asynchronously; merge_status tells you "
            "MERGEABLE/CONFLICTED/CHECKING/BLOCKED/UNKNOWN, never treat a transient CHECKING as a "
            "real failure.\n"
            "8. Cleanup once a merge is independently confirmed: git_finish_ticket - fast-forwards "
            "the parent branch, then UNCONDITIONALLY deletes the feature branch locally AND on the "
            "GitLab remote (an already-gone remote branch, e.g. from GitLab's own "
            "remove-source-branch-on-merge, counts as success, not an error), plus BOTH backup tiers "
            "for this ticket: the continuous backup-latest/<key> pointer (moved on every commit, "
            "step 5) and any timestamped backup/<key>-<timestamp> snapshots (created before every "
            "reverse-merge/conflict-resolution attempt). No opt-in flag needed - the confirm_token "
            "step already shows the human everything about to go. git_delete_branch is for any OTHER "
            "branch removal - it refuses a branch with commits unreachable from the target unless "
            "force=true, and refuses the current branch unconditionally.\n"
            "9. Discarding local changes to specific files only (not a full sync): "
            "git_restore_files - confirmation-gated, shows the exact diff about to be lost, mode "
            "picks worktree/staged/both.\n"
            "10. Diagnosing a stale pinned dependency (package.json/requirements.txt/pyproject.toml "
            "pinning a package to a git ref): git_check_dependency_pins."
        ),
        "pitfalls": (
            "Running a raw git command (checkout/stash/fetch/pull/checkout --ours|--theirs/add on a "
            "conflicted file/merge|rebase|cherry-pick --abort/restore/branch -D/push --delete) "
            "instead of the matching ICX tool bypasses every safety net ICX provides, even when a "
            "native connector or another integration also offers it. Using git_start_branch to "
            "switch to an existing branch by name instead of git_checkout_branch - the name gets "
            "derived/prefixed, creating an unwanted new branch instead of checking out the real one. "
            "Inventing a ticket key OR a project_code to satisfy a tool schema instead of asking the "
            "human (project_code has no remembered default - it is asked fresh every ticketless "
            "branch, deliberately, unlike parent_branch which offers a one-tap remembered default). "
            "Assuming git_reverse_merge has the same confirm_token gate git_create_mr/"
            "git_finish_ticket have - it doesn't; only parent_branch selection is confirmed there. "
            "Treating git_create_mr's transient CHECKING mergeability as a real failure. "
            "Force-pushing or rebasing to rewrite history - never available through ICX by design. "
            "Discarding a conflict by blindly taking one side without actually reviewing "
            "base/ours/theirs first."
        ),
        "verification": (
            "git_repo_status was called first (and again after any state-changing step) and shows "
            "the expected clean/staged/conflict state; only the intended files were touched; every "
            "TYPE B destructive step (stash drop, branch delete, conflict resolution, file restore, "
            "MR creation, finish_ticket cleanup) went through its confirm_token gate with the "
            "human's explicit agreement shown first; every TYPE A branch-selection step showed the "
            "human the actual proposed_default/available_branches rather than assuming one silently."
        ),
    },
    {
        "name": "git-repository-safety-checks",
        "description": (
            "Detects unsafe repository states before a git operation proceeds - dirty tree, "
            "untracked files, uncommitted changes, unpushed commits, branch divergence, unresolved "
            "conflicts, unsafe branch deletion, or accidental staging. Use as a pre-flight check "
            "inside any git workflow (see safe-git-workflow), not a replacement for it."
        ),
        "tags": ["git", "safety", "pre-flight", "branch-deletion", "conflict"],
        "title": "Git Repository Safety Checks",
        "when_to_use": (
            "Before any git operation that could lose work or affect a shared branch - "
            "reverse-merge/pull/sync with a dirty tree, deleting a branch, staging files during "
            "conflict resolution, discarding local changes, or abandoning a merge/cherry-pick/"
            "rebase."
        ),
        "procedure": (
            "1. Working-tree/index state: read git_repo_status's staged/unstaged/untracked/deleted/"
            "renamed/conflicted fields individually - never assume a single 'dirty' boolean tells "
            "you what kind of dirty it is.\n"
            "2. Unpushed commits / divergence: git_repo_status's ahead/behind are both relative to "
            "upstream - ahead>0 means local commits the remote doesn't have yet; behind>0 means the "
            "reverse; both non-zero means diverged. git_pull(strategy='ff-only') correctly refuses "
            "on divergence rather than silently creating a merge commit - read its status before "
            "assuming a plain pull will work.\n"
            "3. Before deleting ANY branch: git_delete_branch itself computes unique_commits "
            "(commits unreachable from the target branch) and refuses outright, before issuing a "
            "token, unless force=true - never run `git branch -D`/`git push --delete` to sidestep "
            "this check, and never pass force=true without showing the human the unique_commits "
            "count first.\n"
            "4. Before staging during conflict resolution: git_conflict_mark_resolved hard-blocks "
            "staging any file that still has literal conflict-marker text, or that isn't actually "
            "an unmerged/conflicted path - reuse it instead of a bare git_stage_and_commit whenever "
            "a conflict is in progress.\n"
            "5. Before discarding local changes to specific files: git_restore_files always returns "
            "a real diff of what would be lost and requires confirm_token - never treat this as "
            "safe-by-default just because it's 'only files, not a branch.'\n"
            "6. Before abandoning a merge/cherry-pick/rebase entirely: git_conflict_abort's "
            "pending_confirmation names which operation and which conflicted files are about to be "
            "abandoned - review that list even if you think you already know, since it reflects the "
            "CURRENT real repo state, not necessarily what you expect (the operation may not have "
            "been started by the current session at all).\n"
            "7. Leftover state from an interrupted prior run - scratch branches, ICX-tagged stashes, "
            "a merge still in progress - is reported by git_repo_status every time. Resolve or "
            "discard it (git_discard_scratch/git_conflict_abort) before starting new work on the "
            "same branch rather than layering a new operation on top of an unresolved one.\n"
            "8. Force-push and rebase are never available through ICX by design, except "
            "git_conflict_abort's own narrow, history-preserving `git rebase --abort` (which only "
            "ever backs one out, never drives one) - if a workflow seems to require either, stop "
            "and ask the human rather than reaching for raw git."
        ),
        "pitfalls": (
            "Trusting a single dirty:true/false boolean instead of reading which files are staged "
            "vs unstaged vs conflicted. Deleting a branch with raw `git branch -D` specifically to "
            "sidestep git_delete_branch's unreachable-commit check. Reading only 'ahead' or only "
            "'behind' and assuming that tells the full divergence story. Force-pushing or rebasing "
            "because a raw git command felt faster than finding the right ICX tool. Passing "
            "force=true to git_delete_branch without having shown the human the actual "
            "unique_commits count first."
        ),
        "verification": (
            "Every destructive action was preceded by actually reading the relevant preview field "
            "(git_repo_status's status fields, git_get_conflict_details's conflict_state, "
            "git_delete_branch's unique_commits, git_restore_files's diff, git_conflict_abort's "
            "operation/conflicted_files) rather than assuming, and no raw git command bypassed the "
            "matching ICX safety check."
        ),
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
        "when_to_use": "Any request to test a screen, UI flow, or API endpoint when a testing session tool is connected - fetch this in full (icx_skill_get) before responding to any testing_resume_session gate you have not already seen the spec for in this conversation.",
        "procedure": (
            "0. Start a testing session rather than writing ad hoc test scripts by hand: "
            "testing_start_session (RULE 0 there: ASK the user automated-vs-manual BEFORE any tool "
            "call). Let the census/discovery step establish what actually exists before authoring any "
            "test; author from that discovered structure, not assumptions about the UI; run the "
            "verify/heal loop to confirm the authored test actually passes before considering it done.\n\n"
            "AUTOMATED TOOL SEQUENCE (exact call order):\n"
            "  [1] testing_start_session\n"
            "  [2] testing_resume_session  Gate 1 (\"expand\") - file list confirmation\n"
            "  [3] testing_resume_session  Gate 2b / author_flow - AI authors the test flow (AGENT-GENERATE)\n"
            "  [4] testing_resume_session  Gate 3 - layer selection + config (URL MUST be user-confirmed)\n"
            "       --- ICX runs the local verification suite (unit/api/ui), no external tester ---\n"
            "  [5] testing_resume_session  Gate 4 - show issues, propose fixes to user\n"
            "  [6] testing_resume_session  Gate 5 - user confirms fixes applied (loop 4-6 until issues=0 or Limit Gate)\n"
            "  [7] testing_resume_session  Gate ui_check - MANDATORY\n"
            "  [8] testing_resume_session  Gate memory_save - MANDATORY\n\n"
            "MANUAL TOOL SEQUENCE (exact call order):\n"
            "  [1] testing_start_session(test_mode=\"manual\")\n"
            "  [2] testing_resume_session  Gate 1 (\"expand\") - file list confirmation\n"
            "  [3] testing_resume_session  Gate manual - user runs test, confirms done\n"
            "  [4] testing_resume_session  Gate manual_result - user reports result + issues\n"
            "  [5] testing_resume_session  Gate ui_check - MANDATORY\n"
            "  [6] testing_resume_session  Gate memory_save - MANDATORY\n\n"
            "SIDE GATES (automated path only, fire when conditions are met): Error Gate (\"error\") - a "
            "verification run failed, options retry/skip_iteration/end_session. Limit Gate (\"limit\") - "
            "max iterations reached with issues remaining, options continue (+3 iterations)/end_session.\n\n"
            "PER-GATE RULES (apply across the whole flow, in addition to the per-gate formats below):\n"
            "  RULE 3 - Gate 3 URL is ALWAYS user-confirmed: display the URL, wait for explicit "
            "confirmation. NEVER submit a URL you guessed, constructed, or assumed.\n"
            "  RULE 4 - Gate 4 is display-only, ALWAYS followed by Gate 5: never skip Gate 5 after Gate 4.\n"
            "  RULE 5 - Gate ui_check requires REAL user confirmation: never respond confirmed:true until "
            "the user has explicitly said the UI looks correct.\n"
            "  RULE 6 - Gate memory_save is ALWAYS {\"save\": \"yes\"}: never skip or respond \"no\" "
            "unassisted - the workflow is not complete until this gate is answered.\n"
            "  RULE 7 - confirmed_files at Gate \"expand\" MUST be frontend/UI files only (.js .jsx .tsx "
            ".vue .html) - NEVER backend files (.java .py .go .cs .rb .kt). If the user only provides "
            "backend files, ask which UI screen file(s) test the feature.\n\n"
            "EXACT PER-GATE DISPLAY TEXT AND RESPONSE FORMAT - every field listed is mandatory to show "
            "the user; responding without showing every field is a violation:\n\n"
            "Gate \"mode\" [USER-DECISION]: show \"Select test mode: 1. automated (ICX runs verification) "
            "2. manual (you run it and report). What is your choice?\" Response: {\"choice\": "
            "\"automated\"|\"manual\"}.\n\n"
            "Gate \"pick_type\" [USER-DECISION]: the ONLY gate that asks test type (Gate 3 only confirms "
            "URL later, never re-asks type). Show \"Select test type: 1. agent (you write+run a real "
            "Playwright test, self-heal until it passes, needs a URL) 2. api (REST endpoint test, needs a "
            "URL) 3. unit (repo's own unit tests, no URL/app). What is your choice?\" Response: "
            "{\"test_type\": \"agent\"|\"api\"|\"unit\"}.\n\n"
            "Gate \"known_screen\" [USER-DECISION, agent-type only, rare]: only appears when ICX found a "
            "provably fresh cached clearance of this exact screen (every cached file byte-identical, no "
            "new related file found) - otherwise ICX already moved straight to expand_scan, no action "
            "needed. When it appears: show cached_at/confirmed_files count/functionality_count/coverage, "
            "ask reuse-and-skip-to-confirmation vs redo-from-scratch. Response: {\"decision\": "
            "\"fast_path\"|\"rescan\"} (anything other than fast_path is treated as rescan).\n\n"
            "Gate \"expand_scan\" [AGENT-GENERATE]: yours to produce, do not show to user. Search the repo "
            "for files related to gate.seeds (importers, callers, same-feature components, the route/page "
            "that renders them) using your own tools; gate.graph_expanded already lists what the graph "
            "found, add what it missed. Response: {\"related_files\": [<paths>], \"read_receipts\": "
            "[{\"path\":..., \"line_count\":..., \"last_line\":...}]} - one read_receipt per file you "
            "opened and read fully this step; do not include files already in graph_expanded unless "
            "independently confirmed.\n\n"
            "Gate \"expand\" [USER-DECISION]: ICX expanded the seed file(s) into the full related screen "
            "set. If gate.graph_available is false, expand the seeds yourself by grep (each seed's own "
            "imports plus repo-wide importers, 1-2 hops) before asking. Show changed_files, "
            "expanded_files, graph_available (and any grep-expanded files if graph unavailable); state "
            "the UI layer needs FRONTEND files only, never backend. Response: {\"confirmed_files\": "
            "[\"abs/path/Screen.jsx\", ...], \"url\": \"<optional>\"} - this list IS the file set for "
            "every later gate; an omitted file will not reappear; resuming without confirmed_files keeps "
            "the full candidate list.\n\n"
            "Gate \"analyze_screen\" [AGENT-GENERATE]: apply the framework-specific analyzer prompt in "
            "gate.analyzer_prompt to the confirmed files, return its strict JSON census wrapped as "
            "{\"screen_model\": {...}} enumerating every interactive element/field/validation/message "
            "with reconciled counts (coverageReport.reconciliation). ICX lints structurally and re-asks "
            "on hard defects: CREATE and EDIT/MODIFY need different submit selectors, every create/edit "
            "form needs its own submit+trigger, every field needs a domSelectors/selector, no duplicate "
            "functionality ids. Capture length/format constraints (maxLength/minLength/min/max/pattern, "
            "type email/tel/url/number) from the code. Response: {\"screen_model\": {...}, "
            "\"read_receipts\": [...]}.\n\n"
            "Gate \"unit_author\" [AGENT-GENERATE]: given the Element Census (gate.screen_model) "
            "enumerating every testable unit/routine/function, write comprehensive tests (happy path + "
            "edge/invalid/error cases + every validation) using your editor, in the repo, in the "
            "framework named in gate.message (GoogleTest/Catch2, utPLSQL/tSQLt/pgTAP, "
            "pytest/JUnit/jest/go test/cargo/rspec/phpunit). Response: {\"read_receipts\": [...]}.\n\n"
            "Gate \"compat_scan\" [AGENT-GENERATE]: yours to produce, do not show to user. Read every "
            "file in gate.file_paths completely; reason from first principles about everything a test "
            "must do for gate.test_type (reach the screen, locate/see/interact with each control, "
            "observe results) - no fixed checklist, anything that could stop a deterministic test is in "
            "scope. Never defer to the runner's tolerance (\"probably works\"/\"should be ok\" are not "
            "verdicts - uncertain means it's a finding). Report every concern as a finding, do not "
            "silently decide; the user resolves each at compat_check. Response: {\"all_compatible\": "
            "true|false, \"findings\": [{\"path\":..., \"compatible\":..., \"reasons\": [\"path:line "
            "detail\"], \"required_changes\": [\"concrete edit\"]}], \"read_receipts\": [...]}.\n\n"
            "Gate \"compat_check\" [USER-DECISION]: present the compat_scan findings; user decides "
            "approve (you already applied required_changes, ICX re-scans) or reject (per-file: "
            "drop/manual/accept). Response (approve): {\"decision\": \"approve\", \"edited_files\": "
            "[...]}. Response (reject): {\"decision\": \"reject\", \"resolution\": {\"<path>\": "
            "\"drop\"|\"manual\"|\"accept\"}}.\n\n"
            "Gate \"2\" [automated only, USER-DECISION]: show detection mode (1 auto_detect - Playwright "
            "scans live page, app must be running; 2 json_spec - AI reads source directly, no browser, "
            "use when URL needs VPN/auth), scope (1 ticket - recommended, changed functionality only; 2 "
            "full - whole screen), merge_files (only when >1 file), and URL (required for auto_detect). "
            "Response: {\"mode\":\"1\"|\"2\"|\"auto_detect\"|\"json_spec\", \"scope\":\"1\"|\"2\"|"
            "\"ticket\"|\"full\", \"merge_files\":\"1\"|\"2\"|true|false, \"url\":\"http://...\"}.\n\n"
            "Gate \"2a\" [auto_detect only, USER-DECISION, fires before 2b]: show URL, page_title, "
            "detected field groups; confirm URL is correct and fields look right. Response: {\"url\": "
            "\"<confirmed url>\"}.\n\n"
            "Gate \"2b\" [AGENT-GENERATE, both modes] - STRICT MANDATORY RULES:\n"
            "  - Read EVERY file in gate.file_paths completely (chunked if >1000 lines) before writing "
            "any JSON - \"I already know\" is never a valid skip.\n"
            "  - Follow gate.rules (from ~/.icx/testing_rules/2b.md) exactly; output MUST include ALL "
            "top-level sections: screenName, fileName, filePath, associatedFiles, moduleName, "
            "description, rootFile{fileName,filePath,describesUrl,containsTriggers[]}, modalFiles[], "
            "techStack{framework,stateManagement,uiLibrary[],notifications[],httpClient,caching}, "
            "functionalitySummaryTable, functionalities[], dependencyGraph, validationMatrix (each entry "
            "errorDisplayMode toast|inline|both), apiMappingSummary (each entry callerFunction), "
            "responseCodeMappingSummary, permissionsMatrix, modalsSummary, notificationsSummary, "
            "inlineErrorsSummary, loaderHandling, selectorAudit (every selector produced must appear). "
            "gate.rules also carries the full per-functionality/per-field key checklist - satisfy it in "
            "full, never simplify/condense/rename/reorder.\n"
            "  - domSelectors MUST contain a working Playwright selector for every field, priority: "
            "#id > [data-testid=\"...\"] > input[placeholder=\"...\"] > input[name=\"...\"] (only if "
            "literally in JSX). Never guess - only use selectors you actually saw.\n"
            "  - Every pushNotify/toast/NotificationManager call site needs a notifications.messages[] "
            "entry with exact text; every field error-state/validation needs an inlineErrors.messages[] "
            "entry with exact text.\n"
            "  - Never produce a simplified/shortcut spec, never use token budget as an excuse to omit "
            "or truncate.\n"
            "  - Before submitting, confirm: every file read, every functionality documented, every "
            "field has a selector, every notification/inline-error has an entry, selectorAudit is "
            "complete, all sections present.\n"
            "  - ICX structurally checks completeness after submission and re-asks with "
            "gate.missing_sections naming exact paths (e.g. functionalities[2].businessLogic) if "
            "anything is missing - regenerate complete, do not argue. Only resume with "
            "accept_incomplete:true if the user has reviewed and knowingly accepted a gap.\n"
            "  Response: {\"json_spec\": \"{...}\", \"read_receipts\": [...]} (add "
            "\"accept_incomplete\": true only after user acceptance of a named gap).\n\n"
            "Gate \"api_manual\" [USER-DECISION, api test type only]: ask endpoint URL, HTTP method, "
            "payload, payload type - never assume or pre-fill any field. Response: {\"api_endpoint\":..., "
            "\"api_method\":..., \"api_payload\":..., \"api_payload_type\": \"json\"|\"form\"|\"none\"}.\n\n"
            "Gate \"3\" [automated only, USER-DECISION]: test type was already chosen at pick_type, don't "
            "re-ask. Show the chosen type's target URL (or note unit needs none), let the user accept or "
            "override layers. For agent type: ask headless (default) vs visible, and if visible ask "
            "slowmo ms (default 1000, 0 when headless). Response: {\"layers\":[...], \"url\":\"http://...\", "
            "\"visible\": true|false, \"slowmo\": 1000}. After this response ICX may return "
            "{\"status\":\"running\"} - poll testing_get_session_status rather than assuming a hang.\n\n"
            "Gate \"auth_gate\" [USER-DECISION]: offer public/reuse/capture/inline. capture calls "
            "icx_ui_auth_capture (opens a real browser for manual login - never ask for credentials in "
            "chat); inline calls icx_ui_auth_inline (app credentials go straight to ICX's browser "
            "process, never into chat history); reuse uses the stored session for this project+host. "
            "Response (after the capture/inline tool itself returns ok): {\"auth_mode\": "
            "\"public\"|\"reuse\"|\"capture\"|\"inline\"}. Port drift: if gate.other_host_sessions is "
            "non-empty, the same project has a session at a different port - offer {\"auth_mode\": "
            "\"reuse\", \"reuse_host\": \"<host>\"}; cookie auth survives the port change but "
            "localStorage/sessionStorage auth does not, so be ready to fall back to capture.\n\n"
            "Gate \"author_flow\" [AGENT-GENERATE, agent test type only]: yours to produce. "
            "gate.screen_model is the COMBINED census (live-DOM crawl fused with source census, "
            "selectors already resolve); gate.rules is the mandatory checklist (CRUD lifecycle, "
            "validation, security, a11y, error-handling, data safety). Write a real Playwright test file "
            "in the repo covering every functionality, run it yourself against ICX's own pinned install "
            "(gate.playwright gives node/env), point the JUnit reporter at gate.report_path, read real "
            "failures and self-heal by fixing your own script (never force a false pass by weakening an "
            "assertion - report a genuine app bug as a finding instead). Use gate.headless/gate.slowmo "
            "for your browser context; if gate.auth_mode is capture/inline/reuse, load "
            "gate.storage_state and go straight to gate.url (do not author login steps) - otherwise "
            "author real login steps from the actual form. Response: {\"report_path\":..., "
            "\"test_file\":..., \"covered\": [...], \"findings\": [...]}.\n\n"
            "Gate \"4\" [automated only, USER-DECISION-ish, display-only]: list every issue "
            "(name/description/severity) with proposed fixes; respond with {} only after presenting "
            "everything - Gate 5 always follows.\n\n"
            "Gate \"5\" [automated only, USER-DECISION]: ask if fixes for this iteration were applied. "
            "Response: {\"approve_iteration\": true|false, \"fixes_applied\": [...]}.\n\n"
            "Gate \"manual\" [manual path, USER-DECISION]: tell the user to run the test manually against "
            "the app now, listing file_paths in scope; wait for them to say done. Response: "
            "{\"done\": true}.\n\n"
            "Gate \"manual_result\" [manual path, USER-DECISION]: ask pass/fail, issues found (if any), "
            "notes. Response: {\"passed\": \"yes\"|\"no\", \"issues\": [...], \"notes\": \"<text>\"}.\n\n"
            "Gate \"ui_check\" [both paths, USER-DECISION]: tell the user to open the app and visually "
            "verify layout/navigation/functionality/error-states for the files tested; wait for explicit "
            "yes/no. Response: {\"choice\": \"yes\"|\"no\", \"notes\": \"<optional>\"}.\n\n"
            "Gate \"memory_save\" [both paths, USER-DECISION]: show the full session summary (files, "
            "mode, result, iterations), ask save yes/no. Response: {\"save\": \"yes\"|\"no\"}.\n\n"
            "Gate \"error\" [automated only, USER-DECISION]: show gate.message and offer "
            "retry/skip_iteration/end_session. Response: {\"choice\":\"1\"|\"2\"|\"3\"|\"retry\"|"
            "\"skip_iteration\"|\"end_session\"}.\n\n"
            "Gate \"limit\" [automated only, USER-DECISION]: show max_iterations reached and remaining "
            "issue count, offer continue (+3 iterations) or end_session. Response: "
            "{\"choice\":\"1\"|\"2\"|\"continue\"|\"end_session\"}."
        ),
        "pitfalls": (
            "Writing tests against assumed selectors or endpoints instead of what discovery actually "
            "found, producing tests that pass without truly exercising the target. Auto-filling or "
            "defaulting a USER-DECISION gate's response instead of asking and waiting. Guessing a "
            "gate's response format instead of matching gate.gate to the exact spec above. Skipping "
            "Gate 5 after Gate 4, skipping ui_check or memory_save, or resuming while status is still "
            "\"running\". Sending backend files as confirmed_files at Gate \"expand\". Producing a "
            "shortened/simplified 2b json_spec instead of the full gate.rules-mandated structure."
        ),
        "verification": (
            "Every USER-DECISION gate was shown in full and the human's actual reply was used (never "
            "auto-filled); every AGENT-GENERATE gate's read_receipts prove a full, fresh read of every "
            "listed file; the response sent for each gate matched that gate's exact JSON shape; ui_check "
            "and memory_save were both answered before the session was considered complete."
        ),
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
