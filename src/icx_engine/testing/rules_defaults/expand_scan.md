# Gate expand_scan - related file discovery (AGENT-GENERATE)

Find the files related to the seeds so the test covers the real feature, not a
fragment.

RULES:
- Search the repository for importers, callers, same-feature components, and the
  route or page that renders the seeds. Read with your own tools.
- gate.graph_expanded already lists what the graph found; add what it missed. Do
  not drop a related file because the list is getting long.
- Return only files you can justify as related, with read_receipts for those you
  opened. Do not invent paths.
