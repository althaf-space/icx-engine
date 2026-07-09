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


# ---------------------------------------------------------------------------
# definition_batch - pipelined queries. Order-preserving (matched by id) so the
# resulting edge set is identical to a serial loop; the circuit breaker still
# abandons the tail on repeated timeouts.
# ---------------------------------------------------------------------------

_NO_REPLY = object()


def _client_instant(reply_for):
    from pathlib import Path
    from icx_engine.graph.parser.lsp_client import LSPClient
    c = LSPClient(["x"], Path("a").resolve(), timeout=0.05)

    def fake_send(msg):
        mid = msg.get("id")
        if mid is None:
            return
        res = reply_for(msg.get("params", {}))
        if res is _NO_REPLY:
            return
        q = c._pending.get(mid)
        if q is not None:
            q.put({"jsonrpc": "2.0", "id": mid, "result": res})

    c._send = fake_send
    return c


def test_definition_batch_preserves_order():
    from pathlib import Path
    def reply(params):
        line = params["position"]["line"]
        return [{"uri": "file:///p/T.java", "range": {"start": {"line": 100 + line, "character": 0}}}]
    c = _client_instant(reply)
    reqs = [(Path("a.java").resolve(), i, 0) for i in range(7)]
    out = c.definition_batch(reqs, window=3)
    assert len(out) == 7
    for i, locs in enumerate(out):
        assert len(locs) == 1 and locs[0].line == 100 + i   # aligned to request i


def test_definition_batch_matches_serial_result_set():
    from pathlib import Path
    def reply(params):
        line = params["position"]["line"]
        if line % 2 == 0:  # only even lines resolve
            return [{"uri": "file:///p/T.java", "range": {"start": {"line": line, "character": 0}}}]
        return None
    reqs = [(Path("a.java").resolve(), i, 0) for i in range(10)]
    c1 = _client_instant(reply)
    serial = [c1.definition(p, l, ch) for (p, l, ch) in reqs]
    c2 = _client_instant(reply)
    batched = c2.definition_batch(reqs, window=4)
    ser_set = {(i, tuple((x.path, x.line) for x in serial[i])) for i in range(10)}
    bat_set = {(i, tuple((x.path, x.line) for x in batched[i])) for i in range(10)}
    assert ser_set == bat_set   # identical result set -> zero edge loss


def test_definition_batch_circuit_breaker_abandons_tail():
    from pathlib import Path
    c = _client_instant(lambda p: _NO_REPLY)   # never replies
    reqs = [(Path("a.java").resolve(), i, 0) for i in range(20)]
    out = c.definition_batch(reqs, window=2, abort_after_consecutive_timeouts=2)
    assert len(out) == 20
    assert all(x == [] for x in out)
    assert c.consecutive_timeouts >= 2
