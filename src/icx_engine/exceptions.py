class ICXError(Exception):
    """Base exception for all ICX errors."""


class AuthError(ICXError):
    """401 or 403 from a source - authentication or permission failure."""


class IssueNotFound(ICXError):
    """404 - issue key does not exist or is not accessible."""


class RateLimited(ICXError):
    """429 - source rate limit hit."""


class SourceUnavailable(ICXError):
    """5xx - source server error."""


class InvalidInput(ICXError):
    """Bad URL or issue key format from user input."""


class NoConnectionError(ICXError):
    """No connection configured, or requested domain not found."""


class NoLLMError(ICXError):
    """No LLM provider configured (required for CLI mode)."""


class OAuthRefreshError(ICXError):
    """OAuth access token refresh failed."""


class ManagementError(ICXError):
    """Invalid usage of a management command (e.g. unknown profile name)."""


class ContextBuildError(ICXError):
    """LLM returned malformed JSON or failed Pydantic validation."""

    def __init__(self, message: str, raw_output: str = ""):
        super().__init__(message)
        self.raw_output = raw_output


class ConfigError(ICXError):
    """Config file is corrupted or D-Lock decryption failed."""


class ICXMemoryError(ICXError):
    """Local memory operation failed (LanceDB, embeddings, or storage)."""


# Back-compat alias. Prefer ICXMemoryError; the bare name `MemoryError` shadows
# the Python builtin, so new code must import ICXMemoryError.
MemoryError = ICXMemoryError


class GraphError(ICXError):
    """Codebase graph operation failed (build, query, storage, or registration)."""
