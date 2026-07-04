"""Tests for lsp_client.py load-bearing, platform-sensitive helpers:
_kill_tree (psutil tree-kill + taskkill fallback) and uri_to_path framing.
"""
from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest

from icx_engine.graph.parser import lsp_client
from icx_engine.graph.parser.lsp_client import uri_to_path, _kill_tree


# ---------------------------------------------------------------------------
# uri_to_path - decode is driven by URI content, not the host OS, so these
# assertions hold on every platform.
# ---------------------------------------------------------------------------

def test_uri_to_path_windows_uri_with_encoded_space():
    # file:///C:/a%20b/x.java -> Windows drive path with decoded space
    assert uri_to_path("file:///C:/a%20b/x.java") == "C:\\a b\\x.java"


def test_uri_to_path_posix_uri_unchanged():
    assert uri_to_path("file:///home/user/x.py") == "/home/user/x.py"


def test_uri_to_path_decodes_percent_escapes():
    assert uri_to_path("file:///home/a%20b/c.py") == "/home/a b/c.py"


# ---------------------------------------------------------------------------
# _kill_tree - psutil path kills children then root; ImportError falls back to
# taskkill on win32, plain kill elsewhere.
# ---------------------------------------------------------------------------

def test_kill_tree_psutil_kills_children_and_root():
    proc = MagicMock()
    proc.pid = 4321
    child_a, child_b = MagicMock(), MagicMock()
    fake_psutil = MagicMock()
    root = MagicMock()
    root.children.return_value = [child_a, child_b]
    fake_psutil.Process.return_value = root
    fake_psutil.NoSuchProcess = Exception
    fake_psutil.AccessDenied = Exception

    with patch.dict(sys.modules, {"psutil": fake_psutil}):
        _kill_tree(proc)

    child_a.kill.assert_called_once()
    child_b.kill.assert_called_once()
    root.kill.assert_called_once()


def test_kill_tree_taskkill_fallback_on_win32_without_psutil():
    proc = MagicMock()
    proc.pid = 999

    # Force the `import psutil` inside _kill_tree to fail.
    real_import = __import__

    def _no_psutil(name, *args, **kwargs):
        if name == "psutil":
            raise ImportError("no psutil")
        return real_import(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=_no_psutil):
        with patch.object(lsp_client.sys, "platform", "win32"):
            with patch.object(lsp_client.subprocess, "run") as mock_run:
                _kill_tree(proc)

    mock_run.assert_called_once()
    args = mock_run.call_args[0][0]
    assert args[0] == "taskkill"
    assert "999" in args


def test_kill_tree_posix_fallback_calls_proc_kill():
    proc = MagicMock()
    proc.pid = 111

    real_import = __import__

    def _no_psutil(name, *args, **kwargs):
        if name == "psutil":
            raise ImportError("no psutil")
        return real_import(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=_no_psutil):
        with patch.object(lsp_client.sys, "platform", "linux"):
            _kill_tree(proc)

    proc.kill.assert_called_once()
