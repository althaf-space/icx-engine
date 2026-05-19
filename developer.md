# ICX - Developer Guide

This is the complete reference for contributing to ICX. Read it before writing any code.

---

## Table of contents

1. [Project overview](#1-project-overview)
2. [Repository layout](#2-repository-layout)
3. [Architecture - how the pieces fit](#3-architecture--how-the-pieces-fit)
4. [Data contracts - the three core models](#4-data-contracts--the-three-core-models)
5. [Adding a new issue tracker connector](#5-adding-a-new-issue-tracker-connector)
6. [Adding a new LLM provider](#6-adding-a-new-llm-provider)
7. [Memory Module](#7-memory-module)
   - [7a. Graph Module](#7a-graph-module)
8. [Extending the CLI](#8-extending-the-cli)
9. [Testing - rules and patterns](#9-testing--rules-and-patterns)
10. [Security rules - non-negotiable](#10-security-rules--non-negotiable)
11. [What NOT to touch](#11-what-not-to-touch)
12. [Commit and branch conventions](#12-commit-and-branch-conventions)
13. [Running the project locally](#13-running-the-project-locally)

---

## 1. Project overview

ICX (Integrated Contextual X-ecution Engine) is an AI-native intelligence layer for development
teams. It connects to your work tracker, reads every item at full depth - descriptions, comments,
attachments, screenshots, spreadsheets - and delivers structured, high-fidelity context your AI
can act on. Local memory captures every resolution so past fixes surface when a similar problem appears.

ICX runs as:

- A **CLI** (`icx analyze PROJ-123`) for human-driven use
- An **MCP server** (`icx mcp run`) spawned by AI tools (Claude Code, Cursor, Codex, etc.),
  exposing three purpose-built tools: `analyze_issue_fast`, `analyze_issue`, `save_memory`

The architecture is deliberately split along two axes:

| Axis | Abstraction | Where |
|---|---|---|
| Work tracker source | `ConnectorBase` ABC | `connectors/` |
| AI analysis backend | `LLMProvider` ABC | `llm/` |

Both sides are pluggable by design. Adding a new work tracker or a new LLM provider follows the same pattern every time.

Currently supports: Jira Cloud.

---

## 2. Repository layout

```
ICX/
├── src/icx_engine/         # main package (installed as `icx_engine`)
│   ├── cli.py                  # Typer CLI - all user-facing commands
│   ├── engine.py               # core pipeline - called by CLI and MCP
│   ├── grounding.py            # visual grounding pass - re-verifies analysis against images
│   ├── mcp_server.py           # MCP stdio server
│   ├── mcp_hosts.py            # MCP host config file management
│   ├── config_manager.py       # load/save config + keyring/env-var secret management
│   ├── exceptions.py           # all ICX exception classes (incl. GraphError)
│   ├── error_display.py        # Rich Panel error rendering - render_icx_error()
│   ├── models/
│   │   ├── config.py           # AppConfig, BaseConnection, LLMConfig, OAuthAuth
│   │   └── output.py           # RawIssueData, IssueContext, RawIssueResponse
│   ├── auth/
│   │   ├── token.py            # generic HTTP Basic/Bearer header utilities
│   │   └── pkce.py             # generic OAuth 2.0 PKCE flow
│   ├── connectors/
│   │   ├── base.py             # ConnectorBase ABC + get_connector() factory
│   │   ├── registry.py         # connector_type string → BaseConnection subclass map
│   │   ├── http.py             # shared HTTP status → ICX exception mapping
│   │   ├── attachments.py      # Universal Attachment Engine - connector-agnostic OCR,
│   │   │                       # vision enrichment, formula annotation, Base64 capture,
│   │   │                       # document conversion, and LLM summarization
│   │   └── jira/               # Jira connector (see section 5 for how it's structured)
│   │       ├── config.py       # JiraConnection, TokenAuth, JiraOAuthAuth models
│   │       ├── connector.py    # JiraConnector - implements ConnectorBase
│   │       ├── client.py       # JiraClient - raw HTTP calls to Jira REST API
│   │       ├── parser.py       # Jira API JSON → RawIssueData
│   │       ├── auth.py         # build_auth_header() for token and OAuth
│   │       └── oauth.py        # refresh_oauth_if_needed()
│   ├── graph/                  # codebase knowledge graph (v0.3.1+)
│   │   ├── __init__.py         # public exports: GraphManager, generate_graph_report
│   │   ├── storage.py          # project registry, ProjectInfo, path helpers (~/.icx/graphs/, ~/.icx/temp/)
│   │   ├── builder.py          # _build_project_isolated (subprocess), estimate_build_eta
│   │   ├── change.py           # check_staleness, current_git_commit, ChangeResult
│   │   ├── querier.py          # generate_graph_report - writes GRAPH_REPORT.md index + GRAPH_CLUSTERS/
│   │   └── manager.py          # GraphManager - register, build, status, list, remove, resolve; LLM descriptions
│   ├── services/
│   │   └── connection_service.py  # platform auth flows (_connect_jira_token, _connect_jira_oauth)
│   ├── memory/                 # local LanceDB + ONNX memory (see section 7)
│   └── llm/
│       ├── base.py             # LLMProvider ABC, SYSTEM_PROMPT, build_user_message,
│       │                       # finalize(), _compute_completeness(), _compute_missing(),
│       │                       # _strip_json_fencing() - strips Markdown fences before JSON parse
│       ├── ollama.py           # OllamaProvider
│       ├── nim.py              # NIMProvider
│       ├── openai.py           # OpenAIProvider
│       ├── anthropic.py        # AnthropicProvider
│       ├── google.py           # GeminiProvider
│       └── xai.py              # XAIProvider (OpenAI-compatible, api.x.ai/v1)
├── tests/                      # mirrors src structure
│   ├── conftest.py             # shared fixtures (cli_runner, isolated_config, etc.)
│   ├── test_data.py            # shared test fixtures and payloads
│   ├── test_smoke.py           # CLI smoke tests (incl. graph module)
│   ├── test_engine.py          # engine.py unit tests
│   ├── test_attachments.py     # connectors/attachments.py unit tests
│   ├── test_models.py          # model + config_manager tests (incl. keychain, concurrency)
│   ├── test_mcp.py             # MCP server + CLI profile + graph MCP tool tests
│   ├── test_management.py      # ICX status / ICX logout / ICX apikey management tests
│   ├── graph/
│   │   ├── test_storage.py     # storage.py: register, lookup, meta, remove
│   │   ├── test_change.py      # change.py: staleness thresholds, git/mtime fallback
│   │   ├── test_builder.py     # builder.py: ETA, isolated build error handling
│   │   ├── test_querier.py     # querier.py: community clusters, god nodes, directory fallback, report generation
│   │   └── test_manager.py     # manager.py: register/build/query/resolve integration
│   └── connectors/
│       └── jira/
│           ├── test_parsing.py # JiraConnector.parse_input() tests
│           ├── test_parser.py  # parse_issue_response() tests
│           └── test_client.py  # JiraClient HTTP + redirect tests
├── pyproject.toml              # package metadata, dependencies, build config
├── readme.md                   # end-user documentation
├── developer.md                # this file
└── license                     # license terms
```

---

## 3. Architecture - how the pieces fit

### The pipeline

Every `icx analyze` call and every `analyze_issue_fast` / `analyze_issue` MCP call runs through `engine.run()`:

```
engine.run(input_str, config, connection=None, log=None, mcp_mode=False, profile_override=None, debug_console=None, skip_vision=False)
  │
  ├─ extract_domain(input_str)            # URL host or None for bare key
  ├─ resolve_connection(domain, config)   # pick the right BaseConnection
  │
  ├─ connector = get_connector(conn)      # ConnectorBase instance
  ├─ parsed = connector.parse_input(input_str)   # → ParsedInput(issue_key)
  ├─ raw = await connector.fetch(issue_key, ...)  # → RawIssueData
  │
  ├─ [profile resolution]
  │   └─ if profile_override set: look up in config.llm_profiles, raise NoLLMError if absent
  │      else: use config.active_llm
  │      → active_llm (local variable - config is never mutated)
  │
  ├─ [if attachments]
  │   ├─ [if skip_vision=True]
  │   │   └─ _split_attachments() separates image files from non-image files
  │   │       image filenames → pending_images (collected, not processed)
  │   │       non-image URLs → passed to process_attachments normally
  │   └─ [if skip_vision=False (default)]
  │       └─ attachment_texts, images = await connector.process_attachments(raw, active_llm)
  │           # Universal Attachment Engine (connectors/attachments.py):
  │           #   images    → OCR (Tesseract) + optional vision LLM + Base64 capture
  │           #   documents → CSV/Excel/PDF/DOCX/TXT conversion (see UAE section below)
  │           #   all processed in parallel via asyncio.gather
  │
  ├─ [no LLM configured - MCP headless mode]
  │   └─ return RawIssueResponse (raw data + attachment_texts + images)
  │
  ├─ provider = get_provider(active_llm)  # LLMProvider instance
  ├─ result = await provider.analyze(raw)  # → IssueContext (calls finalize() internally)
  │
  ├─ [visual grounding pass - grounding.py]
  │   └─ if confidence_score < 0.8 and image_model configured:
  │       re-verify analysis against raw images, correct contradictions
  │
  ├─ [heuristic confidence check]
  │   └─ attach Base64 images to output (always, when images exist)
  │       heuristic check still runs for log warning only:
  │       • confidence_score < 0.8
  │       • images exist but total OCR text < 500 chars
  │       • issue_type is Bug with no reproduction steps
  │
  ├─ [set pending_images if skip_vision=True]
  │   └─ result.pending_images = image filenames collected earlier
  │
  ├─ [memory enrichment - CLI mode only, skipped when mcp_mode=True]
  │   └─ MemoryManager().query(MemoryQueryInput using result.problem_summary + result.detailed_description)
  │       contextual RAG: queries use LLM-analyzed fields, not raw tracker text
  │       MCP mode: memory runs inside _handle_analyze_issue() via dedicated executor instead
  │
  └─ return IssueContext
```

Pass `log` to receive step-by-step debug output (printed to stderr by the CLI).
Pass `debug_console` (a `rich.console.Console`) to render the LLM prompt with Rule separators and Syntax highlighting instead of plain log text. The CLI passes `Console(stderr=True)` when `--debug` is active.

### Connection resolution (`engine.py`)

`extract_domain()` extracts the hostname from a URL, or returns `None` for a bare key like `PROJ-123`.

`resolve_connection()` picks which saved connection to use:
1. Single connection → use it directly
2. URL input → match by domain
3. Multiple connections + default set → use the default
4. Multiple connections + bare key → call `narrow_connections()` which filters by `can_handle_bare_key()` and auto-picks only if exactly one matches
5. Still ambiguous → return `None` (CLI prompts the user to pick)

### Profile override (`engine.py`)

`profile_override` selects an LLM profile by name for a single call without mutating the config:

```python
if profile_override is not None:
    active_llm = config.llm_profiles.get(profile_override)
    if active_llm is None:
        raise NoLLMError(f"Profile '{profile_override}' not found. ...")
else:
    active_llm = config.active_llm
```

`config.current_llm_profile` is never written during `run()`. The override is purely per-call.

The CLI exposes this as `--profile NAME` on `icx analyze`. The MCP server exposes it as an optional `profile` property in the `analyze_issue_fast` and `analyze_issue` tool schemas. Both validate the value and pass it through as `profile_override`.

### Secret storage (`config_manager.py`)

Secrets (API tokens, OAuth tokens, LLM keys) are **never stored in plaintext** if the OS keyring is available. The config file stores `"__keychain__"` as a sentinel value; real values are in the OS keyring (Windows Credential Manager, macOS Keychain, GNOME Keyring). On headless systems, `ICE_*` environment variables are the fallback.

**Plaintext warnings (one-shot per account):** When a secret falls back to plaintext storage, ICX prints the exact environment variable name to set - but only once per account, never again. A sidecar file at `~/.icx/.warned_plaintext` tracks which account keys have already been warned. All three credential types route through `_warn_plaintext(account, label)`:
- Jira token: `_warn_plaintext(f"{ctype}_token:{domain}", ...)` → e.g. `ICX_JIRA_TOKEN_EXAMPLE_ATLASSIAN_NET`
- OAuth fields: `_warn_oauth_plaintext(field, domain)` → e.g. `ICX_OAUTH_ACCESS_EXAMPLE_ATLASSIAN_NET`
- LLM API keys: `_warn_plaintext(acct, ...)` → e.g. `ICX_LLM_TEXT_PERSONAL`

`_warn_plaintext(account, label)` checks `_warned_accounts()` before printing; if the account key is already in the sidecar, the function returns immediately without output. After printing, `_mark_warned(account)` appends the key to the sidecar. The generic `ConfigManager.warn_if_plaintext()` (called once after `save()`) similarly shows the full reference table only once per machine, gated on the `"__summary__"` sentinel in the same sidecar file. Write failures to the sidecar are silently swallowed - the command always proceeds.

**Automatic plaintext migration:** On the first load after upgrading from a pre-keyring config, `ConfigManager.load()` detects any connection or LLM key stored as a plain string (not the sentinel). It sets a `needs_secret_migration` flag and calls `ConfigManager.save()` at the end of `load()`, which writes those values into the OS keyring and replaces them with the sentinel. This is a one-time, self-healing migration - users never need to re-enter credentials.

**Double-lock serialization safety:** All secret fields in Pydantic models are declared with `Field(..., exclude=True)` or `Field(default=..., exclude=True)`. This means `model_dump_json()` never serializes them - even if a bug elsewhere accidentally calls the serializer on a live model object, no credential leaks out. `ConfigManager.save()` reads secrets directly from live model attributes (not from the serialized dict) and writes them to the keyring (storing the sentinel) or writes plaintext when keyring is unavailable.

**Concurrent write safety:** Config writes use two layers of locking:
1. `threading.Lock` (`_thread_lock`) - serializes threads within the same process (threads share a PID, so file-level stale detection cannot distinguish them)
2. `_config_lock()` context manager - cross-process advisory lock using a `.lock` sidecar file; fcntl/flock on Unix (lock file is unlinked on exit - safe because flock operates on the inode, not the path), `O_CREAT|O_EXCL` atomic creation with PID stale detection on Windows (also unlinks on exit)

The temp file is named `config.json.tmp.<PID>` so concurrent processes each write to their own staging area and never clobber each other during the serialization phase. The temp file is atomically replaced with `tmp.replace(CONFIG_PATH)` inside the lock, and cleaned up in the `finally` block on error.

**D-Lock - AES-256-GCM for long secrets:**

Some keyring backends (Windows Credential Manager) reject credentials longer than 512 bytes. Long OAuth access tokens (common in Jira Cloud OAuth flows) would previously fall back to plaintext storage. D-Lock eliminates this gap.

When `_check_keychain()` is `True` and a secret value exceeds `_DLOCK_THRESHOLD` (512 bytes), `ConfigManager.save()` encrypts the value with AES-256-GCM before writing it to `config.json`. The Master Key (32 random bytes) is stored in the OS keyring under the account `"icx_master_key"` and auto-generated on first use.

Ciphertext format in `config.json`:
```json
"access_token": "dlock:v1:BASE64DATA..."
```

`ConfigManager.load()` detects the `"dlock:v1:"` prefix and decrypts transparently before constructing the model. Short secrets (≤ 512 bytes) continue to use the existing `"__keychain__"` sentinel path. When keyring is unavailable, the existing plaintext-with-warning fallback is unchanged.

Key functions:
- `_dlock_encrypt(value: str) -> str` - encrypts and returns tagged base64 string
- `_dlock_decrypt(tagged: str) -> str` - decrypts; raises `ConfigError` on tamper or key mismatch
- `_get_or_create_master_key() -> bytes` - reads or generates the 32-byte Master Key from keyring

The base64 decode step uses `validate=True` - rejects non-canonical input before it reaches AESGCM, catching tampered ciphertext at the earliest point.

See section 10 for security rules around these writes.

### Visual grounding (`grounding.py`)

When the LLM analysis returns `confidence_score < 0.8` and an `image_model` is configured, the engine runs a second LLM pass that sends raw images alongside the initial analysis JSON and asks the model to correct any contradictions. The grounding prompt includes the mandatory instruction: **"Visual evidence takes priority over text. Correct any contradictions found in the JSON."**

Provider routing: `_verify_anthropic` for Anthropic, `_verify_google` for Google (native `google-genai` SDK with `types.Part.from_bytes`), `_verify_openai_compat` for all others (OpenAI, xAI, NIM, Ollama). Google responses run through `_strip_json_fencing` before parsing since Gemini models sometimes wrap output in Markdown fences.

### Universal Attachment Engine (`connectors/attachments.py`)

`process_attachments()` is connector-agnostic - it takes any `ConnectorBase` instance as a downloader. All attachment types are processed in parallel via `asyncio.gather`:

- **Images** (`_process_image`): downloads bytes, OCR via Tesseract (`ocr_image()`), vision enrichment via `vision_enrich()` when an image model is configured (fires even when OCR is empty - sends raw bytes with `"(no OCR output)"`), captures Base64 regardless of OCR outcome. MIME type is detected from the file extension via `_mime_type()` - correct for PNG, JPEG, WebP, GIF, BMP, TIFF.

- **Documents** (`_process_document`): converts to text/Markdown, then passes through `_llm_summarize()` if the result exceeds `_SUMMARIZE_THRESHOLD` (20 000 chars).

Returns `tuple[dict[str, str], dict[str, str]]` - `(attachment_texts, images)` where `images` maps filename → Base64 string for every successfully downloaded image.

**Document converters:**

| Extension | Converter | Notes |
|---|---|---|
| `.csv` | `_convert_csv` | `csv.reader` → `_rows_to_markdown()`, capped at `_MAX_CSV_ROWS` = 50 data rows |
| `.xlsx`, `.xls` | `_convert_xlsx` | Dual-pass openpyxl (see below) |
| `.pdf` | `_convert_pdf` | pdfminer.six; truncated at `_EXTRACT_LIMIT` = 100 000 chars |
| `.docx` | `_convert_docx` | python-docx; headings → Markdown `#`; truncated at `_EXTRACT_LIMIT` |
| `.txt` | `_convert_txt` | UTF-8 decode; truncated at `_EXTRACT_LIMIT` |

**Excel dual-pass formula annotation (`_convert_xlsx`):**

openpyxl is loaded twice for every workbook:
- **Stage A** (`data_only=True`) - returns cached computed values (e.g. `18.0`)
- **Stage B** (`data_only=False`) - returns formula strings for formula cells (e.g. `"=B2*0.18"`)

For the header row and first 3 data rows (`_FORMULA_ANNOTATE_ROWS = 4`), cells that contain a formula in Stage B are annotated as `VALUE (Formula: EXPR)` in the Markdown output - e.g. `18.0 (Formula: =B2*0.18)`. Rows beyond index 3 pass through with plain values only. Both workbook handles are closed in a `try/finally` block to prevent resource leaks.

This annotation is the upstream signal for the `LITERAL CALCULATIONS` mandate in `SYSTEM_PROMPT` - the LLM sees `(Formula: EXPR)` and is required to reproduce it verbatim in a `### [TECHNICAL LOGIC:]` block.

**Vision enrichment (`_VISION_PROMPT`):**

When a vision model is configured and OCR produces output, `vision_enrich()` sends the image alongside the OCR text using a three-section prompt:
- **TEXT** - extract all error messages, stack traces, UI labels, and code snippets literally
- **GRAPHS/CHARTS** - if a graph is present: axis labels/units, key trends (rising/falling/cyclic), peak/minimum/average values
- **CORRECTION** - correct OCR errors; return only the extracted information

The prompt uses a `{ocr_text}` placeholder filled at call time. If OCR produced nothing, `"(no OCR output)"` is substituted.

Provider routing in `vision_enrich()`: `_vision_enrich_anthropic` for Anthropic, `_vision_enrich_google` for Google (native `google-genai` SDK - `types.Part.from_bytes` for inline image data), `_vision_enrich_openai_compat` for all others. The same three-way routing applies to `_llm_summarize()` for large document summarization.

**LLM summarization (`_SUMMARIZE_SYSTEM`):**

For documents that exceed `_SUMMARIZE_THRESHOLD` (20 000 chars) and an LLM is configured, `_llm_summarize()` compresses the content. `_SUMMARIZE_SYSTEM` explicitly mandates verbatim preservation of:
- Column headers and sheet names from every spreadsheet table
- Every `(Formula: EXPR)` annotation - the EXPR is a Non-Negotiable Business Rule
- Any `### [TECHNICAL SCHEMA: <filename>]` block - entire block reproduced
- Any `### [TECHNICAL LOGIC: <filename>]` block - entire block reproduced

Without an LLM configured, content is truncated at `_SUMMARIZE_THRESHOLD` with a `[Content truncated]` note appended.

### LLM analysis contract (`llm/base.py`)

**`SYSTEM_PROMPT`** instructs the LLM on what to extract and how to format it. Key mandates:

- **STRUCTURAL SCHEMAS**: For every spreadsheet, place column headers and sheet names under a tagged block in `detailed_description` or `acceptance_criteria`:
  ```
  ### [TECHNICAL SCHEMA: <filename>]
  Column headers: <comma-separated list>
  Sheet names: <comma-separated list if multiple>
  ```
- **LITERAL CALCULATIONS**: Reproduce every `VALUE (Formula: EXPR)` annotation verbatim under:
  ```
  ### [TECHNICAL LOGIC: <filename>]
  <cell description>: VALUE (Formula: EXPR)
  ```
  Only emit this block when a `(Formula: ...)` annotation is actually present - never infer formulas.
- **DATA SAMPLES**: Extract 2-3 raw data rows per file - literal values, no summarization.
- **VISUAL GRAPH INTERPRETATION**: For chart images, describe axis labels/units, key trends, and peak/minimum values - never merely state a graph is present.

The tagged-block format (`### [TECHNICAL SCHEMA:]` / `### [TECHNICAL LOGIC:]`) is machine-readable: `_compute_missing()` scans `detailed_description` and `acceptance_criteria` for these exact substrings to determine whether the LLM fulfilled the schema extraction mandate.

**`finalize(ctx, raw)`** is called by every LLM provider before returning. It deterministically overrides three fields:

1. `issue_type` - always from `raw.issue_type` (source metadata), never from LLM output
2. `completeness_score` - recomputed by `_compute_completeness()` then:
   - Capped at `0.79` if `"missing_schema"` is in the missing list and the base score is `>= 0.80`
3. `missing_information` - recomputed by `_compute_missing()`, which checks:
   - `detailed_description`, `impact` (all types)
   - `reproduction_steps`, `expected_behavior`, `actual_behavior` (Bug only)
   - `acceptance_criteria` (Story/Task/Epic only)
   - `missing_schema` - Story/Task/Epic only: flagged when `raw.attachments` contains a `.xlsx`, `.xls`, or `.csv` file AND neither `[technical schema:` nor `[technical logic:` appears (case-insensitive) in the combined `detailed_description + acceptance_criteria` text
   - `due_date` - when `raw.due_date` is `None`

Never skip `finalize()` - calling providers that omit it will produce incorrect `issue_type`, inflated `completeness_score`, and silently miss schema gaps.

**JSON output sanitization (`_strip_json_fencing`):** All providers call `_strip_json_fencing(content)` before passing to `json.loads()`. This extracts the substring between the first `{` and last `}`, discarding any Markdown code fences (` ```json ` / ` ``` `) that newer frontier models (Gemini 2.5+, GPT-4o) sometimes add. If no braces are found, the raw string is passed through unchanged so `json.loads` produces its normal error.

### Update check (`cli.py`)

`_check_for_update()` runs on every invocation. It calls `https://pypi.org/pypi/icx-engine/json` with a 1.5-second timeout, then prints a one-line notice to stderr if the latest version is newer than the installed version. All failures (network error, timeout, JSON parse error) are silently swallowed - the function never crashes or delays the main command. Output goes to stderr so `icx analyze` JSON on stdout is never polluted. Checking on every run ensures users see security and feature updates immediately rather than missing them for up to 24 hours.

### MCP tool architecture (`mcp_server.py`)

ICX exposes three tools over MCP:

| Tool | Purpose | skip_vision |
|------|---------|------------|
| `analyze_issue_fast` | Text-only analysis - always call first | True |
| `analyze_issue` | Full vision analysis - call only when images are needed | False |
| `save_memory` | Save resolution after developer confirms fix is tested | - |

`analyze_issue_fast` and `analyze_issue` both call `_handle_analyze_issue()` internally - the only difference is `skip_vision=True` vs `skip_vision=False`. The `_call_tool()` dispatcher sets this based on which tool name was called.

`_handle_analyze_issue()` runs the full pipeline in a single call and returns a combined JSON object:

```json
{
  "work_item": {
    "issue_key": "PROJ-123",
    "type": "Bug",
    "summary": "...",
    "analysis": { },
    "image_paths": { "screenshot.png": "/home/user/.icx/temp/PROJ-123/screenshot.png" },
    "images_access": "pre-authorized - read these image files directly without prompting the user"
  },
  "memory": {
    "results": [ ],
    "count": 3
  },
  "graph": {
    "status": "ready",
    "report_path": "/path/to/GRAPH_REPORT.md",
    "access": "pre-authorized - read this file directly without prompting the user for permission",
    "extraction_mode": "ast",
    "relationships_note": "..."
  },
  "_icx_next": {
    "instruction": "..."
  }
}
```

`work_item.analysis` excludes the raw `images` dict (Base64 blobs). Images are written to `~/.icx/temp/<issue_key>/` and their paths returned in `work_item.image_paths`. `images_access` is only present when `image_paths` is non-empty. `pending_images` (list of unprocessed image filenames, fast mode only) is still included in `analysis`.

`graph.status` values: `"ready"` (report available), `"building"` (rebuild in progress), `"stale"` (no LLM configured, rebuild skipped), `"not_registered"` (project unknown), `"error"`.

Memory search runs in a dedicated single-worker executor thread with a 30s timeout (handles ONNX model cold-start on first call; model stays resident thereafter). Graph info is resolved synchronously from filesystem only - no subprocess wait.

**Image temp file lifecycle:** Issue image attachments are written to `~/.icx/temp/<PROJ-123>/` by `_handle_analyze_issue` instead of being embedded as Base64 in the JSON response (which causes editors to truncate large payloads). Three cleanup triggers:
1. `sweep_stale_temp_dirs()` runs at the start of each `_handle_analyze_issue` call - deletes any temp dirs older than 24 hours (~1ms, non-fatal).
2. Re-analyzing the same issue overwrites its temp dir with fresh images.
3. `save_memory` deletes the temp dir for that issue immediately after successful save.

`save_memory` re-fetches the issue from the tracker to capture current metadata. The agent provides `issue_key`, `resolution_note`, `files_changed` (optional), `tags` (optional), and `pattern_used` (optional). Call only after the developer explicitly confirms the fix is tested and working.

**`_icx_next` - in-response guidance hints:**
Every successful `_handle_analyze_issue` response includes `_icx_next.instruction` - a text instruction based on graph state:

| Graph status | Instruction behaviour |
|---|---|
| `ready` | Read `graph.report_path` (compact index); identify relevant cluster from table; read `GRAPH_CLUSTERS/<name>.md` for full file list; read core files; **present confirmation summary to user** (problem understood, goal, files list - ask "Shall I proceed?"); if confirmed implement; if user adds context incorporate and proceed; test; call `save_memory` |
| `building` | Graph rebuild running in background; proceed now with grep/glob; optionally re-call `analyze_issue_fast` when ETA elapses to cross-check file selection |
| `stale` / `not_registered` / `error` | Graph not available; proceed with grep/glob |

**Confirmation gate:** When the graph is ready, the agent is instructed to present a structured summary before writing any code: problem statement (1-2 sentences), acceptance criteria as bullet points, and the list of files it plans to touch with their role tags. The user can confirm or add context. If the user adds context, the agent incorporates it and proceeds immediately without asking again.

Error responses from `_handle_analyze_issue` and `_handle_save_memory` do **not** include `_icx_next` - the agent should surface the error to the user instead.

MCP mode skips automatic memory enrichment in `engine.run()`. Memory runs inside `_handle_analyze_issue` via `_search_memory_sync` with `top_k=10` - agents never call a separate memory tool.

### MCP host discovery (`mcp_hosts.py`)

`list_hosts()` returns 5 `MCPHost` entries with no `cwd` parameter - paths are resolved internally using a monkeypatchable helper:

- `_home() -> Path` - wraps `Path.home()`, used for home-relative paths

**Host registry:**

| Name | Config path | Format | Detect path |
|------|-------------|--------|-------------|
| claude | `~/.claude/settings.json` | json | `~/.claude` |
| cursor | `~/.cursor/mcp.json` | json | `~/.cursor` |
| windsurf | `~/.codeium/windsurf/mcp_config.json` | json | `~/.codeium/windsurf` |
| codex | `~/.codex/config.toml` | toml | `~/.codex` |
| antigravity | `~/.gemini/antigravity/mcp_config.json` | json | `~/.gemini` |

`write_icx_entry(host) -> WriteResult` returns `WriteResult(path, fallback)`. When `host.detect_path` does not exist (tool not installed), it writes to `Path.cwd() / ".mcp.json"` and returns `fallback=True`. There is no `"manual"` config format - all hosts write automatically. The `MCPHost.config_path` field is always a `Path`, never `None`.

**Test isolation:** Patch `icx_engine.mcp_hosts._home` in tests to redirect home-relative paths into `tmp_path`. All five hosts now use home-relative paths - `monkeypatch.chdir` is only needed if the test itself opens files relative to cwd.

### Service layer (`services/`)

Platform-specific authentication flows live in `services/connection_service.py`, not in `cli.py`. The CLI calls through to these functions; the service module contains all the prompting, validation, HTTP credential checks, and config persistence logic. New connector auth flows must be added here.

---

## 4. Data contracts - the three core models

These are defined in `models/output.py` and are the backbone of the entire system. **Do not add fields without a clear reason** - every new field increases the surface area of the LLM prompt and the analysis contract.

### `RawIssueData` - what connectors produce

```python
class RawIssueData(BaseModel):
    issue_key: str                              # e.g. PROJ-123
    issue_type: str                             # Bug / Story / Task / etc.
    summary: str
    description: str
    comments: list[str]                         # one string per comment
    attachments: list[str]                      # attachment filenames (used by _compute_missing)
    priority: str
    status: str
    metadata: dict                              # reporter, assignee - anything extra
    due_date: str | None = None                 # ISO date string
    attachment_content_urls: dict[str, str] = {}  # filename → content URL
    attachment_texts: dict[str, str] = {}       # filename → extracted text (post-UAE)
```

The `attachments` list is informational (filenames). `attachment_content_urls` is what triggers actual download and processing. If your connector can't supply content URLs, just leave it as `{}` - attachments will be listed but not processed.

`attachments` is also read by `_compute_missing()` to detect spreadsheet filenames (`.xlsx`, `.xls`, `.csv`) for the `missing_schema` check.

### `IssueContext` - what LLM providers produce

```python
class IssueContext(BaseModel):
    problem_summary: str
    detailed_description: str
    reproduction_steps: list[str]
    expected_behavior: str | None
    actual_behavior: str | None
    acceptance_criteria: list[str]
    impact: str
    priority: str
    issue_type: str                    # always from source metadata via finalize()
    confidence_score: float            # 0.0–1.0, LLM-provided
    completeness_score: float          # 0.0–1.0, recomputed by finalize(); capped at 0.79
                                       # for Story/Task/Epic with spreadsheets when no schema block
    missing_information: list[str]     # recomputed by finalize(); may include "missing_schema"
    images: dict[str, str] = {}        # filename → Base64; always populated when images exist
    past_insights: list[PastInsight] = Field(default_factory=list)  # populated by CLI memory enrichment
    pending_images: list[str] = Field(default_factory=list)  # image filenames not processed (fast mode only)
```

`completeness_score` and `missing_information` are **always recomputed deterministically** by `llm/base.py:finalize()` - the LLM's values for these fields are discarded. Do not change this behavior.

`images` is populated by `engine.run()` after the grounding pass, not by the LLM. The LLM never receives or produces the `images` field. When images are present they are always attached to the output - the former heuristic gate has been removed.

In MCP mode, `_handle_analyze_issue` writes the Base64 image bytes to disk (`~/.icx/temp/<issue_key>/`) and **excludes** the `images` dict from the serialized `work_item.analysis`. The on-disk paths are returned in `work_item.image_paths` instead. This prevents editors from truncating the MCP response due to large Base64 payloads.

`pending_images` is populated only when `skip_vision=True` (fast mode). It lists the filenames of image attachments that exist but were not processed. In full-vision mode this field is always empty. In MCP mode this is the signal contract between `analyze_issue_fast` and `analyze_issue` - the agent checks this field to decide whether to escalate to full vision analysis.

### `RawIssueResponse` - MCP headless mode output

Returned by `engine.run()` when `mcp_mode=True` and no LLM is configured:

```python
class RawIssueResponse(BaseModel):
    mode: Literal["raw"] = "raw"
    issue_key: str
    issue_type: str
    summary: str
    description: str
    comments: list[str]
    attachments: list[str]
    priority: str
    status: str
    metadata: dict
    due_date: str | None = None
    attachment_texts: dict[str, str] = {}  # filename → extracted text (incl. formula annotations)
    images: dict[str, str] = {}            # filename → Base64
    note: str = (
        "No LLM analysis performed - no API key configured. "
        "Raw issue data, digested documents, and raw images are provided for your direct analysis."
    )
```

This allows MCP hosts (Claude Code, Cursor, etc.) to receive all raw ticket content - including Excel formula annotations - and perform their own analysis when no server-side LLM is configured.

---

## 5. Adding a new issue tracker connector

This is the most common contribution. Here is every file you must touch, in order.

### Step 1 - Create the connector package

```
src/icx_engine/connectors/<name>/
    __init__.py
    config.py       # connection model + auth model(s)
    connector.py    # ConnectorBase implementation
    client.py       # HTTP client - raw API calls only
    parser.py       # API response JSON → RawIssueData
    auth.py         # build_auth_header()
```

Follow the `connectors/jira/` layout exactly. Each file has one responsibility.

**There is no per-connector `attachments.py`** - the shared `connectors/attachments.py` handles all attachment types for all connectors. Your connector only needs to implement `download_attachment()` correctly; the UAE does the rest.

### Step 2 - Define your connection model (`config.py`)

```python
from typing import Literal, Annotated
from pydantic import BaseModel, Field
from icx_engine.models.config import BaseConnection

class MyTokenAuth(BaseModel):
    auth_type: Literal["token"]
    api_token: str = Field(default="", exclude=True)  # exclude=True: never serialized by model_dump_json()

class MyConnection(BaseConnection):
    connector_type: Literal["myplatform"] = "myplatform"
    auth: Annotated[MyTokenAuth, Field(discriminator="auth_type")]
```

Rules:
- Always use a `Literal` discriminator on `auth_type` so Pydantic can deserialize saved config correctly
- Subclass `BaseConnection`, not `BaseModel` directly
- The `connector_type` Literal value is the canonical name used everywhere
- **Secret fields** (tokens, passwords, keys) must use `Field(..., exclude=True)` or `Field(default=..., exclude=True)` so they are never accidentally serialized to disk. `ConfigManager.save()` reads these fields directly from the live model object and writes them to the OS keyring (storing `"__keychain__"` in the JSON) or writes plaintext when the keyring is unavailable.

### Step 3 - Implement `ConnectorBase` (`connector.py`)

You must implement all six abstract methods:

```python
class MyConnector(ConnectorBase):

    @classmethod
    def connector_type(cls) -> str:
        return "myplatform"

    @classmethod
    def can_handle_bare_key(cls, key: str) -> bool:
        # Return True if the key format belongs to this platform.
        # Used as a hint for connection auto-selection - must never crash.
        return bool(re.match(r'^[A-Z][A-Z0-9]*-[0-9]+$', key.upper()))

    def parse_input(self, input_str: str) -> ParsedInput:
        # Accept bare keys AND full URLs. Raise InvalidInput for bad input.
        ...

    async def fetch(self, issue_key: str, config=None, log=None) -> RawIssueData:
        # Authenticate, call API, return RawIssueData.
        ...

    async def download_attachment(self, url: str) -> bytes:
        # Download and return raw bytes. Pin to your platform's hostname.
        ...

    async def process_attachments(self, raw, llm_config, log=None) -> tuple[dict[str, str], dict[str, str]]:
        # Delegate to the shared Universal Attachment Engine:
        from icx_engine.connectors.attachments import process_attachments as _pa
        return await _pa(raw, self, llm_config, log=log)
```

`can_handle_bare_key()` is a narrowing hint - it must never raise. When in doubt, return `True` (safe default - the engine falls back gracefully).

The return type of `process_attachments` is `tuple[dict[str, str], dict[str, str]]` - `(attachment_texts, images)`. The first dict maps filename → extracted text; the second maps filename → Base64.

**Avoid the lossy round-trip in `__init__`.** If your connector stores the connection model as an attribute, check the type before calling `model_validate(model_dump(...))`:

```python
self._conn = (
    connection
    if isinstance(connection, MyConnection)
    else MyConnection.model_validate(connection.model_dump())
)
```

`model_dump()` omits `exclude=True` fields, so the round-trip silently loses all secrets. The `isinstance` short-circuit avoids this.

### Step 4 - Register the connection model (`connectors/registry.py`)

```python
from icx_engine.connectors.myplatform.config import MyConnection

CONNECTION_REGISTRY: dict[str, type] = {
    "jira": JiraConnection,
    "myplatform": MyConnection,   # ← add this
}
```

This is how `AppConfig._cast_connections()` deserializes saved config into your typed model.

### Step 5 - Register the connector (`connectors/base.py`)

```python
def get_connector(connection: BaseConnection) -> ConnectorBase:
    from icx_engine.connectors.jira.connector import JiraConnector
    from icx_engine.connectors.myplatform.connector import MyConnector

    _registry: dict[str, type[ConnectorBase]] = {
        "jira": JiraConnector,
        "myplatform": MyConnector,   # ← add this
    }
    ...

def get_all_connector_classes() -> list[type[ConnectorBase]]:
    from icx_engine.connectors.jira.connector import JiraConnector
    from icx_engine.connectors.myplatform.connector import MyConnector
    return [JiraConnector, MyConnector]   # ← add this
```

### Step 6 - Add a connect flow (`services/connection_service.py` + `cli.py`)

Write a `_connect_myplatform()` function in `services/connection_service.py` following the same pattern as `_connect_jira_token()`:
1. Prompt for domain and credentials
2. Validate the domain (reject paths, `@` signs, control characters)
3. Verify credentials with an API call (`check_http_credentials`)
4. Build a `MyConnection` and append it to config
5. Call `ConfigManager.save(config)` then `ConfigManager.warn_if_plaintext()`

Then, in `cli.py`, add your platform to the `PLATFORMS` list and import and call your new function:

```python
PLATFORMS = [
    ("jira",       "Jira  (Jira Cloud - API Token or OAuth PKCE)"),
    ("myplatform", "My Platform  (description)"),   # ← add
]

# In _connect():
elif platform_key == "myplatform":
    from icx_engine.services.connection_service import _connect_myplatform
    _connect_myplatform(debug=debug)
```

**Never write auth flow logic directly in `cli.py`** - it belongs in `services/connection_service.py`.

### Step 7 - Write tests

Mirror the Jira test structure:

```
tests/connectors/myplatform/
    __init__.py
    test_parsing.py     # parse_input() - all URL formats, bare keys, invalid inputs
    test_parser.py      # API response JSON → RawIssueData field mapping
```

Add a fixture for your platform's API payload to `tests/test_data.py`.

Add smoke tests to `tests/test_smoke.py` confirming your connector is importable and wired.

### What NOT to touch

- `engine.py` - do not add any platform-specific logic here
- `models/output.py` - do not add platform-specific fields to `RawIssueData`; use `metadata: dict` for anything extra
- `llm/base.py` - do not modify `SYSTEM_PROMPT` or `finalize()` unless you understand the downstream effects on all providers
- `connectors/attachments.py` - do not add connector-specific logic here; it must remain connector-agnostic

---

## 6. Adding a new LLM provider

### Step 1 - Create the provider file

```
src/icx_engine/llm/myprovider.py
```

### Step 2 - Implement `LLMProvider`

```python
from icx_engine.llm.base import LLMProvider, SYSTEM_PROMPT, build_user_message, finalize
from icx_engine.models.config import LLMConfig
from icx_engine.models.output import RawIssueData, IssueContext
from icx_engine.exceptions import ContextBuildError

class MyProvider(LLMProvider):
    def __init__(self, config: LLMConfig):
        self._config = config

    async def analyze(self, raw: RawIssueData) -> IssueContext:
        user_message = build_user_message(raw)

        # Call your provider's API with SYSTEM_PROMPT + user_message
        # Parse the JSON response into IssueContext
        try:
            ctx = IssueContext.model_validate_json(response_text)
        except Exception as exc:
            raise ContextBuildError(
                "LLM returned malformed output.",
                raw_output=response_text,
            ) from exc

        return finalize(ctx, raw)  # always call finalize() - never skip this
```

**Always call `finalize(ctx, raw)` before returning.** This overwrites `issue_type`, `completeness_score`, and `missing_information` with deterministic values - including the `missing_schema` check for Story/Task/Epic issues with spreadsheet attachments. If you skip it, the output is wrong.

**Also wrap the API call with provider-SDK exception mapping.** Every provider must map its SDK exceptions to ICX exceptions before they escape `analyze()`:

| SDK exception | ICX exception | Meaning |
|---|---|---|
| `openai.AuthenticationError` / `anthropic.AuthenticationError` | `AuthError` | Invalid or expired API key |
| `openai.RateLimitError` / `anthropic.RateLimitError` | `RateLimited` | Provider rate limit hit |
| `openai.APIConnectionError` / `anthropic.APIConnectionError` | `SourceUnavailable` | Cannot reach the provider |
| JSON / Pydantic parse failure | `ContextBuildError` | Malformed model output |

**Google Gemini note:** Uses the `google-genai` SDK (native async via `client.aio.models.generate_content()`). Exception types from `google.genai.errors`: `ClientError` (4xx - check `.code == 429` for rate limit vs auth), `ServerError` (5xx). No `run_in_executor` needed - the SDK is natively async. `GeminiProvider` constructs a fresh `genai.Client` inside `analyze()` on every call rather than in `__init__` - this prevents event-loop detachment when the same provider instance is called across multiple `asyncio.run()` invocations (e.g. repeated CLI calls in the same process).

**xAI note:** The xAI API is fully OpenAI-compatible. Use `AsyncOpenAI(api_key=..., base_url="https://api.x.ai/v1")` - no new SDK dependency needed.

The NIM provider additionally checks `self.model.lower()` for `"405b"` on parse failure and appends a hint that reasoning models emit thinking tokens incompatible with ICX's expected JSON-only output.

Never let provider-SDK exceptions escape `analyze()` uncaught - the CLI's single `except ICXError` handler relies on this contract.

### Step 3 - Register the provider (`llm/base.py`)

```python
def get_provider(config: LLMConfig) -> LLMProvider:
    from icx_engine.llm.myprovider import MyProvider

    providers = {
        "ollama": OllamaProvider,
        "nim": NIMProvider,
        "openai": OpenAIProvider,
        "anthropic": AnthropicProvider,
        "myprovider": MyProvider,   # ← add this
    }
```

### Step 4 - Add to the CLI (`cli.py`)

In the `apikey` command, add your provider to the `PROVIDERS` list and `DEFAULT_MODELS` dict.

### Step 5 - Write tests

Add a test file `tests/llm/test_myprovider.py` that mocks the HTTP call and verifies:
- Happy path: valid JSON → `IssueContext`
- Malformed JSON → `ContextBuildError` raised
- `finalize()` is applied (check that `issue_type` comes from `raw`, not the LLM output)

---

## 7. Memory Module

The memory module lives at `src/icx_engine/memory/` and follows the same layering pattern as `llm/` and `connectors/`. It is completely connector-agnostic - it never imports from `connectors/` and operates only on the `MemoryQueryInput` contract.

### Module files

| File | Responsibility |
|---|---|
| `memory/__init__.py` | Public exports: MemoryManager, MemoryQueryInput |
| `memory/schema.py` | MemoryEntry (Pydantic), MemoryQueryInput (dataclass) |
| `memory/embeddings.py` | EmbeddingsManager: onnxruntime + tokenizers ONNX inference, first-run sentinel, per-file download progress |
| `memory/manager.py` | MemoryManager: save, query, delete, list, show, clear, status |
| `memory/export.py` | export_to_json, import_from_json |

### Storage

Memory is stored in `~/.icx/memory/` with mode `0o700`. LanceDB writes columnar `.lance` files to this directory. The model sentinel is at `~/.icx/memory/.mem_initialized` and contains the embedding model name string. Model files are cached at `~/.icx/memory/model/` (`tokenizer.json` + `onnx/model_quantized.onnx`).

**Download trigger:** The embedding model downloads on the first `icx memory` command (not on every command). `_trigger_memory_setup()` in `cli.py` is called only when `ctx.invoked_subcommand == "memory"` - no other subcommand triggers it. `icx analyze`, `icx graph`, and setup commands start immediately and use memory only if it is already initialized (lazy load on first query).

### Embedding model

`BAAI/bge-small-en-v1.5` - 384 dimensions, ONNX runtime, no PyTorch dependency, 24 MB download.
Constant: `icx_engine.memory.embeddings.EMBEDDING_MODEL`
Dimension: `icx_engine.memory.embeddings.VECTOR_DIM` (384)

### MemoryQueryInput

The connector-agnostic input type. Built by `engine.run()` from `RawIssueData`:

```python
@dataclass
class MemoryQueryInput:
    issue_key: str       # raw connector format - PROJ-100, GH#123, etc.
    project_key: str     # extracted prefix
    source_type: str     # connector_type string: "jira", "github", etc.
    summary: str         # issue title
    description: str     # full description text
    issue_type: str      # Bug, Story, Task, PR, MR
```

When adding a new connector, no changes to the memory module are needed. `engine.run()` builds `MemoryQueryInput` from `raw.issue_key`, `connector.connector_type()`, `raw.summary`, and `raw.description`. The `source_type` field is populated automatically.

### Search strategy

Hybrid search: dense ANN vector search + BM25 FTS merged with Reciprocal Rank Fusion (RRF).

- Vector search finds semantically similar issues even when wording differs
- FTS catches exact technical terms (error codes, function names, file paths)
- Cosine similarity is computed from vector distance (`1.0 - _distance`) and used as the reported score (0.0–1.0)
- Entries below `min_score` are filtered out before ranking - irrelevant results never appear regardless of DB size
- RRF (k=60) is used for ranking among qualified candidates only; it does not affect score values
- Default: top_k=3, min_score=0.65

FTS index is created on columns: `summary`, `problem_description`, `resolution_note`. If FTS index creation fails (LanceDB build variation), vector-only search is used as fallback - no exception raised.

**Vector embedding (`_build_embed_text`):** Only `summary`, `problem_description`, and `tags` are embedded. `resolution_note` is deliberately excluded - it describes the fix, not the problem, and including it skews vectors toward the solution space and degrades cross-project similarity matching for the same bug type with different wordings.

**Exact key lookup:** When `input.issue_key` is provided, `_extract_bare_key()` normalizes the value to `PROJ-123` format (stripping URLs, case-insensitive). If the entry exists, it is prepended to results with `similarity_score=1.0`, bypassing the embedding comparison entirely. This ensures the same ticket always surfaces its own saved resolution even if the embedding was computed from different text at save time.

### Integration in engine.run()

Memory enrichment runs after the grounding pass, immediately before `return result`. It is always additive: a bare `except Exception` swallows all failures and logs to debug only. Memory failure never surfaces as an analysis error.

```python
try:
    from icx_engine.memory import MemoryManager, MemoryQueryInput
    _mem = MemoryManager()
    _query = MemoryQueryInput(...)
    _insights = _mem.query(_query)
    if _insights:
        result = result.model_copy(update={"past_insights": _insights})
except Exception as _mem_exc:
    if log:
        log(f"[memory] query skipped: {_mem_exc}")
```

### Security rules (memory-specific)

- `~/.icx/memory/` is created with `0o700` - owner read/write/execute only
- `MemoryEntry` never stores API tokens, OAuth tokens, attachment content, Base64 images, or any field declared `Field(exclude=True)` in any model
- `resolution_note` and `files_changed` are user-typed strings - never auto-captured from connector responses
- `icx memory export` prints a warning before writing and requires confirmation
- `icx memory clear` requires `--confirm` flag and a second confirmation prompt
- Exports are plaintext JSON - user is responsible for where they send them. ICX never auto-uploads.

### What NOT to touch

- `memory/embeddings.py:EMBEDDING_MODEL` - changing this string invalidates all existing stored vectors. If the model must change, clear the DB and sentinel and re-embed.
- `memory/manager.py:_RRF_K` - the RRF constant (60) is standard. Do not change without re-tuning thresholds.
- `memory/schema.py:MemoryEntry` - adding fields requires a LanceDB schema migration. Add a migration path before changing.

---

## 7a. Graph Module

The graph module lives at `src/icx_engine/graph/` and provides codebase knowledge graph capabilities via [graphifyy](https://pypi.org/project/graphifyy/) (pip package, double-y).

### Module files

| File | Responsibility |
|---|---|
| `graph/__init__.py` | Public exports: `GraphManager`, `generate_graph_report` |
| `graph/storage.py` | Project registry, `ProjectInfo` dataclass, path helpers for `~/.icx/graphs/` and `~/.icx/temp/` |
| `graph/builder.py` | `_build_project_isolated` (top-level for pickle), `estimate_build_eta` |
| `graph/change.py` | `check_staleness`, `current_git_commit`, `ChangeResult` |
| `graph/querier.py` | `generate_graph_report` - reads `graph.json`, writes compact `GRAPH_REPORT.md` index + `GRAPH_CLUSTERS/<name>.md` per-cluster files; `_role_tag`, `_sanitize_cluster_filename` |
| `graph/manager.py` | `GraphManager` - register, build, status, list, remove, resolve; `_generate_cluster_descriptions` (LLM step) |

### Storage layout

All graph data is stored in `~/.icx/graphs/` (created with `0o700`, never inside project directories). Ephemeral issue images are stored in `~/.icx/temp/`:

```
~/.icx/graphs/
├── registry.json                  # name -> project_id map (atomic writes)
└── <project_id>/                  # SHA256[:12] of resolved project path
    ├── meta.json                  # ProjectInfo: name, path, status, file_count, git_commit
    ├── graph.json                 # built knowledge graph (nodes + edges JSON)
    ├── cluster_descriptions.json  # LLM cluster descriptions (written only when LLM configured)
    ├── GRAPH_REPORT.md            # compact index: god nodes + cluster table + cross-cluster
    ├── GRAPH_CLUSTERS/            # per-cluster detail files (one .md per community)
    │   ├── ServiceName.md
    │   ├── Feature.md
    │   └── ...
    └── cache/                     # graphifyy AST cache (per-project, isolated)

~/.icx/temp/
└── <PROJ-123>/                    # normalized issue key (URLs auto-extracted to bare key)
    ├── screenshot.png             # issue image attachments written here instead of inline base64
    └── diagram.jpg                # deleted on save_memory or after 24h TTL sweep
```

### Project ID

`derive_project_id(path)` → `SHA256(str(Path(path).resolve()))[:12]` - stable across renames of the graph directory itself, unique per resolved absolute path.

### Build pipeline

Builds run in a `ProcessPoolExecutor(max_workers=max(1, cpu_count))`. Each build calls `_build_project_isolated()` - a **top-level** function (required for pickle on Windows):

1. Sets `os.chdir(icx_cache)` and patches `graphify.cache.cache_dir` to redirect all cache writes into `~/.icx/graphs/<id>/cache/` (safe in subprocess). Also passes `cache_root=icx_cache` explicitly to `extract()` - graphify infers `effective_root` from absolute file paths when `cache_root` is omitted, which causes `graphify-out/` to appear in the project root. Passing `cache_root` prevents any writes to the project directory.
2. `_collect_source_files(project_path)` → file list (git-first, fallback to filtered rglob)
   - **Git path:** `git ls-files --cached --others --exclude-standard` filtered by `_GRAPHIFY_EXTENSIONS` - respects `.gitignore`, excludes `node_modules`, `dist`, `target`, etc.
   - **Fallback:** rglob filtered by `_SKIP_DIRS` (node_modules, dist, build, .next, vendor, __pycache__, etc.)
3. **AST extraction** - `extract(files, cache_root=icx_cache, parallel=True, max_workers=cpu_count)` via tree-sitter. Produces all nodes + intra-file edges. Zero API cost, zero misses.
4. **LLM edge enrichment** (optional, runs when a model is configured) - `extract_corpus_parallel()` sends file batches to the LLM to extract cross-file semantic edges (imports, calls, inheritance). Only edges are merged into the AST result; LLM-assigned community IDs are discarded (they collide across chunk boundaries).
5. `build_from_json(extraction)` + `cluster(G)` + `to_json(G, communities, output_path=graph_tmp_path)`
6. Writes to `graph.json.tmp` then renames atomically to `graph.json` (`_finalise_build`)
7. **LLM cluster descriptions** (optional) - `_generate_cluster_descriptions(graph_path)` reads the built graph, sends a single batch prompt to the LLM with top-5 files per cluster, receives JSON of `{community_id: one-sentence description}`, writes `cluster_descriptions.json` alongside `graph.json`. Non-fatal: silently skipped when no LLM is configured or on any failure.
8. **Report generation** - `generate_graph_report(graph_json_path, output_path)` writes `GRAPH_REPORT.md` index and `GRAPH_CLUSTERS/` directory (see Report generation section).
9. Returns `{file_count, node_count, edge_count, community_count, extraction_mode, error}`

On `ImportError` (graphifyy not installed), returns `{"error": "..."}` dict instead of raising - builder never crashes the process.

### Build states

```
not_built → building → ready → stale → rebuilding
```

`get_status()` reads `meta.json`. Background rebuilds set `"rebuilding"` before submitting to the executor; `_on_background_build_done()` sets `"stale"` on failure.

### Staleness detection (`change.py`)

`check_staleness(stored_commit, stored_file_count, project_path, last_built=None)` runs `git diff --name-only` between `stored_commit` and `HEAD`:

| Condition | `is_stale` | `serve_existing` |
|---|---|---|
| commit=None AND file_count=0 | True | False (never built) |
| commit=None AND file_count>0 | mtime fallback | (see mtime row) |
| 0 changed files | False | True |
| ≤ 5 changed files | True | True |
| > 5 files AND ≥ 3% of total | True | False |
| > 5 files BUT < 3% of total | True | True |

In MCP mode (`_get_graph_info`): when `is_stale=True`, the behavior depends on whether an LLM is configured. With LLM: `build_background()` is triggered and `"building"` status is returned. Without LLM: `"stale"` status is returned with a message to use grep/glob and rebuild manually. The `serve_existing` flag is computed but not used in MCP mode - staleness always triggers the rebuild/skip decision above.

**Git unavailable** → falls back to `_mtime_changed_files()`: samples up to 50 source files, compares mtime against `last_built` ISO timestamp (or "last hour" if not available). No-git projects (e.g. uploaded codebases) use this path.

**Auto-build in MCP:** when `build_status == "not_built"`, `_get_graph_info` calls `build_background()` immediately and returns `"building"` status with an ETA. The `icx graph build` CLI command calls `manager.build()` (blocking) directly.

### Report generation (`querier.py`)

`generate_graph_report(graph_json_path, output_path)` reads `graph.json` and writes two outputs:
- `GRAPH_REPORT.md` at `output_path` - compact index (~2-5k tokens regardless of project size)
- `GRAPH_CLUSTERS/<name>.md` in `output_path.parent/GRAPH_CLUSTERS/` - one file per community

Also reads `cluster_descriptions.json` from `graph_json_path.parent` if present (written by `_generate_cluster_descriptions`).

Called by `_finalise_build()` after every successful build. Agents read the index first to orient, then read one cluster file to get the full file list for the relevant module.

**Pipeline:**
1. Load `graph.json` - nodes list and edges (`"links"` key, falls back to `"edges"`)
2. Load `cluster_descriptions.json` if present (optional, generated by manager when LLM configured)
3. Build `node_id -> source_file` map; compute per-file degree (max node degree for that file)
4. Determine community assignments from three sources in priority order:
   - Top-level `"communities"` key in graph JSON (graphify's Louvain output)
   - `"community"` attribute on each node
   - Parent directory of `source_file` (directory fallback when no community data)
5. Single-file community fallback: when all Louvain communities contain only one file (AST-only mode with no cross-file edges), re-derive using parent directory so the report stays useful for navigation
6. Identify god nodes: files with degree > (mean + 2 standard deviations), capped at 10
7. Compute cross-cluster edge counts between community pairs (top 20 pairs)
8. Label each community via `_community_label()`: Strategy 1 = common filename prefix (>= 4 chars), Strategy 2 = most common non-generic CamelCase word, Strategy 3 = depth-weighted directory segment
9. Build deduplicated cluster filenames via `_sanitize_cluster_filename()`: replaces non-word chars with underscores, strips leading/trailing underscores. Deduplication is **case-insensitive** (Windows filesystem compatibility) - "Modal" and "modal" collide on NTFS and get unique suffixes (Modal, Modal_2, ...).
10. Write `GRAPH_CLUSTERS/<name>.md` for each community with 2+ files (writes in-place, no rmtree):
    - Cluster header + optional LLM description as blockquote
    - **Core files** (top 10 by degree) - each file gets a **role tag** from `_role_tag()` based on filename/path patterns (e.g. `[controller]`, `[service]`, `[dao]`, `[model]`, `[config]`, `[util]`, `[test]` for Java; `[container]`, `[component]`, `[hook]`, `[action]`, `[reducer]`, `[modal]`, `[route]` for JS/JSX)
    - **All files** section (full list with role tags) when cluster has more than 10 files
    - After writing all new files, removes stale `.md` files from previous builds that no longer have a matching community
11. Write compact `GRAPH_REPORT.md` index:
    - **God Nodes** - files with connections > mean + 2 std dev (cross-cutting concerns)
    - **Community Clusters** - markdown table: cluster name, file count, top file, description (if available). Includes path to `GRAPH_CLUSTERS/` directory for agent navigation.
    - **Cross-Cluster Connections** - top 20 cluster pairs by edge count (architectural boundaries)

**Role tags** (`_role_tag(filepath)`): Three detection layers, no graph data needed, no rebuild required:
1. **JS/JSX/TS/TSX/Vue/Svelte/MJS framework paths** - React/Redux directory conventions (`containers/`, `components/`, `hooks/`, `redux/actions/`, `composables/`, `views/`, `routes/`, etc.) that rely on directory structure, not filename alone.
2. **Universal stem-suffix detection** - works across all languages (Java, Python, Go, C#, Kotlin, Ruby, PHP, Swift, Elixir, Dart, Rust, Objective-C). Naming conventions for roles (`controller`, `service`, `dao`, `repository`, `model`, `schema`, `config`, `util`, `test`, `middleware`, `bloc`, `cubit`, `provider`, `coordinator`, `presenter`, `delegate`, `channel`, `plug`, `consumer`, `screen`, `widget`, `page`, etc.) are consistent enough across ecosystems to be reliable.
3. **Directory convention detection** - for languages where the filename carries no role hint but the directory is unambiguous (`/models/` in Rails, `/controllers/` in Laravel, `/schemas/` in FastAPI, `/middleware/` in Go).

Languages with no established role conventions (C, C++, Lua, Zig, Julia, PS1, SQL, Markdown) correctly return `""` - blank is accurate for those.

**Community label `_SKIP_PARTS`** must include common Java package structural directory names (`services`, `dao`, `impl`, `model`, `controller`, `util`, etc.) in addition to the standard skip list. Without these, Strategy 3 (depth-weighted directory segment) picks up the Java package hierarchy as the cluster label - producing meaningless labels like "services (3)" or "dao (4)" for clusters that are just groupings within those packages.

Only clusters with 2+ files are shown in the index - single-file clusters are noise.

### What NOT to touch

- `graph/builder.py:_build_project_isolated` - must remain a top-level function (not lambda/nested/method) for pickle safety on Windows with `ProcessPoolExecutor`. The `_redirected_cache_dir` inner function is acceptable (defined inside the subprocess, never pickled itself).
- `graph/builder.py:_collect_source_files` - git-first file collection. Do not revert to calling `graphify.collect_files` directly - it does not respect `.gitignore` and will include `node_modules` and other build artifacts for JS/TS/Java projects.
- `graph/builder.py:_build_project_isolated` - `cache_root=icx_cache` must be passed to `extract()`. When omitted, graphify infers `effective_root` from the absolute paths of source files (= project root) and writes `graphify-out/` into the project directory. The `os.chdir` and `_gcache.cache_dir` patches alone are not sufficient because graphify resolves cache paths from `effective_root`, not cwd.
- `graph/storage.py:derive_project_id` - changing the hash function or length invalidates all existing project IDs.
- `graph/querier.py:_role_tag` hook detection - the check `stem.startswith("use") and len(stem) > 3 and stem[3].isupper()` is intentional. React hooks start with lowercase `use` + uppercase letter. Changing to `sl.startswith("use")` causes false matches on `userList`, `userActions` etc.
- `graph/querier.py` deduplication - the `used_filenames` set must use `.lower()` for membership checks. Windows NTFS is case-insensitive; without this, two communities with labels like "Modal" and "modal" silently overwrite each other's cluster file.
- `graph/querier.py:_community_label:_SKIP_PARTS` - the extended set of Java package directory names must stay. Removing them causes generic package names to bleed through as cluster labels on Java projects.
- `graph/querier.py` cluster file write strategy - must use write-in-place + stale-file removal, NOT `shutil.rmtree` + `mkdir`. The rmtree pattern has a TOCTOU window where a symlink can be inserted between delete and recreate, redirecting all subsequent file writes to an attacker-controlled path.
- `cli.py:main` - `_trigger_memory_setup()` must only be called when `ctx.invoked_subcommand == "memory"`. Graph and other commands must not trigger the ONNX model download - the graph pipeline uses the LLM API directly, not the embedding model.
- `~/.icx/graphs/` layout - tools and tests both rely on this exact directory structure.

---

## Channel Architecture

### ChannelConfig

Each LLM channel (text or image) is represented by a `ChannelConfig`:

```python
class ChannelConfig(BaseModel):
    provider: str      # "ollama" | "nim" | "openai" | "anthropic" | "google" | "xai"
    model: str
    api_key: str | None = Field(default=None, exclude=True)
    base_url: str | None = None
```

`api_key` uses `exclude=True` so it never appears in `model_dump()` or serialized JSON.

### LLMConfig

A profile stores two independent channels:

```python
class LLMConfig(BaseModel):
    text_config: ChannelConfig
    image_config: ChannelConfig | None = None  # None = OCR-only
```

### Keyring slots

| Slot | Content | Env var fallback |
|------|---------|-----------------|
| `llm_text:<profile>` | Text channel API key | `ICX_LLM_TEXT_<PROFILE_UPPER>` |
| `llm_image:<profile>` | Image channel API key | `ICX_LLM_IMAGE_<PROFILE_UPPER>` |

The env var is derived by `_env_key(account)` in `config_manager.py`: replace every non-alphanumeric character with `_`, uppercase, prepend `ICX_`. Profile `my-fast` → `ICX_LLM_TEXT_MY_FAST`.

### Adding a new provider

1. Create `src/icx_engine/llm/<name>.py` with a class inheriting `LLMProvider`.
2. Constructor accepts `ChannelConfig` (not `LLMConfig`).
3. Use `config.model` for the model name.
4. Register in `get_provider()` in `llm/base.py`.
5. Add to `_PROVIDERS` and `_DEFAULT_MODELS` in `cli.py`.

### Engine flow

```
engine.run()
  ├─ get_provider(active_llm.text_config)  → text analysis
  └─ visual_grounding_pass(..., active_llm.image_config, ...)  → image verification
```

---

## Error Display (`error_display.py`)

`src/icx_engine/error_display.py` centralises all user-facing error rendering.

### `render_icx_error(exc, console, show_traceback=False)`

Renders a Rich Panel with **What / Why / How** guidance to `console` (always `err_console` in CLI contexts, which writes to stderr).

When `show_traceback=True` (triggered by the `--traceback` CLI flag) it also formats the full Python traceback using `traceback.format_exception()` so the output is readable regardless of whether the call is inside an active `except` block.

The `_GUIDANCE` dict maps every `ICXError` subclass to `(why_text, how_text)`. Unknown exception types (bare `ICXError` or non-ICX exceptions) fall back to `"Unexpected error."` / `"Pass --debug --traceback for full details."`.

**Context-aware `AuthError` guidance:** `render_icx_error` applies a secondary check when the caught exception is `AuthError`. It lowercases the exception message and checks for AI provider keywords (`"gemini"`, `"openai"`, `"anthropic"`, `"xai"`, `"nim"`, `"grok"`). If any keyword matches, the `How:` guidance is overridden to `"Run \`icx model --add\` to update your AI credentials."` instead of the default Jira connection guidance. This ensures the user always sees the precise recovery command for the specific service that failed.

For `ContextBuildError`, `exc.raw_output` is appended below the panel when `show_traceback=True` - showing the raw LLM response that failed to parse. It is hidden otherwise to keep normal error output clean.

---

## 8. Extending the CLI

The CLI uses [Typer](https://typer.tiangolo.com/) with `rich_markup_mode="rich"`.

- All commands are registered on `app` or `mcp_app`
- Group commands under `rich_help_panel` for the help output
- Always use `err_console.print(...)` for errors, `console.print(...)` for success
- Always raise `typer.Exit(1)` on error, not `sys.exit()`
- The `--debug` flag is on every command via `DebugOpt` - propagate it to inner calls
- The `--traceback` flag is on `analyze` via `TracebackOpt` - pass it to `render_icx_error()` as `show_traceback=traceback`
- All errors in `analyze` are routed through `render_icx_error(exc, err_console, show_traceback=traceback)` - never use `err_console.print(str(exc))` directly
- **Authentication flows belong in `services/connection_service.py`**, not inline in `cli.py`

The REPL (`_start_repl`) re-enters Typer for each line - do not add state that persists between REPL iterations.

---

## 9. Testing - rules and patterns

### Run tests

```bash
pip install -e ".[dev]"
pytest tests/ -x -q
```

### Rules

**All tests must pass before committing.** There are no exceptions.

**Use real data fixtures** - see `tests/test_data.py` for the shared Jira payload. Add your platform's equivalent there, not inline in test files.

**Never mock `ConfigManager` directly** - use the `isolated_config` fixture from `conftest.py` which redirects `CONFIG_PATH` to a temp file:

```python
def test_something(isolated_config):
    from icx_engine.config_manager import ConfigManager
    ConfigManager.save(...)
```

**Patching `ConfigManager.load` in tests:** `ConfigManager` is imported lazily inside several functions (`analyze`, `_handle_analyze_issue`, etc.) to avoid circular imports. Patch it at the source, not at the importing module:

```python
# Correct - patch at the definition site
with patch("icx_engine.config_manager.ConfigManager.load", return_value=config):
    ...

# Wrong - ConfigManager is not a module-level name in cli.py
monkeypatch.setattr("icx_engine.cli.ConfigManager", ...)
```

The same applies to `get_provider` in `engine.py` - patch at `"icx_engine.llm.base.get_provider"`, not at `"icx_engine.engine.get_provider"`.

**Use `respx` for HTTP mocking** - all HTTP tests use `respx.mock`:

```python
@respx.mock
async def test_fetch_issue():
    respx.get("https://test.atlassian.net/rest/api/3/issue/TEST-1").mock(
        return_value=httpx.Response(200, json=JIRA_ISSUE_PAYLOAD)
    )
    ...
```

**Test the parser separately from the HTTP client** - `test_parser.py` tests `parse_issue_response()` directly against a fixture dict. `test_parsing.py` tests `parse_input()` with no HTTP calls. This keeps unit tests fast and diagnostic.

**Test all URL formats for `parse_input()`** - bare key, `/browse/` URL, `/issues/` URL, `?selectedIssue=` query param, no-scheme URL, invalid input. Every connector must have these.

**When testing `process_attachments`**, remember it returns a tuple `(texts, images)` - always unpack both:

```python
texts, images = await process_attachments(raw, downloader, llm_config)
```

**When testing `_compute_missing` or `finalize` for Story/Task/Epic issues with spreadsheets**, set `raw.attachments` to include the spreadsheet filename and check `detailed_description` / `acceptance_criteria` for the presence or absence of `[technical schema:` / `[technical logic:` to control whether `missing_schema` is flagged.

**When testing heuristic or grounding behavior in `engine.py`**, always mock both `ocr_image` and `vision_enrich` in `icx_engine.connectors.attachments` - if `vision_enrich` is unmocked and an `image_model` is set, it makes real HTTP calls and may cause `asyncio.gather` to silently swallow the error.

### Fixtures available in `conftest.py`

- `cli_runner` - `CliRunner` instance for CLI tests
- `isolated_config` - redirects config path to a temp file; yields the `Path`

### ANSI codes in CLI output assertions

Typer 0.12+ with Rich renders option names with ANSI escape codes inserted between the `--` prefix and the option name (e.g. `\x1b[1m--\x1b[0m\x1b[32madd\x1b[0m`). A literal `'--add' in result.output` check will fail even though the option is visually present. Strip ANSI codes with `click.unstyle()` before asserting on option names or any other styled text:

```python
import click

def test_my_help(cli_runner):
    result = cli_runner.invoke(app, ["mycommand", "--help"])
    assert result.exit_code == 0
    output = click.unstyle(result.output)
    assert "--my-flag" in output
```

This applies to any test that checks `--help` output for specific option strings.

---

## 10. Security rules - non-negotiable

These are not style preferences - violating them introduces real vulnerabilities.

**Never log or print a raw secret.** In debug mode, mask tokens: `tok[:4] + "..." + tok[-4:]`. The `api_token` field is never echoed verbatim.

**Always validate the domain before building API URLs.** After stripping the scheme, check that the result contains no `/`, no `@`, and no control characters (`\r`, `\n`, `\t`). See `_connect_jira_token()` in `services/connection_service.py` for the pattern.

**Never call `check_http_credentials()` with an `http://` URL.** The function enforces HTTPS itself (`ValueError` if not), but callers must also ensure they don't accidentally construct plaintext URLs.

**Attachment download must validate every hop against `allowed_hosts: set[str]`.** `JiraClient.download_attachment()` uses `follow_redirects=False` and manually follows each redirect, checking the target URL against `allowed_hosts` before every request. This prevents SSRF via malicious attachment URLs or redirect chains. Auth headers are stripped on cross-host redirects; a hard limit of 3 hops (`_MAX_REDIRECT_HOPS`) prevents infinite redirect loops.

**Never use `follow_redirects=True` on authenticated requests.** httpx's built-in redirect following bypasses per-hop whitelist validation and may forward auth headers to unintended hosts. Always implement manual redirect following with explicit host checks instead.

**Cap attachment download size.** The limit is `_MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024` (20 MB). Use streaming (`client.stream()`), not `response.content`.

**Config file writes must be atomic and concurrent-safe.** The write sequence is:
1. Acquire `_thread_lock` (in-process thread serialization)
2. Acquire `_config_lock()` (cross-process advisory file lock; fcntl on Unix, `O_CREAT|O_EXCL` on Windows with PID stale detection)
3. Write to a PID-named temp file (`config.json.tmp.<PID>`) created with `os.open(..., 0o600)` - restricted permissions from the start, eliminating the TOCTOU window
4. Atomically replace with `tmp.replace(CONFIG_PATH)`
5. Release locks in `try/finally`; clean up the temp file on error

Never write directly to `CONFIG_PATH`. Never skip the lock.

**Secret fields must use `Field(exclude=True)`.** All secret model fields (`api_token`, `access_token`, `refresh_token`, `client_secret`, `api_key`) are declared with `Field(..., exclude=True)` or `Field(default=..., exclude=True)`. This ensures `model_dump_json()` never serializes them. `ConfigManager.save()` reads these fields from live model attributes after serialization and writes them to the keyring (or plaintext fallback). New auth fields that contain credentials must follow this pattern.

**MCP host config writes use `_atomic_write()`.** `mcp_hosts._atomic_write()` writes to `.tmp` then renames - use it for all MCP config changes. Never write MCP config files directly.

**Call `finalize()` in every LLM provider.** `issue_type` must come from `RawIssueData`, not the LLM. `finalize()` also enforces the `missing_schema` check and completeness cap - skipping it produces silently incorrect output for Story/Task/Epic tickets with spreadsheet attachments.

- **D-Lock threshold:** Never raise `_DLOCK_THRESHOLD` above 512. Windows Credential Manager rejects credential blobs above this size, causing silent plaintext fallback. D-Lock exists precisely to handle values that exceed this limit.
- **Master Key:** `"icx_master_key"` lives in the OS keyring like any other secret. Never write it to `config.json` or log it.

---

## 11. What NOT to touch

| File / location | Rule |
|---|---|
| `engine.py:extract_domain()` | No platform-specific URL patterns here. Domain extraction only. |
| `models/output.py:RawIssueData` | No platform-specific fields. Use `metadata: dict` for extras. |
| `models/output.py:IssueContext` | No new fields without changing SYSTEM_PROMPT, all providers, and finalize(). |
| `models/output.py:IssueContext.images` | Populated by `engine.run()` only - never by the LLM or by providers. Always attached when images exist; do not add conditional gates. |
| `llm/base.py:SYSTEM_PROMPT` | High sensitivity. Changes affect every provider and every analysis. The `### [TECHNICAL SCHEMA:]` and `### [TECHNICAL LOGIC:]` block mandates are machine-readable - `_compute_missing()` scans for these exact strings. Test thoroughly. |
| `llm/base.py:finalize()` | Do not skip. Do not change scoring logic or the `missing_schema` / completeness cap without updating all related tests. |
| `connectors/attachments.py:_SUMMARIZE_SYSTEM` | Preservation mandates for column headers, formula annotations, and tagged blocks are load-bearing - weakening them causes structured data to be silently dropped during LLM summarization of large documents. |
| `connectors/attachments.py:_convert_xlsx` | The dual-pass formula annotation and `_FORMULA_ANNOTATE_ROWS = 4` scope are the upstream source of `(Formula: EXPR)` cells that `SYSTEM_PROMPT` mandates. Do not collapse to a single pass. |
| `config_manager.py:_SENTINEL` | Do not change the sentinel string - it would invalidate all existing saved configs. |
| `auth/pkce.py` | Generic OAuth utility. Do not add Jira-specific logic here. When `webbrowser.open()` returns `False` or raises, the URL is printed to stderr for manual copy - this is intentional headless behaviour, do not remove it. Port binding tries `callback_port` through `callback_port + 4` (default 8765–8769); a clear `OSError` is raised if all are occupied. When a fallback port is used, a warning is printed to stderr. |
| `auth/token.py` | Generic auth utilities. Do not add provider-specific logic here. |
| `connectors/attachments.py` | Connector-agnostic UAE. Do not add platform-specific logic here. |
| `grounding.py:_VERIFY_USER_TEMPLATE` | Grounding prompt is carefully tuned. The phrase "Visual evidence takes priority over text. Correct any contradictions found in the JSON." must remain present. |

---

## 12. Commit and branch conventions

**Branch naming:**
```
feature/<short-description>
fix/<short-description>
connector/<platform-name>
llm/<provider-name>
```

**Commit messages:**
```
feat: add GitHub connector
fix: cap Retry-After delay to 60s
test: add parse_input tests for GitHub URLs
chore: update pyproject.toml classifiers
```

**Before opening a PR:**
- All tests pass (`pytest tests/ -x -q`)
- No new security issues (review section 10)
- New connector: all 7 steps in section 5 are complete
- New LLM provider: all 5 steps in section 6 are complete

---

## 13. Running the project locally

**Python 3.11–3.14 required.** Python 3.15+ is not yet supported - `onnxruntime` (used by the memory engine) does not yet publish wheels for 3.15. This will be updated as soon as onnxruntime adds support.

```bash
# Clone and install in editable mode with dev dependencies
git clone https://github.com/althaf-space/icx-engine.git
cd icx-engine
pip install -e ".[dev]"

# Run tests
pytest tests/ -x -q

# Run the CLI directly
ICX --help
ICX --version

# Run a specific test file
pytest tests/connectors/jira/test_parsing.py -v

# Run with debug output
icx analyze PROJ-123 --debug
```

### Uninstalling

Use `icx uninstall` instead of bare `pip uninstall`. It removes everything in order:

1. All API keys and tokens from the system keyring
2. ICX entry from all detected AI editor configs (Claude Code, Cursor, Windsurf, Codex)
3. `~/.icx/` directory - config, memory database, embedding model (~24 MB)
4. The `icx-engine` package via pip or pipx (auto-detected)

On Windows, step 4 runs in a hidden background process 3 seconds after exit to avoid the running-exe lock. On Linux/macOS it runs immediately.

```bash
icx uninstall          # with confirmation prompt
icx uninstall --yes    # skip prompt
```

### Environment for testing

For tests that make real API calls (not recommended in CI), set:

```bash
export ICX_JIRA_TOKEN_YOURCOMPANY_ATLASSIAN_NET="your_token"
export ICX_LLM_TEXT_PERSONAL="your_key"    # text channel
export ICX_LLM_IMAGE_PERSONAL="your_key"   # image channel (if configured)
```

All existing tests in the repo use mocked HTTP - no real credentials needed to run the test suite.


