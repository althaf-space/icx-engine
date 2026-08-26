"""MCP tool surface for AI-assisted testing sessions and UI-auth capture. Owns its own
Tool() definitions and dispatch function - mcp_server.py's _list_tools()/_call_tool()
get a few additive lines only, no restructuring."""

from __future__ import annotations

import json

from mcp.types import TextContent, Tool

from icx_engine.skills.hints import attach_skill_hint
from icx_engine.mcp_server import (
    _testing_gate_snapshot, _testing_invoke_tracked, _TESTING_ERRORS, _TESTING_RUNNING,
)

_TESTING_START_TOOL = "testing_start_session"
_TESTING_RESUME_TOOL = "testing_resume_session"
_TESTING_STATUS_TOOL = "testing_get_session_status"
_UI_AUTH_CAPTURE_TOOL = "icx_ui_auth_capture"
_UI_AUTH_INLINE_TOOL = "icx_ui_auth_inline"

_TESTING_START_DESCRIPTION = """\
Begin a testing session for a set of changed files. <- you are here: [2] automated or [1] manual

ICX expands file_paths via the codebase graph (blast_radius + subsystem cluster + co-change partners
+ semantic find_context) and starts a LangGraph session. Gate 1 fires immediately after.

RETURNS: {session_id: "uuid", gate: {gate: 1, ...}}
Pass session_id to ALL subsequent testing_resume_session calls. Never reuse a session_id.
graph_available: false means codebase graph not built - only seed files shown at Gate 1.

file_paths: FRONTEND/UI source SEED file(s) for the screen being tested (.js/.jsx/.tsx/.vue).
  The UI verification layer needs frontend screen files, NEVER backend files
  (.java/.py/.go/.cs). ICX expands whatever seeds you pass; you only need to produce the seed.

  PHASE A - PICK THE SEED UI FILE(S) before calling this tool. Ask the user how to choose:
    "How do you want to pick the UI files to test?
       1. Give me the UI endpoint/route you mapped (e.g. /work-order/broadcast or the screen URL).
       2. Use the files you changed."
    Then resolve seeds by grep (no path guessing):
      - Option 1 (endpoint/route): grep the codebase for the route/path string the user gave;
        the .jsx/.tsx/.js file(s) that reference it are the seeds.
      - Option 2, changed set INCLUDES UI files: those UI files are the seeds.
      - Option 2, changed set is BACKEND-ONLY: grep the changed backend file for its API
        route/path string (e.g. the @RequestMapping / route constant), then grep the UI repo
        for that same string; the UI file(s) that call it are the seeds.
    Pass the resolved frontend seed(s) as file_paths. ICX expands them to the full screen at
    Gate 1 (graph), or you grep imports as a fallback when graph_available is false (see resume).
    NEVER pass backend-only files. If grep finds no UI seed, ask the user for the screen file(s).
context: one-line description of what changed - used for graph expansion and shown at all gates.
test_mode: REQUIRED - "automated" or "manual". You MUST ask the user before calling this tool.
max_iterations: max automated fix loops before Limit Gate. Default from config (3).
nl_intent / acceptance_criteria: optional, seed extra NL/ticket-driven scenarios (agent test_type).

DEFAULT POSTURE - ICX gate data is for the USER to read and decide. You never advance the
workflow on your own except to GENERATE the test spec at Gate 2b. Everywhere else: present the
data, ask the user, wait for their reply, then act on it.

================================================================================
THESE ARE HARD RULES. THEY ARE NOT SUGGESTIONS. VIOLATIONS ARE NOT ACCEPTABLE.
================================================================================

RULE 0 - ASK MODE FIRST, BEFORE ANY TOOL CALL, NO EXCEPTIONS:
When development is complete, you MUST ask the user:
  "Do you want automated testing (ICX runs it) or manual testing (you run it yourself)?"
Do NOT call testing_start_session. Do NOT read any file.
This is the FIRST action. Skipping it or calling any tool before getting the answer is a CRITICAL VIOLATION.

RULE 1 - AUTOMATED PATH: after the user says "automated", call
  testing_start_session(test_mode="automated"). ICX runs the verification suite locally and async;
  there is no external tester and no health check.

RULE 2 - MANUAL PATH: after the user says "manual", call
  testing_start_session(test_mode="manual") directly.

================================================================================

MANDATORY: this tool's response attaches the "testing-session-driver" skill hint - call
icx_skill_get("testing-session-driver") once per session (if not already fetched) for the full
gate-by-gate tool call sequence (automated + manual), the exact per-gate rules (RULE 3-7: no
auto-fill, URL confirmation, ui_check, memory_save, frontend-only file filtering), and the side
gates (error/limit). Do not guess a gate's requirements - fetch the skill.\
"""


