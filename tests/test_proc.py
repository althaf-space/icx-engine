"""Tests for the shared process-tree killer (icx_engine._proc.kill_tree)."""
from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from icx_engine._proc import kill_tree


def test_kill_tree_reaps_real_child_tree():
    """The whole tree (parent + grandchild) must die, not just the direct child."""
    psutil = pytest.importorskip("psutil")
    parent = subprocess.Popen([
        sys.executable, "-c",
        "import subprocess,sys,time;"
        "subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
        "time.sleep(60)",
    ])
    try:
        # Wait for the grandchild to appear.
        deadline = time.time() + 5
        kids = []
        while time.time() < deadline:
            try:
                kids = psutil.Process(parent.pid).children(recursive=True)
            except psutil.NoSuchProcess:
                break
            if kids:
                break
            time.sleep(0.1)
        assert kids, "grandchild never spawned"

        def _dead(k):
            try:
                return (not k.is_running()) or k.status() == psutil.STATUS_ZOMBIE
            except psutil.NoSuchProcess:
                return True

        kill_tree(parent.pid)

        deadline = time.time() + 5
        while time.time() < deadline and not all(_dead(k) for k in kids):
            time.sleep(0.1)
        assert all(_dead(k) for k in kids), "orphan survived tree kill"
    finally:
        try:
            parent.kill()
            parent.wait(timeout=5)
        except Exception:
            pass


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="POSIX process groups only")
def test_kill_tree_uses_process_group_when_requested(monkeypatch):
    seen = {}
    monkeypatch.setattr(os, "getpgid", lambda pid: 777)
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: seen.update(pgid=pgid, sig=sig))
    kill_tree(1234, process_group=True)
    assert seen["pgid"] == 777


def test_kill_tree_psutil_kills_children_and_root(monkeypatch):
    import types
    killed = {"children": 0, "root": 0}

    class _P:
        def __init__(self, _pid): pass
        def children(self, recursive=False):
            c = types.SimpleNamespace(kill=lambda: killed.__setitem__("children", killed["children"] + 1))
            return [c, c]
        def kill(self): killed["root"] += 1

    fake = types.SimpleNamespace(Process=_P, NoSuchProcess=Exception, AccessDenied=Exception)
    monkeypatch.setitem(sys.modules, "psutil", fake)
    kill_tree(4321, process_group=False)
    assert killed["children"] == 2 and killed["root"] == 1


def test_kill_tree_posix_fallback_uses_os_kill(monkeypatch):
    calls = {}
    real_import = __import__

    def _no_psutil(name, *a, **k):
        if name == "psutil":
            raise ImportError("no psutil")
        return real_import(name, *a, **k)

    monkeypatch.setattr("builtins.__import__", _no_psutil)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(os, "kill", lambda pid, sig: calls.update(pid=pid, sig=sig))
    kill_tree(111, process_group=False)
    assert calls["pid"] == 111


def test_kill_tree_win_fallback_uses_taskkill(monkeypatch):
    import icx_engine._proc as _proc
    calls = {}
    real_import = __import__

    def _no_psutil(name, *a, **k):
        if name == "psutil":
            raise ImportError("no psutil")
        return real_import(name, *a, **k)

    monkeypatch.setattr("builtins.__import__", _no_psutil)
    monkeypatch.setattr(_proc.sys, "platform", "win32")
    monkeypatch.setattr(_proc.subprocess, "run", lambda cmd, **k: calls.update(cmd=cmd))
    kill_tree(999, process_group=False)
    assert calls["cmd"][0] == "taskkill" and "999" in calls["cmd"]


# -- win_argv: cross-platform .cmd/.bat handling -----------------------------------

from icx_engine._proc import win_argv
import icx_engine._proc as _proc


def test_win_argv_posix_passthrough(monkeypatch):
    monkeypatch.setattr(_proc.os, "name", "posix")
    assert win_argv(["npm", "install"]) == ["npm", "install"]


def test_win_argv_empty():
    assert win_argv([]) == []


def test_win_argv_wraps_cmd_on_windows(monkeypatch):
    monkeypatch.setattr(_proc.os, "name", "nt")
    monkeypatch.setattr(_proc.shutil, "which", lambda e: r"C:\node\npm.cmd")
    out = win_argv(["npm", "install", "x"])
    assert out[0] == "cmd" and out[1] == "/c"
    assert out[2].lower().endswith("npm.cmd")
    assert out[3:] == ["install", "x"]


def test_win_argv_exe_passthrough_on_windows(monkeypatch):
    monkeypatch.setattr(_proc.os, "name", "nt")
    monkeypatch.setattr(_proc.shutil, "which", lambda e: r"C:\node\node.exe")
    out = win_argv(["node", "x.mjs"])
    assert out[0].lower().endswith("node.exe") and out[1] == "x.mjs"
    assert "cmd" not in out[0].lower()


def test_win_argv_runs_real_npm_cross_os():
    # Real proof: on Windows npm is npm.cmd (unrunnable via bare subprocess); win_argv must make it
    # work. On POSIX npm is a normal exe. Skips only when node/npm is genuinely absent.
    import shutil
    if not shutil.which("npm"):
        pytest.skip("npm not installed on this runner")
    r = subprocess.run(win_argv(["npm", "--version"]), capture_output=True, text=True, timeout=90)
    assert r.returncode == 0
    assert r.stdout.strip()          # prints a version -> the .cmd launcher actually ran
