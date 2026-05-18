# Security Policy

## Supported Versions

| Version | Status |
|---------|--------|
| 0.2.x   | Actively supported - security patches prioritized |
| < 0.2.0 | Unsupported |

---

## Reporting a Vulnerability

**Do not open a public issue for security vulnerabilities.**

Use one of these private channels:

### Preferred: GitHub Security Advisory

1. Go to the [Security tab](https://github.com/althaf-space/icx-engine/security) of this repository
2. Click **"Report a vulnerability"**
3. Describe the issue, affected component, and reproduction steps

This channel is private - only you and the maintainers see it until disclosure.

### Alternative: Direct Email

Send details to [althaf.space@gmail.com](mailto:althaf.space@gmail.com) with subject `[ICX Security]`.

---

## What We Consider a Vulnerability

Given ICX's architecture - it handles Jira credentials, OAuth tokens, LLM API keys, and arbitrary attachment downloads - these areas are highest priority:

| Area | Examples |
|------|---------|
| **Credential exposure** | Any path where API tokens, OAuth tokens, or LLM keys leave the OS keyring or `~/.icx/config.json` unexpectedly - via logs, stdout, exception messages, or debug output |
| **SSRF** | Bypassing the attachment download allowlist (`allowed_hosts`) via crafted redirect chains to internal IPs, cloud metadata endpoints (`169.254.169.254`), or localhost |
| **Config file integrity** | Race conditions, TOCTOU windows, or lock bypass that corrupt `~/.icx/config.json` or allow unauthorized reads/writes of credentials |
| **D-Lock cryptography** | Weaknesses in the AES-256-GCM Master Key lifecycle - generation, keyring storage, or decryption paths that leak the key or allow ciphertext forgery |
| **Path traversal** | Attachment filenames used as-is in file operations, allowing writes outside the intended temp directory |
| **Prompt injection** | Crafted Jira issue content that manipulates the LLM into exfiltrating credentials or producing malicious structured output consumed by an AI coding agent |
| **OAuth flow weaknesses** | PKCE state validation bypass, redirect URI manipulation, or authorization code interception in the `icx connection --add` OAuth PKCE flow. Note: both `token_endpoint` and `refresh_token` calls enforce HTTPS at the library level. |
| **Plaintext secret fallback** | Any code path that silently falls back to writing unencrypted credentials to `~/.icx/config.json` without the documented warning on platforms where the OS keyring is available |

The CI/headless `ICX_*` environment variable fallback is an intentional, documented feature - not a vulnerability in itself. However, if you find an ICX code path that leaks those values into shell history, process lists, or logs, please report it.

---

## Security Architecture

Understanding ICX's security design helps frame valid reports.

### Credential storage

ICX never stores credentials in plaintext when an OS keyring is available:

1. **OS keyring first** - credentials go into Windows Credential Manager, macOS Keychain, or GNOME Keyring via the `keyring` library. `~/.icx/config.json` stores only a `"__keychain__"` sentinel.
2. **D-Lock (AES-256-GCM)** - tokens longer than 512 bytes (common in Jira Cloud OAuth flows) exceed the Windows Credential Manager blob limit. ICX encrypts these with AES-256-GCM using a randomly-generated 32-byte Master Key, which is itself stored in the OS keyring. Ciphertext is tagged `dlock:v1:BASE64` in `config.json`. No readable credential ever appears.
3. **Double-lock serialization** - all secret Pydantic fields (`api_token`, `access_token`, `refresh_token`, `client_secret`, `api_key`) declare `Field(exclude=True)`. `model_dump_json()` never serializes them even if called unexpectedly. `ConfigManager.save()` reads secrets from live model attributes and writes them directly to the keyring.
4. **Headless/CI fallback** - when no keyring daemon is available, ICX writes a warning to stderr and falls back to the `ICX_*` environment variable path. Plaintext storage to disk is a last resort with an explicit user-visible warning that fires **once per account and once per machine** - tracked via a `~/.icx/.warned_plaintext` sidecar file so repeated commands (e.g. OAuth token refreshes) do not spam the terminal.

### SSRF protection

Attachment download (`JiraClient.download_attachment`) validates every hop of every redirect chain:

- `follow_redirects=False` - httpx's built-in redirect following is never used on authenticated requests
- Every redirect target is checked against `allowed_hosts` before the next request is made
- Auth headers are stripped on cross-host redirects
- Hard limit of 3 redirect hops (`_MAX_REDIRECT_HOPS`)
- Maximum download size of 20 MB (`_MAX_ATTACHMENT_BYTES`)

### Concurrent write safety

Config writes use a two-layer lock:

1. `threading.Lock` (`_thread_lock`) - serializes threads within the same process
2. `_config_lock()` - cross-process advisory file lock (fcntl/flock on Unix; `O_CREAT|O_EXCL` with PID stale detection on Windows). The `.lock` sidecar file is removed on exit on both platforms.

Writes go to a PID-named temp file (`config.json.tmp.<PID>`) created at mode `0600`, then atomically replaced with `tmp.replace(CONFIG_PATH)`.

---

## Response Process

| Step | Target timeline |
|------|----------------|
| Acknowledgement | Within 48 hours |
| Triage and severity assessment | Within 5 business days |
| Fix developed and tested | Severity-dependent; critical issues are prioritized |
| Coordinated disclosure | After the fix ships - we will notify you before public announcement |
| Credit | We will credit you by name or handle in the release notes if you wish |

---

## Out of Scope

- Vulnerabilities in third-party dependencies (report to the upstream project; we will upgrade promptly when patches are released)
- Self-inflicted exposure: copying `~/.icx/config.json` to a public location, pasting credentials into chat, etc.
- Theoretical denial-of-service via very large Jira tickets (ICX caps download sizes but has no strict CPU/memory budget)
- Social engineering attacks against maintainers

Thank you for helping keep ICX secure.


