"""Runner-install manager - ICX brings its own test-runner tooling under ~/.icx/testing/.

Sibling of graph/parser/lsp_manager.py, same discipline: version-pinned, reuse-if-present, checksum
option, NEVER silent (install requires user approval), fail-closed when required. This is for ICX's
OWN tooling (Playwright/Schemathesis/Hurl/k6/mutation tools) - NOT the user's language
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
    # UI bundle: installs Playwright AND downloads the Chromium browser into the pinned home (never
    # global, never the user's repo) - so approving "playwright" makes UI test authoring/execution
    # fully runnable. The agent runs its own hand-written Playwright tests against this install.
    "playwright":   RunnerSpec("playwright", "ui-bundle", "playwright", "1.48.0"),
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
    """True only for a COMPLETE install - not merely a non-empty dir. A prior run that failed
    partway (e.g. npm succeeded but the Chromium download did not) must NOT count as installed, or
    setup would skip it and leave the UI layer broken.
    """
    spec = RUNNER_SPECS.get(name)
    if spec is None:
        return False
    home = runner_home(name, spec.version)
    if not home.is_dir():
        return False

    if spec.kind == "ui-bundle":
        # Need Playwright, the @playwright/test runner, AND the Chromium browser downloaded.
        nm = home / "node_modules"
        browsers = browsers_dir(home)
        return (
            (nm / "playwright").is_dir()
            and (nm / "@playwright" / "test").is_dir()
            and browsers.is_dir() and any(browsers.iterdir())
        )
    if spec.kind == "binary":
        # The extracted binary file must be present.
        return (home / spec.package).is_file() or (home / f"{spec.package}.exe").is_file()
    # npm / pip / go / cargo: a non-empty home is sufficient.
    return any(home.iterdir())


def remove_runner(name: str) -> None:
    """Delete a runner's pinned install dir (for --force reinstall / cleaning a broken partial)."""
    spec = RUNNER_SPECS.get(name)
    if spec is None:
        return
    home = runner_home(name, spec.version)
    if home.exists():
        shutil.rmtree(home, ignore_errors=True)


def installed_path(name: str) -> str | None:
    spec = RUNNER_SPECS.get(name)
    if spec is None or not is_installed(name):
        return None
    return str(runner_home(name, spec.version))


def _do_install(spec: RunnerSpec, dest: Path, node_dir: str | None = None) -> bool:
    """Perform the actual install into dest. Returns True on success. Mockable in tests; real network
    only runs here. Never raises - returns False on any failure.

    node_dir: when set (UI bundle), prepend it to PATH so npm/npx/playwright resolve to that exact
    Node toolchain (the one the user chose for the harness), not whatever is on the global PATH."""
    dest.mkdir(parents=True, exist_ok=True)
    run_env = None
    try:
        if spec.kind == "ui-bundle":
            return _install_ui_bundle(spec, dest, node_dir)
        if spec.kind == "npm":
            cmd = ["npm", "install", "--prefix", str(dest), f"{spec.package}@{spec.version}"]
        elif spec.kind == "pip":
            cmd = ["pip", "install", "--target", str(dest), f"{spec.package}=={spec.version}"]
        elif spec.kind == "go":
            # `go install` writes the binary to GOBIN; point GOBIN at dest so the pinned home is
            # non-empty afterward (else is_installed stays False and we reinstall every run).
            cmd = ["go", "install", f"{spec.package}@v{spec.version}"]
            run_env = {**os.environ, "GOBIN": str(dest)}
        elif spec.kind == "cargo":
            cmd = ["cargo", "install", spec.package, "--version", spec.version, "--root", str(dest)]
        elif spec.kind == "binary":
            return _install_binary(spec, dest)
        else:
            return False
        from icx_engine._proc import win_argv
        proc = subprocess.run(win_argv(cmd), capture_output=True, text=True, timeout=600, env=run_env)
        return proc.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def browsers_dir(dest: Path) -> Path:
    """Where Playwright's Chromium is cached for this install (inside the pinned home, never global)."""
    return dest / "browsers"


# Per-binary download descriptors. Each maps (system, arch) -> release asset. Cross-platform.
_HURL_RELEASE = "https://github.com/Orange-OpenSource/hurl/releases/download"


