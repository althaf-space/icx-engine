"""Tests for Phase 5: citation hub detection, semantic pattern detection, threshold changes."""
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


def _save_entry(mgr, issue_key: str, pattern: str = "stale_cache_reference", files=None, ticket_text=""):
    from icx_engine.memory.schema import MemoryEntry
    import uuid
    e = MemoryEntry(
        id=str(uuid.uuid4()),
        issue_key=issue_key,
        project_key="TEST",
        source_type="jira",
        issue_type="Bug",
        summary=f"Bug {issue_key}",
        problem_description="Description",
        resolution_note="Fix applied",
        files_changed=files or ["src/app.py"],
        resolution_confirmed=True,
        saved_at="2026-01-01T00:00:00",
        root_cause_pattern=pattern,
        full_ticket_text=ticket_text,
    )
    mgr.save(e)
    return e


class TestCitationHubDetection:
    def test_5_entries_same_pattern_same_citation_detects_hub(self, tmp_path):
        from icx_engine.memory.patterns import detect_patterns
        from icx_engine.memory.schema import MemoryEntry
        import uuid
        entries = []
        for i in range(5):
            e = MemoryEntry(
                id=str(uuid.uuid4()),
                issue_key=f"PROJ-{i}",
                project_key="TEST",
                source_type="jira",
                issue_type="Bug",
                summary=f"Bug {i}",
                problem_description="Desc",
                resolution_note="Fix",
                files_changed=["src/cache.py"],
                resolution_confirmed=True,
                saved_at="2026-01-01T00:00:00",
                root_cause_pattern="stale_cache_reference",
                used_by_tickets=["HUB-KEY"],
            )
            entries.append(e)
        patterns = detect_patterns(entries)
        hub_patterns = [p for p in patterns if p["pattern_type"] == "citation_hub"]
        assert len(hub_patterns) >= 1
        assert any(p["evidence"]["hub_key"] == "HUB-KEY" for p in hub_patterns)

    def test_group_of_2_no_hub_detected(self):
        from icx_engine.memory.patterns import detect_patterns
        from icx_engine.memory.schema import MemoryEntry
        import uuid
        entries = [
            MemoryEntry(
                id=str(uuid.uuid4()),
                issue_key=f"PROJ-{i}",
                project_key="TEST",
                source_type="jira",
                issue_type="Bug",
                summary="Bug",
                problem_description="Desc",
                resolution_note="Fix",
                files_changed=[],
                resolution_confirmed=True,
                saved_at="2026-01-01T00:00:00",
                root_cause_pattern="stale_cache_reference",
                used_by_tickets=["HUB-KEY"],
            )
            for i in range(2)
        ]
        patterns = detect_patterns(entries)
        hub_patterns = [p for p in patterns if p["pattern_type"] == "citation_hub"]
        assert len(hub_patterns) == 0

    def test_pattern_refresh_triggers_at_5_saves(self, tmp_path):
        mgr = _make_manager(tmp_path)
        refresh_called = []
        original_refresh = mgr._patterns.refresh

        def tracking_refresh(*args, **kwargs):
            refresh_called.append(1)
            return original_refresh(*args, **kwargs)

        mgr._patterns.refresh = tracking_refresh
        for i in range(5):
            _save_entry(mgr, f"PROJ-{i}")
        assert len(refresh_called) >= 1

    def test_get_patterns_includes_citation_hub_type(self, tmp_path):
        mgr = _make_manager(tmp_path)
        # Manually inject a citation_hub pattern
        import uuid, json
        from datetime import datetime, timezone
        table = mgr._patterns._get_table()
        table.add([{
            "id": str(uuid.uuid4()),
            "project_key": "TEST",
            "pattern_type": "citation_hub",
            "label": "TEST-HUB cited by 4/5 resolutions",
            "evidence": json.dumps({"hub_key": "TEST-HUB", "citation_count": 4}),
            "entry_count": 5,
            "detected_at": datetime.now(timezone.utc).isoformat(),
        }])
        patterns = mgr.get_patterns()
        hub = [p for p in patterns if p["pattern_type"] == "citation_hub"]
        assert len(hub) >= 1
        assert hub[0]["evidence"]["hub_key"] == "TEST-HUB"


class TestSemanticSignalDetection:
    def test_semantic_signal_detected_with_5_entries(self):
        from icx_engine.memory.patterns import detect_patterns
        from icx_engine.memory.schema import MemoryEntry
        import uuid
        entries = []
        for i in range(5):
            e = MemoryEntry(
                id=str(uuid.uuid4()),
                issue_key=f"PROJ-{i}",
                project_key="TEST",
                source_type="jira",
                issue_type="Bug",
                summary="Token expiry",
                problem_description="Desc",
                resolution_note="Fix",
                files_changed=["src/auth/refresh.py"],
                resolution_confirmed=True,
                saved_at="2026-01-01T00:00:00",
                root_cause_pattern="stale_cache_reference",
                full_ticket_text="token expiry validation fails for users after midnight refresh",
            )
            entries.append(e)
        patterns = detect_patterns(entries)
        semantic = [p for p in patterns if p["pattern_type"] == "semantic_signal"]
        assert len(semantic) >= 1
        # Signal words should include 'token' and 'expiry' (or similar)
        ev = semantic[0]["evidence"]
        assert ev["top_fix_file"] == "src/auth/refresh.py"
        assert ev["top_fix_file_rate"] >= 0.5

    def test_semantic_not_detected_with_2_entries(self):
        from icx_engine.memory.patterns import detect_patterns
        from icx_engine.memory.schema import MemoryEntry
        import uuid
        entries = [
            MemoryEntry(
                id=str(uuid.uuid4()),
                issue_key=f"PROJ-{i}",
                project_key="TEST",
                source_type="jira",
                issue_type="Bug",
                summary="Token",
                problem_description="Desc",
                resolution_note="Fix",
                files_changed=["src/auth.py"],
                resolution_confirmed=True,
                saved_at="2026-01-01T00:00:00",
                root_cause_pattern="stale_cache_reference",
                full_ticket_text="token expiry issue",
            )
            for i in range(2)
        ]
        patterns = detect_patterns(entries)
        semantic = [p for p in patterns if p["pattern_type"] == "semantic_signal"]
        assert len(semantic) == 0