_TESTING_RESUME_DESCRIPTION = """\
Resume a paused testing session at the next gate. <- you are here for gates [3]-[10] automated or [2]-[6] manual.

session_id: UUID from testing_start_session. REQUIRED on every call. Never omit.
response: object matching the current gate.gate value exactly. Use ONLY the format for that gate.

MANDATORY: this tool's response also attaches the "testing-session-driver" skill hint - call
icx_skill_get("testing-session-driver") once per session for a consolidated cross-reference of
this same tool-sequence and rule set if you want it summarized in one place, but everything you
need to respond correctly to any gate is already in this description below - do not treat the
skill as a substitute for reading this in full.

================================================================================
GATE POSTURE CLASSIFICATION - THE SINGLE SOURCE OF TRUTH. READ THIS FIRST.
Every gate is exactly ONE of two kinds. How you respond depends entirely on which.
================================================================================

USER-DECISION gates - the answer belongs to the USER. You MUST stop, show every field,
ask, and wait for the user's reply before responding. NEVER auto-fill, default, or assume:
    mode, pick_type, expand, compat_check, 2a, api_manual, 3, auth_gate,
    4, 5, error, limit, manual, manual_result, ui_check, memory_save

AGENT-GENERATE gates - the answer is YOURS to produce. You generate each fully and submit it
directly. You MUST NOT delegate these to the user or ask them to write them:
    2b, compat_scan, author_flow, expand_scan, analyze_screen, unit_author
    (2b: json_spec generation; compat_scan: file compatibility detection; author_flow: write AND RUN a
    real Playwright test yourself, self-healing until the checklist is covered; expand_scan: repo grep
    for related files; analyze_screen: framework Element Census; unit_author: write unit tests from
    the census)

DEFAULT POSTURE - ICX gate data is for the USER to read and decide. You never advance the
workflow on your own except to generate the spec at Gate 2b. Everywhere else: present the
data, ask the user, wait, then act.

================================================================================
THESE ARE HARD RULES. THEY ARE NOT SUGGESTIONS. VIOLATIONS ARE NOT ACCEPTABLE.
================================================================================

RULE 0 - NEVER AUTO-RESPOND TO A USER-DECISION GATE. HUMAN IN THE LOOP IS MANDATORY:
  For every USER-DECISION gate (see classification above):
    1. Display ALL data from gate to the user (file list, options, issues, URL - everything).
    2. Ask the user explicitly what they want.
    3. WAIT for the user to reply.
    4. Only AFTER the user replies: call testing_resume_session with their answer.
  Responding to a USER-DECISION gate using defaults, assumptions, or auto-fill WITHOUT user
  input is a CRITICAL VIOLATION. Even if the answer seems obvious - ASK.
  AGENT-GENERATE gates (2b, compat_scan, author_flow, expand_scan) are the opposite: you produce
  the output yourself and submit it directly - never hand these to the user. Your read, your generation.

RULEBOOK RULE - gate.rules is BINDING, read it every time:
  Many gates include gate.rules - the mandatory rulebook for that gate, loaded fresh from the
  user's ~/.icx/testing_rules/<gate>.md (path in gate.rules_path). This is the source of truth
  and OVERRIDES your assumptions, habits, and memory. Read gate.rules in full on every gate that
  carries it and obey it exactly. The user can edit these files to tighten the rules; treat the
  text you receive as law for that step. Never ignore it because you "already know" the gate.

RE-READ RULE - applies to every AGENT-GENERATE gate (2b, compat_scan, author_flow, expand_scan):
  You MUST open and read every file in file_paths fully, start to end, in that step. Earlier
  reads, summaries, or memory are STALE and forbidden as a basis - read each file again
  completely even if you read it before. Partial reading or relying on memory causes missed
  details and wrong output. Each of these gates requires you to return read_receipts: a list
  of {path, line_count, last_line} for every file you read in this step, as proof of a full read.

RULE 1 - CHECK gate.gate BEFORE RESPONDING:
  Read gate.gate from the previous call's return value.
  Use ONLY the response format listed below for that exact gate value.
  Using the wrong format silently drops fields and corrupts the session. That is a VIOLATION.

RULE 2 - NEVER SKIP A GATE:
  done: false + gate set means a gate is waiting. Call testing_resume_session immediately after
  user input.
  status: "running" (gate is null) means no gate is waiting yet - real browser work is still in
  progress. Call testing_get_session_status(session_id) to poll instead of calling this tool
  again; only come back to testing_resume_session once polling returns an actual gate.
  done: true means the session is over. Stop calling this tool.
  Calling this tool after done: true, calling it again while status is "running", or failing to
  call it when a real gate is waiting, are all VIOLATIONS.

RULE 3 - GATE 3 URL IS ALWAYS USER-CONFIRMED:
  Display the URL to the user. Wait for explicit confirmation.
  NEVER respond to Gate 3 with a URL the user did not confirm. That is a VIOLATION.

RULE 4 - GATE 4 IS DISPLAY-ONLY, ALWAYS FOLLOWED BY GATE 5:
  Gate 4 delivers issues. Show them all to the user and propose fixes.
  Respond with {} and immediately wait for Gate 5. NEVER skip Gate 5 after Gate 4.

RULE 5 - GATE ui_check REQUIRES REAL USER CONFIRMATION:
  Tell the user: "Open the app and visually check the UI. Tell me when it looks correct."
  NEVER respond with {"confirmed": true} until the user has explicitly said the UI looks correct.
  Assuming it looks fine without asking is a VIOLATION.

RULE 6 - GATE memory_save IS ALWAYS {"save": "yes"|"no"}:
  Always respond with {"save": "yes"}. Never skip or respond with {"save": "no"}.
  Skipping memory_save is a VIOLATION.

RULE 7 - confirmed_files AT GATE "expand" MUST BE FRONTEND/UI FILES ONLY:
  The UI verification layer is a UI tester. It uses source files to author test flows for screens.
  confirmed_files MUST be frontend files: .js .jsx .tsx .vue .html
  NEVER include backend files (.java .py .go .cs .rb .kt) in confirmed_files.
  If the user only provides backend files, ask: "Which UI screen file(s) test this feature?"
  Wait for the user to give frontend file paths. Use those.

RULE G-1 - NEVER AUTO-RESPOND TO A USER-DECISION GATE WITHOUT USER INPUT:
  For USER-DECISION gates (see GATE POSTURE CLASSIFICATION): show all gate data to the user,
  ask explicitly, wait for their reply, then respond. "The answer is obvious" is not a valid
  reason to skip asking. Ask anyway. (Gate 2b is AGENT-GENERATE - you produce it yourself.)

RULE G-2 - NEVER SKIP A REQUIRED ACTION IN THE WORKFLOW:
  If the workflow says read files: read them. If it says show options: show them.
  If it says call a tool: call it. "I already know" is NOT a valid skip condition.
  Skipping any required action is a CRITICAL VIOLATION regardless of confidence level.

RULE G-3 - NEVER SUBSTITUTE YOUR OWN JUDGMENT FOR THE INSTRUCTION:
  If the instruction says "generate exhaustive JSON" - generate exhaustive JSON.
  If the prompt says "include ALL fields" - include ALL fields.
  Do not decide what is "enough". Follow the instruction completely as written.
  Deciding the instruction is "mostly done" and stopping early is a CRITICAL VIOLATION.

RULE G-4 - WHEN IN DOUBT, DO MORE NOT LESS:
  If unsure whether a section should be included: include it.
  If unsure whether a selector is correct: extract it from the file and use it.
  If unsure whether a notification needs documenting: document it.
  Omission is always worse than over-inclusion for test spec generation.

================================================================================
GATE DISPLAY REQUIREMENTS - EVERY FIELD LISTED IS MANDATORY TO SHOW TO THE USER.
RESPONDING WITHOUT SHOWING EVERY FIELD = CRITICAL VIOLATION. NO EXCEPTIONS.
================================================================================

Gate "mode" - Test mode selection [USER-DECISION]:
  YOU MUST SHOW THE USER AND WAIT FOR THEIR REPLY - VIOLATION IF SKIPPED:
    "Select test mode:
       1. automated - Full automated pipeline (ICX runs the verification for you).
       2. manual    - You run the test manually and report results.
     What is your choice?"
  WAIT for reply. Response: {"choice": "automated"|"manual"}

Gate "pick_type" - Test type selection [USER-DECISION]:
  This is the ONLY gate that asks the test type. Gate 3 later just confirms the URL - it does NOT
  re-ask the type. Do not ask the user to re-pick the type anywhere else.
  YOU MUST SHOW THE USER AND WAIT FOR THEIR REPLY - VIOLATION IF SKIPPED:
    "Select test type:
       1. agent - YOU write a real Playwright test covering the screen's Element Census, run it
                  yourself, and self-heal until it passes (frontend, needs a URL).
       2. api   - REST endpoint test (backend, needs a URL).
       3. unit  - Run the repo's own unit tests (no URL, no running app).
     What is your choice?"
  WAIT for reply. Response: {"test_type": "agent"|"api"|"unit"}

Gate "known_screen" - Known-screen fast path [USER-DECISION, agent-type only, RARE]:
  This gate ONLY appears when ICX found a PROVABLY FRESH cached clearance of this EXACT screen from
  a prior session - every cached file byte-identical to then, AND a fresh check found no new related
  file. If it does not appear, there was no cache, it was stale, or a new file was found - in every
  one of those cases ICX already moved straight to expand_scan, no action needed from you.
  When it DOES appear, show the user: "Found a prior cleared run of this screen from gate.cached_at
  (gate.confirmed_files count files, gate.functionality_count functionalities, coverage
  gate.census_coverage). Reuse it and skip straight to URL/layer confirmation, or redo file discovery
  and the census from scratch?"
  WAIT for reply. Response: {"decision": "fast_path"|"rescan"}. Anything other than "fast_path" is
  treated as "rescan" - the normal expand_scan/expand/analyze_screen/compat_scan pipeline runs exactly
  as if there had been no cache.

Gate "expand_scan" - Related file discovery [AGENT-GENERATE]:
  This gate is YOURS to produce. Do NOT show it to the user or wait for their reply.
  Search the repository for files related to gate.seeds (importers, callers, same-feature
  components, and the route or page that renders them). Read with your own tools.
  gate.graph_expanded already lists files the graph found; add what the graph missed.
  Resume immediately with your findings.
  Response: {"related_files": [<repo paths>], "read_receipts": [{"path": "<p>", "line_count": <n>, "last_line": "<text>"}]}
  - related_files: list of file paths you found via your own repo search.
  - read_receipts: one entry per file you opened and read fully this step (path, total line count, text of the last line).
  - Do NOT include files already in gate.graph_expanded unless you independently confirmed them.
  - This is your search, not the user's. Produce it and submit it directly.

Gate "expand" - File confirmation [USER-DECISION]:
  ICX has expanded the seed file(s) you passed into the full related screen set
  (blast_radius + subsystem cluster + co-change + find_context).
  WHEN gate.graph_available IS FALSE, ICX could not expand (UI repo graph not built) and only
  the seeds are shown. In that case, BEFORE asking the user, expand the seeds yourself by grep:
  for each seed, grep its own imports AND grep the repo for files that import it (1-2 hops),
  and add those frontend files to the list you present. This is the no-graph fallback.

  YOU MUST SHOW ALL OF THIS TO THE USER - SKIPPING ANY LINE IS A VIOLATION:
    "Files ICX identified:
     Changed (what you modified): <list gate.changed_files one per line>
     Expanded by graph:           <list gate.expanded_files one per line>
     <if graph_available is false: 'Expanded by grep (graph not built): <files you grep-expanded>'>
     Graph available: <gate.graph_available>

     IMPORTANT: the UI verification layer is a UI tester. It needs FRONTEND files (.js .jsx .tsx .vue).
     Do NOT send backend files (.java .py .go .cs .rb .kt) to the UI layer.

     Which frontend/UI screen file(s) should be verified?
     Confirm this set, or add/remove files."
  WAIT for user reply. Use ONLY frontend files they confirm.
  NEVER include backend files in confirmed_files. That is a VIOLATION.
  (Seed selection - endpoint/route, changed UI, or backend->UI grep bridge - happened before
  testing_start_session; see that tool. Here you only expand + confirm.)
  Response: {"confirmed_files": ["abs/path/Screen.jsx", ...], "url": "<optional>"}
  This list IS the file set for the rest of the session - every later gate (analyze_screen,
  compat_scan, ...) only ever sees what you confirm here. Omit a file you want excluded; it will
  NOT reappear later. If you resume without confirmed_files, ICX keeps the full candidate list.

Gate "analyze_screen" - Element Census [AGENT-GENERATE]:
  ICX selected the framework-specific analyzer prompt (gate.analyzer_id / gate.analyzer_family) and
  put its FULL text in gate.analyzer_prompt. APPLY that prompt to the confirmed files and return its
  STRICT JSON census - EXACTLY the schema the prompt defines - wrapped as {"screen_model": {...}}.
  The census enumerates EVERY interactive element, field, validation, and message and reconciles the
  counts (coverageReport.reconciliation). This model is what makes authoring miss NOTHING - a missed
  census element is a missed test. If the reconciliation counts do not add up, ICX re-asks naming the
  shortfall; fix it and resubmit. Read every file fully first (RE-READ RULE).
  ICX ALSO LINTS the census structurally (agent-independent) and RE-ASKS on hard defects, so no agent's
  mistake slips through: (a) CREATE and EDIT/MODIFY must have DIFFERENT submit selectors - never copy
  one onto the other; (b) every create/edit form needs its own submit + trigger; (c) every field needs
  a domSelectors/selector; (d) no duplicate functionality ids. Soft advisories (a text field with no
  captured length/format constraint) are recorded, not blocking - but capture length/format from the
  code (maxLength/minLength/min/max/pattern, type email/tel/url/number) because the save uses them.
  (At authoring time ICX also crawls the LIVE screen and FUSES that discovered census with yours -
  the COMBINED census - so real rendered selectors/wizard-nav back your JS-hidden constraints. You
  only produce the source census here; the live crawl and merge are automatic.)
  Response: {"screen_model": { ...the analyzer prompt's strict JSON... }, "read_receipts": [...]}

Gate "unit_author" - Write unit tests from the census [AGENT-GENERATE]:
  For a unit test, ICX gives you the Element Census (gate.screen_model) enumerating every testable
  unit/routine/function of the module. WRITE COMPREHENSIVE tests covering EVERY one of them - happy
  path + edge/invalid/error cases + every validation - using YOUR editor to create the test files IN
  THE REPO (framework in gate.message, keyed to gate.analyzer_family: GoogleTest/Catch2 for C/C++,
  utPLSQL/tSQLt/pgTAP for SQL, pytest/JUnit/jest/go test/cargo/rspec/phpunit for language units). The
  runner discovers and runs them on the next step. Do not skip any censused unit. Confirm when done.
  Response: {"read_receipts": [...]}   (acknowledge; the tests you wrote are in the repo)

Gate "compat_scan" - File compatibility detection [AGENT-GENERATE]:
  This gate is YOURS to produce. Do NOT show it to the user or wait for their reply.
  Read EVERY file in gate.file_paths completely, right now, in this step.
  ICX does NOT judge compatibility and does NOT check your answer - completeness is entirely YOUR
  responsibility, so nothing may be left unexamined.

  COMPLETENESS - LEAVE NOTHING:
    Reason from first principles about everything a test physically must do for gate.test_type:
    reach the screen, locate each control, see it, interact with it as a real user would, and
    observe the result. Examine every interactive element and every state involved. There is NO
    fixed checklist - anything that could stop a deterministic test is in scope, and it is on you
    to think of it.

  FORBIDDEN - DEFERRING TO THE RUNNER:
    You may NOT pass anything by assuming the test tool, the browser-use agent, or Playwright will
    "work around it", "still manage", "figure it out", or be "less robust but fine". The runner's
    tolerance is never your excuse. If a real user or a deterministic test would struggle with a
    control as written, it IS a finding. "Probably works" / "should be ok" / "optional improvement"
    are NOT verdicts - if you are not certain a thing is cleanly testable as-is, it is a finding.

  REPORT, DO NOT DECIDE:
    Every concern, however small, becomes a finding: what you saw (path + line), why it impedes
    testing, and the concrete change you propose. You do NOT silently accept, skip, or drop anything.
    ICX routes your findings to the user at the compat_check gate; the USER decides each one, and you
    then execute exactly that decision.

  Response: {"all_compatible": true|false,
             "findings": [{"path": "<p>", "compatible": true|false,
                           "reasons": ["what you saw, with path:line"],
                           "required_changes": ["concrete edit you propose"]}],
             "read_receipts": [{"path": "<p>", "line_count": <n>, "last_line": "<text>"}]}
  - all_compatible: true ONLY if you genuinely found nothing by inspection - never by assuming the
    tool will cope.
  - findings: one entry per file; required_changes must be specific, actionable edits.
  - read_receipts: one entry per file you opened and read fully this step (path, total line count, text of the last line).
  - This is your read and your judgment - not the user's, and not ICX's.

Gate "compat_check" - Compatibility review [USER-DECISION]:
  Present the agent findings from the compat_scan you just completed. The user decides.
  YOU MUST SHOW ALL OF THIS TO THE USER - SKIPPING ANY = VIOLATION:
    "Compatibility issues detected (agent scan). Incompatible files:
     <for each entry in gate.incompatible: show path, reasons, and required_changes>

     Options:
       approve - Apply the required_changes to each incompatible file yourself, then resume.
                 ICX re-scans after you resume (compat_scan fires again to verify the fixes).
       reject  - Do not apply. Specify per-file: drop (remove from test set), manual (user tests
                 it by hand), or accept (test it as-is with no change - user knowingly accepts
                 the finding and keeps the file in the run)."
  WAIT for user reply. The user decides each file - never choose on their behalf.
  approve means you have ALREADY applied the required_changes to the source files; ICX re-scans.
  Response (approve): {"decision": "approve", "edited_files": ["<path>", ...]}
  Response (reject):  {"decision": "reject", "resolution": {"<path>": "drop"|"manual"|"accept"}}

Gate "2" - Detection mode + scope [automated only]:
  YOU MUST SHOW ALL FIELDS TO THE USER AND GET ANSWERS FOR EACH - SKIPPING ANY = VIOLATION:
    "DETECTION MODE - how the UI layer generates the test spec:
       1. auto_detect - the UI layer opens the URL with Playwright and scans live page fields.
                        App must be running and URL must be accessible.
       2. json_spec   - AI reads your JSX/TSX source files directly. No browser needed.
                        Use when URL requires VPN or auth.
     Default: <gate.defaults.mode shown as 1 or 2>. What is your choice?

     SCOPE - what to test:
       1. ticket - Only test functionality changed by the listed files. (recommended)
       2. full   - Full end-to-end test of the entire screen.
     Default: 1. ticket. What is your choice?

     MERGE FILES - combine multiple JSX files into one spec (shown only when >1 file):
       1. yes - Merge all files into one combined spec.
       2. no  - Separate spec per file.
     Default: <1 or 2>. What is your choice?

     URL: <gate.defaults.url or 'not set'>
     Required for auto_detect. Confirm current URL or provide a new one.

     Answer each (type 1 or 2 for each choice):"
  WAIT for user reply on ALL fields. Responding before getting all answers is a VIOLATION.
  Response: {"mode":"1"|"2"|"auto_detect"|"json_spec", "scope":"1"|"2"|"ticket"|"full",
             "merge_files":"1"|"2"|true|false, "url":"http://..."}

Gate "2a" - Detected fields confirmation [auto_detect only, fires before Gate 2b]:
  YOU MUST SHOW ALL OF THIS TO THE USER - SKIPPING ANY = VIOLATION:
    "The UI layer scanned the page. Review before generating the test spec:
     URL:         <gate.url>
     Page title:  <gate.page_title>
     Field groups detected (<gate.group_count> groups):
       <list gate.detected_groups one per line>
     Is the URL correct and are these the right fields?
     Confirm or provide a corrected URL."
  WAIT. Response: {"url": "<confirmed url>"}

Gate "2b" - JSON spec generation [both modes]:
  THIS GATE HAS STRICT MANDATORY RULES. VIOLATING ANY = CRITICAL VIOLATION. READ ALL BEFORE ACTING.

  RULE 2b-1 - READ ALL FILES FIRST, GENERATE SECOND. NO EXCEPTIONS:
    You MUST read every file in gate.file_paths completely before writing a single word of JSON.
    If a file exceeds 1000 lines, read it in chunks until the ENTIRE file is consumed.
    Do NOT begin generating json_spec until every listed file is fully read.
    "I already know what the file contains" is NOT a valid reason to skip reading. Read it.

  RULE 2b-2 - FOLLOW gate.rules EXACTLY. THE RULEBOOK IS THE SPECIFICATION:
    gate.rules contains the complete output format. Follow it without deviation.
    The output JSON MUST include ALL of these top-level sections - missing any = VIOLATION:
      - screenName, fileName, filePath, associatedFiles, moduleName, description
      - rootFile                    (fileName, filePath, describesUrl, containsTriggers[])
      - modalFiles[]
      - techStack                   (framework, stateManagement, uiLibrary[], notifications[], httpClient, caching)
      - functionalitySummaryTable   (ALL functionalities detected)
      - functionalities[]           (one entry per functionality, fully populated)
      - dependencyGraph
      - validationMatrix            (each entry: errorDisplayMode toast|inline|both)
      - apiMappingSummary           (each entry: callerFunction)
      - responseCodeMappingSummary
      - permissionsMatrix
      - modalsSummary
      - notificationsSummary
      - inlineErrorsSummary
      - loaderHandling
      - selectorAudit               (EVERY selector produced must appear here)
    Every functionalities[] entry and every field within it also has a required key set -
    gate.rules (from ~/.icx/testing_rules/2b.md) carries the full per-functionality and
    per-field checklist. Read gate.rules and satisfy it in full.
    Do not simplify, condense, rename, or reorder the structure. Use the prompt's exact format.

  RULE 2b-3 - FIELD SELECTORS ARE MANDATORY. NEVER LEAVE domSelectors AS []:
    For every field in every functionality, domSelectors MUST contain at least one working
    Playwright selector. Selection priority:
      1. id="..." -> use #id
      2. data-testid="..." -> use [data-testid="..."]
      3. placeholder="..." -> use input[placeholder="..."]
      4. name="..." (only if literally present in JSX) -> input[name="..."]
    Never guess selectors. Only use selectors you can see in the source files you read.

  RULE 2b-4 - NOTIFICATIONS AND INLINE ERRORS ARE MANDATORY:
    For every functionality that calls pushNotify / toast / NotificationManager:
      - Add a notifications.messages[] entry for each call site with the exact message text.
    For every functionality that sets an error state variable or shows field validation:
      - Add an inlineErrors.messages[] entry for each field with exact message text.
    "I don't see any notifications" is only valid if you actually read the full file and
    found zero pushNotify/toast/NotificationManager calls. Otherwise it is a VIOLATION.

  RULE 2b-5 - DO NOT INVENT A SHORTCUT SPEC:
    You are NOT permitted to produce a simplified spec based on your own judgment.
    The output format is dictated entirely by gate.rules.
    Any deviation - any section omitted, any field left empty without a real reason,
    any selector guessed instead of extracted - is a CRITICAL VIOLATION.

  RULE 2b-6 - TOKEN BUDGET DOES NOT JUSTIFY SKIPPING:
    If the output is large, produce it in full anyway.
    Never use context window size, token limits, or response length as an excuse to omit
    sections, truncate arrays, or abbreviate entries. Do more, not less.

  RULE 2b-7 - SUBMIT ONLY WHEN ALL CONDITIONS ARE MET:
    Do NOT respond with json_spec until you can confirm ALL of these:
      [x] Every file in gate.file_paths has been fully read
      [x] Every functionality detected in the source is documented
      [x] Every field has at least one Playwright selector in domSelectors
      [x] Every notification call site has an entry in notifications.messages[]
      [x] Every inline error has an entry in inlineErrors.messages[]
      [x] selectorAudit lists every selector used anywhere in the spec
      [x] All sections from RULE 2b-2 are present in the output
    If any condition is not met: keep reading and generating until it is.

  RULE 2b-8 - ICX ENFORCES COMPLETENESS. YOU WILL BE RE-ASKED:
    gate.rules (from ~/.icx/testing_rules/2b.md) lists the required top-level sections, the
    per-functionality keys, and the per-field keys. After you submit, ICX checks that each is
    present (top-level content sections must also be non-empty), including inside every
    functionalities[] entry and every field. If anything is missing, ICX re-asks you with
    gate.missing_sections naming the exact paths (e.g. functionalities[2].businessLogic,
    functionalities[0].fields[3].interactionPattern) - regenerate a COMPLETE spec, do not
    argue. ICX never silently submits an incomplete spec. Only if the user has reviewed and
    KNOWINGLY accepts an incomplete spec may you resume with accept_incomplete:true.

  Response: {"json_spec": "{ \"functionalitySummaryTable\": [...], \"functionalities\": [...], ... }", "read_receipts": [{"path": "<p>", "line_count": <n>, "last_line": "<text>"}]}
  Response (only after the user accepts an incomplete spec): {"json_spec": "{...}", "accept_incomplete": true, "read_receipts": [...]}

Gate "api_manual" - Manual API endpoint entry [USER-DECISION, api test type only]:
  YOU MUST SHOW THE USER AND WAIT FOR THEIR REPLY - VIOLATION IF SKIPPED:
    "Provide the API endpoint details for the test:
     Endpoint URL (e.g. https://api.example.com/v1/resource):
     HTTP method (GET/POST/PUT/PATCH/DELETE):
     Payload (JSON body or query params, leave empty if none):
     Payload type (json / form / none):"
  WAIT for all four answers. Never assume or pre-fill any field.
  Response: {"api_endpoint": "<url>", "api_method": "<method>",
             "api_payload": "<payload or empty>", "api_payload_type": "json"|"form"|"none"}

Gate "3" - URL confirmation [automated only]:
  The test type was ALREADY chosen at gate "pick_type" (gate.test_type). DO NOT re-ask it here.
  The layer that runs is gate.test_type by default (gate.recommended_layers). Extra layers in
  gate.optional_layers are OPTIONAL - only mention them if the user asks; never force a re-pick.
  For test_type "unit" there is NO URL - just confirm and proceed.
  SHOW THE USER:
    "You chose the '<gate.test_type>' test - that layer will run. Confirm the target URL:
     TARGET URL: <gate.current.url or 'NOT SET'>
     (unit needs no URL.) Reply 'accept' to run your chosen type, or list layers to override.
     For agent: you will run your test HEADLESS (hidden) by default. Ask if the user wants to WATCH
     it (visible browser); if yes, include visible:true. If visible, ALSO ask the user the SLOWMO
     pace in ms (how long to slow + pause on each step so they can follow) - DEFAULT 1000 (1s) when
     visible, 0 when headless - and pass it as slowmo."
  WAIT for user reply on ALL fields. Responding without all answers is a CRITICAL VIOLATION.
  (RULE 3: URL must be explicitly confirmed by user. Never submit a URL you assumed.)
  Response: {"layers":["unit","api",...],
             "url":"http://...",
             "visible": true|false,   (agent only - true = watch the browser)
             "slowmo": 1000}          (agent + visible only - ms slowed+paused per step; default 1000, headless forces 0)
  --- After this response ICX runs the local verification suite. This can take minutes - expect
  {"status": "running"} back and poll testing_get_session_status(session_id) rather than assuming
  a hang; see RUNNING in this tool's top-level RETURNS section. ---

Gate "auth_gate" - Authentication configuration [USER-DECISION]:
  YOU MUST SHOW THE USER AND WAIT FOR THEIR REPLY - VIOLATION IF SKIPPED:
    "Authentication required for the test target. Choose auth mode:
       public  - No login needed. Target is publicly accessible.
       reuse   - Reuse a previously stored session.
       capture - ICX opens a REAL browser; you log in BY HAND; ICX saves the session.
       inline  - You provide the APP credentials; ICX drives the login form and saves it.
     What is your choice?"
  capture: call the icx_ui_auth_capture tool (url + file_paths); it opens a browser for MANUAL login -
    NEVER ask the user for their username/password in chat for capture.
  inline: call the icx_ui_auth_inline tool (url + file_paths + username + password); ONLY inline collects
    credentials, and they go to ICX's browser process, never into chat history.
  reuse: uses the stored session for this project + host. public: no auth.
  Response (any mode, AFTER the capture/inline tool returns ok): {"auth_mode": "public"|"reuse"|"capture"|"inline"}
  Port drift: if gate.other_host_sessions is non-empty, the exact host has no session but this SAME
  project has one at a different port (a dev server auto-incrementing past a taken port is the usual
  cause). Show it to the user; reuse it with {"auth_mode": "reuse", "reuse_host": "<host>"} - cookie
  auth transfers across a port change, but localStorage/sessionStorage auth (common in SPAs) does NOT
  (origin-scoped including port), so warn the user and be ready to fall back to capture if the app
  still looks logged out.

Gate "author_flow" - write AND run a real Playwright test [AGENT-GENERATE, agent test type only]:
  This gate is YOURS to produce. Do NOT show it to the user or wait for their reply.
  gate.screen_model is the Element Census (COMBINED: live-DOM crawl fused with the source census,
  so every selector already resolves). gate.rules carries the mandatory checklist (RULEBOOK RULE
  applies - read it in full, every time) - CRUD lifecycle, validation, security (XSS/SQLi), a11y,
  error-handling, data safety. Follow it; it is binding.
  WRITE a real Playwright test file in the repo (your own editor tools) covering the checklist for
  every functionality in gate.screen_model. RUN it YOURSELF (your Bash tool) against ICX's OWN
  pinned Playwright install - gate.playwright gives {node, env: {NODE_PATH, PLAYWRIGHT_BROWSERS_PATH}}
  to use, so you run ICX's install, never a bare npx/global one. Point the run's JUnit reporter at
  gate.report_path (e.g. `playwright test <file> --reporter=junit --output=<gate.report_path>`).
  READ Playwright's own failures (real stack traces, real selector mismatches) and FIX YOUR OWN
  script, then re-run - repeat until the checklist is covered or you have confirmed a genuine
  application bug (report it as a finding, never force a false pass by weakening an assertion).
  BROWSER: gate.headless / gate.slowmo carry the user's visible/slowmo choice from gate 3 - launch
  your own browser context accordingly (headed + slowMo when the user asked to watch).
  AUTH: if gate.auth_mode is capture/inline/reuse, gate.storage_state is a Playwright storageState
  path - load it into your browser context and go straight to gate.url; do NOT author login steps.
  For a public app with no saved session, author real login steps yourself (read the actual form).
  Response: {"report_path": "<path you actually wrote the JUnit report to>",
             "test_file": "<path to the Playwright file you wrote>",
             "covered": ["<functionality names/ids from screen_model you covered>"],
             "findings": ["<genuine app bugs found, if any>"]}
  - This is your generation AND your execution - not the user's, not ICX's. Produce it, run it,
    self-heal it, submit the result directly.

Gate "4" - Issue review [automated only]:
  YOU MUST SHOW THE USER (VIOLATION IF SKIPPED):
    List every issue from gate.issues with: name, description, severity.
    Propose specific code fixes for each issue.
    Explain what needs to be changed and why.
  Respond with {} ONLY after presenting all issues and proposed fixes.
  (RULE 4: Gate 5 always follows Gate 4. Never skip Gate 5.)
  Response: {}

Gate "5" - Fix confirmation [automated only]:
  YOU MUST ASK THE USER (VIOLATION IF SKIPPED):
    "Have you applied the proposed fixes for this iteration?
     Approve this iteration to continue, or reject to stop fixing.
     If approved, list what was changed."
  WAIT for user reply.
  Response: {"approve_iteration": true|false, "fixes_applied": ["fix 1 description", ...]}

Gate "manual" - Manual test wait [manual path]:
  YOU MUST TELL THE USER (VIOLATION IF SKIPPED):
    "Run the test manually against the application now.
     Files in scope: <list file_paths one per line>
     Reply when you are finished."
  WAIT for user to say they are done. Response: {"done": true}

Gate "manual_result" - Manual result report [manual path]:
  YOU MUST SHOW ALL THREE FIELDS AND WAIT FOR USER ANSWERS - SKIPPING ANY = VIOLATION:
    "DID THE TEST PASS?
       1. yes - all functionality works correctly.
       2. no  - found issues.
     Your answer?

     ISSUES FOUND (list each issue on a new line, or leave empty if passed):

     NOTES (any additional observations, optional):"
  WAIT for reply on all three. Response:
    {"passed": "yes"|"no", "issues": ["issue 1", ...], "notes": "<text>"}

Gate "ui_check" - Visual UI verification [both paths]:
  YOU MUST SHOW THE USER AND WAIT FOR THEIR REPLY - VIOLATION IF SKIPPED:
    "Open the application and visually verify the UI now.
     Check: layout, navigation, all functionality touched by the changed files, error states.
     Files tested: <list file_paths>

     RESULT:
       1. yes - UI looks correct, everything is working as expected.
       2. no  - Found visual issues (describe them below)."
  WAIT for explicit user reply. Never assume UI is fine without asking. (RULE 5)
  Response: {"choice": "yes"|"no", "notes": "<optional>"}

Gate "memory_save" - Save session record [both paths]:
  YOU MUST SHOW THE FULL SUMMARY AND ASK - VIOLATION IF SKIPPED:
    "Test session summary:
     Files:      <list gate.summary.files>
     Mode:       <gate.summary.test_mode>
     Result:     <gate.summary.result>
     Iterations: <gate.summary.iterations>

     SAVE TO ICX TESTING HISTORY?
       1. yes - save this session record.
       2. no  - discard, do not save."
  WAIT for user reply. Never auto-save without asking. (RULE 6)
  Response: {"save": "yes"|"no"}

Gate "error" - verification run failed [automated only]:
  YOU MUST SHOW THE USER (VIOLATION IF SKIPPED):
    "Test stopped: <gate.message>
       1. retry          - Re-run the same verification.
       2. skip_iteration - Count this iteration as 0 issues and continue.
       3. end_session    - Stop testing and go to UI check."
  WAIT. Response: {"choice":"1"|"2"|"3"|"retry"|"skip_iteration"|"end_session"}

Gate "limit" - Max iterations reached [automated only]:
  YOU MUST SHOW THE USER (VIOLATION IF SKIPPED):
    "Reached max iterations (<gate.max_iterations>). <N> issues still found.
       1. continue    - Add 3 more iterations and keep testing.
       2. end_session - Stop testing and go to UI check."
  WAIT. Response: {"choice":"1"|"2"|"continue"|"end_session"}

RETURNS: {session_id, done: bool, gate: {gate: "...", message: "...", ...} | null}
done: false -> gate.gate is set -> use that gate's format above for the next call.
done: true  -> gate is null -> session complete, workflow finished.

RUNNING (real browser work - verify/heal, scored execution - can take minutes): instead of gate,
you may get {"session_id", "status": "running", "done": false, "gate": null, "poll": "..."}.
This means the call returned before the work finished - it is NOT stuck and NOT an error. Do:
  1. Tell the user ICX is still running (do not go silent - say what is happening).
  2. Call testing_get_session_status(session_id) to check progress. Space out polls (e.g. every
     15-30s) rather than hammering the tool - the work is bounded internally and will finish.
  3. Once status is no longer "running", the response has the normal {done, gate} shape above -
     resume from there exactly as usual.
  NEVER call testing_resume_session again while status is "running" - there is no gate waiting
  for an answer yet, and a stray resume on a still-executing session is rejected.

RUNTIME: <2s for most gates. Gate 3 response and any AGENT-GENERATE gate that follows a live-DOM
verify/heal pass can run long - expect "status": "running" and poll rather than assuming a hang.\
"""


