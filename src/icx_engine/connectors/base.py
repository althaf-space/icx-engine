from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass
from icx_engine.models.config import BaseConnection
from icx_engine.models.output import RawIssueData

_CONNECTOR_CLASSES: dict[str, type] = {}
_CONNECTION_CLASSES: dict[str, type] = {}


@dataclass
class ParsedInput:
    """Structured result of connector input parsing."""
    issue_key: str


class ConnectorBase(ABC):
    def __init__(self, connection: BaseConnection):
        self.connection = connection

    @classmethod
    @abstractmethod
    def connector_type(cls) -> str: ...

    @classmethod
    @abstractmethod
    def can_handle_bare_key(cls, key: str) -> bool: ...

    @abstractmethod
    def parse_input(self, input_str: str) -> ParsedInput:
        """Parse user input (URL or bare key) and return the issue key.
        Raises InvalidInput for unrecognised formats."""
        ...

    @abstractmethod
    async def fetch(self, issue_key: str, config=None, log=None) -> RawIssueData: ...

    @abstractmethod
    async def download_attachment(self, url: str) -> bytes: ...

    @abstractmethod
    async def process_attachments(self, raw: RawIssueData, llm_config, log=None) -> tuple[dict[str, str], dict[str, str], dict[str, str], dict[str, str]]: ...

    @classmethod
    def extract_project_key(cls, issue_key: str) -> str:
        """Return the project/namespace prefix of an issue key.

        Default heuristic matches Jira-style "PROJ-123" -> "PROJ". Connectors
        with a different key format (e.g. "owner/repo#123") should override.
        """
        return issue_key.split("-", 1)[0] if "-" in issue_key else issue_key

    @classmethod
    def extract_bare_key_from_ref(cls, ref: str) -> str | None:
        """Return the bare issue key if `ref` (a bare key or issue URL) matches
        this connector's conventions, else None. Used to resolve a graph project
        from a ticket reference without an active connection."""
        return None

    async def refresh_credentials(self) -> None:
        """Refresh OAuth credentials if needed. Override in connectors that use OAuth."""


def register_connector(
    name: str,
    connector_cls: type,
    connection_cls: type,
) -> None:
    """Register a connector for lookup by name."""
    _CONNECTOR_CLASSES[name] = connector_cls
    _CONNECTION_CLASSES[name] = connection_cls


def _connector_registry() -> dict[str, type[ConnectorBase]]:
    if "jira" not in _CONNECTOR_CLASSES:
        from icx_engine.connectors.jira.connector import JiraConnector
        from icx_engine.connectors.jira.config import JiraConnection
        register_connector("jira", JiraConnector, JiraConnection)
    return dict(_CONNECTOR_CLASSES)


def get_connector_class(connector_type: str) -> type[ConnectorBase]:
    """Return the connector class registered for `connector_type`."""
    registry = _connector_registry()
    cls = registry.get(connector_type)
    if cls is None:
        raise ValueError(
            f"Unknown connector type '{connector_type}'. "
            f"Valid types: {', '.join(registry)}"
        )
    return cls


def get_connector(connection: BaseConnection) -> ConnectorBase:
    """Return an initialized connector instance for the given connection."""
    cls = get_connector_class(connection.connector_type)
    return cls(connection)


def get_all_connector_classes() -> list[type[ConnectorBase]]:
    return list(_connector_registry().values())


__all__ = [
    "ParsedInput",
    "ConnectorBase",
    "register_connector",
    "get_connector_class",
    "get_connector",
    "get_all_connector_classes",
    "_CONNECTOR_CLASSES",
    "_CONNECTION_CLASSES",
]
