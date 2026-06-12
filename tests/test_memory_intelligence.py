"""Tests for Phase 10: pre-analysis intelligence layer, _build_intelligence, verdicts."""
from __future__ import annotations
import pytest
from unittest.mock import MagicMock, patch, AsyncMock
import json


class TestBuildIntelligence:
    def _call(self, memory_results=None, patterns=None, problem_summary="auth token bug"):
        from icx_engine.mcp_server import _build_intelligence
        memory_results = memory_results or {"results": [], "negative_signals": [], "decay_applied": False}
        patterns = patterns or []
        graph_info = {"context_files": ["src/auth.py"], "_problem_summary": problem_summary}
        return _build_intelligence(
            issue_key="PROJ-TEST",
            memory_results=memory_results,
            graph_info=graph_info,
            pattern_results=patterns,
        )

    def test_memory_hit_above_072_verdict_seen_before(self):
        memory_results = {
            "results": [{"issue_key": "PROJ-88", "similarity_score": 0.75, "outcome_verified": False}],
            "negative_signals": [],
        }
        intel = self._call(memory_results=memory_results)
        assert intel["verdict"] == "seen_before"
        assert intel["confidence"] >= 0.72

    def test_skip_diagnosis_requires_verified_and_high_confidence(self):
        memory_results = {
            "results": [{"issue_key": "PROJ-88", "similarity_score": 0.85, "outcome_verified": True}],
            "negative_signals": [],
        }
        intel = self._call(memory_results=memory_results)
        assert intel["skip_diagnosis"] is True

    def test_verified_but_confidence_below_080_no_skip(self):
        memory_results = {
            "results": [{"issue_key": "PROJ-88", "similarity_score": 0.75, "outcome_verified": True}],
            "negative_signals": [],
        }
        intel = self._call(memory_results=memory_results)
        assert intel["skip_diagnosis"] is False

    def test_no_memory_semantic_pattern_hit_verdict_pattern_match(self):
        patterns = [{
            "pattern_type": "semantic_signal",
            "evidence": {
                "signal_words": ["auth", "token"],
                "root_cause_pattern": "stale_cache_reference",
                "top_fix_file": "src/auth.py",
                "top_fix_file_rate": 0.9,
                "group_size": 5,
            },
        }]
        intel = self._call(patterns=patterns, problem_summary="auth token expiry fails midnight")
        assert intel["verdict"] == "pattern_match"
        assert intel["confidence"] >= 0.55

    def test_no_memory_no_pattern_verdict_novel(self):
        intel = self._call()
        assert intel["verdict"] == "novel"
        assert intel["confidence"] == 0.0

    def test_negated_entry_in_negative_signals_not_prior_resolution(self):
        memory_results = {
            "results": [],
            "negative_signals": [{"issue_key": "PROJ-BAD", "negation_reason": "Caused deadlock"}],
        }
        intel = self._call(memory_results=memory_results)
        assert intel["prior_resolution"] is None
        assert len(intel["negative_signals"]) == 1
        assert intel["negative_signals"][0]["issue_key"] == "PROJ-BAD"

    def test_token_budget_formula_correct(self):
        # No prior resolution: 500 + (1 * 800) + 0 = 1300 (1 suggested file)
        intel = self._call()
        assert intel["token_budget_estimate"] == 1300  # 500 + 1*800

    def test_token_budget_with_prior_resolution(self):
        memory_results = {
            "results": [{"issue_key": "PROJ-88", "similarity_score": 0.75, "outcome_verified": False}],
            "negative_signals": [],
        }
        intel = self._call(memory_results=memory_results)
        # 500 + (1 * 800) + 300 = 1600
        assert intel["token_budget_estimate"] == 1600

    def test_session_context_stores_verdict_and_files(self):
        import icx_engine.mcp_server as mcp
        intel = self._call()
        verdict = mcp._session_get("PROJ-TEST", "intelligence_verdict", None)
        assert verdict == "novel"
        files = mcp._session_get("PROJ-TEST", "suggested_files", None)
        assert files == ["src/auth.py"]

    async def test_intelligence_field_present_in_analyze_response(self, monkeypatch):
        """Intelligence field appears in response when memory is ready."""
        from icx_engine.models.output import IssueContext
        fake_result = IssueContext(
            problem_summary="Auth token bug",
            detailed_description="Desc",
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
        from icx_engine.models.config import AppConfig
        import icx_engine.mcp_server as mcp

        monkeypatch.setattr("icx_engine.mcp_server.ConfigManager.load", lambda: AppConfig())
        monkeypatch.setattr("icx_engine.engine.run", AsyncMock(return_value=fake_result))
        monkeypatch.setattr("icx_engine.mcp_server._get_graphs_info", lambda ps: [{
            "path": "/fake", "status": "not_built", "report_path": None,
            "freshness": "not_checked", "eta_seconds": None, "access": "",
        }])

        original_state = mcp._memory_state
        mcp._set_memory_state("ready")
        try:
            with patch.object(mcp, "_quick_memory_search_sync", return_value={
                "results": [], "negative_signals": [], "decay_applied": False
            }):
                with patch.object(mcp, "_get_patterns_sync", return_value=[]):
                    result_json = await mcp._handle_analyze_issue("TEST-1", project_paths=["/fake"])
            data = json.loads(result_json)
            assert "intelligence" in data
            assert data["intelligence"]["verdict"] in ("novel", "seen_before", "pattern_match")
        finally:
            mcp._set_memory_state(original_state)
