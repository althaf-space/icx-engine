from __future__ import annotations

import pytest
from pathlib import Path
from unittest.mock import patch

from langgraph.checkpoint.memory import InMemorySaver

from icx_engine.testing.graph import get_testing_graph, get_db_path


def test_get_db_path_under_icx():
    p = get_db_path()
    assert p.name == "testing_sessions.db"
    assert ".icx" in str(p)


async def test_graph_compiles_without_error():
    saver = InMemorySaver()
    graph = await get_testing_graph(checkpointer=saver)
    assert graph is not None


async def test_graph_has_expected_nodes():
    saver = InMemorySaver()
    graph = await get_testing_graph(checkpointer=saver)
    node_names = set(graph.nodes.keys())
    expected = {
        "expand_files", "mode_select", "pick_type", "compat_check", "generate_context",
        "config_gate", "submit", "poll", "error_gate", "parse_report", "review",
        "limit_gate", "manual_wait", "manual_result", "ui_check", "memory_save",
    }
    assert expected.issubset(node_names)


@pytest.mark.asyncio
async def test_graph_compiles_with_new_nodes():
    from langgraph.checkpoint.memory import MemorySaver
    graph = await get_testing_graph(checkpointer=MemorySaver())
    nodes = set(graph.get_graph().nodes)
    assert "pick_type" in nodes
    assert "compat_check" in nodes
    assert "expand_files" in nodes


@pytest.mark.asyncio
async def test_graph_has_auth_gate():
    from langgraph.checkpoint.memory import MemorySaver
    graph = await get_testing_graph(checkpointer=MemorySaver())
    assert "auth_gate" in set(graph.get_graph().nodes)


@pytest.mark.asyncio
async def test_graph_has_profile_push_and_testing_is_sonar_free():
    from langgraph.checkpoint.memory import MemorySaver
    graph = await get_testing_graph(checkpointer=MemorySaver())
    nodes_set = set(graph.get_graph().nodes)
    assert "profile_push" in nodes_set
    # Sonar is a distinct feature - it must NOT be part of the testing graph
    assert "sonar_enrich" not in nodes_set


@pytest.mark.asyncio
async def test_graph_has_compat_scan():
    from langgraph.checkpoint.memory import MemorySaver
    graph = await get_testing_graph(checkpointer=MemorySaver())
    assert "compat_scan" in set(graph.get_graph().nodes)