def _plat_arch() -> str:
    import platform
    m = platform.machine().lower()
    return "aarch64" if m in ("arm64", "aarch64") else "x86_64"


def _hurl_asset(version: str) -> tuple[str, str, str] | None:
    """(download url, archive kind 'zip'|'tar', binary name) for this OS/arch, or None if unsupported."""
    import platform
    arch = _plat_arch()
    system = platform.system()
    if system == "Windows":
        target, kind, binname = f"{arch}-pc-windows-msvc", "zip", "hurl.exe"
    elif system == "Linux":
        target, kind, binname = f"{arch}-unknown-linux-gnu", "tar", "hurl"
    elif system == "Darwin":
        target, kind, binname = f"{arch}-apple-darwin", "tar", "hurl"
    else:
        return None
    url = f"{_HURL_RELEASE}/{version}/hurl-{version}-{target}.{kind == 'zip' and 'zip' or 'tar.gz'}"
    return url, kind, binname


_BINARY_ASSETS = {"hurl": _hurl_asset}


def _install_binary(spec: RunnerSpec, dest: Path) -> bool:
    """Download + extract a standalone binary (e.g. hurl) into dest for THIS OS/arch. Uses only the
    stdlib (urllib + zip/tar). Guarded - returns False on any failure, never raises.

    Security: downloads over HTTPS from the tool's official release host. If spec.checksum is set it
    is verified (sha256); when ICX_REQUIRE_RUNNER_CHECKSUM=1 a missing checksum already fails closed
    upstream in ensure_runner.
    """
    import hashlib
    import tarfile
    import tempfile
    import urllib.request
    import zipfile

    asset_fn = _BINARY_ASSETS.get(spec.name)
    if asset_fn is None:
        return False
    info = asset_fn(spec.version)
    if info is None:
        return False
    url, kind, binname = info

    tmpdir = Path(tempfile.mkdtemp(prefix="icx-bin-"))
    try:
        archive = tmpdir / f"download.{'zip' if kind == 'zip' else 'tar.gz'}"
        try:
            urllib.request.urlretrieve(url, archive)     # noqa: S310 - official HTTPS release host
        except (OSError, ValueError):
            return False

        if spec.checksum:
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            if digest.lower() != spec.checksum.lower():
                return False

        extract_dir = tmpdir / "x"
        extract_dir.mkdir()
        try:
            if kind == "zip":
                with zipfile.ZipFile(archive) as z:
                    z.extractall(extract_dir)
            else:
                with tarfile.open(archive) as t:
                    t.extractall(extract_dir)          # noqa: S202 - trusted release archive
        except (OSError, zipfile.BadZipFile, tarfile.TarError):
            return False

        found = next((p for p in extract_dir.rglob(binname) if p.is_file()), None)
        if found is None:
            return False
        dest.mkdir(parents=True, exist_ok=True)
        target = dest / binname
        shutil.copy2(found, target)
        if os.name != "nt":
            target.chmod(0o755)
        return True
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _node_path_env(node_dir: str | None) -> dict:
    """Env with the chosen Node's dir prepended to PATH so npm/npx/playwright come from it."""
    env = {**os.environ}
    if node_dir:
        env["PATH"] = str(node_dir) + os.pathsep + env.get("PATH", "")
    return env


def _node_exe(node_dir: str | None) -> str:
    """The node executable for the given Node dir (or bare 'node')."""
    if node_dir:
        d = Path(node_dir)
        for c in (d / "node.exe", d / "node", d / "bin" / "node.exe", d / "bin" / "node"):
            if c.is_file():
                return str(c)
    return "node"


def _npm_for(node_dir: str | None) -> str:
    """Locate the npm launcher that belongs to the given Node (nvm/install dirs put npm.cmd/npm next
    to node or in bin/). Falls back to a bare 'npm' (PATH) only if none is found."""
    if node_dir:
        d = Path(node_dir)
        for c in (d / "npm.cmd", d / "npm", d / "bin" / "npm.cmd", d / "bin" / "npm"):
            if c.is_file():
                return str(c)
    return "npm"


