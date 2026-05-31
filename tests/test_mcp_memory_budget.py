"""Tests for MCP memory state reporting in mcp_server.py.

Verifies that _handle_analyze_issue:
- reports memory.status correctly based on memory engine state
- never blocks on memory search (search is now agent-driven via memory_search tool)
- always returns work_item, graphs, _icx_next intact regardless of memory state
"""
from __future__ import annotations

import asyncio
import json
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
    assert "note" in data["memory"]
    assert "work_item" in data
    assert "graphs" in data


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
# _handle_analyze_issue: memory state reported, no search executed
# ---------------------------------------------------------------------------

async def test_memory_status_ready_when_state_is_ready(patched_engine):
    """State=ready: response reports memory.status='ready', no count or results fields."""
    mcp._set_memory_state("ready")
    result_json = await mcp._handle_analyze_issue("TEST-1", project_paths=["/fake/project"])
    data = json.loads(result_json)
    assert data["memory"]["status"] == "ready"
    assert "count" not in data["memory"]
    assert "results" not in data["memory"]
    assert "note" not in data["memory"]
    assert "work_item" in data
    assert "graphs" in data


async def test_memory_no_note_when_ready(patched_engine):
    """State=ready: memory section has status only, no note."""
    mcp._set_memory_state("ready")
    result_json = await mcp._handle_analyze_issue("TEST-1", project_paths=["/fake/project"])
    data = json.loads(result_json)
    assert data["memory"]["status"] == "ready"
    assert "note" not in data["memory"]
    assert "work_item" in data
    assert "_icx_next" in data


async def test_primary_context_intact_when_memory_failed(patched_engine):
    """Memory failed state must not suppress work_item, graphs, or _icx_next."""
    mcp._set_memory_state("failed")
    result_json = await mcp._handle_analyze_issue("TEST-1", project_paths=["/fake/project"])
    data = json.loads(result_json)
    assert "work_item" in data
    assert "graphs" in data
    assert "_icx_next" in data
    assert data["memory"]["status"] == "failed"
    assert "note" in data["memory"]


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
