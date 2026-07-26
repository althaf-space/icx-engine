# ICX testing rules - COMMON (apply at every gate)

These rules are binding. They are loaded fresh from `~/.icx/testing_rules/` and
injected into every gate. They override any assumption, memory, or habit you have.
You are editing a real user's real code - correctness matters more than speed.

1. READ, DO NOT REMEMBER. At any gate that names files, open and read each file
   fully in THIS step. A prior read, a summary, or "I already saw it" is stale and
   must not be your basis. Return read_receipts when the gate asks for them.

2. NEVER SIMPLIFY TO SAVE EFFORT OR TOKENS. Token budget, length, or "this is
   probably enough" never justify skipping a required section, field, or check.
   Completeness is mandatory, not optional.

3. NEVER DEFER A PROBLEM TO A TOOL. You may not pass something by assuming the test
   runner, the UI verification layer, or Playwright will "work around it", "still
   manage", or be "good enough". If it is not correct as-is, it is a finding.

4. WHEN UNSURE, SURFACE - DO NOT DECIDE. If you are not certain, do MORE not less,
   and show the user. Never silently accept, skip, drop, or omit anything. On a
   user-decision gate the user decides; you execute exactly what they choose.

5. NO INVENTION. Do not fabricate selectors, fields, endpoints, or file contents.
   Extract them from the actual source. If it is not in the source, say so.

If a gate's own rules add specifics below, follow both. Where they conflict, the
gate-specific rule wins.
