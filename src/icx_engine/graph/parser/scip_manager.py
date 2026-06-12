"""SCIP indexer lifecycle manager.

Mirrors lsp_manager.py pattern for SCIP indexers:
  detect runtime -> compare stored version -> (if changed) rmtree + reinstall
  -> write version file -> return (cmd, extra_env)

All installs go under ~/.icx/scip/<lang>/.
Supported auto-install targets:
  python     -> @sourcegraph/scip-python    (requires Node + npm)
  typescript -> @sourcegraph/scip-typescript (requires Node + npm)
  javascript -> same binary as typescript
  go         -> github.com/sourcegraph/scip-go (requires Go runtime)
  java       -> com.sourcegraph:scip-java    (requires JDK + coursier)
  kotlin     -> same binary as java

scip-ruby is intentionally excluded: cross-platform GEM_HOME complications.
Users install scip-ruby manually and ICX picks it up via PATH detection.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import stat
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

_log = logging.getLogger(__name__)

ICX_HOME = Path.home() / ".icx"

_VERSION_TIMEOUT_S = 5
_INSTALL_TIMEOUT_S = 300
_RMTREE_RETRY = 5


@dataclass
class SCIPIndexerConfig:
    """All language-specific knowledge for one SCIP indexer."""
    language: str                                                       # "python", "go", etc.
    name: str                                                           # "scip-python", "scip-go", etc.
    get_runtime: Callable[[], tuple[str, str] | None]                  # -> (runtime_path, version)
    install_dir: Path                                                   # ~/.icx/scip/<lang>
    binary_fn: Callable[[Path], Path | None]                           # finds binary inside install_dir
    install_fn: Callable[[str, Path], bool]                            # installs using runtime_path
    cmd_fn: Callable[[Path, str], list[str]]                           # (binary, runtime_path) -> full cmd
    extra_env_fn: Callable[[Path, str], dict] | None = field(default=None)  # optional extra env vars


def ensure_indexer(config: SCIPIndexerConfig) -> tuple[list[str], dict] | None:
    """Return (run_command, extra_env) for config's indexer, installing/reinstalling as needed.

    Returns None when runtime is absent, install fails, or binary cannot be found.
    extra_env is always {} for current indexers (reserved for future Ruby support).
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
        env = config.extra_env_fn(binary, runtime_path) if config.extra_env_fn else {}
        return config.cmd_fn(binary, runtime_path), env

    if stored is not None and stored != version:
        _log.debug(
            "%s: runtime version changed (%s -> %s), reinstalling",
            config.name, stored, version,
        )
        _force_rmtree(config.install_dir)
        config.install_dir.mkdir(parents=True, exist_ok=True)

    _log.debug("%s: installing into %s ...", config.name, config.install_dir)
    ok = config.install_fn(runtime_path, config.install_dir)
    if not ok:
        _log.debug("%s: install failed", config.name)
        return None

    _write_version(config.install_dir, version)
    binary = config.binary_fn(config.install_dir)
    if binary is None:
        _log.debug("%s: binary not found after install", config.name)
        return None

    env = config.extra_env_fn(binary, runtime_path) if config.extra_env_fn else {}
    return config.cmd_fn(binary, runtime_path), env


def ensure_scip_indexers(languages: list[str]) -> dict[str, tuple[list[str], dict]]:
    """Ensure SCIP indexers for detected languages. Returns {lang: (cmd, extra_env)}.

    Deduplicates installs when multiple languages share the same indexer
    (typescript and javascript both use scip-typescript).
    """
    result: dict[str, tuple[list[str], dict]] = {}
    installed: dict[str, tuple[list[str], dict] | None] = {}

    for lang in languages:
        lang_lower = lang.lower()
        config = _CONFIGS.get(lang_lower)
        if config is None:
            continue
        dir_key = str(config.install_dir)
        if dir_key not in installed:
            installed[dir_key] = ensure_indexer(config)
        cmd_env = installed[dir_key]
        if cmd_env is not None:
            result[lang_lower] = cmd_env

    return result


# ---------------------------------------------------------------------------
# Runtime detectors
# ---------------------------------------------------------------------------

