from icx_engine.auth.token import build_basic_auth_header, build_bearer_header
from icx_engine.connectors.jira.config import JiraConnection, TokenAuth, JiraOAuthAuth


def build_auth_header(connection: JiraConnection) -> str:
    """Return the Authorization header value for the given Jira connection."""
    auth = connection.auth
    if isinstance(auth, TokenAuth):
        return build_basic_auth_header(auth.email, auth.api_token)
    if isinstance(auth, JiraOAuthAuth):
        return build_bearer_header(auth.access_token)
    raise ValueError(f"Unknown auth type: {type(auth)}")  # unreachable with discriminated union
