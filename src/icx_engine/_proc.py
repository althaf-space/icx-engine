"""Shared, cross-platform process-tree termination.

Canonical helper for new code. A bare ``kill()`` reaps only the direct child; long-running test
runners and language servers spawn trees (npx -> node -> Playwright -> chromium, mvn -> JVM ->
surefire fork, pytest-xdist workers, gradle daemons). Survivors orphan and hold RAM + file locks.

``kill_tree`` uses the strongest available mechanism:
  1. POSIX process GROUP kill (when the process was spawned with ``start_new_session=True`` so its
     pid is the group leader) - this reaps the whole session, including children that already
     reparented to init, which ``psutil.children`` can miss.
  2. ``psutil`` recursive child enumeration (cross-platform).
  3. ``taskkill /F /T`` on Windows / ``os.kill`` on POSIX when psutil is absent.
"""
from __future__ import annotations

import contextlib
import os
import shutil
import signal
import subprocess
import sys


def win_argv(command: list[str]) -> list[str]:
    """Make a command list runnable across OSes when invoked with shell=False.

    On Windows, ``subprocess``/``create_subprocess_exec`` cannot find or execute ``.cmd``/``.bat``
    shims (npm, npx, mvn, gradlew, playwright, ...): CreateProcess only auto-appends ``.exe`` and
    cannot run a batch file directly. This resolves the launcher via PATH and, when it is a
    ``.cmd``/``.bat``, runs it through ``cmd /c``. On POSIX (and for real ``.exe`` launchers) the
    command is returned unchanged.
    """
    if os.name != "nt" or not command:
        return command
    exe = command[0]
    resolved = shutil.which(exe) or exe
    if resolved.lower().endswith((".cmd", ".bat")):
        return ["cmd", "/c", resolved, *command[1:]]
    return [resolved, *command[1:]]

# SIGKILL does not exist on Windows; fall back to SIGTERM there (the Windows paths use taskkill
# anyway, so this constant is only reached on POSIX / when the platform is faked in tests).
_SIGKILL = getattr(signal, "SIGKILL", getattr(signal, "SIGTERM", 15))


def kill_tree(pid: int, *, process_group: bool = False) -> None:
    """Kill process ``pid`` and every descendant. Never raises.

    Set ``process_group=True`` only when ``pid`` was spawned with ``start_new_session=True`` (POSIX);
    the group kill then reaps the entire session in one call. On Windows or without a session the
    call falls through to psutil / taskkill.
    """
    if process_group and hasattr(os, "killpg"):
        try:
            os.killpg(os.getpgid(pid), _SIGKILL)
            return
        except (ProcessLookupError, PermissionError, OSError):
            pass  # fall through to psutil / os.kill

    try:
        import psutil
        try:
            root = psutil.Process(pid)
            children = root.children(recursive=True)
        except psutil.NoSuchProcess:
            return
        for child in children:
            with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
                child.kill()
        with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
            root.kill()
    except ImportError:
        if sys.platform == "win32":
            with contextlib.suppress(OSError):
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)
        else:
            with contextlib.suppress(OSError, ProcessLookupError):
                os.kill(pid, _SIGKILL)