def node_runtime() -> tuple[str, str] | None:
    """Detect Node and return (node_path, version_string) or None."""
    node = shutil.which("node")
    if not node:
        return None
    try:
        r = subprocess.run(
            [node, "--version"],
            capture_output=True, text=True, timeout=_VERSION_TIMEOUT_S,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if r.returncode != 0:
        return None
    version = (r.stdout or "").strip()
    return (node, version) if version.startswith("v") else None


def go_runtime() -> tuple[str, str] | None:
    """Detect Go and return (go_path, version_string) or None."""
    go = shutil.which("go")
    if not go:
        return None
    try:
        r = subprocess.run(
            [go, "version"],
            capture_output=True, text=True, timeout=_VERSION_TIMEOUT_S,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if r.returncode != 0:
        return None
    version = (r.stdout or "").strip()
    return (go, version) if version.startswith("go version") else None


# ---------------------------------------------------------------------------
# Shared npm helper
# ---------------------------------------------------------------------------

def _npm_install(install_dir: Path, packages: list[str]) -> bool:
    npm = shutil.which("npm")
    if not npm:
        _log.debug("npm not found on PATH")
        return False
    try:
        r = subprocess.run(
            [npm, "install", *packages,
             "--prefix", str(install_dir),
             "--silent", "--no-audit", "--no-fund"],
            capture_output=True, text=True, timeout=_INSTALL_TIMEOUT_S,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        _log.debug("npm install error: %s", exc)
        return False
    if r.returncode != 0:
        _log.debug("npm install failed: %s", r.stderr[-300:])
    return r.returncode == 0


def _npm_script(install_dir: Path, pkg_name: str) -> Path | None:
    """Find the main executable JS entry point for an npm package via package.json bin field.

    Returns the .js/.mjs file to run with `node`, not the .bin wrapper. This is
    the cross-platform approach used by lsp_manager.py for typescript-language-server.
    """
    pkg_dir = install_dir / "node_modules" / pkg_name
    if not pkg_dir.exists():
        return None
    pkg_json = pkg_dir / "package.json"
    if not pkg_json.exists():
        return None
    try:
        data = json.loads(pkg_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    bin_field = data.get("bin")
    if isinstance(bin_field, str):
        p = (pkg_dir / bin_field).resolve()
        if p.exists() and p.is_relative_to(install_dir):
            return p
    if isinstance(bin_field, dict):
        for rel in bin_field.values():
            p = (pkg_dir / rel).resolve()
            if p.exists() and p.is_relative_to(install_dir):
                return p
    return None


# ---------------------------------------------------------------------------
# scip-python  (@sourcegraph/scip-python, npm)
# ---------------------------------------------------------------------------

def _scip_python_binary(install_dir: Path) -> Path | None:
    return _npm_script(install_dir, "@sourcegraph/scip-python")


def _scip_python_install(runtime_path: str, install_dir: Path) -> bool:
    return _npm_install(install_dir, ["@sourcegraph/scip-python"])


def _scip_python_cmd(binary: Path, runtime_path: str) -> list[str]:
    return [runtime_path, str(binary), "index", "--project-name", "icx-graph", "."]


def _scip_python_env(binary: Path, runtime_path: str) -> dict:
    return {"NODE_OPTIONS": "--max-old-space-size=4096"}


SCIP_PYTHON = SCIPIndexerConfig(
    language="python",
    name="scip-python",
    get_runtime=lambda: node_runtime(),
    install_dir=ICX_HOME / "scip" / "python",
    binary_fn=_scip_python_binary,
    install_fn=_scip_python_install,
    cmd_fn=_scip_python_cmd,
    extra_env_fn=_scip_python_env,
)


# ---------------------------------------------------------------------------
# scip-typescript  (@sourcegraph/scip-typescript, npm)
# Also used for javascript - same binary handles both.
# ---------------------------------------------------------------------------

def _scip_ts_binary(install_dir: Path) -> Path | None:
    return _npm_script(install_dir, "@sourcegraph/scip-typescript")


def _scip_ts_install(runtime_path: str, install_dir: Path) -> bool:
    return _npm_install(install_dir, ["@sourcegraph/scip-typescript", "typescript"])


def _scip_ts_cmd(binary: Path, runtime_path: str) -> list[str]:
    return [runtime_path, str(binary), "index", "--infer-tsconfig"]


SCIP_TYPESCRIPT = SCIPIndexerConfig(
    language="typescript",
    name="scip-typescript",
    get_runtime=lambda: node_runtime(),
    install_dir=ICX_HOME / "scip" / "typescript",
    binary_fn=_scip_ts_binary,
    install_fn=_scip_ts_install,
    cmd_fn=_scip_ts_cmd,
)


# ---------------------------------------------------------------------------
# scip-go  (github.com/sourcegraph/scip-go, go install)
# ---------------------------------------------------------------------------

def _scip_go_binary(install_dir: Path) -> Path | None:
    for candidate in (
        install_dir / "bin" / "scip-go.exe",  # Windows
        install_dir / "bin" / "scip-go",       # Unix / macOS
    ):
        if candidate.exists():
            return candidate
    return None


def _scip_go_install(runtime_path: str, install_dir: Path) -> bool:
    env = {
        **os.environ,
        "GOPATH": str(install_dir),
        "GOBIN": str(install_dir / "bin"),
    }
    try:
        r = subprocess.run(
            [runtime_path, "install",
             "github.com/sourcegraph/scip-go/cmd/scip-go@latest"],
            env=env, capture_output=True, text=True, timeout=_INSTALL_TIMEOUT_S,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        _log.debug("scip-go install error: %s", exc)
        return False
    if r.returncode != 0:
        _log.debug("scip-go install failed: %s", r.stderr[-300:])
    return r.returncode == 0


def _scip_go_cmd(binary: Path, runtime_path: str) -> list[str]:
    # runtime_path (the go binary) is not used at run time: scip-go is statically compiled.
    return [str(binary), "."]


SCIP_GO = SCIPIndexerConfig(
    language="go",
    name="scip-go",
    get_runtime=lambda: go_runtime(),
    install_dir=ICX_HOME / "scip" / "go",
    binary_fn=_scip_go_binary,
    install_fn=_scip_go_install,
    cmd_fn=_scip_go_cmd,
)


# ---------------------------------------------------------------------------
# scip-java  (com.sourcegraph:scip-java, coursier)
# Also used for kotlin - same binary handles both.
# ---------------------------------------------------------------------------

def java_runtime() -> tuple[str, str] | None:
    """Detect JDK and return (java_path, version_string) or None."""
    java = shutil.which("java")
    if not java:
        return None
    try:
        r = subprocess.run(
            [java, "-version"],
            capture_output=True, text=True, timeout=_VERSION_TIMEOUT_S,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    # java -version writes to stderr: openjdk version "17.0.1" ...
    stderr = (r.stderr or "").strip()
    m = re.search(r'"([^"]+)"', stderr)
    if not m:
        return None
    return (java, m.group(1))


def _find_coursier() -> str | None:
    """Find coursier CLI. Checks PATH then ~/.icx/."""
    for name in ("coursier", "coursier.bat"):
        c = shutil.which(name)
        if c:
            return c
    platform_names = ("coursier.bat", "coursier") if os.name == "nt" else ("coursier",)
    for name in platform_names:
        p = ICX_HOME / name
        if p.exists():
            return str(p)
    return None


def _scip_java_binary(install_dir: Path) -> Path | None:
    for name in ("scip-java.bat", "scip-java"):
        p = install_dir / name
        if p.exists():
            return p
    return None


_SCIP_JAVA_COORD = "com.sourcegraph:scip-java_2.13:0.12.3"
_SCIP_JAVA_MAIN = "com.sourcegraph.scip_java.ScipJava"


def _scip_java_install(runtime_path: str, install_dir: Path) -> bool:
    coursier = _find_coursier()
    if not coursier:
        _log.debug("scip-java: coursier not found on PATH, ~/.icx/, or project root")
        return False
    out_path = install_dir / "scip-java"
    r = subprocess.run(
        [coursier, "bootstrap", "--standalone",
         "-M", _SCIP_JAVA_MAIN,
         "-o", str(out_path),
         _SCIP_JAVA_COORD],
        capture_output=True, text=True, timeout=_INSTALL_TIMEOUT_S,
    )
    if r.returncode != 0:
        _log.debug("scip-java bootstrap failed: %s", r.stderr[-300:])
    return r.returncode == 0


def _scip_java_cmd(binary: Path, runtime_path: str) -> list[str]:
    return [str(binary), "index"]


SCIP_JAVA = SCIPIndexerConfig(
    language="java",
    name="scip-java",
    get_runtime=lambda: java_runtime(),
    install_dir=ICX_HOME / "scip" / "java",
    binary_fn=_scip_java_binary,
    install_fn=_scip_java_install,
    cmd_fn=_scip_java_cmd,
)

SCIP_KOTLIN = SCIPIndexerConfig(
    language="kotlin",
    name="scip-java",  # same binary handles Java and Kotlin
    get_runtime=lambda: java_runtime(),
    install_dir=ICX_HOME / "scip" / "java",  # shared dir deduplicates install
    binary_fn=_scip_java_binary,
    install_fn=_scip_java_install,
    cmd_fn=_scip_java_cmd,
)


# ---------------------------------------------------------------------------
# Config registry
# ---------------------------------------------------------------------------

_CONFIGS: dict[str, SCIPIndexerConfig] = {
    "python":     SCIP_PYTHON,
    "typescript": SCIP_TYPESCRIPT,
    "javascript": SCIP_TYPESCRIPT,  # same binary handles JS and TS
    "go":         SCIP_GO,
    "java":       SCIP_JAVA,
    "kotlin":     SCIP_KOTLIN,      # same binary as java
}


# ---------------------------------------------------------------------------
# Temp tsconfig generator for scip-typescript JS/TS runs
# ---------------------------------------------------------------------------

_TS_NOISE_DIRS = frozenset({
    "node_modules", "dist", "build", "out", ".next", ".nuxt",
    "coverage", ".cache", "vendor", ".git",
})


_TS_EXTENSIONS: frozenset[str] = frozenset({".ts", ".tsx"})
_JS_EXTENSIONS: frozenset[str] = frozenset({".js", ".jsx", ".mjs", ".cjs", ".vue", ".svelte"})


def write_ts_tsconfig(
    project_path: str,
    source_files: list[str],
    dest: Path,
    project_tsconfig: str | None = None,
) -> None:
    """Write a tsconfig for scip-typescript into dest (ICX cache dir).

    Two modes:
    - project_tsconfig provided: generates a SCIP-optimised wrapper that extends
      the project's own tsconfig. Limits indexing to TypeScript-only files
      (no .js/.jsx) and enables incremental compilation so re-runs after source
      changes are fast. This avoids scip-typescript processing thousands of .jsx
      files that allowJs would otherwise include.
    - project_tsconfig absent: standalone tsconfig for JS-only projects with no
      existing tsconfig, sets allowJs=True and scopes to source directories.

    dest must be outside the project directory - never writes into the user repo.
    """
    proj = Path(project_path)

    if project_tsconfig:
        # SCIP-optimised wrapper: extend project tsconfig, TypeScript files only.
        ts_files = [
            Path(f).as_posix()
            for f in source_files
            if Path(f).suffix.lower() in _TS_EXTENSIONS
        ]
        tsconfig = {
            # Absolute path works in extends; avoids fragile relative path
            # from the cache dir to the project root.
            "extends": Path(project_tsconfig).as_posix(),
            "compilerOptions": {
                "noEmit": True,
                "skipLibCheck": True,
                # Incremental cache stored in scip_cache so subsequent builds
                # reprocess only changed files (not the full 116+ file set).
                "incremental": True,
                "tsBuildInfoFile": dest.with_name("tsconfig.tsbuildinfo").as_posix(),
            },
            # "files" replaces any "include" from the base tsconfig.
            # Limiting to .ts/.tsx means TypeScript skips .js/.jsx files which
            # can number in the thousands for React projects, causing timeouts.
            "files": ts_files,
        }
        dest.write_text(json.dumps(tsconfig, indent=2), encoding="utf-8")
        return

    # Fallback: standalone tsconfig for JS-only projects without tsconfig.json.
    src_dirs: set[str] = set()
    for f_str in source_files:
        try:
            rel = Path(f_str).relative_to(proj)
        except ValueError:
            continue
        top = rel.parts[0] if rel.parts else ""
        if top and top not in _TS_NOISE_DIRS:
            src_dirs.add(top)

    if src_dirs:
        include = [(proj / d).as_posix() + "/**/*" for d in sorted(src_dirs)]
    else:
        include = [proj.as_posix() + "/**/*"]

    exclude = [(proj / d).as_posix() for d in sorted(_TS_NOISE_DIRS)]
    try:
        for item in proj.iterdir():
            if item.suffix.lower() == ".war" and item.is_dir():
                p = item.as_posix()
                if p not in exclude:
                    exclude.append(p)
    except OSError:
        pass

    tsconfig = {
        "compilerOptions": {
            "allowJs": True,
            "noEmit": True,
            "skipLibCheck": True,
            # baseUrl anchors bare-specifier module resolution to the project root
            # regardless of where this tsconfig file lives (ICX cache dir).
            "baseUrl": proj.as_posix(),
        },
        "include": include,
        "exclude": exclude,
    }
    dest.write_text(json.dumps(tsconfig, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Internal utilities  (mirrors lsp_manager.py private helpers)
# ---------------------------------------------------------------------------

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
    except OSError as exc:
        _log.debug("failed to write version file %s: %s", vf, exc)


def _force_rmtree(path: Path) -> None:
    if not path.exists():
        return

    def _on_error(func, target, _exc_info):
        try:
            os.chmod(target, stat.S_IRWXU)
            func(target)
        except OSError as exc:
            _log.debug("rmtree error on %s: %s", target, exc)

    for attempt in range(_RMTREE_RETRY):
        try:
            shutil.rmtree(path, onerror=_on_error)
            if not path.exists():
                return
        except OSError:
            pass
        time.sleep(0.3 * (attempt + 1))
