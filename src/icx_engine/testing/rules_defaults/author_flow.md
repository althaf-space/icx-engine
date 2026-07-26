# Gate author_flow - write and run a real Playwright test (AGENT-GENERATE)

You author AND execute the test. ICX does not run anything for you and does not
generate the test file - you write real Playwright code, run it yourself, read its
own failures, and fix your own script until every item below is covered or you have
confirmed a genuine app bug. This is the standard checklist; treat it as a floor, not
a ceiling - screen-specific behavior you find while testing belongs in the test too.

BOTH SOURCES, NOT JUST ONE (non-negotiable): `screen_model` (the census) is a floor,
never a ceiling. You are the one actually reading this app's source code - if you find
a functionality, field, tag, or control the census never listed (an upload button, an
export/report action, a feature-flagged control), TEST IT TOO. Never skip something
real just because ICX's census didn't name it - that is exactly the gap this rule
exists to close. Report anything you added this way separately (see "Resume with"
below) so it is visible, not silently folded in. If the live app or the source
disagrees with what the census says, trust what you actually see/read, adapt your
script, and say why.

WHAT ICX GIVES YOU:
- The Element Census (`screen_model`) - every functionality, field, validation,
  wizard step, and selector for this screen, fused with a live-DOM crawl so every
  selector in it already resolves on the real page. A floor, not the whole truth -
  see above.
- The target URL and, unless `auth_mode` is `public`, a `storage_state` path
  (Playwright storageState JSON - cookies + localStorage). Load it yourself via
  `browser.newContext({ storageState: ... })` - do not author login steps. If a
  file named `<storage_state>.session` also exists next to it, that is a captured
  `sessionStorage` snapshot (storageState does not cover sessionStorage); read its
  JSON and call `context.addInitScript` (or set it on the page before first
  navigation) to replay those keys, or the app may treat you as logged out.
- The path to ICX's own pinned Playwright install (`playwright.node`/`playwright.env`
  in the gate payload) - use it, not a bare `npx`/global install.

WHAT YOU DO:
1. Write a real Playwright Test file (`test('...', async ({ page }) => { ... })`)
   in the repo, covering every item below for every functionality in the census,
   PLUS anything you discover yourself per "BOTH SOURCES" above.
2. Run it yourself (Bash) against ICX's pinned Playwright, with a JUnit reporter
   pointed at the report path ICX gave you.
3. Read the failures Playwright itself reports - real stack traces, real selector
   mismatches, real timeouts. Fix your OWN script. Re-run.
4. Repeat until everything below is covered and passing, OR you have confirmed
   (not guessed) that a failure is a genuine application bug, not a bad selector or
   a bad wait - report those as findings, do not force a false pass by weakening
   the assertion. Your self-fix budget is bounded (see the gate message for the
   exact count this run) - once it's spent, stop and resume with what you have
   rather than looping indefinitely.
5. Resume with `{report_path, test_file, covered, discovered, findings}` -
   `covered` = census functionality names/ids you tested; `discovered` =
   functionality/tag names you found by reading code that the census never listed,
   and tested anyway; `findings` = genuine app bugs.

THE CHECKLIST (cover every applicable item per functionality; skip only what
genuinely does not apply to this screen, and say why):

CRUD lifecycle (in order - create before edit/delete need a row to act on):
- CREATE: fill every field with a valid value, submit, verify the record appears
  (search/list it back if the screen has a way to).
- VIEW/EDIT/DELETE MUST ALL TARGET THE SAME RECORD YOU JUST CREATED (non-negotiable):
  never open Edit, Delete, or any mutating action on a pre-existing row instead of
  the one you created this run - that includes validation/constraint/security probes
  authored under the Edit flow. Search or otherwise locate YOUR row by its unique tag
  before acting on it.
- VIEW/EDIT: open the created record, assert the fields show what you expect,
  change something, save, verify the change persisted. If the screen supports it,
  REVERT the edit back to the original value afterward so the test leaves no
  lasting change.
- DELETE: remove only the record YOU created (scope the delete to it - a `tr`/row
  containing your own unique tag, never a first-row or unscoped delete), verify
  it is actually gone.
- Use a unique, clearly test-owned, GENERIC value for the identifying field on every
  run (e.g. a `Test`/`QA` prefix + a token from the current timestamp) so re-runs
  never collide with a prior run's data and existing records are never touched by
  accident. NEVER embed a tool/vendor name (e.g. "ICX") in any data value - it is
  internal tooling, not app data.
- MULTI-STEP WIZARDS: every step must be exercised in order (fill -> next), the
  identifying field goes wherever it actually lives, and the terminal submit is
  whatever the LAST step's real action is - never assume step 1's fields cover it.

NO-CREATE-STEP FUNCTIONALITIES (export, download, upload, reports/dashboards):
some functionality has nothing to create - there's no "row" to make first. Do NOT
skip it for lack of a record. Exercise it against whatever real data already exists
on the screen right now (trigger the export/download and assert a file/response
came back, run the report against existing filters, exercise the upload control with
a throwaway file). If you find one of these that the census never mentioned, test it
and add it to `discovered`.

Validation / constraints:
- For every field with a maxLength/minLength/min/max/pattern/format in the census,
  try a violating value and assert the app rejects it (native constraint API,
  inline error, or blocked submit - whichever this app actually does).
- Try submitting with required fields empty and assert the app blocks it.

Security (do this even though it feels adversarial - it is the point):
- Inject an XSS payload (e.g. `"><img src=x onerror=window.__x=1>`) into free-text
  fields (not just create/edit - search boxes and filters too) and assert it did
  NOT execute (the app escaped it).
- For any field/endpoint that looks SQL-adjacent, try an injection-shaped value
  and assert the app does not crash or leak an error with internals in it.

Accessibility and error handling:
- Run an accessibility check on the rendered screen (missing labels, missing
  alt text, no discernible button/link text, duplicate ids, missing `<html lang>`)
  and report violations.
- If you can intercept/mock a network call (Playwright `page.route`), fault one
  backend call and assert the app degrades gracefully (an error message, not a
  blank/broken screen).

Data safety (non-negotiable):
- NEVER delete, edit, or otherwise mutate a record you did not create yourself.
- NEVER submit with real/production-looking data - use clearly test-tagged,
  generic values (never a tool/vendor name).

WHEN A CENSUS FIELD DESCRIBES A NON-NATIVE CONTROL (react-select/antd/MUI-style
dropdown, not a plain `<select>`): click to open it, then click the option - do not
assume `selectOption()` works on it.

Read `screen_model.coverageReport` before you start - it tells you exactly how many
functionalities/fields/validations exist, so you know when you are actually done,
not just when the obvious ones pass. Remember: that count is a floor - your own
reading of the source is the other half of "done."
