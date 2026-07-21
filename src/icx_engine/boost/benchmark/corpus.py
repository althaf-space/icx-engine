"""Benchmark corpus - measures REQUIREMENT COVERAGE, not keyword trivia.

Each prompt carries a list of the real requirements a COMPLETE answer must cover (rubric items). The
grader scores the fraction of requirements an answer covers. Prompts are tagged by `difficulty`:

  underspecified - a vague one-liner (what a real user types). A raw answer predictably misses most of
                   the implicit requirements; the ICX methodology forces enumerating them, so this is
                   where the boost shows its real, large lift.
  hard           - specific but full of traps/edge cases a fast answer skips.
  easy           - a strong model already answers well (near-ceiling) - included as an honest contrast
                   so the report shows WHERE boost helps and where there is simply no headroom.

`any_of` = the accepted ways an answer can express covering that requirement (lowercased substring match)."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RubricItem:
    check: str                         # the requirement being checked
    any_of: list                       # answer covers it if it contains ANY of these substrings
    weight: int = 1


@dataclass
class BenchPrompt:
    id: str
    prompt: str
    archetype: str
    rubric: list = field(default_factory=list)
    difficulty: str = "hard"           # underspecified | hard | easy


def load_corpus() -> list:
    """Return the built-in benchmark corpus. Deterministic; no I/O."""
    def R(check, *any_of, weight=1):
        return RubricItem(check=check, any_of=list(any_of), weight=weight)

    # ---- UNDERSPECIFIED: vague one-liners; raw answers miss the implicit requirements ----------
    underspecified = [
        BenchPrompt("u-login", "Add a login feature.", "security", difficulty="underspecified", rubric=[
            R("hash passwords (never store plaintext)", "hash", "bcrypt", "argon", "scrypt", "pbkdf2"),
            R("rate limit / lockout on repeated attempts", "rate limit", "lockout", "throttle",
              "brute force", "attempts"),
            R("validate input", "validat", "sanitiz", "required"),
            R("session or token issuance", "session", "token", "jwt", "cookie"),
            R("safe error messages (no user enumeration)", "generic error", "do not reveal",
              "enumeration", "same message", "invalid credentials"),
            R("transport security / HTTPS", "https", "tls", "secure cookie", "httponly"),
            R("tests", "test", "assert", "example")]),
        BenchPrompt("u-upload", "Let users upload a file.", "security", difficulty="underspecified",
            rubric=[
            R("validate real content type", "content-type", "mime", "magic", "sniff", "actual type"),
            R("limit size", "size limit", "max size", "too large", "413"),
            R("sanitize filename / prevent path traversal", "sanitize", "path travers", "../",
              "random name", "uuid", "filename"),
            R("store safely (outside webroot / not executable)", "webroot", "outside", "executable",
              "s3", "object storage", "no execute"),
            R("scan or restrict types (allowlist)", "scan", "allowlist", "whitelist", "restrict"),
            R("tests", "test", "example")]),
        BenchPrompt("u-users-api", "Build a REST API for users.", "coding",
            difficulty="underspecified", rubric=[
            R("full CRUD", "create", "read", "update", "delete", "crud"),
            R("input validation", "validat", "schema", "required"),
            R("authentication/authorization", "auth", "token", "permission"),
            R("pagination for the list", "pagination", "page", "limit", "offset", "cursor"),
            R("correct status codes", "201", "404", "400", "status code"),
            R("error handling", "error", "exception", "handle")]),
        BenchPrompt("u-cache", "Cache the API responses.", "performance",
            difficulty="underspecified", rubric=[
            R("invalidation strategy", "invalidat", "evict", "stale", "purge"),
            R("TTL / expiry", "ttl", "expire", "expiry", "time to live"),
            R("cache key strategy", "key", "vary", "per user", "namespace"),
            R("stampede / thundering herd protection", "stampede", "thundering", "lock", "single flight"),
            R("what NOT to cache (auth/personalized)", "do not cache", "private", "personalized",
              "authenticated", "no-store")]),
        BenchPrompt("u-search", "Add search to the app.", "coding", difficulty="underspecified", rubric=[
            R("sanitize/validate the query", "sanitiz", "validat", "escape", "injection"),
            R("pagination / limit results", "pagination", "limit", "page", "top"),
            R("indexing for performance", "index", "full-text", "elasticsearch", "tsvector"),
            R("handle empty / no results", "empty", "no results", "not found", "zero"),
            R("relevance / ordering", "relevance", "rank", "order", "score")]),
        BenchPrompt("u-csv-export", "Add a CSV export.", "security", difficulty="underspecified",
            rubric=[
            R("CSV formula injection protection", "formula injection", "csv injection", "=", "prefix",
              "leading", "@", "sanitiz"),
            R("proper quoting/escaping", "quote", "escape", "delimiter", "csv module"),
            R("encoding (UTF-8/BOM)", "encoding", "utf-8", "bom"),
            R("stream large data", "stream", "chunk", "generator", "memory"),
            R("headers row", "header", "column name")]),
        BenchPrompt("u-delete", "Add a delete button for records.", "coding",
            difficulty="underspecified", rubric=[
            R("confirmation before delete", "confirm", "are you sure", "dialog"),
            R("authorization check", "authoriz", "permission", "owner", "allowed"),
            R("soft vs hard delete decision", "soft delete", "hard delete", "archiv", "restore"),
            R("cascade / related records", "cascade", "related", "foreign key", "orphan"),
            R("audit / log the deletion", "audit", "log", "who", "history")]),
        BenchPrompt("u-payment", "Add a payment endpoint.", "security",
            difficulty="underspecified", rubric=[
            R("idempotency (no double charge)", "idempoten", "double charge", "duplicate", "key"),
            R("never store raw card data (PCI)", "pci", "do not store", "token", "never store card",
              "vault"),
            R("validate amount/currency", "validat", "amount", "currency", "negative"),
            R("verify webhooks / signatures", "webhook", "signature", "verify"),
            R("error handling + logging (no secrets)", "error", "log", "no secret", "mask")]),
        BenchPrompt("u-notify", "Send email notifications to users.", "coding",
            difficulty="underspecified", rubric=[
            R("send async / queue (don't block request)", "async", "queue", "background", "worker"),
            R("retry on failure + backoff", "retry", "backoff", "requeue"),
            R("rate limit / batching", "rate limit", "batch", "throttle"),
            R("handle bounces / unsubscribes", "bounce", "unsubscribe", "opt-out", "suppression"),
            R("template injection safety", "template injection", "escape", "sanitiz", "autoescape")]),
        BenchPrompt("u-config", "Read the config from a file.", "coding",
            difficulty="underspecified", rubric=[
            R("handle missing file", "missing", "not found", "default", "fallback"),
            R("handle malformed content", "malformed", "invalid", "parse error", "try"),
            R("validate required keys / types", "validat", "required", "schema", "type"),
            R("env / override precedence", "env", "environment", "override", "precedence"),
            R("do not log secrets in the config", "secret", "mask", "redact", "no log")]),
        BenchPrompt("u-webhook", "Add a webhook receiver endpoint.", "security",
            difficulty="underspecified", rubric=[
            R("verify the signature", "signature", "hmac", "verify", "secret"),
            R("idempotency / handle duplicate deliveries", "idempoten", "duplicate", "already",
              "dedup"),
            R("respond fast / process async", "async", "queue", "200 quickly", "background", "ack"),
            R("validate + guard the payload", "validat", "schema", "size", "reject"),
            R("handle retries from the sender", "retry", "replay", "timestamp", "expire")]),
        BenchPrompt("u-migrate", "Add a database migration to add a column.", "database",
            difficulty="underspecified", rubric=[
            R("avoid long locks on a big table", "lock", "concurrent", "online", "batches",
              "no downtime"),
            R("nullable or default for existing rows", "nullable", "default", "backfill",
              "existing rows"),
            R("reversible / down migration", "reversible", "rollback", "down", "revert"),
            R("deploy order (code vs schema)", "deploy", "order", "backward compatible", "two-phase")]),
        BenchPrompt("u-bg-job", "Add a background job to clean up old records.", "coding",
            difficulty="underspecified", rubric=[
            R("batch / avoid loading everything", "batch", "chunk", "limit", "pagination"),
            R("idempotent / safe to re-run", "idempoten", "safe to re-run", "resume", "checkpoint"),
            R("avoid deleting wrong data (scope/filter)", "filter", "scope", "criteria", "only",
              "where"),
            R("logging / observability", "log", "metric", "monitor", "alert"),
            R("failure handling / retry", "retry", "failure", "error", "dead letter")]),
        BenchPrompt("u-password-reset", "Add a password reset flow.", "security",
            difficulty="underspecified", rubric=[
            R("time-limited single-use token", "token", "expire", "single-use", "one-time", "ttl"),
            R("do not reveal if the email exists", "enumeration", "same response", "do not reveal",
              "always"),
            R("secure token storage (hash the token)", "hash", "random", "secure", "constant-time"),
            R("rate limit the request", "rate limit", "throttle", "abuse"),
            R("invalidate old sessions after reset", "invalidate", "sessions", "logout", "revoke")]),
        BenchPrompt("u-graphql", "Expose the data over an API.", "performance",
            difficulty="underspecified", rubric=[
            R("pagination", "pagination", "page", "limit", "cursor"),
            R("avoid N+1 / batch loading", "n+1", "batch", "dataloader", "eager", "join"),
            R("auth on each field/resource", "auth", "permission", "authoriz"),
            R("rate limiting / query cost", "rate limit", "depth", "complexity", "cost", "throttle"),
            R("caching", "cache", "ttl", "etag")]),
    ]

    # ---- HARD: specific prompts with traps a fast answer skips ---------------------------------
    hard = [
        BenchPrompt("h-jwt", "Our API verifies JWTs with jwt.decode. Is that enough?", "security",
            difficulty="hard", rubric=[
            R("algorithm confusion / alg=none", "alg", "none", "algorithm", "confus"),
            R("pin expected algorithm", "specify", "pin", "allowlist", "algorithms="),
            R("verify exp/issuer/audience", "exp", "expiry", "issuer", "audience", "aud", "iss"),
            R("authz is separate from authn", "authoriz", "authz", "still check", "claims")]),
        BenchPrompt("h-flaky", "One test passes locally but fails randomly in CI. Diagnose and fix.",
            "debugging", difficulty="hard", rubric=[
            R("nondeterminism sources", "race", "timing", "order", "shared state", "clock", "random",
              "parallel"),
            R("isolate/reproduce with seed", "isolat", "reproduce", "seed", "repeat"),
            R("fix the class not just this test", "root", "class", "not just", "underlying"),
            R("do not just retry/skip to hide", "not hide", "avoid retry", "do not skip", "quarantine")]),
        BenchPrompt("h-money", "Report totals are off by a few cents from the line items. Why?",
            "debugging", difficulty="hard", rubric=[
            R("floating-point error", "float", "ieee", "binary", "representation"),
            R("fix with decimal/integer cents", "decimal", "integer", "cents", "minor unit"),
            R("rounding per-line vs total", "round", "per line", "per-item", "order of"),
            R("verify against a known case", "verify", "test", "reproduce")]),
        BenchPrompt("h-deadlock", "Two services occasionally deadlock updating the same two rows. Fix.",
            "debugging", difficulty="hard", rubric=[
            R("consistent lock ordering", "lock order", "same order", "ordering", "acquire in"),
            R("shorten/scope the transaction", "transaction", "shorten", "scope", "shorter"),
            R("retry on deadlock", "retry", "deadlock victim", "backoff"),
            R("reproduce under concurrency", "reproduce", "concurren", "two", "simultaneous")]),
        BenchPrompt("h-timezone", "Scheduled report runs at the wrong time for some users. Why?",
            "debugging", difficulty="hard", rubric=[
            R("timezone / UTC vs local", "timezone", "utc", "local", "tz"),
            R("DST transitions", "dst", "daylight", "savings", "transition"),
            R("store/compute in UTC, convert at edges", "store utc", "convert", "at the edge",
              "normalize"),
            R("verify with a spanning case", "verify", "test", "reproduce", "edge")]),
    ]

    # ---- EASY: near-ceiling; honest contrast (little/no headroom) ------------------------------
    easy = [
        BenchPrompt("e-idempotent", "What does idempotent mean for an HTTP API?", "doubt",
            difficulty="easy", rubric=[
            R("same effect on repeat", "same", "repeat", "multiple times", "once"),
            R("which methods", "put", "delete", "get", "post")]),
        BenchPrompt("e-reverse", "How do I reverse a string in Python?", "doubt",
            difficulty="easy", rubric=[
            R("slice or reversed", "[::-1]", "reversed", "slice"),
            R("shows code", "def ", "=", "return", "[")]),
    ]

    return underspecified + hard + easy
