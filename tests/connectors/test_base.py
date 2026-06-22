from __future__ import annotations
import pytest

from icx_engine.connectors.base import (
    ConnectorBase,
    ParsedInput,
    get_connector_class,
    register_connector,
    _CONNECTOR_CLASSES,
    _CONNECTION_CLASSES,
)


def test_get_connector_class_jira_returns_jira_connector():
    from icx_engine.connectors.jira.connector import JiraConnector
    cls = get_connector_class("jira")
    assert cls is JiraConnector


def test_get_connector_class_unknown_raises_value_error():
    with pytest.raises(ValueError, match="Unknown connector type"):
        get_connector_class("nonexistent_tracker_type_xyz")


def test_get_connector_class_error_message_lists_valid_types():
    with pytest.raises(ValueError, match="jira"):
        get_connector_class("not-real")


def test_register_connector_adds_to_registries():
    class FakeConnector(ConnectorBase):
        @classmethod
        def connector_type(cls) -> str:
            return "fake"

        @classmethod
        def can_handle_bare_key(cls, key: str) -> bool:
            return key.startswith("FAKE-")

        def parse_input(self, input_str: str) -> ParsedInput:
            return ParsedInput(issue_key=input_str)

        async def fetch(self, issue_key, config=None, log=None):
            raise NotImplementedError

        async def download_attachment(self, url: str) -> bytes:
            raise NotImplementedError

        async def process_attachments(self, raw, llm_config, log=None):
            raise NotImplementedError

    class FakeConnection:
        pass

    register_connector("fake", FakeConnector, FakeConnection)
    assert _CONNECTOR_CLASSES["fake"] is FakeConnector
    assert _CONNECTION_CLASSES["fake"] is FakeConnection
    assert get_connector_class("fake") is FakeConnector

    # cleanup to avoid polluting other tests
    _CONNECTOR_CLASSES.pop("fake", None)
    _CONNECTION_CLASSES.pop("fake", None)


def test_connector_base_refresh_credentials_is_noop():
    """refresh_credentials() default impl must not raise."""
    import asyncio
    from icx_engine.connectors.jira.connector import JiraConnector
    from icx_engine.connectors.jira.config import JiraConnection, TokenAuth
    conn = JiraConnection(
        connector_type="jira",
        domain="example.atlassian.net",
        auth=TokenAuth(auth_type="token", email="u@example.com", api_token="tok"),
    )
    jira = JiraConnector(conn)
    asyncio.run(jira.refresh_credentials())


def test_connector_base_extract_project_key_default():
    from icx_engine.connectors.jira.connector import JiraConnector
    assert JiraConnector.extract_project_key("PROJ-123") == "PROJ"
    assert JiraConnector.extract_project_key("AB-1") == "AB"
    assert JiraConnector.extract_project_key("NOHYPHEN") == "NOHYPHEN"
