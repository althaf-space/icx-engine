# Gate compat_check - user reviews findings (USER-DECISION)

Show the user EVERY finding from compat_scan: path, line, why it impedes testing,
and the concrete change proposed. Do not hide, merge, or soften any finding.

The user decides each file - never choose on their behalf:
- approve  - you apply the required_changes to the source, then ICX re-scans.
- drop     - remove the file from the test set.
- manual   - keep it; the user will test it by hand.
- accept   - keep it in the automated run unchanged; the user knowingly accepts
             the finding.

Execute exactly the user's decision. If they approve, you must actually make the
edits before resuming.
