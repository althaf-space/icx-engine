from icx_engine.exceptions import ICXError, MemoryError


def test_memory_error_is_ice_error():
    err = MemoryError("storage path not writable")
    assert isinstance(err, ICXError)
    assert str(err) == "storage path not writable"


def test_memory_error_guidance_exists():
    from icx_engine.error_display import _GUIDANCE
    assert MemoryError in _GUIDANCE
    why, how = _GUIDANCE[MemoryError]
    assert "memory" in why.lower() or "storage" in why.lower()
    assert "icx memory status" in how


from icx_engine.models.output import IssueContext, PastInsight


def test_past_insight_fields():
    insight = PastInsight(
        issue_key="PROJ-87",
        source_type="jira",
        summary="Login timeout after OAuth refresh",
        resolution_note="Updated TTL from 1h to 24h.",
        files_changed=["src/auth/token.py"],
        similarity_score=0.91,
        saved_at="2026-03-12T09:14:22Z",
    )
    assert insight.issue_key == "PROJ-87"
    assert insight.similarity_score == 0.91
    assert insight.files_changed == ["src/auth/token.py"]


def test_issue_context_past_insights_defaults_empty():
    ctx = IssueContext(
        problem_summary="x",
        detailed_description="x",
        reproduction_steps=[],
        expected_behavior=None,
        actual_behavior=None,
        acceptance_criteria=[],
        impact="x",
        priority="High",
        issue_type="Bug",
        confidence_score=1.0,
        completeness_score=1.0,
        missing_information=[],
    )
    assert ctx.past_insights == []


def test_issue_context_accepts_past_insights():
    insight = PastInsight(
        issue_key="PROJ-1",
        source_type="jira",
        summary="s",
        resolution_note="r",
        files_changed=[],
        similarity_score=0.8,
        saved_at="2026-01-01T00:00:00Z",
    )
    ctx = IssueContext(
        problem_summary="x",
        detailed_description="x",
        reproduction_steps=[],
        expected_behavior=None,
        actual_behavior=None,
        acceptance_criteria=[],
        impact="x",
        priority="High",
        issue_type="Bug",
        confidence_score=1.0,
        completeness_score=1.0,
        missing_information=[],
        past_insights=[insight],
    )
    assert len(ctx.past_insights) == 1
    assert ctx.past_insights[0].issue_key == "PROJ-1"


from icx_engine.memory.schema import MemoryEntry, MemoryQueryInput


def test_memory_entry_all_fields():
    entry = MemoryEntry(
        id="abc-123",
        issue_key="PROJ-100",
        project_key="PROJ",
        source_type="jira",
        issue_type="Bug",
        summary="Auth fails",
        problem_description="JWT expired",
        impact="All users",
        resolution_note="Updated TTL",
        files_changed=["src/auth/token.py"],
        resolution_confirmed=True,
        saved_at="2026-05-12T10:00:00Z",
        tags=["jwt", "auth"],
    )
    assert entry.issue_key == "PROJ-100"
    assert entry.resolution_confirmed is True
    assert entry.tags == ["jwt", "auth"]


def test_memory_entry_optional_fields_default():
    entry = MemoryEntry(
        id="abc-123",
        issue_key="PROJ-100",
        project_key="PROJ",
        source_type="jira",
        issue_type="Bug",
        summary="Auth fails",
        problem_description="JWT expired",
        resolution_note="Fixed",
        files_changed=[],
        resolution_confirmed=True,
        saved_at="2026-05-12T10:00:00Z",
    )
    assert entry.impact == ""
    assert entry.tags == []


def test_memory_query_input():
    q = MemoryQueryInput(
        issue_key="PROJ-200",
        project_key="PROJ",
        source_type="jira",
        summary="Login broken",
        description="Users cannot log in",
        issue_type="Bug",
    )
    assert q.issue_key == "PROJ-200"
    assert q.source_type == "jira"
