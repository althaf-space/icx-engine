"""Tests for MCP memory budget enforcement (mcp_server.py).

Verifies that memory search is non-blocking:
- slow/cold/warming/failed states return immediately with status codes
- normal tool responses are never blocked by ONNX model loading
- memory results are included when the model is already warm and search is fast
"""
from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import patch, MagicMock, AsyncMock

import pytest

import icx_engine.mcp_server as mcp


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_issue_context():
    from icx_engine.models.output import IssueContext
    return IssueContext(
        problem_summary="Test bug",
        detailed_description="Description",
        reproduction_steps=[],
        expected_behavior=None,
        actual_behavior=None,
        acceptance_criteria=[],
        impact="medium",
        priority="High",
        issue_type="Bug",
        confidence_score=0.9,
        completeness_score=0.9,
        missing_information=[],
    )


def _make_fake_config():
    from icx_engine.models.config import AppConfig
    return AppConfig(connections=[], llm_profiles={}, default_connection=None)


_FAKE_GRAPH_INFO = {
    "path": "/fake/project",
    "status": "not_built",
    "report_path": None,
    "freshness": "not_checked",
    "eta_seconds": None,
    "access": "",
}


@pytest.fixture(autouse=True)
def reset_memory_state():
    """Reset global memory state to 'cold' before each test."""
    original = mcp._memory_state
    yield
    mcp._set_memory_state(original)


@pytest.fixture(scope="module", autouse=True)
def warm_executor():
    """Pre-create the memory executor thread once per module.

    On Windows, ThreadPoolExecutor thread creation + asyncio event loop init
    can take 2-5s on first submit. Pre-warm it so timing tests are reliable.
    """
    executor = mcp._get_memory_executor()
    future = executor.submit(lambda: None)
    try:
        future.result(timeout=15.0)
    except Exception:
        pass  # non-fatal - tests proceed even if prewarm fails


@pytest.fixture
def patched_engine(monkeypatch):
    """Mock engine, config, and graph info so tests only exercise the memory path."""
    fake_result = _make_issue_context()
    fake_config = _make_fake_config()
    monkeypatch.setattr("icx_engine.mcp_server.ConfigManager.load", lambda: fake_config)
    monkeypatch.setattr("icx_engine.engine.run", AsyncMock(return_value=fake_result))
    monkeypatch.setattr("icx_engine.mcp_server._get_graph_info", lambda p: _FAKE_GRAPH_INFO)
    monkeypatch.setattr("icx_engine.mcp_server._get_graphs_info", lambda ps: [_FAKE_GRAPH_INFO])
    return fake_result


# ---------------------------------------------------------------------------
# State machine unit tests
# ---------------------------------------------------------------------------

def test_initial_state_is_cold():
    mcp._set_memory_state("cold")
    assert mcp._get_memory_state() == "cold"


def test_set_get_state_transitions():
    for state in ("cold", "warming", "ready", "failed"):
        mcp._set_memory_state(state)
        assert mcp._get_memory_state() == state


def test_prewarm_memory_sets_warming_then_ready():
    mcp._set_memory_state("cold")
    states_seen: list[str] = []

    original_set = mcp._set_memory_state

    def recording_set(s: str) -> None:
        states_seen.append(s)
        original_set(s)

    with patch.object(mcp, "_set_memory_state", side_effect=recording_set):
        with patch.object(mcp, "_ensure_memory_manager") as mock_em:
            mock_mem = MagicMock()
            mock_em.return_value = mock_mem
            mcp._prewarm_memory()

    assert "warming" in states_seen
    assert "ready" in states_seen
    assert states_seen.index("warming") < states_seen.index("ready")
    mock_mem.prewarm.assert_called_once()


def test_prewarm_memory_sets_failed_on_exception():
    mcp._set_memory_state("cold")
    with patch.object(mcp, "_ensure_memory_manager", side_effect=RuntimeError("onnx crash")):
        mcp._prewarm_memory()
    assert mcp._get_memory_state() == "failed"


# ---------------------------------------------------------------------------
# _handle_analyze_issue: memory skipped when warming/cold/failed
# ---------------------------------------------------------------------------

async def test_memory_skipped_warming_returns_warming_up(patched_engine, monkeypatch):
    """State=warming: no executor submission, response includes warming_up status."""
    mcp._set_memory_state("warming")
    with patch.object(mcp, "_search_memory_sync") as mock_search:
        result_json = await mcp._handle_analyze_issue(
            "TEST-1", project_paths=["/fake/project"]
        )
    mock_search.assert_not_called()
    data = json.loads(result_json)
    assert data["memory"]["status"] == "warming_up"
    assert data["memory"]["count"] == 0
    assert "note" in data["memory"]
    assert "work_item" in data
    assert "graph" in data


async def test_memory_skipped_cold_returns_warming_up(patched_engine, monkeypatch):
    """State=cold: no executor submission, returns warming_up (triggering prewarm)."""
    mcp._set_memory_state("cold")
    with patch.object(mcp, "_search_memory_sync") as mock_search:
        result_json = await mcp._handle_analyze_issue(
            "TEST-1", project_paths=["/fake/project"]
        )
    mock_search.assert_not_called()
    data = json.loads(result_json)
    assert data["memory"]["status"] == "warming_up"
    assert "work_item" in data


