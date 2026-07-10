from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import aiosqlite
from langgraph.graph import StateGraph, END
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from icx_engine.testing.state import TestingState
from icx_engine.testing.nodes import (
    node_expand_files,
    node_mode_select,
    node_mode_gate,
    node_pick_type,
    node_compat_scan,
    node_compat_check,
    node_config_gate,
    node_auth_gate,
    node_author_flow,
    route_after_auth,
    node_local_run,
    node_review,
    node_limit_gate,
    node_manual_wait,
    node_manual_result,
    node_ui_check,
    node_memory_save,
    route_after_mode_select,
    route_after_check_issues,
    route_after_expand,
    route_after_scan,
    route_after_compat,
)


def get_db_path() -> Path:
    return Path.home() / ".icx" / "testing_sessions.db"


async def _make_checkpointer() -> AsyncSqliteSaver:
    db_path = get_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True, **({"mode": 0o700} if sys.platform != "win32" else {}))
    conn = await aiosqlite.connect(str(db_path))
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA busy_timeout=5000")
    if sys.platform != "win32" and db_path.exists():
        db_path.chmod(0o600)
    saver = AsyncSqliteSaver(conn)
    await saver.setup()
    return saver


def _purge_old_sessions(db_path: Path) -> None:
    from icx_engine.testing.session_store import purge_old_sessions
    purge_old_sessions(db_path, days=7)


_GRAPH_INSTANCE = None


def _limit_gate_route(state: TestingState) -> str:
    if state["status"] == "cancelled":
        return "ui_check"
    return "local_run"


async def get_testing_graph(checkpointer: Any | None = None) -> Any:
    global _GRAPH_INSTANCE
    if _GRAPH_INSTANCE is not None and checkpointer is None:
        return _GRAPH_INSTANCE

    _should_cache = checkpointer is None
    if checkpointer is None:
        db_path = get_db_path()
        _purge_old_sessions(db_path)
        checkpointer = await _make_checkpointer()

    builder = StateGraph(TestingState)

    # -- nodes ------------------------------------------------------------
    builder.add_node("expand_files",    node_expand_files)
    builder.add_node("mode_select",     node_mode_select)
    builder.add_node("pick_type",       node_pick_type)
    builder.add_node("compat_scan",     node_compat_scan)
    builder.add_node("compat_check",    node_compat_check)
    builder.add_node("config_gate",     node_config_gate)
    builder.add_node("auth_gate",       node_auth_gate)
    builder.add_node("author_flow",     node_author_flow)
    builder.add_node("local_run",       node_local_run)
    builder.add_node("review",          node_review)
    builder.add_node("limit_gate",      node_limit_gate)
    builder.add_node("manual_wait",     node_manual_wait)
    builder.add_node("manual_result",   node_manual_result)
    builder.add_node("ui_check",        node_ui_check)
    builder.add_node("memory_save",     node_memory_save)

    # -- entry -------------------------------------------------------------
    builder.set_entry_point("mode_select")

    # -- mode selection -> pick_type or expand_files -----------------------
    builder.add_conditional_edges("mode_select", route_after_mode_select, {
        "pick_type":    "pick_type",
        "expand_files": "expand_files",
    })
    builder.add_edge("pick_type", "expand_files")
    builder.add_conditional_edges("expand_files", route_after_expand, {
        "compat_scan": "compat_scan",
        "manual_wait": "manual_wait",
    })
    builder.add_conditional_edges("compat_scan", route_after_scan, {
        "compat_check": "compat_check",
        "config_gate":  "config_gate",
    })
    builder.add_conditional_edges("compat_check", route_after_compat, {
        "compat_scan": "compat_scan",
        "config_gate": "config_gate",
        "ui_check":    "ui_check",
    })

    # -- automated path (local engine) ------------------------------------
    builder.add_edge("config_gate", "auth_gate")
    builder.add_conditional_edges("auth_gate", route_after_auth, {
        "author_flow": "author_flow",
        "local_run":   "local_run",
    })
    builder.add_edge("author_flow", "local_run")
    builder.add_edge("local_run",   "review")
    builder.add_conditional_edges("review", route_after_check_issues, {
        "ui_check":   "ui_check",
        "loop":       "local_run",
        "limit_gate": "limit_gate",
    })
    builder.add_conditional_edges("limit_gate", _limit_gate_route, {
        "ui_check":  "ui_check",
        "local_run": "local_run",
    })

    # -- manual path -------------------------------------------------------
    builder.add_edge("manual_wait",   "manual_result")
    builder.add_edge("manual_result", "ui_check")

    # -- shared tail: UI check -> memory save -> done ----------------------
    builder.add_edge("ui_check",    "memory_save")
    builder.add_edge("memory_save", END)

    graph = builder.compile(checkpointer=checkpointer)

    if _should_cache:
        _GRAPH_INSTANCE = graph

    return graph