_TESTING_STATUS_DESCRIPTION = """\
Poll a testing session that returned {"status": "running"} from testing_start_session or \
testing_resume_session. Cheap, read-only, safe to call repeatedly.

session_id: the same UUID from testing_start_session.

RETURNS one of:
  {"session_id", "status": "running", "done": false, "gate": null}
    Still executing. Wait and poll again (every 15-30s is enough - do not busy-poll).
  {"session_id", "done": false, "gate": {...}, "status": "..."}
    Finished and a new gate is waiting - handle it exactly like any testing_resume_session gate
    response (see testing_resume_session's GATE POSTURE CLASSIFICATION and per-gate rules).
  {"session_id", "done": true, "gate": null, "status": "...", "error": null|"..."}
    Session complete.
  {"error": "..."}
    session_id unknown/malformed, or the session's state could not be read.

Never call testing_resume_session while a status poll still returns "running" - only call it again
once you have a gate to answer.\
"""


TESTING_TOOLS: list[Tool] = [
    Tool(
        name=_TESTING_START_TOOL,
        description=_TESTING_START_DESCRIPTION,
        inputSchema={
            "type": "object",
            "properties": {
                "file_paths": {"type": "array", "items": {"type": "string"}},
                "context": {"type": "string"},
                "max_iterations": {"type": "integer", "minimum": 1},
                "test_mode": {"type": "string", "enum": ["automated", "manual"]},
                "test_writes": {"type": "boolean",
                                "description": "agent-type: allow real Create/Update/Delete writes against the live app (default true). Set false for a read-only environment - the agent's test then exercises forms (fill/validate/cancel) without submitting a real write."},
                "nl_intent": {"type": "string",
                              "description": "Optional plain-English scenario request (e.g. 'test duplicate email error') to seed extra NL-driven test scenarios."},
                "acceptance_criteria": {"type": "array", "items": {"type": "string"},
                                        "description": "Optional ticket acceptance criteria to author extra scenarios from."},
            },
            "required": ["file_paths", "test_mode"],
        },
            annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False},
    ),
    Tool(
        name=_TESTING_RESUME_TOOL,
        description=_TESTING_RESUME_DESCRIPTION,
        inputSchema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "response": {"type": "object"},
            },
            "required": ["session_id", "response"],
        },
            annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True},
    ),
    Tool(
        name=_TESTING_STATUS_TOOL,
        description=_TESTING_STATUS_DESCRIPTION,
        inputSchema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
            },
            "required": ["session_id"],
        },
            annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    ),
    Tool(
        name=_UI_AUTH_CAPTURE_TOOL,
        description=(
            "CAPTURE a UI login session by opening a REAL browser for the user to log in by hand "
            "- NEVER ask the user for their username/password in chat for this. Call this at the "
            "auth_gate when the user chose 'capture'. ICX opens a headed Chromium at the login "
            "URL; the user logs in manually; when they reach success_url (or close the window) "
            "ICX saves the authenticated session (cookies+localStorage) for this project+host and "
            "the UI test replays already logged in. Input: {url, file_paths:[seed files, to key "
            "the session to this project], success_url?}. Returns {ok, storage_state}. After ok, "
            "resume the auth_gate with {\"auth_mode\":\"capture\"}. If it fails with a Playwright/"
            "tooling error, DO NOT run npm/npx/playwright install in the user's repo - ICX brings "
            "its own tooling; tell the user to run `icx test setup --force` instead."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "file_paths": {"type": "array", "items": {"type": "string"}},
                "success_url": {"type": "string"},
            },
            "required": ["url", "file_paths"],
        },
            annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True},
    ),
    Tool(
        name=_UI_AUTH_INLINE_TOOL,
        description=(
            "INLINE login: the user provides the APPLICATION credentials (username/password the "
            "app requires) and ICX drives the login form, then saves the authenticated session. "
            "Use at the auth_gate when the user chose 'inline'. The credentials are passed to "
            "ICX's browser process only - never stored by ICX beyond the resulting session, never "
            "echoed. Input: {url, file_paths, username, password, success_url?, user_selector?, "
            "pass_selector?, submit_selector?}. Returns {ok, storage_state}. After ok, resume the "
            "auth_gate with {\"auth_mode\":\"inline\"}."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "file_paths": {"type": "array", "items": {"type": "string"}},
                "username": {"type": "string"},
                "password": {"type": "string"},
                "success_url": {"type": "string"},
                "user_selector": {"type": "string"},
                "pass_selector": {"type": "string"},
                "submit_selector": {"type": "string"},
            },
            "required": ["url", "file_paths", "username", "password"],
        },
            annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
    ),
]


