"""Runner-install manager - ICX brings its own test-runner tooling under ~/.icx/testing/.

Sibling of graph/parser/lsp_manager.py, same discipline: version-pinned, reuse-if-present, checksum
option, NEVER silent (install requires user approval), fail-closed when required. This is for ICX's
OWN tooling (Playwright/Stagehand/Schemathesis/Hurl/k6/mutation tools) - NOT the user's language
SDKs (those go through runtime_manager as a discover/ask/remember registry).

Adapters call `ensure_runner(name, approve=...)` before building their command. When the runner is
missing and not approved, they get None and report the layer as unavailable - never a crash.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass(frozen=True)
class RunnerSpec:
    name: str
    kind: str            # npm | pip | binary | go | cargo | maven
    package: str         # package/module/binary identifier
    version: str         # pinned version
    checksum: str = ""   # optional sha256 for binary kind


# Pinned runner tooling. Versions are deliberate; bump intentionally, never "latest".
RUNNER_SPECS: dict[str, RunnerSpec] = {
    "playwright":   RunnerSpec("playwright", "npm", "playwright", "1.48.0"),
    "stagehand":    RunnerSpec("stagehand", "npm", "@browserbasehq/stagehand", "1.9.0"),
    "jest-junit":   RunnerSpec("jest-junit", "npm", "jest-junit", "16.0.0"),
    "stryker":      RunnerSpec("stryker", "npm", "@stryker-mutator/core", "8.6.0"),
    "schemathesis": RunnerSpec("schemathesis", "pip", "schemathesis", "3.36.0"),
    "mutmut":       RunnerSpec("mutmut", "pip", "mutmut", "2.5.1"),
    "hurl":         RunnerSpec("hurl", "binary", "hurl", "5.0.1"),
    "gotestsum":    RunnerSpec("gotestsum", "go", "gotest.tools/gotestsum", "1.12.0"),
    "nextest":      RunnerSpec("nextest", "cargo", "cargo-nextest", "0.9.78"),
}

_REQUIRE_CHECKSUM_ENV = "ICX_REQUIRE_RUNNER_CHECKSUM"


def _install_root() -> Path:
    return Path.home() / ".icx" / "testing"


def runner_home(name: str, version: str) -> Path:
    return _install_root() / name / version


def is_installed(name: str) -> bool:
    """A runner is installed if its version-pinned home dir exists and is non-empty."""
    spec = RUNNER_SPECS.get(name)
    if spec is None:
        return False
    home = runner_home(name, spec.version)
    return home.is_dir() and any(home.iterdir())


def installed_path(name: str) -> str | None:
    spec = RUNNER_SPECS.get(name)
    if spec is None or not is_installed(name):
        return None
    return str(runner_home(name, spec.version))


def _do_install(spec: RunnerSpec, dest: Path) -> bool:
    """Perform the actual install into dest. Returns True on success. Mockable in tests; real network
    only runs here. Never raises - returns False on any failure."""
    dest.mkdir(parents=True, exist_ok=True)
    try:
        if spec.kind == "npm":
            cmd = ["npm", "install", "--prefix", str(dest), f"{spec.package}@{spec.version}"]
        elif spec.kind == "pip":
            cmd = ["pip", "install", "--target", str(dest), f"{spec.package}=={spec.version}"]
        elif spec.kind == "go":
            cmd = ["go", "install", f"{spec.package}@v{spec.version}"]
        elif spec.kind == "cargo":
            cmd = ["cargo", "install", spec.package, "--version", spec.version, "--root", str(dest)]
        elif spec.kind == "binary":
            # Binary downloads are platform-specific; the concrete URL/extract is handled by a
            # per-binary helper (not shelled generically). Left for the binary installer wiring.
            return False
        else:
            return False
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        return proc.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def ensure_runner(name: str, approve: Callable[[str], bool] | None = None) -> str | None:
    """Return the install path for a runner, installing it (USER-APPROVED) if missing.

    - Already installed -> return path (reuse; no network).
    - Missing + not approved -> return None (caller reports the layer unavailable; never crashes).
    - Missing + approved -> install, then return the path (or None if the install failed).
    Approval: `approve(name) -> bool`. When None, auto-approve only if ICX_AUTO_INSTALL_RUNNERS=1
    (default off) - so nothing installs silently.
    Checksum: when ICX_REQUIRE_RUNNER_CHECKSUM=1 and a binary spec has no checksum, fail closed.
    """
    spec = RUNNER_SPECS.get(name)
    if spec is None:
        return None
    if is_installed(name):
        return str(runner_home(name, spec.version))

    approved = approve(name) if approve is not None else os.environ.get("ICX_AUTO_INSTALL_RUNNERS") == "1"
    if not approved:
        return None

    if (spec.kind == "binary" and not spec.checksum
            and os.environ.get(_REQUIRE_CHECKSUM_ENV) == "1"):
        return None  # fail closed: required checksum missing

    dest = runner_home(name, spec.version)
    if _do_install(spec, dest):
        return str(dest)
    return None


def harness_path() -> str:
    """Path to the packaged Stagehand replay harness (.mjs ships with ICX)."""
    return str(Path(__file__).parent / "assets" / "icx-replay.mjs")


def which(cmd: str) -> str | None:
    """Thin wrapper so adapters can prefer a PATH tool when present (unchanged behavior)."""
    return shutil.which(cmd)
