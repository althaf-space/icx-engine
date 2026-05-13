from __future__ import annotations
from typing import Literal, Annotated
from pydantic import BaseModel, Field
from icx_engine.models.config import BaseConnection, OAuthAuth


class TokenAuth(BaseModel):
    """Jira API token authentication - email + API token (Atlassian-specific pattern)."""
    auth_type: Literal["token"]
    email: str
    api_token: str = Field(default="", exclude=True)


class JiraOAuthAuth(OAuthAuth):
    """
    Jira OAuth 2.0 tokens.

    Extends the generic OAuthAuth with Atlassian's cloud_id, which is required
    to route API calls to the correct Atlassian cloud instance.
    """
    cloud_id: str = ""  # Atlassian cloud instance identifier


class JiraConnection(BaseConnection):
    """Typed Jira connection. Cast from BaseConnection via .model_validate(conn.model_dump())."""

    connector_type: Literal["jira"] = "jira"
    auth: Annotated[JiraOAuthAuth | TokenAuth, Field(discriminator="auth_type")]
