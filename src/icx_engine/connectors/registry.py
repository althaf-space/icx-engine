from __future__ import annotations
from icx_engine.connectors.jira.config import JiraConnection

# Maps connector_type string → BaseConnection subclass.
# Add new connectors here as they are implemented.
CONNECTION_REGISTRY: dict = {
    "jira": JiraConnection,
}
