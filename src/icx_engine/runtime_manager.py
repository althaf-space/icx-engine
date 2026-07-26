"""Runtime Manager - per-repo runtime detection + isolation, as a REGISTRY not an installer.

Model: Discover -> Ask -> Remember -> Reuse. ICX never installs/downloads SDKs, never modifies the
global PATH, never overwrites or removes user software. The only file written is
``~/.icx/runtimes.json`` (a registry of user-approved, validated runtime PATHS).

Per-language detectors read REPO configuration (repo overrides machine). ``resolve_runtime`` returns
a Resolution STATE - the interactive ask/choose is surfaced by the caller (CLI prompt or MCP gate);
this module never blocks on stdin.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field, asdict
from pathlib import Path


def _home() -> Path:
    return Path.home()


def _registry_path() -> Path:
    return _home() / ".icx" / "runtimes.json"


# ---------------------------------------------------------------------------
# Per-language detection (repo config -> required version). Pure file reads.
# ---------------------------------------------------------------------------

def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _first(*vals: str | None) -> str | None:
    for v in vals:
        if v:
            v = v.strip()
            if v:
                return v
    return None


def detect_java(repo: Path) -> str | None:
    v = _read(repo / ".java-version").strip()
    if v:
        return v.splitlines()[0].strip()
    sdkmanrc = _read(repo / ".sdkmanrc")
    m = re.search(r"java\s*=\s*([0-9][^\s]*)", sdkmanrc)
    if m:
        return m.group(1)
    pom = _read(repo / "pom.xml")
    m = (re.search(r"<maven\.compiler\.release>\s*([0-9.]+)", pom)
         or re.search(r"<maven\.compiler\.target>\s*([0-9.]+)", pom)
         or re.search(r"<maven\.compiler\.source>\s*([0-9.]+)", pom))
    if m:
        return m.group(1)
    for gradle in ("build.gradle", "build.gradle.kts"):
        g = _read(repo / gradle)
        m = re.search(r"languageVersion\s*=?\.?\s*JavaLanguageVersion\.of\(\s*([0-9]+)\s*\)", g)
        if m:
            return m.group(1)
        m = re.search(r"sourceCompatibility\s*=?\s*['\"]?([0-9.]+)", g)
        if m:
            return m.group(1)
    return None


def detect_node(repo: Path) -> str | None:
    v = _first(_read(repo / ".nvmrc"), _read(repo / ".node-version"))
    if v:
        return v.splitlines()[0].strip().lstrip("v")
    pkg = _read(repo / "package.json")
    if pkg:
        try:
            data = json.loads(pkg)
            volta = (data.get("volta") or {}).get("node")
            if volta:
                return str(volta)
            eng = (data.get("engines") or {}).get("node")
            if eng:
                return str(eng)
        except json.JSONDecodeError:
            pass
    return None


def detect_python(repo: Path) -> str | None:
    v = _read(repo / ".python-version").strip()
    if v:
        return v.splitlines()[0].strip()
    py = _read(repo / "pyproject.toml")
    m = re.search(r'requires-python\s*=\s*["\']([^"\']+)["\']', py)
    if m:
        return m.group(1).strip()
    rt = _read(repo / "runtime.txt").strip()
    if rt.lower().startswith("python-"):
        return rt.split("-", 1)[1].strip()
    pipfile = _read(repo / "Pipfile")
    m = re.search(r'python_version\s*=\s*["\']([^"\']+)["\']', pipfile)
    if m:
        return m.group(1)
    return None


def detect_dotnet(repo: Path) -> str | None:
    gj = _read(repo / "global.json")
    if gj:
        try:
            data = json.loads(gj)
            ver = (data.get("sdk") or {}).get("version")
            if ver:
                return str(ver)
        except json.JSONDecodeError:
            pass
    for csproj in repo.glob("*.csproj"):
        m = re.search(r"<TargetFramework>\s*([^<]+)</TargetFramework>", _read(csproj))
        if m:
            return m.group(1).strip()
    return None


def detect_go(repo: Path) -> str | None:
    mod = _read(repo / "go.mod")
    m = re.search(r"^toolchain\s+go([0-9.]+)", mod, re.MULTILINE)
    if m:
        return m.group(1)
    m = re.search(r"^go\s+([0-9.]+)", mod, re.MULTILINE)
    if m:
        return m.group(1)
    return None


def detect_rust(repo: Path) -> str | None:
    rt = _read(repo / "rust-toolchain.toml")
    m = re.search(r'channel\s*=\s*["\']([^"\']+)["\']', rt)
    if m:
        return m.group(1)
    plain = _read(repo / "rust-toolchain").strip()
    if plain and "\n" not in plain:
        return plain
    cargo = _read(repo / "Cargo.toml")
    m = re.search(r'rust-version\s*=\s*["\']([^"\']+)["\']', cargo)
    if m:
        return m.group(1)
    return None


def detect_php(repo: Path) -> str | None:
    comp = _read(repo / "composer.json")
    if comp:
        try:
            data = json.loads(comp)
            php = (data.get("require") or {}).get("php")
            if php:
                return str(php)
        except json.JSONDecodeError:
            pass
    return None


def detect_ruby(repo: Path) -> str | None:
    v = _read(repo / ".ruby-version").strip()
    if v:
        return v.splitlines()[0].strip()
    gemfile = _read(repo / "Gemfile")
    m = re.search(r'ruby\s+["\']([^"\']+)["\']', gemfile)
    if m:
        return m.group(1)
    return None


_DETECTORS = {
    "java": detect_java,
    "node": detect_node,
    "python": detect_python,
    "dotnet": detect_dotnet,
    "go": detect_go,
    "rust": detect_rust,
    "php": detect_php,
    "ruby": detect_ruby,
}


def detect_required_runtime(lang: str, repo_path) -> str | None:
    """Return the runtime version the repo requires for ``lang``, or None if undetectable."""
    fn = _DETECTORS.get(lang.lower())
    if fn is None:
        return None
    try:
        return fn(Path(repo_path))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Registry (~/.icx/runtimes.json): validated paths only, never downloads.
# ---------------------------------------------------------------------------

def _load_registry() -> dict:
    p = _registry_path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_registry(reg: dict) -> None:
    p = _registry_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.parent / f"{p.name}.{os.getpid()}.{os.urandom(4).hex()}.tmp"
    tmp.write_text(json.dumps(reg, indent=2), encoding="utf-8")
    tmp.replace(p)


def lookup_runtime(lang: str, version: str) -> str | None:
    """Return a previously-validated path for lang+version, or None. Prunes stale (missing) paths."""
    reg = _load_registry()
    path = (reg.get(lang.lower()) or {}).get(version)
    if not path:
        return None
    if not Path(path).exists():
        # stale entry - prune and miss
        reg[lang.lower()].pop(version, None)
        _save_registry(reg)
        return None
    return path


def remember_runtime(lang: str, version: str, path: str) -> None:
    """Persist a user-approved, validated runtime path. Never installs anything."""
    reg = _load_registry()
    reg.setdefault(lang.lower(), {})[version] = str(path)
    _save_registry(reg)


# ---------------------------------------------------------------------------
# Discovery + validation (monkeypatchable) + version-manager detection.
# ---------------------------------------------------------------------------

@dataclass
class RuntimeCandidate:
    path: str
    version: str
    source: str  # "registry" | "discovered" | "version-manager"


# lang -> (executable name, version-command args)
_VERSION_CMD = {
    "java": ("java", ["-version"]),
    "node": ("node", ["--version"]),
    "python": ("python", ["--version"]),
    "dotnet": ("dotnet", ["--version"]),
    "go": ("go", ["version"]),
    "rust": ("rustc", ["--version"]),
    "php": ("php", ["--version"]),
    "ruby": ("ruby", ["--version"]),
}

_VERSION_MANAGER_MARKERS = {
    "node": [".nvm", ".volta", ".fnm"],
    "python": [".pyenv"],
    "java": [".sdkman", ".jabba"],
    "rust": [".rustup", ".cargo"],
    "go": [".goenv"],
}


# Version-manager install layouts (globs under the home dir) so ICX can enumerate EVERY installed
# version, not only the one currently on PATH. Best-effort; missing dirs simply yield nothing.
_VM_EXE_GLOBS: dict[str, list[str]] = {
    "node": [
        ".nvm/versions/node/*/bin/node",
        ".volta/tools/image/node/*/bin/node",
        ".fnm/node-versions/*/installation/bin/node",
        ".asdf/installs/nodejs/*/bin/node",
        ".config/nvm/versions/node/*/bin/node",
        # nvm-windows / Volta on Windows
        "AppData/Roaming/nvm/*/node.exe",
        "AppData/Local/Volta/tools/image/node/*/node.exe",
    ],
    "java": [
        ".sdkman/candidates/java/*/bin/java",
        ".jabba/jdk/*/bin/java",
        ".asdf/installs/java/*/bin/java",
    ],
    "python": [
        ".pyenv/versions/*/bin/python",
        ".asdf/installs/python/*/bin/python",
    ],
    "ruby": [
        ".rbenv/versions/*/bin/ruby",
        ".rvm/rubies/*/bin/ruby",
        ".asdf/installs/ruby/*/bin/ruby",
    ],
    "go": [".goenv/versions/*/bin/go", ".asdf/installs/golang/*/go/bin/go"],
}


def _vm_candidate_paths(lang: str) -> list[str]:
    """Every version-manager-installed executable path for lang under the home dir (all versions)."""
    home = _home()
    out: list[str] = []
    for pattern in _VM_EXE_GLOBS.get(lang.lower(), []):
        try:
            for p in home.glob(pattern):
                if p.is_file():
                    out.append(str(p))
        except OSError:
            pass
    return out


def discover_runtimes(lang: str) -> list[RuntimeCandidate]:
    """Discover ALL installed runtimes for lang: the PATH one PLUS every version-manager install
    (nvm/volta/fnm/pyenv/sdkman/rbenv/asdf...). Best-effort + monkeypatchable in tests. Never
    installs; only inspects what already exists. De-duplicated by real path."""
    exe = (_VERSION_CMD.get(lang.lower()) or (None, None))[0]
    if not exe:
        return []
    paths: list[str] = []
    found = shutil.which(exe)
    if found:
        paths.append(found)
    paths.extend(_vm_candidate_paths(lang))

    out: list[RuntimeCandidate] = []
    seen: set[str] = set()
    for p in paths:
        try:
            real = os.path.realpath(p)
        except OSError:
            real = p
        if real in seen:
            continue
        seen.add(real)
        ver = validate_runtime(lang, p)
        if ver:
            out.append(RuntimeCandidate(path=p, version=ver, source="discovered"))
    return out


_VER_RE = re.compile(r"([0-9]+(?:\.[0-9]+){0,2})")


def validate_runtime(lang: str, path: str) -> str | None:
    """Execute the runtime's version command and return the parsed version string, or None.
    Read-only - runs ``<runtime> --version`` and never mutates anything."""
    args = _VERSION_CMD.get(lang.lower())
    if not args:
        return None
    _exe, flags = args
    try:
        proc = subprocess.run(
            [path, *flags], capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    blob = (proc.stdout or "") + (proc.stderr or "")  # java prints version to stderr
    m = _VER_RE.search(blob)
    return m.group(1) if m else None


def detect_version_manager(lang: str) -> str | None:
    """Return a detected version-manager marker dir name for lang, if one exists in the home dir."""
    for marker in _VERSION_MANAGER_MARKERS.get(lang.lower(), []):
        if (_home() / marker).exists():
            return marker.lstrip(".")
    return None


# ---------------------------------------------------------------------------
# Orchestration: Discover -> Ask -> Remember -> Reuse. Never installs.
# ---------------------------------------------------------------------------

@dataclass
class Resolution:
    status: str  # "resolved" | "choose" | "ask" | "not_required"
    lang: str
    required_version: str | None = None
    path: str | None = None
    version: str | None = None
    candidates: list = field(default_factory=list)
    version_manager: str | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["candidates"] = [asdict(c) if isinstance(c, RuntimeCandidate) else c
                           for c in self.candidates]
        return d


def _version_matches(required: str, found: str) -> bool:
    """Loose match: found satisfies required if found starts with the numeric core of required
    (e.g. required '17' matches found '17.0.9'; '3.11' matches '3.11.4')."""
    core = _VER_RE.search(required or "")
    if not core:
        return True  # non-numeric constraint (e.g. 'stable') - accept, user confirms
    want = core.group(1)
    return found == want or found.startswith(want + ".") or want.startswith(found + ".") or found == want


def resolve_runtime(lang: str, repo_path) -> Resolution:
    """Resolve the runtime for a repo following Discover -> Ask -> Remember -> Reuse. Never installs.

    Returns a Resolution state; the caller (CLI/MCP gate) surfaces ask/choose to the user.
    """
    lang = lang.lower()
    required = detect_required_runtime(lang, repo_path)
    vm = detect_version_manager(lang)
    if not required:
        return Resolution(status="not_required", lang=lang, version_manager=vm)

    # Reuse: registry hit.
    cached = lookup_runtime(lang, required)
    if cached:
        return Resolution(status="resolved", lang=lang, required_version=required,
                          path=cached, version=required, version_manager=vm)

    # Discover existing installs and keep those matching the required version.
    try:
        found = discover_runtimes(lang)
    except Exception:
        found = []
    matches = [c for c in found if _version_matches(required, c.version)]

    if len(matches) == 1:
        remember_runtime(lang, required, matches[0].path)
        return Resolution(status="resolved", lang=lang, required_version=required,
                          path=matches[0].path, version=matches[0].version, version_manager=vm)
    if len(matches) > 1:
        return Resolution(status="choose", lang=lang, required_version=required,
                          candidates=matches, version_manager=vm)
    return Resolution(status="ask", lang=lang, required_version=required,
                      candidates=found, version_manager=vm)


# Registry key for the UI-harness node - deliberately NOT a real version so it never collides with a
# project's node entry. The harness node is decoupled from the app's node (see resolve_harness_node).
_HARNESS_NODE_KEY = "harness"
_HARNESS_MIN_MAJOR = 18
# Explicit override: point ICX at a specific node for the UI harness (e.g. a node-20 you distribute
# to your team). Takes precedence over discovery; the app's node is never touched.
_HARNESS_NODE_ENV = "ICX_HARNESS_NODE"


def _major(version: str) -> int:
    m = _VER_RE.search(version or "")
    try:
        return int(m.group(1).split(".")[0]) if m else 0
    except (ValueError, AttributeError):
        return 0


def normalize_node_exe(path: str | None) -> str | None:
    """Resolve a user-given node path to the actual executable.

    Accepts the node executable itself OR a directory (e.g. an nvm version dir like
    ``.../nvm/v22.23.1`` or a ``.../bin`` dir) and finds ``node`` / ``node.exe`` inside. Returns the
    executable path, or None if none is found.
    """
    if not path:
        return None
    p = Path(path)
    if p.is_file():
        return str(p)
    if p.is_dir():
        for cand in (p / "node.exe", p / "node", p / "bin" / "node.exe", p / "bin" / "node"):
            if cand.is_file():
                return str(cand)
        return None
    # Bare command name (e.g. "node") -> resolve on PATH.
    return shutil.which(path)


def resolve_harness_node(min_major: int = _HARNESS_MIN_MAJOR) -> str | None:
    """Return a path to a node runtime new enough to run the ICX UI harness (Playwright),
    INDEPENDENT of any project's node version.

    Rationale: ICX does not run the app under test - the app is already serving at the confirmed URL
    on its own node. The harness is a separate process that only drives a browser, so it just needs a
    modern node (>= min_major). A node-14/16 project still gets UI testing because the harness runs on
    a discovered node-18+. Discover -> Remember -> Reuse, never installs. Returns None when no modern
    node exists (caller falls back to PATH ``node`` or reports the UI layer unavailable).

    Override: set ``ICX_HARNESS_NODE`` to a node executable path to force it (for pre-provisioned /
    air-gapped setups). It wins over discovery and is used as-is when the path exists.
    """
    # Precedence: env override -> configured path (config.json) -> registry -> discovery.
    # Both may be given as a node executable OR a directory (nvm version dir) - normalize to the exe.
    override = normalize_node_exe(os.environ.get(_HARNESS_NODE_ENV))
    if override:
        return override

    try:
        from icx_engine.config_manager import ConfigManager
        configured = normalize_node_exe(ConfigManager.load().harness_node_path)
        if configured:
            return configured
    except Exception:
        pass

    cached = lookup_runtime("node", _HARNESS_NODE_KEY)
    if cached:
        return cached
    try:
        candidates = discover_runtimes("node")
    except Exception:
        candidates = []
    best: RuntimeCandidate | None = None
    for c in candidates:
        if _major(c.version) >= min_major and (best is None or _major(c.version) > _major(best.version)):
            best = c
    if best is not None:
        remember_runtime("node", _HARNESS_NODE_KEY, best.path)
        return best.path
    return None
