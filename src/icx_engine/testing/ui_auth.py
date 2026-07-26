"""Run the packaged Playwright session-capture harness and persist the captured session.

Two modes, both fully async (never block the event loop):
  - capture: open a HEADED browser; the user logs in by hand; ICX saves the storageState. No
    credentials ever pass through an agent chat.
  - inline:  the app credentials are passed straight to the harness process (not the chat); it drives
    the login form and saves the storageState.

The captured storageState (cookies + localStorage) is stored per (project, host) and later loaded by
the replay harness so UI tests run already authenticated.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

from icx_engine.testing import auth as _auth


def _harness_env() -> tuple[str, dict]:
    """(node executable, env) for the auth harness - the modern harness node + the ICX-installed
    Playwright + its Chromium (all under ~/.icx/testing, never global)."""
    from icx_engine.runtime_manager import resolve_harness_node
    from icx_engine.testing.runners.install import installed_path, browsers_dir

    node = resolve_harness_node() or "node"
    env = {**os.environ}
    pw = installed_path("playwright")
    if pw:
        env["NODE_PATH"] = str(Path(pw) / "node_modules")
        env["PLAYWRIGHT_BROWSERS_PATH"] = str(browsers_dir(Path(pw)))
    return node, env


async def _run_harness(mode: str, url: str, out: str, extra: list[str], timeout: float) -> bool:
    """Launch the auth harness as an async subprocess. Returns (ok, detail) - detail carries the
    harness's own error output on failure so the real reason is not swallowed. Guarded - never
    raises (except a genuine cancellation)."""
    import logging
    from icx_engine.testing.runners.install import auth_harness_path, runtime_harness_path
    from icx_engine._proc import win_argv, kill_tree

    _log = logging.getLogger("icx.testing.ui_auth")
    node, env = _harness_env()
    # Run the harness FROM the install dir (next to node_modules) so ESM `import "playwright"`
    # resolves - NODE_PATH is ignored for ESM imports.
    harness = runtime_harness_path("icx-auth.mjs", auth_harness_path())
    cmd = [node, harness, "--mode", mode, "--url", url, "--out", out,
           "--timeout", str(int(timeout))] + list(extra)
    try:
        proc = await asyncio.create_subprocess_exec(
            *win_argv(cmd), env=env,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
    except (OSError, ValueError) as exc:
        return False, f"could not launch the browser harness (node/playwright): {exc}"
    stderr_b = b""
    try:
        # Manual login (capture) can take a while; give the process the harness timeout + slack.
        _, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout + 30)
    except asyncio.TimeoutError:
        kill_tree(proc.pid, process_group=True)
        return False, f"timed out after {int(timeout)}s (login not completed)"
    except asyncio.CancelledError:
        kill_tree(proc.pid, process_group=True)
        raise
    detail = (stderr_b.decode("utf-8", "replace") if stderr_b else "").strip()[-800:]
    ok = proc.returncode == 0 and Path(out).exists()
    if not ok:
        _log.warning("auth harness (%s) failed rc=%s: %s", mode, proc.returncode, detail)
    return ok, detail


async def capture_session(project: str, host: str, url: str,
                          success_url: str = "", timeout: float = 300.0) -> tuple[str | None, str]:
    """Open a headed browser for manual login; on success save + register the session. Returns
    (storageState path | None, detail) - detail is the failure reason when the path is None."""
    out = str(_auth.session_state_path(project, host))
    extra = ["--success-url", success_url] if success_url else []
    ok, detail = await _run_harness("capture", url, out, extra, timeout)
    if not ok:
        return None, detail or "capture did not complete"
    _auth.save_session(project, host, out, storage_state=out)
    return out, ""


async def inline_session(project: str, host: str, url: str, username: str, password: str,
                         success_url: str = "", user_selector: str = "", pass_selector: str = "",
                         submit_selector: str = "", timeout: float = 120.0) -> tuple[str | None, str]:
    """Drive the login form with the app credentials (passed to the process, never via chat), then
    save + register the session. Returns (storageState path | None, detail)."""
    out = str(_auth.session_state_path(project, host))
    extra = ["--user", username, "--pass", password]
    if success_url:
        extra += ["--success-url", success_url]
    if user_selector:
        extra += ["--user-selector", user_selector]
    if pass_selector:
        extra += ["--pass-selector", pass_selector]
    if submit_selector:
        extra += ["--submit-selector", submit_selector]
    ok, detail = await _run_harness("inline", url, out, extra, timeout)
    if not ok:
        return None, detail or "inline login did not complete"
    _auth.save_session(project, host, out, storage_state=out)
    return out, ""
