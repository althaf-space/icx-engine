from __future__ import annotations
from typing import Literal, Any
from pydantic import BaseModel, ConfigDict, Field, SerializeAsAny, field_validator


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


class ChannelConfig(BaseModel):
    provider: str                    # "ollama" | "nim" | "openai" | "anthropic" | "google" | "xai"
    model: str
    api_key: str | None = Field(default=None, exclude=True)
    base_url: str | None = None


class LLMConfig(BaseModel):
    text_config: ChannelConfig
    image_config: ChannelConfig | None = None


def _get_connection_registry() -> dict:
    """Return a mapping of connector_type → BaseConnection subclass. Lazily built to avoid circular imports."""
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

    @property
    def active_llm(self) -> LLMConfig | None:
        if self.current_llm_profile:
            return self.llm_profiles.get(self.current_llm_profile)
        return None

    @field_validator("connections", mode="before")
    @classmethod
    def _cast_connections(cls, v: list) -> list:
        return [_cast_connection(item) for item in (v or [])]
