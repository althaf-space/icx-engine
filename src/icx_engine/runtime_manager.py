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


def discover_runtimes(lang: str) -> list[RuntimeCandidate]:
    """Discover installed runtimes for lang on PATH. Best-effort + monkeypatchable in tests.
    Never installs; only inspects what already exists."""
    exe = (_VERSION_CMD.get(lang.lower()) or (None, None))[0]
    if not exe:
        return []
    found = shutil.which(exe)
    if not found:
        return []
    ver = validate_runtime(lang, found)
    if not ver:
        return []
    return [RuntimeCandidate(path=found, version=ver, source="discovered")]


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
