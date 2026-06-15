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
import platform
import re
import shutil
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

_log = logging.getLogger(__name__)

ICX_HOME = Path.home() / ".icx"

_TERMINATE_GRACE_SECONDS = 3
_VERSION_TIMEOUT_SECONDS = 5
_INSTALL_TIMEOUT_SECONDS = 300


def _safe_extract_tar(tf, install_dir: Path) -> None:
    """Extract a tar archive, rejecting members that would land outside install_dir."""
    dest_root = install_dir.resolve()
    safe_members = []
    for member in tf.getmembers():
        member_path = (install_dir / member.name).resolve()
        if member_path != dest_root and dest_root not in member_path.parents:
            raise ValueError(f"Illegal tar archive entry: {member.name!r}")
        safe_members.append(member)
    tf.extractall(install_dir, members=safe_members)


def _safe_extract_zip(zf, install_dir: Path) -> None:
    """Extract a zip archive, rejecting members that would land outside install_dir."""
    dest_root = install_dir.resolve()
    safe_members = []
    for member in zf.namelist():
        member_path = (install_dir / member).resolve()
        if member_path != dest_root and dest_root not in member_path.parents:
            raise ValueError(f"Illegal zip archive entry: {member!r}")
        safe_members.append(member)
    zf.extractall(install_dir, members=safe_members)


@dataclass
class LSPServerConfig:
    """All language-specific knowledge for one LSP server."""
    name: str
    get_runtime: Callable[[], tuple[str, str] | None]
    install_dir: Path
    binary_fn: Callable[[Path], Path | None]
    install_fn: Callable[[str, Path], bool]
    start_fn: Callable[[Path, str], list[str]]


def _sanitize_version(version: str) -> str:
    """Turn a runtime version string into a safe directory name."""
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", version.strip())
    return safe[:80] or "unknown"


def ensure_server(config: LSPServerConfig) -> list[str] | None:
    """Return start-command list for config's server, installing if needed.

    Each detected runtime version is installed into its own subdirectory
    under config.install_dir, so switching between projects on different
    runtime versions reuses the already-installed server for that version
    instead of reinstalling.

    Returns None when runtime is absent, install fails, or binary can't be found.
    """
    runtime_info = config.get_runtime()
    if runtime_info is None:
        _log.debug("%s: runtime not found", config.name)
        return None

    runtime_path, version = runtime_info
    versioned_dir = config.install_dir / _sanitize_version(version)
    if not versioned_dir.exists():
        versioned_dir.mkdir(parents=True, exist_ok=True)
        if sys.platform != "win32":
            try:
                versioned_dir.chmod(stat.S_IRWXU)
            except OSError:
                pass

    binary = config.binary_fn(versioned_dir)
    if binary is not None:
        return config.start_fn(binary, runtime_path)

    _log.debug("%s: installing %s under %s ...", config.name, version, versioned_dir)
    ok = config.install_fn(runtime_path, versioned_dir)
    if not ok:
        _log.debug("%s: install failed", config.name)
        return None

    binary = config.binary_fn(versioned_dir)
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


