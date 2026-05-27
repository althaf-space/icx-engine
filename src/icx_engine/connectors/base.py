from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass
from icx_engine.models.config import BaseConnection
from icx_engine.models.output import RawIssueData


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
    async def process_attachments(self, raw: RawIssueData, llm_config, log=None) -> tuple[dict[str, str], dict[str, str]]: ...


def get_connector(connection: BaseConnection) -> ConnectorBase:
    """Return an initialized connector instance for the given connection."""
    from icx_engine.connectors.jira.connector import JiraConnector

    _registry: dict[str, type[ConnectorBase]] = {
        "jira": JiraConnector,
    }
    cls = _registry.get(connection.connector_type)
    if cls is None:
        raise ValueError(
            f"Unknown connector type '{connection.connector_type}'. "
            f"Valid types: {', '.join(_registry)}"
        )
    return cls(connection)


def get_all_connector_classes() -> list[type[ConnectorBase]]:
    from icx_engine.connectors.jira.connector import JiraConnector
    return [JiraConnector]
