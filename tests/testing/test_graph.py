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
        "expand_files", "mode_select", "pick_type", "compat_check",
        "config_gate", "auth_gate", "local_run", "review",
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
async def test_graph_local_run_and_testing_is_sonar_free():
    from langgraph.checkpoint.memory import MemorySaver
    graph = await get_testing_graph(checkpointer=MemorySaver())
    nodes_set = set(graph.get_graph().nodes)
    assert "local_run" in nodes_set
    # Magik execution nodes are gone
    assert "submit" not in nodes_set and "poll" not in nodes_set
    # Sonar is a distinct feature - it must NOT be part of the testing graph
    assert "sonar_enrich" not in nodes_set


@pytest.mark.asyncio
async def test_graph_has_compat_scan():
    from langgraph.checkpoint.memory import MemorySaver
    graph = await get_testing_graph(checkpointer=MemorySaver())
    assert "compat_scan" in set(graph.get_graph().nodes)


# -- graceful shutdown: checkpoint connection is released, idempotently -------------

async def test_close_testing_graph_closes_and_is_idempotent():
    import icx_engine.testing.graph as g
    closed = {"n": 0}

    class _Conn:
        async def close(self):
            closed["n"] += 1

    g._CHECKPOINT_CONN = _Conn()
    g._GRAPH_INSTANCE = object()
    await g.close_testing_graph()
    assert closed["n"] == 1
    assert g._CHECKPOINT_CONN is None and g._GRAPH_INSTANCE is None
    await g.close_testing_graph()          # no conn now -> no crash, no double close
    assert closed["n"] == 1


async def test_close_testing_graph_guards_close_error():
    import icx_engine.testing.graph as g

    class _BadConn:
        async def close(self):
            raise RuntimeError("boom")

    g._CHECKPOINT_CONN = _BadConn()
    g._GRAPH_INSTANCE = object()
    await g.close_testing_graph()          # swallowed, state still reset
    assert g._CHECKPOINT_CONN is None and g._GRAPH_INSTANCE is None