def go_runtime() -> tuple[str, str] | None:
    """Detect Go and return (go_path, version_string)."""
    go = shutil.which("go")
    if not go:
        return None
    try:
        result = subprocess.run(
            [go, "version"],
            capture_output=True, text=True, timeout=_VERSION_TIMEOUT_SECONDS,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    version = (result.stdout or "").strip()
    return (go, version) if version.startswith("go version") else None


def cpp_runtime() -> tuple[str, str] | None:
    """Detect a C++ compiler and return (compiler_path, version_string)."""
    for exe in ("clang++", "g++", "clang", "gcc"):
        compiler = shutil.which(exe)
        if not compiler:
            continue
        try:
            result = subprocess.run(
                [compiler, "--version"],
                capture_output=True, text=True, timeout=_VERSION_TIMEOUT_SECONDS,
            )
        except (subprocess.TimeoutExpired, OSError):
            continue
        if result.returncode != 0:
            continue
        lines = (result.stdout or "").strip().splitlines()
        if lines:
            return (compiler, lines[0])
    return None


def rust_runtime() -> tuple[str, str] | None:
    """Detect Rust and return (rustc_path, version_string)."""
    rustc = shutil.which("rustc")
    if not rustc:
        return None
    try:
        result = subprocess.run(
            [rustc, "--version"],
            capture_output=True, text=True, timeout=_VERSION_TIMEOUT_SECONDS,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    version = (result.stdout or "").strip()
    return (rustc, version) if version.startswith("rustc") else None


def dotnet_runtime() -> tuple[str, str] | None:
    """Detect .NET SDK and return (dotnet_path, version_string)."""
    dotnet = shutil.which("dotnet")
    if not dotnet:
        return None
    try:
        result = subprocess.run(
            [dotnet, "--version"],
            capture_output=True, text=True, timeout=_VERSION_TIMEOUT_SECONDS,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    version = (result.stdout or "").strip()
    return (dotnet, version) if version else None


def php_runtime() -> tuple[str, str] | None:
    """Detect PHP and return (php_path, version_string)."""
    php = shutil.which("php")
    if not php:
        return None
    try:
        result = subprocess.run(
            [php, "--version"],
            capture_output=True, text=True, timeout=_VERSION_TIMEOUT_SECONDS,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    out = (result.stdout or "").strip().splitlines()
    return (php, out[0]) if out else None


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


def _gopls_binary(install_dir: Path) -> Path | None:
    for candidate in (
        install_dir / "bin" / "gopls.exe",
        install_dir / "bin" / "gopls",
    ):
        if candidate.exists():
            return candidate
    return None


def _gopls_install(runtime_path: str, install_dir: Path) -> bool:
    env = {
        **os.environ,
        "GOPATH": str(install_dir),
        "GOBIN": str(install_dir / "bin"),
    }
    try:
        result = subprocess.run(
            [runtime_path, "install", "golang.org/x/tools/gopls@latest"],
            env=env, capture_output=True, text=True, timeout=_INSTALL_TIMEOUT_SECONDS,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        _log.debug("gopls: install error: %s", exc)
        return False
    if result.returncode != 0:
        _log.debug("gopls: install failed: %s", result.stderr[-300:])
    return result.returncode == 0


def _gopls_start(binary: Path, runtime_path: str) -> list[str]:
    return [str(binary), "serve"]


GOPLS = LSPServerConfig(
    name="gopls",
    get_runtime=go_runtime,
    install_dir=ICX_HOME / "gopls",
    binary_fn=_gopls_binary,
    install_fn=_gopls_install,
    start_fn=_gopls_start,
)


_JDTLS_URL = "https://download.eclipse.org/jdtls/snapshots/jdt-language-server-latest.tar.gz"


def _jdtls_config_dir_name() -> str:
    if sys.platform == "win32":
        return "config_win"
    if sys.platform == "darwin":
        return "config_mac"
    return "config_linux"


def _jdtls_binary(install_dir: Path) -> Path | None:
    plugins = install_dir / "plugins"
    if not plugins.is_dir():
        return None
    candidates = sorted(plugins.glob("org.eclipse.equinox.launcher_*.jar"))
    return candidates[0] if candidates else None


def _jdtls_install(runtime_path: str, install_dir: Path) -> bool:
    import tarfile
    import urllib.request

    archive = install_dir / "jdtls.tar.gz"
    try:
        urllib.request.urlretrieve(_JDTLS_URL, str(archive))
        with tarfile.open(archive) as tf:
            _safe_extract_tar(tf, install_dir)
    except Exception as exc:
        _log.debug("jdtls: download/extract failed: %s", exc)
        return False
    finally:
        _unlink_quiet(archive)
    return True


def _jdtls_start(binary: Path, runtime_path: str) -> list[str]:
    install_dir = binary.parent.parent
    config_dir = install_dir / _jdtls_config_dir_name()
    return [
        runtime_path,
        "-Declipse.application=org.eclipse.jdt.ls.core.id1",
        "-Dosgi.bundles.defaultStartLevel=4",
        "-Declipse.product=org.eclipse.jdt.ls.core.product",
        "-Dlog.level=ERROR",
        "-Xmx1G",
        "--add-modules=ALL-SYSTEM",
        "--add-opens", "java.base/java.util=ALL-UNNAMED",
        "--add-opens", "java.base/java.lang=ALL-UNNAMED",
        "-jar", str(binary),
        "-configuration", str(config_dir),
    ]


JDTLS = LSPServerConfig(
    name="jdtls",
    get_runtime=java_runtime,
    install_dir=ICX_HOME / "jdtls",
    binary_fn=_jdtls_binary,
    install_fn=_jdtls_install,
    start_fn=_jdtls_start,
)


_KOTLIN_LS_URL = "https://github.com/fwcd/kotlin-language-server/releases/latest/download/server.zip"


def _kotlin_ls_binary(install_dir: Path) -> Path | None:
    name = "kotlin-language-server.bat" if sys.platform == "win32" else "kotlin-language-server"
    script = install_dir / "server" / "bin" / name
    return script if script.exists() else None


def _kotlin_ls_install(runtime_path: str, install_dir: Path) -> bool:
    import zipfile
    import urllib.request

    archive = install_dir / "server.zip"
    try:
        urllib.request.urlretrieve(_KOTLIN_LS_URL, str(archive))
        with zipfile.ZipFile(archive) as zf:
            _safe_extract_zip(zf, install_dir)
    except Exception as exc:
        _log.debug("kotlin-language-server: download/extract failed: %s", exc)
        return False
    finally:
        _unlink_quiet(archive)

    if sys.platform != "win32":
        script = _kotlin_ls_binary(install_dir)
        if script is not None:
            script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    return _kotlin_ls_binary(install_dir) is not None


def _kotlin_ls_start(binary: Path, runtime_path: str) -> list[str]:
    return [str(binary)]


KOTLIN_LS = LSPServerConfig(
    name="kotlin-language-server",
    get_runtime=java_runtime,
    install_dir=ICX_HOME / "kotlin-language-server",
    binary_fn=_kotlin_ls_binary,
    install_fn=_kotlin_ls_install,
    start_fn=_kotlin_ls_start,
)


def _arch_x64_or_arm64() -> str:
    machine = platform.machine().lower()
    return "arm64" if machine in ("arm64", "aarch64") else "x64"


_RUST_ANALYZER_BASE = "https://github.com/rust-lang/rust-analyzer/releases/latest/download"


def _rust_analyzer_asset() -> str:
    arch = "aarch64" if _arch_x64_or_arm64() == "arm64" else "x86_64"
    if sys.platform == "win32":
        return f"rust-analyzer-{arch}-pc-windows-msvc.gz"
    if sys.platform == "darwin":
        return f"rust-analyzer-{arch}-apple-darwin.gz"
    return f"rust-analyzer-{arch}-unknown-linux-gnu.gz"


def _rust_analyzer_binary(install_dir: Path) -> Path | None:
    name = "rust-analyzer.exe" if sys.platform == "win32" else "rust-analyzer"
    binary = install_dir / "bin" / name
    return binary if binary.exists() else None


def _rust_analyzer_install(runtime_path: str, install_dir: Path) -> bool:
    import gzip
    import urllib.request

    bin_dir = install_dir / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    name = "rust-analyzer.exe" if sys.platform == "win32" else "rust-analyzer"
    target = bin_dir / name
    archive = bin_dir / "rust-analyzer.gz"
    try:
        urllib.request.urlretrieve(f"{_RUST_ANALYZER_BASE}/{_rust_analyzer_asset()}", str(archive))
        with gzip.open(archive, "rb") as src, open(target, "wb") as dst:
            dst.write(src.read())
    except Exception as exc:
        _log.debug("rust-analyzer: download/extract failed: %s", exc)
        return False
    finally:
        _unlink_quiet(archive)

    if sys.platform != "win32":
        target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    return target.exists()


def _rust_analyzer_start(binary: Path, runtime_path: str) -> list[str]:
    return [str(binary)]


RUST_ANALYZER = LSPServerConfig(
    name="rust-analyzer",
    get_runtime=rust_runtime,
    install_dir=ICX_HOME / "rust-analyzer",
    binary_fn=_rust_analyzer_binary,
    install_fn=_rust_analyzer_install,
    start_fn=_rust_analyzer_start,
)


_OMNISHARP_BASE = "https://github.com/OmniSharp/omnisharp-roslyn/releases/latest/download"


def _omnisharp_asset() -> tuple[str, bool]:
    """Return (asset filename, is_zip)."""
    arch = _arch_x64_or_arm64()
    if sys.platform == "win32":
        return f"omnisharp-win-{arch}-net6.0.zip", True
    if sys.platform == "darwin":
        return f"omnisharp-osx-{arch}-net6.0.tar.gz", False
    return f"omnisharp-linux-{arch}-net6.0.tar.gz", False


def _omnisharp_binary(install_dir: Path) -> Path | None:
    name = "OmniSharp.exe" if sys.platform == "win32" else "OmniSharp"
    binary = install_dir / name
    return binary if binary.exists() else None


def _omnisharp_install(runtime_path: str, install_dir: Path) -> bool:
    import tarfile
    import zipfile
    import urllib.request

    asset, is_zip = _omnisharp_asset()
    archive = install_dir / asset
    try:
        urllib.request.urlretrieve(f"{_OMNISHARP_BASE}/{asset}", str(archive))
        if is_zip:
            with zipfile.ZipFile(archive) as zf:
                _safe_extract_zip(zf, install_dir)
        else:
            with tarfile.open(archive) as tf:
                _safe_extract_tar(tf, install_dir)
    except Exception as exc:
        _log.debug("omnisharp: download/extract failed: %s", exc)
        return False
    finally:
        _unlink_quiet(archive)

    if sys.platform != "win32":
        binary = _omnisharp_binary(install_dir)
        if binary is not None:
            binary.chmod(binary.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    return _omnisharp_binary(install_dir) is not None


def _omnisharp_start(binary: Path, runtime_path: str) -> list[str]:
    return [str(binary), "-lsp"]


OMNISHARP = LSPServerConfig(
    name="omnisharp",
    get_runtime=dotnet_runtime,
    install_dir=ICX_HOME / "omnisharp",
    binary_fn=_omnisharp_binary,
    install_fn=_omnisharp_install,
    start_fn=_omnisharp_start,
)


def _intelephense_binary(install_dir: Path) -> Path | None:
    p = install_dir / "node_modules" / "intelephense" / "lib" / "intelephense.js"
    return p if p.exists() else None


def _intelephense_install(runtime_path: str, install_dir: Path) -> bool:
    npm = shutil.which("npm")
    if not npm:
        _log.debug("intelephense: npm not found")
        return False
    result = subprocess.run(
        [npm, "install", "intelephense",
         "--prefix", str(install_dir),
         "--silent", "--no-audit", "--no-fund"],
        capture_output=True, text=True, timeout=_INSTALL_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        _log.debug("intelephense: npm install failed: %s", result.stderr[-300:])
    return result.returncode == 0


def _intelephense_start(binary: Path, runtime_path: str) -> list[str]:
    return [runtime_path, str(binary), "--stdio"]


INTELEPHENSE = LSPServerConfig(
    name="intelephense",
    get_runtime=node_runtime,
    install_dir=ICX_HOME / "intelephense",
    binary_fn=_intelephense_binary,
    install_fn=_intelephense_install,
    start_fn=_intelephense_start,
)


_CLANGD_API_URL = "https://api.github.com/repos/clangd/clangd/releases/latest"


def _clangd_release_tag() -> str | None:
    import urllib.request

    try:
        req = urllib.request.Request(_CLANGD_API_URL, headers={"Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=_VERSION_TIMEOUT_SECONDS) as resp:
            data = json.load(resp)
        tag = data.get("tag_name")
        return tag if tag else None
    except Exception as exc:
        _log.debug("clangd: failed to fetch latest release tag: %s", exc)
        return None


def _clangd_asset(tag: str) -> str:
    if sys.platform == "win32":
        return f"clangd-windows-{tag}.zip"
    if sys.platform == "darwin":
        return f"clangd-mac-{tag}.zip"
    return f"clangd-linux-{tag}.zip"


def _clangd_binary(install_dir: Path) -> Path | None:
    name = "clangd.exe" if sys.platform == "win32" else "clangd"
    matches = sorted(install_dir.glob(f"*/bin/{name}"))
    return matches[0] if matches else None


def _clangd_install(runtime_path: str, install_dir: Path) -> bool:
    import zipfile
    import urllib.request

    tag = _clangd_release_tag()
    if not tag:
        return False

    asset = _clangd_asset(tag)
    archive = install_dir / asset
    try:
        url = f"https://github.com/clangd/clangd/releases/download/{tag}/{asset}"
        urllib.request.urlretrieve(url, str(archive))
        with zipfile.ZipFile(archive) as zf:
            _safe_extract_zip(zf, install_dir)
    except Exception as exc:
        _log.debug("clangd: download/extract failed: %s", exc)
        return False
    finally:
        _unlink_quiet(archive)

    binary = _clangd_binary(install_dir)
    if binary is not None and sys.platform != "win32":
        binary.chmod(binary.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return binary is not None


def _clangd_start(binary: Path, runtime_path: str) -> list[str]:
    return [str(binary)]


CLANGD = LSPServerConfig(
    name="clangd",
    get_runtime=cpp_runtime,
    install_dir=ICX_HOME / "clangd",
    binary_fn=_clangd_binary,
    install_fn=_clangd_install,
    start_fn=_clangd_start,
)
