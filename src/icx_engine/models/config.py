from __future__ import annotations
from typing import Literal, Any
from pydantic import BaseModel, ConfigDict, Field, SerializeAsAny, field_validator, model_validator


class OAuthAuth(BaseModel):
    """
    Generic OAuth 2.0 token storage.

    Stores the standard token fields returned by any OAuth provider.
    Platform connectors that need additional provider-specific fields
    (e.g. Atlassian cloud_id) should subclass this model.
    """
    auth_type: Literal["oauth"]
    access_token: str = Field(..., exclude=True)
    refresh_token: str = Field(..., exclude=True)
    expires_at: int              # Unix timestamp
    client_id: str = ""
    client_secret: str | None = Field(default=None, exclude=True)


class BaseConnection(BaseModel):
    """Generic connector connection. Unknown fields are discarded for safety."""
    model_config = ConfigDict(extra="ignore")

    connector_type: str   # e.g. "jira", "github", "linear"
    domain: str           # e.g. company.example.com
    label: str = ""       # optional human-readable name


class SonarConnection(BaseModel):
    """A single SonarQube server connection. Token stored in the OS keyring."""
    model_config = ConfigDict(extra="ignore")

    name: str
    url: str
    token: str | None = Field(default=None, exclude=True)
    verify_tls: bool = True


class ChannelConfig(BaseModel):
    provider: str                    # "ollama" | "nim" | "openai" | "anthropic" | "google" | "xai"
    model: str
    api_key: str | None = Field(default=None, exclude=True)
    base_url: str | None = None


class LLMConfig(BaseModel):
    text_config: ChannelConfig
    image_config: ChannelConfig | None = None


def _get_connection_registry() -> dict:
    """Return a mapping of connector_type -> BaseConnection subclass. Lazily built to avoid circular imports."""
    from icx_engine.connectors.registry import CONNECTION_REGISTRY  # noqa: PLC0415
    return CONNECTION_REGISTRY


def _cast_connection(item: Any) -> BaseConnection:
    """Cast a raw dict or BaseConnection to the correct typed subclass by connector_type."""
    if isinstance(item, BaseConnection):
        return item
    ctype = item.get("connector_type") if isinstance(item, dict) else None
    registry = _get_connection_registry()
    cls = registry.get(ctype)
    if cls is not None:
        return cls.model_validate(item)
    return BaseConnection.model_validate(item)


class AppConfig(BaseModel):
    connections: list[SerializeAsAny[BaseConnection]] = []
    llm_profiles: dict[str, LLMConfig] = {}
    current_llm_profile: str | None = None
    default_connection: str | None = None  # "connector_type:domain", e.g. "jira:company.atlassian.net", "github:company.github.com"

    # Local testing engine
    test_max_iterations: int = 3
    # Path to the Node executable used for the UI harness (Playwright), decoupled from the
    # app's Node. Set interactively by `icx test setup`; editable here later. Empty = auto-discover.
    harness_node_path: str | None = None

    @field_validator("test_max_iterations")
    @classmethod
    def _clamp_iterations(cls, v: int) -> int:
        # Clamp (never raise) so a hand-edited config.json can't crash load or drive an unbounded loop.
        return max(1, min(int(v), 100))

    # Sonar / code quality (direct SonarQube Web API reader). Multiple named
    # server connections with one active, mirroring llm_profiles/current_llm_profile.
    # Project and branch are chosen per request, never stored.
    sonar_connections: dict[str, SonarConnection] = {}
    active_sonar: str | None = None

    # Legacy single-server fields - retained only for backward-compatible loading
    # of older config files. Resolved into an implicit "default" connection when a
    # config predates sonar_connections. Not written by new code.
    sonar_project_key: str | None = None
    sonar_token: str | None = Field(default=None, exclude=True)
    sonar_enabled: bool = False
    sonar_url: str | None = None
    sonar_verify_tls: bool = True

    @model_validator(mode="after")
    def _migrate_legacy_sonar(self) -> "AppConfig":
        """One-time migration: promote a legacy single-server Sonar config into a
        named connection so it is visible in `icx status`/`icx sonar --list` and
        removable. The connection is created regardless of the old enabled flag
        (so it can be seen and removed), but it is only made ACTIVE when the legacy
        config was enabled - a disabled legacy config stays off (no silent enable)."""
        if self.sonar_url and not self.sonar_connections:
            self.sonar_connections["default"] = SonarConnection(
                name="default", url=self.sonar_url,
                token=self.sonar_token, verify_tls=self.sonar_verify_tls,
            )
            if self.sonar_enabled and self.active_sonar is None:
                self.active_sonar = "default"
            self.sonar_url = None
            self.sonar_token = None
            self.sonar_enabled = False
        return self

    def active_sonar_connection(self) -> "SonarConnection | None":
        """Resolve the active SonarQube connection, or None.

        Prefers a named connection in `sonar_connections`; falls back to the
        legacy single-server fields (only when the legacy `sonar_enabled` flag was
        set, so a previously-disabled legacy config stays disabled)."""
        if self.active_sonar and self.active_sonar in self.sonar_connections:
            return self.sonar_connections[self.active_sonar]
        if self.sonar_enabled and self.sonar_url and self.sonar_token:
            return SonarConnection(
                name="default", url=self.sonar_url,
                token=self.sonar_token, verify_tls=self.sonar_verify_tls,
            )
        return None

    # Generic, pluggable third-party integration settings. New integrations
    # register a config model via `icx_engine.integrations.register_integration`
    # and store their settings here, instead of adding fields to AppConfig.
    # Testing/Sonar settings above remain inline for backward compatibility.
    integrations: dict[str, dict] = {}

    def integration(self, name: str):
        """Return the registered integration's validated config model, or None
        if the integration is not registered or has no stored settings."""
        from icx_engine.integrations import get_integration_model  # noqa: PLC0415
        model_cls = get_integration_model(name)
        data = self.integrations.get(name)
        if model_cls is None or data is None:
            return None
        return model_cls.model_validate(data)

    @property
    def active_llm(self) -> LLMConfig | None:
        if self.current_llm_profile:
            return self.llm_profiles.get(self.current_llm_profile)
        return None

    @field_validator("connections", mode="before")
    @classmethod
    def _cast_connections(cls, v: list) -> list:
        return [_cast_connection(item) for item in (v or [])]
