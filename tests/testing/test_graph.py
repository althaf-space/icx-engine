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


@pytest.mark.asyncio
async def test_graph_has_analyze_screen_between_expand_and_compat():
    from langgraph.checkpoint.memory import MemorySaver
    graph = await get_testing_graph(checkpointer=MemorySaver())
    g = graph.get_graph()
    names = set(g.nodes)
    assert "analyze_screen" in names
    edges = {(e.source, e.target) for e in g.edges}
    # automated path: expand_files -> analyze_screen -> compat_scan
    assert ("analyze_screen", "compat_scan") in edges
    assert any(s == "expand_files" and t == "analyze_screen" for s, t in edges)


def _session_done(snapshot) -> bool:
    """The done-detection used by the MCP start/resume handlers. A session is done ONLY when nothing
    is pending AND no gate is awaiting input. Mirrors mcp_server so the multi-interrupt bug stays
    fixed: a node with several interrupt() calls pauses at its later interrupt with next == () while
    an interrupt is still pending - `not next` alone would wrongly report done."""
    has_interrupt = bool(snapshot.tasks and snapshot.tasks[0].interrupts)
    return (not snapshot.next) and (not has_interrupt)


@pytest.mark.asyncio
async def test_multi_interrupt_node_not_reported_done_midflow():
    # REGRESSION: node_expand_files has TWO interrupts (expand_scan, then expand). After answering
    # the first, LangGraph pauses at the second with snapshot.next == (). The old
    # `is_done = not snapshot.next` reported the session DONE here, so the agent stopped during file
    # expansion and 0 tests ran. The session must be reported NOT done while the `expand` gate waits.
    from langgraph.checkpoint.memory import MemorySaver
    from langgraph.types import Command
    from icx_engine.testing.state import make_initial_state

    graph = await get_testing_graph(checkpointer=MemorySaver())
    cfg = {"configurable": {"thread_id": "multi-int-regression"}}
    init = make_initial_state(file_paths=["a.jsx", "b.jsx"], context="x",
                              max_iterations=1, test_mode="automated")
    await graph.ainvoke(init, config=cfg)

    # pick_type (first interrupt of the run) -> not done
    snap = await graph.aget_state(cfg)
    assert not _session_done(snap)
    assert snap.tasks[0].interrupts[0].value.get("gate") == "pick_type"
    await graph.ainvoke(Command(resume={"test_type": "1"}), config=cfg)

    # expand_scan (first interrupt of expand_files) -> not done
    snap = await graph.aget_state(cfg)
    assert not _session_done(snap)
    assert snap.tasks[0].interrupts[0].value.get("gate") == "expand_scan"
    await graph.ainvoke(Command(resume={"related_files": []}), config=cfg)

    # expand (SECOND interrupt of expand_files): snapshot.next == () but an interrupt is pending.
    # This is the exact state the bug mis-reported as done.
    snap = await graph.aget_state(cfg)
    assert snap.next == ()                                  # the trap
    assert snap.tasks and snap.tasks[0].interrupts          # but a gate IS waiting
    assert snap.tasks[0].interrupts[0].value.get("gate") == "expand"
    assert not _session_done(snap)                          # must NOT be reported done


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
