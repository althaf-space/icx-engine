"""Workstatus integration config.

Workstatus (workstatus.io) has no public API documentation, no OpenAPI/Postman
spec, and no SDK - see developer.md's Workstatus integration section for the
full evidence trail. Every field below was captured live from an authenticated
browser session's network traffic, never fabricated.

Auth model: unlike Jira (email + API token) or GitLab (personal access
token), Workstatus's login request body schema was never captured - only its
endpoint and headers were. Rather than guess the login payload, this
connector uses the same four header values the web app sends on every
authenticated call, pasted by the user from their own browser session
(`icx workstatus --add`). This mirrors Jira's token-based connector: a
pre-issued credential, not an interactive login flow the connector performs
itself.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class WorkstatusConfig(BaseModel):
    """Session credentials for the Workstatus Web API (`web-api.workstatus.io`).

    `user_id` and `org_id` are sent as the `UserID`/`OrgID` headers on every
    authenticated call; `authorization` and `sd_token` are secrets, stored in
    the OS keyring like every other ICX credential.
    """
    model_config = ConfigDict(extra="ignore")

    user_id: str
    org_id: str
    device_type: str = "web"
    authorization: str = Field(..., exclude=True)
    sd_token: str = Field(..., exclude=True)
