"""Tests for the _paginate helper duplicated identically across git/gitlab/jira/workstatus/
memory mcp_tools.py modules (each module owns its own copy, matching the existing _ok/_err
per-module pattern) - one battery of unit tests per copy, since they're independent functions
even though byte-identical. Dispatch-level integration tests for individual paginated tools
live alongside each module's existing MCP tool tests (tests/git/test_mcp_tools.py,
tests/test_mcp.py for memory, etc.), not here."""
from __future__ import annotations
import pytest


PAGINATE_SOURCES = [
    ("icx_engine.git.mcp_tools", "_paginate"),
    ("icx_engine.gitlab.mcp_tools", "_paginate"),
    ("icx_engine.jira.mcp_tools", "_paginate"),
    ("icx_engine.workstatus.mcp_tools", "_paginate"),
    ("icx_engine.memory.mcp_tools", "_paginate"),
]


def _import_paginate(module_path: str, attr: str):
    import importlib
    return getattr(importlib.import_module(module_path), attr)


@pytest.mark.parametrize("module_path,attr", PAGINATE_SOURCES)
def test_paginate_no_limit_returns_items_unchanged_no_extra_fields(module_path, attr):
    paginate = _import_paginate(module_path, attr)
    items = [1, 2, 3, 4, 5]
    result, extra = paginate(items, None, None)
    assert result == items
    assert result is items  # not even copied - true legacy pass-through
    assert extra == {}


@pytest.mark.parametrize("module_path,attr", PAGINATE_SOURCES)
def test_paginate_no_limit_ignores_offset(module_path, attr):
    """offset alone (no limit) must not change behavior - only limit activates pagination."""
    paginate = _import_paginate(module_path, attr)
    items = [1, 2, 3]
    result, extra = paginate(items, None, 1)
    assert result == items
    assert extra == {}


@pytest.mark.parametrize("module_path,attr", PAGINATE_SOURCES)
def test_paginate_with_limit_slices_and_reports_has_more(module_path, attr):
    paginate = _import_paginate(module_path, attr)
    items = list(range(10))
    result, extra = paginate(items, 3, 0)
    assert result == [0, 1, 2]
    assert extra == {"total": 10, "has_more": True, "next_offset": 3}


@pytest.mark.parametrize("module_path,attr", PAGINATE_SOURCES)
def test_paginate_with_limit_and_offset(module_path, attr):
    paginate = _import_paginate(module_path, attr)
    items = list(range(10))
    result, extra = paginate(items, 3, 6)
    assert result == [6, 7, 8]
    assert extra == {"total": 10, "has_more": True, "next_offset": 9}


@pytest.mark.parametrize("module_path,attr", PAGINATE_SOURCES)
def test_paginate_last_page_has_more_false_next_offset_none(module_path, attr):
    paginate = _import_paginate(module_path, attr)
    items = list(range(10))
    result, extra = paginate(items, 5, 8)
    assert result == [8, 9]
    assert extra == {"total": 10, "has_more": False, "next_offset": None}


@pytest.mark.parametrize("module_path,attr", PAGINATE_SOURCES)
def test_paginate_limit_larger_than_remaining_items(module_path, attr):
    paginate = _import_paginate(module_path, attr)
    items = list(range(3))
    result, extra = paginate(items, 100, 0)
    assert result == [0, 1, 2]
    assert extra == {"total": 3, "has_more": False, "next_offset": None}


@pytest.mark.parametrize("module_path,attr", PAGINATE_SOURCES)
def test_paginate_offset_none_defaults_to_zero_when_limit_given(module_path, attr):
    paginate = _import_paginate(module_path, attr)
    items = list(range(5))
    result, extra = paginate(items, 2, None)
    assert result == [0, 1]
    assert extra["next_offset"] == 2


@pytest.mark.parametrize("module_path,attr", PAGINATE_SOURCES)
def test_paginate_empty_items(module_path, attr):
    paginate = _import_paginate(module_path, attr)
    result, extra = paginate([], 5, 0)
    assert result == []
    assert extra == {"total": 0, "has_more": False, "next_offset": None}
