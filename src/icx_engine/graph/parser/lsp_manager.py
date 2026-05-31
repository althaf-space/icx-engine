"""LSP server lifecycle manager.

Mirrors tsserver.py pattern for any language server:
  detect runtime -> compare stored version -> (if changed) kill PIDs + rmtree + reinstall
  -> write version file -> return start command

All installs go under ~/.icx/<server-name>/.
Never installs the runtime itself (Node/Python/Java). Only installs the language server
package using whatever runtime the user already has.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

_log = logging.getLogger(__name__)

ICX_HOME = Path.home() / ".icx"

_TERMINATE_GRACE_SECONDS = 3
_RMTREE_RETRY_ATTEMPTS = 5
_VERSION_TIMEOUT_SECONDS = 5
_INSTALL_TIMEOUT_SECONDS = 300


@dataclass
class LSPServerConfig:
    """All language-specific knowledge for one LSP server."""
    name: str
    get_runtime: Callable[[], tuple[str, str] | None]
    install_dir: Path
    binary_fn: Callable[[Path], Path | None]
    install_fn: Callable[[str, Path], bool]
    start_fn: Callable[[Path, str], list[str]]


def ensure_server(config: LSPServerConfig) -> list[str] | None:
    """Return start-command list for config's server, installing/reinstalling as needed.

    Returns None when runtime is absent, install fails, or binary can't be found.
    """
    runtime_info = config.get_runtime()
    if runtime_info is None:
        _log.debug("%s: runtime not found", config.name)
        return None

    runtime_path, version = runtime_info
    config.install_dir.mkdir(parents=True, exist_ok=True)

    stored = _read_stored_version(config.install_dir)
    binary = config.binary_fn(config.install_dir)

    if binary is not None and stored == version:
        return config.start_fn(binary, runtime_path)

    if stored is not None and stored != version:
        _log.debug(
            "%s: runtime version changed (%s -> %s), reinstalling",
            config.name, stored, version,
        )
        kill_tracked(config.install_dir)
        _force_rmtree(config.install_dir)
        config.install_dir.mkdir(parents=True, exist_ok=True)

    _log.debug("%s: installing under %s %s ...", config.name, config.name, version)
    ok = config.install_fn(runtime_path, config.install_dir)
    if not ok:
        _log.debug("%s: install failed", config.name)
        return None

    _write_version(config.install_dir, version)
    binary = config.binary_fn(config.install_dir)
    if binary is None:
        _log.debug("%s: binary not found after install", config.name)
        return None

    return config.start_fn(binary, runtime_path)


def record_pid(install_dir: Path, pid: int) -> None:
    """Call after spawning the LSP server process. Enables clean kill on version drift."""
    pid_file = install_dir / "pids.json"
    entries: list[dict] = []
    if pid_file.exists():
        try:
            entries = json.loads(pid_file.read_text(encoding="utf-8"))
            if not isinstance(entries, list):
                entries = []
        except (OSError, json.JSONDecodeError):
            entries = []
    entries.append({"pid": int(pid), "started_at": time.time()})
    try:
        pid_file.write_text(json.dumps(entries), encoding="utf-8")
    except OSError:
        pass


def kill_tracked(install_dir: Path) -> int:
    """Kill all PIDs previously recorded for this install dir."""
    pid_file = install_dir / "pids.json"
    if not pid_file.exists():
        return 0
    try:
        entries = json.loads(pid_file.read_text(encoding="utf-8"))
        if not isinstance(entries, list):
            entries = []
    except (OSError, json.JSONDecodeError):
        _unlink_quiet(pid_file)
        return 0

    killed = 0
    try:
        import psutil
        terminated = []
        for entry in entries:
            pid = entry.get("pid")
            if not pid:
                continue
            try:
                proc = psutil.Process(int(pid))
                proc.terminate()
                terminated.append(proc)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        if terminated:
            _, alive = psutil.wait_procs(terminated, timeout=_TERMINATE_GRACE_SECONDS)
            for proc in alive:
                try:
                    proc.kill()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
        killed = len(terminated)
    except ImportError:
        killed = _kill_native(entries)

    _unlink_quiet(pid_file)
    return killed


def _kill_native(entries: list) -> int:
    killed = 0
    for entry in entries:
        pid = entry.get("pid")
        if not pid:
            continue
        try:
            if sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(int(pid))],
                    capture_output=True, check=False, timeout=10,
                )
                killed += 1
            else:
                import signal as _signal
                os.kill(int(pid), _signal.SIGTERM)
                killed += 1
        except (OSError, subprocess.TimeoutExpired):
            continue
    return killed


def _read_stored_version(install_dir: Path) -> str | None:
    vf = install_dir / "runtime-version.json"
    try:
        data = json.loads(vf.read_text(encoding="utf-8"))
        val = data.get("full")
        return val if isinstance(val, str) else None
    except (OSError, json.JSONDecodeError):
        return None


def _write_version(install_dir: Path, version: str) -> None:
    vf = install_dir / "runtime-version.json"
    try:
        vf.write_text(json.dumps({
            "full": version,
            "installed_at": datetime.now(timezone.utc).isoformat(),
        }), encoding="utf-8")
    except OSError:
        pass


def _force_rmtree(path: Path) -> None:
    if not path.exists():
        return

    def _on_error(func, target, _exc_info):
        try:
            os.chmod(target, stat.S_IRWXU | stat.S_IRWXG | stat.S_IRWXO)
            func(target)
        except OSError:
            pass

    for attempt in range(_RMTREE_RETRY_ATTEMPTS):
        try:
            shutil.rmtree(path, onerror=_on_error)
            if not path.exists():
                return
        except OSError:
            pass
        time.sleep(0.3 * (attempt + 1))


def _unlink_quiet(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


# ── Runtime detectors ─────────────────────────────────────────────────────────

def node_runtime() -> tuple[str, str] | None:
    """Detect Node and return (node_path, version_string)."""
    node = shutil.which("node")
    if not node:
        return None
    try:
        result = subprocess.run(
            [node, "--version"],
            capture_output=True, text=True, timeout=_VERSION_TIMEOUT_SECONDS,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    version = (result.stdout or "").strip()
    return (node, version) if version.startswith("v") else None


def python_runtime() -> tuple[str, str] | None:
    """Return (sys.executable, major.minor.micro) for the running Python."""
    path = sys.executable
    if not path:
        return None
    v = sys.version_info
    version = f"{v.major}.{v.minor}.{v.micro}"
    return (path, version)


def java_runtime() -> tuple[str, str] | None:
    """Detect Java and return (java_path, version_string)."""
    java = shutil.which("java")
    if not java:
        return None
    try:
        result = subprocess.run(
            [java, "-version"],
            capture_output=True, text=True, timeout=_VERSION_TIMEOUT_SECONDS,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    out = (result.stderr or result.stdout or "").strip().splitlines()
    return (java, out[0]) if out else None


# ── Pre-built configs ─────────────────────────────────────────────────────────

def _ts_ls_binary(install_dir: Path) -> Path | None:
    pkg = install_dir / "node_modules" / "typescript-language-server"
    for rel in ("lib/cli.mjs", "cli.mjs", "lib/cli.js"):
        p = pkg / rel
        if p.exists():
            return p
    return None


def _ts_ls_install(runtime_path: str, install_dir: Path) -> bool:
    npm = shutil.which("npm")
    if not npm:
        _log.debug("ts-ls: npm not found")
        return False
    result = subprocess.run(
        [npm, "install", "typescript", "typescript-language-server",
         "--prefix", str(install_dir),
         "--silent", "--no-audit", "--no-fund"],
        capture_output=True, text=True, timeout=_INSTALL_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        _log.debug("ts-ls: npm install failed: %s", result.stderr[-300:])
    return result.returncode == 0


def _ts_ls_start(binary: Path, runtime_path: str) -> list[str]:
    return [runtime_path, str(binary), "--stdio"]


TS_LS = LSPServerConfig(
    name="ts-ls",
    get_runtime=node_runtime,
    install_dir=ICX_HOME / "ts-ls",
    binary_fn=_ts_ls_binary,
    install_fn=_ts_ls_install,
    start_fn=_ts_ls_start,
)


def _pyright_venv(install_dir: Path) -> Path:
    return install_dir / "venv"


def _pyright_binary(install_dir: Path) -> Path | None:
    venv = _pyright_venv(install_dir)
    for candidate in (
        venv / "Scripts" / "pyright-langserver.exe",
        venv / "Scripts" / "pyright-langserver",
        venv / "bin" / "pyright-langserver",
    ):
        if candidate.exists():
            return candidate
    return None


def _pyright_install(runtime_path: str, install_dir: Path) -> bool:
    venv = _pyright_venv(install_dir)
    result = subprocess.run(
        [runtime_path, "-m", "venv", str(venv)],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        _log.debug("pyright: venv creation failed: %s", result.stderr[-200:])
        return False

    pip: str | None = None
    for candidate in (
        venv / "Scripts" / "pip.exe",
        venv / "Scripts" / "pip",
        venv / "bin" / "pip",
    ):
        if candidate.exists():
            pip = str(candidate)
            break
    if not pip:
        _log.debug("pyright: pip not found in venv")
        return False

    result = subprocess.run(
        [pip, "install", "pyright", "-q"],
        capture_output=True, text=True, timeout=_INSTALL_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        _log.debug("pyright: pip install failed: %s", result.stderr[-200:])
    return result.returncode == 0


def _pyright_start(binary: Path, runtime_path: str) -> list[str]:
    return [str(binary), "--stdio"]


PYRIGHT = LSPServerConfig(
    name="pyright",
    get_runtime=python_runtime,
    install_dir=ICX_HOME / "pyright",
    binary_fn=_pyright_binary,
    install_fn=_pyright_install,
    start_fn=_pyright_start,
)
