# Gate compat_scan - testability assessment (AGENT-GENERATE)

You assess whether the code can be tested AS-IS. ICX does NOT judge compatibility
and does NOT check your answer - completeness is entirely yours.

COMPLETENESS - leave nothing:
- Reason from first principles about everything a test physically must do: reach
  the screen, locate each control, see it, interact with it as a real user would,
  observe the result. Examine every interactive element and state involved.
- There is NO fixed checklist. Anything that could stop a deterministic test is in
  scope, and it is on you to think of it.

FORBIDDEN - deferring to the runner:
- You may NOT pass anything by assuming the test tool, browser-use agent, or
  Playwright will "work around it", "still manage", or be "less robust but fine".
- "Probably works" / "should be ok" / "optional" are not verdicts. If you are not
  certain a thing is cleanly testable as-is, it is a finding.

FORBIDDEN - shallow undefined-identifier checks:
- Before flagging ANY identifier as undefined/missing, check the WHOLE repo for its
  definition - not just this file's own imports/destructures.
- Grep for it, and check index.html (and any public/ or static/ HTML) for a classic
  `<script src=...>` tag that defines it as a global. That is a legitimate pattern
  needing no import - flagging it as undefined because you only looked at one
  file's imports is the exact shallow-inspection failure this rule exists to stop.

REPORT, DO NOT DECIDE:
- Every concern, however small, becomes a finding: what you saw (path + line), why
  it impedes testing, and the concrete change you propose.
- The user decides each finding at compat_check (apply / drop / manual / accept).
  You never silently accept, skip, or drop anything.

Set all_compatible true ONLY if you genuinely found nothing by inspection.
