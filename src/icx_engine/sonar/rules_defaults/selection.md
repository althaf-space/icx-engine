# Sonar project and branch selection - MANDATORY protocol

These rules are mandatory. Follow them exactly. Do not skip, reorder, or improvise.

## Choosing a project

1. Before fetching anything, you MUST ask the user which way they want to select the project:
   - (a) Let ICX fetch the projects from the SonarQube server, or
   - (b) Paste the exact project key themselves.
   Ask this as a plain either/or question. Do not assume.

2. If the user chooses (b) paste: use the key they give verbatim as `project`. Do NOT call `sonar_projects` at all - go straight to `sonar_report` / `sonar_findings`.

3. If the user chooses (a) fetch:
   - Call `sonar_projects`.
   - If the response has `truncated: true` (the server has more projects than can be listed), you MUST NOT invent or guess a key. Tell the user the total count and ask them to either paste the exact key, or give a search term. Then call `sonar_projects` again with that term as `query`.
   - Only present keys that ICX actually returned in `projects`. Never fabricate a key.

4. Never enumerate hundreds of projects to the user. If `truncated` is true, relay the count and ask to narrow - do not paste a giant list.

## Choosing a branch

5. After the project is fixed, you MUST ask the user which branch they want the report for, using the same either/or:
   - (a) Let ICX fetch the branches, or
   - (b) Paste the branch name themselves.

6. If (b) paste: pass it verbatim as `branch`.

7. If (a) fetch: call `sonar_branches` with the project. If the response is `truncated: true`, ask the user to paste the branch name or give a `query` term, then call `sonar_branches` again with that term.

8. Only use a branch name that the user pasted or that ICX returned. Never guess a branch name.

## Never

- Never call `sonar_report` / `sonar_findings` with a project or branch you invented.
- Never dump a long list of projects or branches into the conversation.
- Never skip the two either/or questions above.
