"""Tests for Phase 8: causal chain recording, full_ticket_text embed, session context."""
from __future__ import annotations
import pytest
from unittest.mock import MagicMock
from pathlib import Path


def _make_manager(tmp_path: Path):
    from icx_engine.memory.manager import MemoryManager
    mgr = MemoryManager(db_path=tmp_path / "mem")
    mgr._embeddings = MagicMock()
    mgr._embeddings.check_ready.return_value = None
    mgr._embeddings.embed.return_value = [0.1] * 768
    return mgr


def _save_with_chain(mgr, issue_key: str, causal_chain: dict = None, ticket_text: str = "", attachment_summary: str = ""):
    from icx_engine.memory.schema import MemoryEntry
    import uuid
    e = MemoryEntry(
        id=str(uuid.uuid4()),
        issue_key=issue_key,
        project_key="TEST",
        source_type="jira",
        issue_type="Bug",
        summary="Bug in auth",
        problem_description="Auth token expired",
        resolution_note="Fixed expiry check",
        files_changed=["src/auth.py"],
        resolution_confirmed=True,
        saved_at="2026-01-01T00:00:00",
        causal_chain=causal_chain or {},
        full_ticket_text=ticket_text,
        attachment_summary=attachment_summary,
    )
    mgr.save(e)
    return mgr._find_by_key(issue_key)


class TestCausalChainStorage:
    def test_causal_chain_persisted_and_retrieved(self, tmp_path):
        mgr = _make_manager(tmp_path)
        chain = {
            "ticket_summary": "JWT expiry bug",
            "intelligence_verdict": "seen_before",
            "graph_cluster": "auth-cluster",
            "suggested_files": ["src/auth.py"],
            "files_agent_opened": ["src/auth.py", "src/token.py"],
            "prior_resolution_used": "PROJ-88",
            "root_cause_confirmed": True,
            "diagnosis_steps": 7,
        }
        entry = _save_with_chain(mgr, "PROJ-1", causal_chain=chain)
        assert entry.causal_chain["ticket_summary"] == "JWT expiry bug"
        assert entry.causal_chain["intelligence_verdict"] == "seen_before"
        assert entry.causal_chain["diagnosis_steps"] == 7

    def test_empty_causal_chain_no_error(self, tmp_path):
        mgr = _make_manager(tmp_path)
        entry = _save_with_chain(mgr, "PROJ-1", causal_chain={})
        assert entry.causal_chain == {}

    def test_full_ticket_text_stored(self, tmp_path):
        mgr = _make_manager(tmp_path)
        text = "User reports JWT token not expiring correctly after midnight rollover."
        entry = _save_with_chain(mgr, "PROJ-1", ticket_text=text)
        assert entry.full_ticket_text == text

    def test_attachment_summary_stored(self, tmp_path):
        mgr = _make_manager(tmp_path)
        summary = "Screenshot shows HTTP 401 on valid token within TTL window."
        entry = _save_with_chain(mgr, "PROJ-1", attachment_summary=summary)
        assert entry.attachment_summary == summary

    def test_full_ticket_text_included_in_embed_text(self, tmp_path):
        from icx_engine.memory.manager import _build_embed_text
        from icx_engine.memory.schema import MemoryEntry
        import uuid
        e = MemoryEntry(
            id=str(uuid.uuid4()),
            issue_key="TEST-1",
            project_key="TEST",
            source_type="jira",
            issue_type="Bug",
            summary="Auth bug",
            problem_description="Desc",
            resolution_note="Fix",
            files_changed=["src/auth.py"],
            resolution_confirmed=True,
            saved_at="2026-01-01T00:00:00",
            full_ticket_text="token expiry validation fails midnight",
        )
        embed_text = _build_embed_text(e)
        assert "token expiry validation fails midnight" in embed_text

    def test_attachment_summary_included_in_embed_text(self, tmp_path):
        from icx_engine.memory.manager import _build_embed_text
        from icx_engine.memory.schema import MemoryEntry
        import uuid
        e = MemoryEntry(
            id=str(uuid.uuid4()),
            issue_key="TEST-1",
            project_key="TEST",
            source_type="jira",
            issue_type="Bug",
            summary="Auth bug",
            problem_description="Desc",
            resolution_note="Fix",
            files_changed=[],
            resolution_confirmed=True,
            saved_at="2026-01-01T00:00:00",
            attachment_summary="Screenshot showed expired token error in logs",
        )
        embed_text = _build_embed_text(e)
        assert "Screenshot showed expired token error" in embed_text


class TestSessionContext:
    def test_session_set_and_get(self):
        import icx_engine.mcp_server as mcp
        mcp._session_set("PROJ-1", "intelligence_verdict", "seen_before")
        assert mcp._session_get("PROJ-1", "intelligence_verdict") == "seen_before"

    def test_session_get_missing_key_returns_default(self):
        import icx_engine.mcp_server as mcp
        result = mcp._session_get("PROJ-99999", "nonexistent_key", "fallback")
        assert result == "fallback"

    def test_session_set_updates_existing(self):
        import icx_engine.mcp_server as mcp
        mcp._session_set("PROJ-2", "verdict", "novel")
        mcp._session_set("PROJ-2", "verdict", "seen_before")
        assert mcp._session_get("PROJ-2", "verdict") == "seen_before"