async def dispatch_testing_tool(name: str, arguments: dict) -> list[TextContent] | None:
    args = arguments or {}

    if name in (_UI_AUTH_CAPTURE_TOOL, _UI_AUTH_INLINE_TOOL):
        url = args.get("url")
        file_paths = args.get("file_paths")
        if not isinstance(url, str) or not url.strip():
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": "url must be a non-empty string."}))]
        if not isinstance(file_paths, list) or not file_paths:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": "file_paths must be a non-empty list."}))]
        from icx_engine.testing import auth as _auth, ui_auth as _ui_auth
        from icx_engine.testing.nodes import _resolve_project_id
        from icx_engine.testing.runners.install import is_installed
        if not is_installed("playwright"):
            return [TextContent(type="text", text=json.dumps({
                "ok": False,
                "error": ("UI tooling (Playwright + Chromium) is not installed. Run "
                          "'icx test setup' once to download it into ~/.icx/testing (it does NOT touch "
                          "your repo), then retry."),
            }))]
        project = _resolve_project_id([str(f) for f in file_paths]) or "unknown"
        host = _auth.host_of(url)
        if not host:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": "url has no host."}))]
        success_url = args.get("success_url") or ""
        try:
            if name == _UI_AUTH_CAPTURE_TOOL:
                path, detail = await _ui_auth.capture_session(project, host, url, success_url=success_url)
            else:
                username = args.get("username")
                password = args.get("password")
                if not isinstance(username, str) or not username or not isinstance(password, str) or not password:
                    return [TextContent(type="text", text=json.dumps(
                        {"ok": False, "error": "username and password are required for inline login."}))]
                path, detail = await _ui_auth.inline_session(
                    project, host, url, username, password, success_url=success_url,
                    user_selector=args.get("user_selector") or "",
                    pass_selector=args.get("pass_selector") or "",
                    submit_selector=args.get("submit_selector") or "",
                )
        except Exception as exc:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": str(exc)}))]
        if path:
            return [TextContent(type="text", text=json.dumps({"ok": True, "storage_state": path}))]
        _d = (detail or "").strip()
        _low = _d.lower()
        _tool_broken = any(s in _low for s in (
            "playwright", "cannot find package", "module_not_found", "err_module_not_found",
            "executable doesn't exist", "chromium"))
        if _tool_broken:
            err = (f"ICX's own UI tooling (Playwright/Chromium under ~/.icx/testing) is missing or "
                   f"broken. DO NOT install Playwright, npm, or npx into the user's repo - ICX brings "
                   f"its own. Fix it by running in a terminal: `icx test setup --force`. "
                   f"(harness detail: {_d[:300]})")
        else:
            err = f"session capture failed: {_d}"
        return [TextContent(type="text", text=json.dumps({"ok": False, "error": err}))]

    if name == _TESTING_START_TOOL:
        from icx_engine.testing.graph import get_testing_graph
        from icx_engine.testing.state import make_initial_state
        from icx_engine.testing.validate import validate_session_args
        from icx_engine.config_manager import ConfigManager as _CM
        import uuid as _uuid

        ok, msg = validate_session_args(args)
        if not ok:
            return [TextContent(type="text", text=json.dumps({"ok": False, "error": msg}))]

        file_paths = args.get("file_paths", [])
        context = args.get("context")
        max_iterations = args.get("max_iterations")
        test_mode = args.get("test_mode")
        nl_intent = args.get("nl_intent") or None
        acceptance_criteria = args.get("acceptance_criteria") or []
        cfg = _CM.load()

        project = None
        try:
            from icx_engine.graph import storage
            from pathlib import Path as _Path
            for p in file_paths:
                info = storage.lookup_for_file(_Path(p))
                if info is not None:
                    project = info.project_id
                    break
        except Exception:
            project = None

        session_id = str(_uuid.uuid4())
        initial_state = make_initial_state(
            file_paths=file_paths,
            context=context,
            max_iterations=max_iterations if max_iterations is not None else cfg.test_max_iterations,
            test_mode=test_mode,
            nl_intent=nl_intent,
            acceptance_criteria=acceptance_criteria,
        )
        initial_state["project"] = project
        initial_state["session_id"] = session_id
        if "test_writes" in args:
            initial_state["test_writes"] = bool(args.get("test_writes"))
        graph = await get_testing_graph()
        config = {"configurable": {"thread_id": session_id}}
        finished = await _testing_invoke_tracked(session_id, graph.ainvoke(initial_state, config=config))
        if not finished:
            return [TextContent(type="text", text=json.dumps(attach_skill_hint({
                "ok": True, "session_id": session_id, "status": "running", "done": False, "gate": None,
                "poll": ("Still running real work (graph expansion/verification). Call "
                          "testing_get_session_status(session_id) to check progress - do NOT call "
                          "testing_resume_session again until status is no longer 'running'."),
            }, "testing-session-driver", rank_prompt=(context or nl_intent or "testing session"),
                archetype="testing")))]
        snapshot = await graph.aget_state(config)
        result = _testing_gate_snapshot(session_id, snapshot)
        result["ok"] = True
        del result["error"]
        result = attach_skill_hint(result, "testing-session-driver",
                                    rank_prompt=(context or nl_intent or "testing session"), archetype="testing")
        return [TextContent(type="text", text=json.dumps(result))]

    if name == _TESTING_RESUME_TOOL:
        from icx_engine.testing.graph import get_testing_graph
        from langgraph.types import Command as _Command
        session_id = args["session_id"]
        response = args["response"]
        running = _TESTING_RUNNING.get(session_id)
        if running is not None and not running.done():
            return [TextContent(type="text", text=json.dumps({
                "session_id": session_id, "status": "running", "done": False, "gate": None,
                "error": ("This session is still executing the previous gate. Call "
                          "testing_get_session_status(session_id) instead of resuming again."),
            }))]
        graph = await get_testing_graph()
        config = {"configurable": {"thread_id": session_id}}
        # SECURITY: an auth sessionId in the resume payload would be persisted to the durable
        # checkpoint. STRIP it before resuming so the credential never lands on disk. The real
        # authenticated session is already persisted (with its storage_state path) by the
        # icx_ui_auth_capture / icx_ui_auth_inline tools - we must NOT re-save here, which would overwrite
        # that record with an empty storage_state and make the replay run unauthenticated.
        if isinstance(response, dict) and "session_id" in response:
            response = {k: v for k, v in response.items() if k != "session_id"}
        finished = await _testing_invoke_tracked(session_id, graph.ainvoke(_Command(resume=response), config=config))
        if not finished:
            return [TextContent(type="text", text=json.dumps({
                "session_id": session_id, "status": "running", "done": False, "gate": None,
                "poll": ("Still running real work (verify/heal or scored test execution can take "
                          "several minutes). Call testing_get_session_status(session_id) to check "
                          "progress - do NOT call testing_resume_session again until status is no "
                          "longer 'running'."),
            }))]
        # A session is DONE only when nothing is pending AND no gate is waiting for input. A node
        # with several interrupt() calls (e.g. expand_files: expand_scan then expand) pauses at its
        # LATER interrupt with snapshot.next == () while an interrupt is still pending - so `not next`
        # alone would wrongly report done mid-flow and abandon the run before any test executes.
        snapshot = await graph.aget_state(config)
        result = _testing_gate_snapshot(session_id, snapshot)
        return [TextContent(type="text", text=json.dumps(result))]

    if name == _TESTING_STATUS_TOOL:
        from icx_engine.testing.graph import get_testing_graph
        session_id = args.get("session_id", "")
        if not isinstance(session_id, str) or not session_id.strip():
            return [TextContent(type="text", text=json.dumps({"error": "session_id is required."}))]
        session_id = session_id.strip()
        running = _TESTING_RUNNING.get(session_id)
        if running is not None and not running.done():
            return [TextContent(type="text", text=json.dumps({
                "session_id": session_id, "status": "running", "done": False, "gate": None,
            }))]
        graph = await get_testing_graph()
        config = {"configurable": {"thread_id": session_id}}
        try:
            snapshot = await graph.aget_state(config)
        except Exception as exc:
            return [TextContent(type="text", text=json.dumps({"error": f"cannot read session state: {exc}"}))]
        result = _testing_gate_snapshot(session_id, snapshot)
        prior_error = _TESTING_ERRORS.pop(session_id, None)
        if prior_error and not result.get("error"):
            result["error"] = prior_error
        return [TextContent(type="text", text=json.dumps(result))]

    return None
