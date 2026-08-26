from __future__ import annotations
import json
import sys
from pathlib import Path

import pytest

from icx_engine.telemetry.logger import ToolCallLogger, estimate_tokens


def test_estimate_tokens_rough_length_over_four():
    assert estimate_tokens("abcdefgh") == 2


def test_estimate_tokens_empty_string_is_zero():
    assert estimate_tokens("") == 0


def test_estimate_tokens_short_string_is_at_least_one():
    assert estimate_tokens("ab") == 1


def test_log_call_writes_one_jsonl_line(tmp_path):
    logger = ToolCallLogger(root=tmp_path)
    logger.log_call("git_repo_status", '{"repo_path": "x"}', '{"ok": true}', 12.5, ok=True, error_type=None)

    files = list(tmp_path.rglob("tool_calls.jsonl"))
    assert len(files) == 1
    lines = files[0].read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["tool"] == "git_repo_status"
    assert record["ok"] is True
    assert record["error_type"] is None
    assert record["duration_ms"] == 12.5
    assert record["input_tokens_est"] > 0
    assert record["output_tokens_est"] > 0
    assert "ts" in record


def test_log_call_records_failure_without_raising(tmp_path):
    logger = ToolCallLogger(root=tmp_path)
    logger.log_call("git_push", "{}", None, 5.0, ok=False, error_type="GitWorkflowError")

    files = list(tmp_path.rglob("tool_calls.jsonl"))
    record = json.loads(files[0].read_text(encoding="utf-8").strip())
    assert record["ok"] is False
    assert record["error_type"] == "GitWorkflowError"
    assert record["output_bytes"] == 0


def test_log_call_two_calls_same_day_append_to_same_file(tmp_path):
    logger = ToolCallLogger(root=tmp_path)
    logger.log_call("a", "{}", "{}", 1.0, ok=True, error_type=None)
    logger.log_call("b", "{}", "{}", 1.0, ok=True, error_type=None)

    files = list(tmp_path.rglob("tool_calls.jsonl"))
    assert len(files) == 1
    lines = files[0].read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only permission bits")
def test_log_call_creates_day_dir_with_0700(tmp_path):
    logger = ToolCallLogger(root=tmp_path)
    logger.log_call("a", "{}", "{}", 1.0, ok=True, error_type=None)

    day_dirs = [p for p in tmp_path.iterdir() if p.is_dir()]
    assert len(day_dirs) == 1
    assert oct(day_dirs[0].stat().st_mode)[-3:] == "700"


def test_log_call_never_raises_when_root_is_unwritable(monkeypatch, tmp_path):
    logger = ToolCallLogger(root=tmp_path)

    def _boom(*args, **kwargs):
        raise OSError("disk full")
    monkeypatch.setattr(Path, "mkdir", _boom)

    logger.log_call("a", "{}", "{}", 1.0, ok=True, error_type=None)  # must not raise
