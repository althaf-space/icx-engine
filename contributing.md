# Contributing to ICX

Thank you for considering a contribution to ICX. This project is built and maintained
by a small team, and every improvement - whether a bug fix, a new connector, or better
documentation - makes a real difference.

## What we need most

The highest-value contributions right now:

1. **New work-tracker connectors** - GitHub Issues, GitLab Issues, Linear, Asana, Jira Data Center
   (distinct from the already-built GitLab git-workflow integration - see `src/icx_engine/gitlab/`
   and `src/icx_engine/git/`). Each connector opens ICX to a whole new user base.
2. **Bug fixes** - If you hit an error, chances are others will too.
3. **Documentation improvements** - Clear docs lower the barrier for everyone.
4. **Test coverage** - More edge cases caught early.

## Getting started

    git clone https://github.com/althaf-space/icx-engine
    cd icx-engine
    pip install -e ".[dev]"
    pytest

The full test suite runs in about 60-90 seconds. Start there.

## Adding a new connector

Connectors live in `src/icx_engine/connectors/`. Each connector implements the base
interface from `connectors/base.py`. See `connectors/jira/` for a complete example.

A new connector needs:
- A `client.py` that fetches raw issue data from the API
- A `parser.py` that maps API responses to `RawIssueData`
- A `config.py` that defines the connection config model
- A `connector.py` that implements `ConnectorBase` and delegates `process_attachments` to the shared Universal Attachment Engine in `connectors/attachments.py`
- Tests in `tests/` that mock the API and verify the mapping

The memory module, LLM analysis, attachment handling (OCR, vision, document conversion, audio/video transcription), and CLI commands all work automatically once your connector
returns a `RawIssueData` object with populated `attachment_content_urls`. You do not need to touch the Universal Attachment Engine itself.

See `developer.md` for a step-by-step guide to adding a connector.

## Code standards

- Python 3.11-3.14, Pydantic v2, async/await for I/O
- Tests required for new code (pytest, no mocking the database)
- No new dependencies without discussion - keep the install footprint small
- Type annotations on all public functions
- Follow the existing module structure

## Submitting a pull request

1. Fork the repo and create a branch from `development`.
2. Write tests first - they help define the scope of the change.
3. Keep the PR focused. One feature or fix per PR.
4. Update `developer.md` if you change any public interfaces.
5. Open the PR against `development`, not `main`.

We review PRs within a few days. We will suggest changes when needed, but we aim to be
constructive, not gatekeeping.

## Questions

Open an issue or start a discussion. We are happy to talk through design questions before
you write a line of code.

