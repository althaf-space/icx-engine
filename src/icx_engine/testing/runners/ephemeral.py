"""Ephemeral repro runner - verify a fix when the repo has NO test framework.

Writes a throwaway script under ~/.icx temp (0o700, sanitized name), runs it via the resolved
runtime, returns (passed, output), then deletes the script. The repo is never mutated. Exit code 0
== passed. Used for the DoD "reproduce -> confirm resolved" flow on pure-logic changes.
"""
from __future__ import annotations

import subprocess
import uuid
from pathlib import Path

# python/node run a script file directly; extension + interpreter per language.
_EPHEMERAL = {
    "python": ("python", ".py"),
    "js-ts": ("node", ".mjs"),
    "javascript": ("node", ".mjs"),
    "node": ("node", ".mjs"),
}


def _temp_dir() -> Path:
    try:
        from icx_engine.graph.storage import temp_root
        d = temp_root() / "ephemeral"
    except Exception:
        d = Path.home() / ".icx" / "temp" / "ephemeral"
    d.mkdir(parents=True, exist_ok=True)
    return d


def run_ephemeral_repro(lang: str, code: str, runtime_path: str | None = None,
                        timeout: float = 30.0) -> tuple[bool, str]:
    """Run a throwaway repro script and return (passed, combined_output).

    passed == the script exited 0. The script should assert the expected behavior so that it fails
    before the fix and passes after. Any error returns (False, message). Temp file always cleaned up.
    """
    interp_ext = _EPHEMERAL.get(lang.lower())
    if interp_ext is None:
        return False, f"ephemeral repro not supported for language '{lang}'"
    default_interp, ext = interp_ext
    interp = runtime_path or default_interp

    d = _temp_dir()
    script = d / f"repro_{uuid.uuid4().hex}{ext}"
    try:
        script.write_text(code, encoding="utf-8")
    except OSError as exc:
        return False, f"could not write repro: {exc}"

    try:
        proc = subprocess.run(
            [interp, str(script)], capture_output=True, text=True, timeout=timeout,
        )
        output = (proc.stdout or "") + (proc.stderr or "")
        return proc.returncode == 0, output.strip()
    except subprocess.TimeoutExpired:
        return False, f"repro timed out after {timeout}s"
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"could not run repro: {exc}"
    finally:
        try:
            script.unlink()
        except OSError:
            pass
