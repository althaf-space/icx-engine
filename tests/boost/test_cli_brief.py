"""`icx boost brief` CLI - headless brief for editor hooks."""
from __future__ import annotations

import json

from typer.testing import CliRunner

from icx_engine.cli import app

_r = CliRunner()


def test_brief_format_outputs_boosted_prompt():
    res = _r.invoke(app, ["boost", "brief", "fix the crash", "--format", "brief"])
    assert res.exit_code == 0
    # the boosted prompt leads with the task and includes the completeness directive
    assert "fix the crash" in res.output
    assert "do NOT skip" in res.output


def test_hook_format_is_valid_userpromptsubmit_json():
    res = _r.invoke(app, ["boost", "brief", "what is a closure?", "--format", "hook"])
    assert res.exit_code == 0
    data = json.loads(res.output)
    assert data["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert data["hookSpecificOutput"]["additionalContext"].strip()


def test_json_format_is_full_brief():
    res = _r.invoke(app, ["boost", "brief", "add a form", "--format", "json"])
    assert res.exit_code == 0
    data = json.loads(res.output)
    assert data["archetype"] and "boosted_prompt" in data and "mandatory_directive" in data
