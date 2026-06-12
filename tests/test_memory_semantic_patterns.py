"""Tests for Phase 9: cross-ticket semantic pattern detection."""
from __future__ import annotations
import pytest
from icx_engine.memory.patterns import detect_patterns


def _make_entry(issue_key: str, pattern: str, ticket_text: str, fix_file: str):
    from icx_engine.memory.schema import MemoryEntry
    import uuid
    return MemoryEntry(
        id=str(uuid.uuid4()),
        issue_key=issue_key,
        project_key="TEST",
        source_type="jira",
        issue_type="Bug",
        summary="Bug",
        problem_description="Desc",
        resolution_note="Fix",
        files_changed=[fix_file],
        resolution_confirmed=True,
        saved_at="2026-01-01T00:00:00",
        root_cause_pattern=pattern,
        full_ticket_text=ticket_text,
    )


class TestSemanticSignalDetection:
    def test_5_entries_common_words_common_file_detects_signal(self):
        entries = [
            _make_entry(
                f"PROJ-{i}",
                "stale_cache_reference",
                "token expiry validation fails for authenticated users refresh expired",
                "src/auth/refresh.py",
            )
            for i in range(5)
        ]
        patterns = detect_patterns(entries)
        semantic = [p for p in patterns if p["pattern_type"] == "semantic_signal"]
        assert len(semantic) >= 1
        ev = semantic[0]["evidence"]
        assert ev["top_fix_file"] == "src/auth/refresh.py"
        assert ev["top_fix_file_rate"] == 1.0
        assert ev["root_cause_pattern"] == "stale_cache_reference"

    def test_signal_words_exclude_stop_words(self):
        from icx_engine.memory.patterns import _STOP_WORDS
        entries = [
            _make_entry(
                f"PROJ-{i}",
                "stale_cache_reference",
                "token expiry validation fails the is for and with they",
                "src/auth.py",
            )
            for i in range(5)
        ]
        patterns = detect_patterns(entries)
        semantic = [p for p in patterns if p["pattern_type"] == "semantic_signal"]
        for p in semantic:
            for word in p["evidence"]["signal_words"]:
                assert word not in _STOP_WORDS

    def test_less_than_3_entries_no_detection(self):
        entries = [
            _make_entry(
                f"PROJ-{i}",
                "stale_cache_reference",
                "token expiry fails refresh expired midnight",
                "src/auth.py",
            )
            for i in range(2)
        ]
        patterns = detect_patterns(entries)
        semantic = [p for p in patterns if p["pattern_type"] == "semantic_signal"]
        assert len(semantic) == 0

    def test_top_file_below_50_percent_no_detection(self):
        # Each entry has a different fix file
        entries = [
            _make_entry(
                f"PROJ-{i}",
                "stale_cache_reference",
                "token expiry validation fails midnight refresh expired",
                f"src/file_{i}.py",
            )
            for i in range(5)
        ]
        patterns = detect_patterns(entries)
        semantic = [p for p in patterns if p["pattern_type"] == "semantic_signal"]
        # No single file appears in >= 50% of entries
        assert len(semantic) == 0

    def test_uncategorized_pattern_excluded_from_semantic(self):
        entries = [
            _make_entry(
                f"PROJ-{i}",
                "uncategorized",
                "token expiry validation fails midnight refresh",
                "src/auth.py",
            )
            for i in range(5)
        ]
        patterns = detect_patterns(entries)
        semantic = [p for p in patterns if p["pattern_type"] == "semantic_signal"]
        # uncategorized is excluded
        assert all(p["evidence"]["root_cause_pattern"] != "uncategorized" for p in semantic)


class TestCheckSemanticPatterns:
    def test_2_signal_words_in_ticket_returns_warning(self):
        from icx_engine.mcp_server import _check_semantic_patterns
        import json
        patterns = [{
            "pattern_type": "semantic_signal",
            "evidence": {
                "signal_words": ["token", "expiry", "midnight"],
                "root_cause_pattern": "stale_cache_reference",
                "top_fix_file": "src/auth/refresh.py",
                "top_fix_file_rate": 0.8,
                "group_size": 5,
            },
        }]
        warning = _check_semantic_patterns(patterns, "token expiry issue in auth service")
        assert len(warning) > 0
        assert "src/auth/refresh.py" in warning

    def test_1_signal_word_in_ticket_no_warning(self):
        from icx_engine.mcp_server import _check_semantic_patterns
        patterns = [{
            "pattern_type": "semantic_signal",
            "evidence": {
                "signal_words": ["token", "expiry"],
                "root_cause_pattern": "stale_cache_reference",
                "top_fix_file": "src/auth.py",
                "top_fix_file_rate": 0.9,
                "group_size": 5,
            },
        }]
        warning = _check_semantic_patterns(patterns, "only token mentioned here")
        assert warning == ""

    def test_non_semantic_pattern_ignored(self):
        from icx_engine.mcp_server import _check_semantic_patterns
        patterns = [{
            "pattern_type": "frequent_file",
            "evidence": {"signal_words": ["token", "expiry"], "top_fix_file": "src/app.py"},
        }]
        warning = _check_semantic_patterns(patterns, "token expiry failing")
        assert warning == ""