async def test_memory_skipped_failed_returns_failed(patched_engine, monkeypatch):
    """State=failed: no executor submission, response includes failed status."""
    mcp._set_memory_state("failed")
    with patch.object(mcp, "_search_memory_sync") as mock_search:
        result_json = await mcp._handle_analyze_issue(
            "TEST-1", project_paths=["/fake/project"]
        )
    mock_search.assert_not_called()
    data = json.loads(result_json)
    assert data["memory"]["status"] == "failed"
    assert "work_item" in data


# ---------------------------------------------------------------------------
# Memory budget: slow model load must not block for 30s
# ---------------------------------------------------------------------------

async def test_memory_timeout_returns_within_budget(patched_engine, monkeypatch):
    """State=ready but slow search: returns after MCP_MEMORY_TIMEOUT_SECONDS, not 30s."""
    mcp._set_memory_state("ready")
    monkeypatch.setattr(mcp, "MCP_MEMORY_TIMEOUT_SECONDS", 0.15)

    def slow_search(_qi):
        time.sleep(10)  # simulates ONNX loading
        return []

    monkeypatch.setattr(mcp, "_search_memory_sync", slow_search)

    t0 = time.perf_counter()
    result_json = await mcp._handle_analyze_issue("TEST-1", project_paths=["/fake/project"])
    elapsed = time.perf_counter() - t0

    assert elapsed < 2.0, f"Expected fast return, got {elapsed:.2f}s"
    data = json.loads(result_json)
    assert data["memory"]["status"] == "skipped_timeout"
    assert data["memory"]["count"] == 0
    assert "note" in data["memory"]
    assert "work_item" in data
    assert "graph" in data


async def test_memory_results_returned_when_ready_and_fast(patched_engine, monkeypatch):
    """State=ready and fast search: results included with ready status.

    Uses a large timeout budget so Windows asyncio+executor cold-start latency
    doesn't interfere. This test checks response shape, not timing.
    """
    mcp._set_memory_state("ready")
    monkeypatch.setattr(mcp, "MCP_MEMORY_TIMEOUT_SECONDS", 30.0)

    fake_results = [
        {"issue_key": "PREV-1", "resolution_note": "Fixed by X", "similarity_score": 0.9,
         "source_type": "jira", "summary": "s", "files_changed": [], "saved_at": "2026-01-01",
         "work_item_type": "bug", "pattern_used": ""}
    ]
    monkeypatch.setattr(mcp, "_search_memory_sync", lambda _qi: fake_results)

    result_json = await mcp._handle_analyze_issue("TEST-1", project_paths=["/fake/project"])

    data = json.loads(result_json)
    assert data["memory"]["status"] == "ready"
    assert data["memory"]["count"] == 1
    assert data["memory"]["results"] == fake_results
    assert "note" not in data["memory"]


async def test_primary_context_intact_when_memory_errors(patched_engine, monkeypatch):
    """Memory error must not suppress work_item, graph, or _icx_next.

    Uses large timeout so executor cold-start doesn't mask the error path.
    """
    mcp._set_memory_state("ready")
    monkeypatch.setattr(mcp, "MCP_MEMORY_TIMEOUT_SECONDS", 30.0)

    def failing_search(_qi):
        raise RuntimeError("LanceDB exploded")

    monkeypatch.setattr(mcp, "_search_memory_sync", failing_search)

    result_json = await mcp._handle_analyze_issue("TEST-1", project_paths=["/fake/project"])

    data = json.loads(result_json)
    assert "work_item" in data
    assert "graph" in data
    assert "_icx_next" in data
    assert data["memory"]["status"] == "failed"
    assert data["memory"]["count"] == 0


# ---------------------------------------------------------------------------
# prewarm() loads model test
# ---------------------------------------------------------------------------

def test_prewarm_calls_check_ready_only():
    """prewarm() must call check_ready() + _load_model() (eager), NOT ensure_ready() or embed()."""
    from icx_engine.memory.manager import MemoryManager
    mgr = MemoryManager.__new__(MemoryManager)
    mgr._embeddings = MagicMock()
    mgr.prewarm()
    mgr._embeddings.check_ready.assert_called_once()
    mgr._embeddings._load_model.assert_called_once()
    mgr._embeddings.ensure_ready.assert_not_called()
    mgr._embeddings.embed.assert_not_called()


def test_prewarm_check_ready_failure_propagates():
    """check_ready failure during prewarm propagates (handled by _prewarm_memory caller)."""
    from icx_engine.memory.manager import MemoryManager
    mgr = MemoryManager.__new__(MemoryManager)
    mgr._embeddings = MagicMock()
    mgr._embeddings.check_ready.side_effect = RuntimeError("model not downloaded")
    with pytest.raises(RuntimeError):
        mgr.prewarm()