def _install_ui_bundle(spec: RunnerSpec, dest: Path, node_dir: str | None = None) -> bool:
    """Install Playwright into dest, then download Chromium into dest/browsers.

    Everything lands under ~/.icx/testing (never global, never the user's repo). Returns True only
    when BOTH the npm install and the browser download succeed. Never raises. npm is taken from the
    chosen Node (not the parent shell's PATH, where nvm may be inactive), and that Node's dir is on
    PATH so the npm/playwright shims find node.
    """
    import logging
    from icx_engine._proc import win_argv
    _log = logging.getLogger("icx.testing.install")
    base_env = _node_path_env(node_dir)
    npm_exe = _npm_for(node_dir)
    try:
        # @playwright/test is the actual test-runner package (the `playwright test` CLI, `test()`/
        # `expect()`, --reporter=junit) - the base `playwright` package alone provides only the
        # browser-automation API + browser install, not the runner the agent is told to invoke.
        # Pinned to the same version so the two stay compatible (they must match).
        npm = [npm_exe, "install", "--prefix", str(dest),
               f"{spec.package}@{spec.version}", f"@playwright/test@{spec.version}"]
        r1 = subprocess.run(win_argv(npm), capture_output=True, text=True, timeout=600, env=base_env)
        if r1.returncode != 0:
            _log.warning("UI npm install failed (rc=%s): %s", r1.returncode,
                         (getattr(r1, "stderr", "") or getattr(r1, "stdout", "") or "")[-800:])
            return False
        browsers = browsers_dir(dest)
        browsers.mkdir(parents=True, exist_ok=True)
        env = {**base_env, "PLAYWRIGHT_BROWSERS_PATH": str(browsers)}
        # CRITICAL: download Chromium with the SAME Playwright that gets imported at runtime -
        # `node node_modules/playwright/cli.js install chromium`. The `.bin/playwright` shim can
        # resolve a different Playwright and fetch a mismatched browser build (e.g. runtime wants
        # chromium-1140 but the shim downloads 1228 -> "Executable doesn't exist").
        cli_js = None
        for rel in ("node_modules/playwright/cli.js", "node_modules/playwright-core/cli.js"):
            cand = dest / rel
            if cand.is_file():
                cli_js = str(cand)
                break
        if cli_js:
            browser_cmd = [_node_exe(node_dir), cli_js, "install", "chromium"]
        else:
            pw_bin = dest / "node_modules" / ".bin" / ("playwright.cmd" if os.name == "nt" else "playwright")
            browser_cmd = [str(pw_bin), "install", "chromium"]
        # Browser download is large; allow it more time than a package install.
        r2 = subprocess.run(win_argv(browser_cmd),
                            capture_output=True, text=True, timeout=1800, env=env)
        if r2.returncode != 0:
            _log.warning("playwright install chromium failed (rc=%s): %s", r2.returncode,
                         (getattr(r2, "stderr", "") or getattr(r2, "stdout", "") or "")[-800:])
            return False
        return True
    except (OSError, subprocess.SubprocessError) as exc:
        _log.warning("UI bundle install error: %s", exc)
        return False


