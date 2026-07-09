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
    node_generate_context,
    node_config_gate,
    node_auth_gate,
    node_profile_push,
    node_submit,
    node_poll,
    node_error_gate,
    node_parse_report,
    node_review,
    node_limit_gate,
    node_manual_wait,
    node_manual_result,
    node_ui_check,
    node_memory_save,
    route_after_mode_select,
    route_after_check_issues,
    route_after_poll,
    route_after_error_gate,
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
    return "submit"


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
    builder.add_node("generate_context", node_generate_context)
    builder.add_node("config_gate",     node_config_gate)
    builder.add_node("auth_gate",       node_auth_gate)
    builder.add_node("profile_push",    node_profile_push)
    builder.add_node("submit",          node_submit)
    builder.add_node("poll",            node_poll)
    builder.add_node("error_gate",      node_error_gate)
    builder.add_node("parse_report",    node_parse_report)
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
        "compat_check":     "compat_check",
        "generate_context": "generate_context",
    })
    builder.add_conditional_edges("compat_check", route_after_compat, {
        "compat_scan":      "compat_scan",
        "generate_context": "generate_context",
        "ui_check":         "ui_check",
    })

    # -- automated path ----------------------------------------------------
    builder.add_edge("generate_context", "config_gate")
    builder.add_edge("config_gate",      "auth_gate")
    builder.add_edge("auth_gate",        "profile_push")
    builder.add_edge("profile_push",     "submit")
    builder.add_edge("submit",           "poll")
    builder.add_conditional_edges("poll", route_after_poll, {
        "error_gate":   "error_gate",
        "parse_report": "parse_report",
    })
    builder.add_conditional_edges("error_gate", route_after_error_gate, {
        "ui_check":     "ui_check",
        "parse_report": "parse_report",
        "submit":       "submit",
        "auth_gate":    "auth_gate",
    })
    builder.add_edge("parse_report", "review")
    builder.add_conditional_edges("review", route_after_check_issues, {
        "ui_check":   "ui_check",
        "loop":       "submit",
        "limit_gate": "limit_gate",
    })
    builder.add_conditional_edges("limit_gate", _limit_gate_route, {
        "ui_check": "ui_check",
        "submit":   "submit",
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