def ensure_runner(name: str, approve: Callable[[str], bool] | None = None,
                  node_dir: str | None = None) -> str | None:
    """Return the install path for a runner, installing it (USER-APPROVED) if missing.

    - Already installed -> return path (reuse; no network).
    - Missing + not approved -> return None (caller reports the layer unavailable; never crashes).
    - Missing + approved -> install, then return the path (or None if the install failed).
    Approval: `approve(name) -> bool`. When None, auto-approve only if ICX_AUTO_INSTALL_RUNNERS=1
    (default off) - so nothing installs silently.
    Checksum: when ICX_REQUIRE_RUNNER_CHECKSUM=1 and a binary spec has no checksum, fail closed.
    node_dir: for the UI bundle, the chosen Node's dir to put on PATH for npm/playwright.
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
    if _do_install(spec, dest, node_dir=node_dir):
        return str(dest)
    return None


_BROWSER_ENGINES = ("chromium", "firefox", "webkit")


def _harness_node_dir() -> str | None:
    """Resolve the pinned harness Node's dir the same way `_install_ui_bundle` gets its `node_dir` -
    via `runtime_manager.resolve_harness_node()` (env override -> configured path -> registry ->
    discovery), same resolution `ui_auth._harness_env()` uses. Best-effort: any failure or an
    unresolved node falls back to None, so the caller uses bare 'node' from PATH (prior behavior)."""
    try:
        from icx_engine.runtime_manager import resolve_harness_node
        node_exe = resolve_harness_node()
        return str(Path(node_exe).parent) if node_exe else None
    except Exception:
        return None


def ensure_browser(engine: str, approve: Callable[[str], bool] | None = None) -> bool:
    """Ensure the Playwright `engine` browser binary is present in the UI bundle's pinned browsers
    dir, installing it there (never globally) only when missing AND approved. Returns True when
    present or successfully installed, False otherwise. Never raises.

    Reuses the same mechanism as the Chromium download in `_install_ui_bundle`: the browsers dir
    pinned under the Playwright install, `node node_modules/playwright/cli.js install <engine>` run
    with PLAYWRIGHT_BROWSERS_PATH pointed at that dir (falling back to the playwright-core cli.js or
    the .bin/playwright shim, same as the Chromium path). The node used is the pinned harness Node
    resolved via `_harness_node_dir()` (falls back to bare PATH 'node' if unresolved), never a bare
    'node' by default - avoids picking up whatever stray/old node happens to be on PATH. Approval:
    `approve(name)->bool`; when None, auto-approve only if ICX_AUTO_INSTALL_RUNNERS=1 (same gate as
    ensure_runner).
    """
    engine = (engine or "").strip().lower()
    if engine not in _BROWSER_ENGINES:
        return False
    pw = installed_path("playwright")
    if not pw:
        return False
    dest = Path(pw)
    root = browsers_dir(dest)
    if root.is_dir() and any(root.glob(f"{engine}-*")):
        return True                                     # already installed

    approved = approve(engine) if approve is not None else os.environ.get("ICX_AUTO_INSTALL_RUNNERS") == "1"
    if not approved:
        return False

    node_dir = _harness_node_dir()
    cli_js = None
    for rel in ("node_modules/playwright/cli.js", "node_modules/playwright-core/cli.js"):
        cand = dest / rel
        if cand.is_file():
            cli_js = str(cand)
            break
    if cli_js:
        cmd = [_node_exe(node_dir), cli_js, "install", engine]
    else:
        pw_bin = dest / "node_modules" / ".bin" / ("playwright.cmd" if os.name == "nt" else "playwright")
        cmd = [str(pw_bin), "install", engine]

    from icx_engine._proc import win_argv
    try:
        root.mkdir(parents=True, exist_ok=True)
        env = {**_node_path_env(node_dir), "PLAYWRIGHT_BROWSERS_PATH": str(root)}
        r = subprocess.run(win_argv(cmd), capture_output=True, text=True, timeout=1800, env=env)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0 and root.is_dir() and any(root.glob(f"{engine}-*"))


def auth_harness_path() -> str:
    """Path to the packaged Playwright session-capture harness (.mjs ships with ICX)."""
    return str(Path(__file__).parent / "assets" / "icx-auth.mjs")


def discover_harness_path() -> str:
    """Path to the packaged runtime census AUTO-DISCOVERY harness (.mjs ships with ICX)."""
    return str(Path(__file__).parent / "assets" / "icx-discover.mjs")


def runtime_harness_path(basename: str, packaged: str) -> str:
    """Path to run a harness .mjs FROM. ESM `import "playwright"` resolves relative to the file's
    own location (NODE_PATH is ignored for ESM), so the harness must sit next to the installed
    node_modules. Copies the packaged .mjs into the UI install dir on first use and returns that
    copy; falls back to the packaged path when the UI bundle is not installed (e.g. in ICX's own
    tests)."""
    pw = installed_path("playwright")
    if not pw:
        return packaged
    dst = Path(pw) / basename
    # Always refresh from the packaged copy so an ICX upgrade's newer harness reaches EXISTING
    # installs (copy-if-missing would leave old users running a stale .mjs). Cheap - tiny file.
    try:
        src_p = Path(packaged)
        if src_p.is_file() and (not dst.is_file() or dst.read_bytes() != src_p.read_bytes()):
            shutil.copy2(src_p, dst)
    except OSError:
        return packaged if Path(packaged).is_file() else str(dst)
    return str(dst)


def which(cmd: str) -> str | None:
    """Thin wrapper so adapters can prefer a PATH tool when present (unchanged behavior)."""
    return shutil.which(cmd)
