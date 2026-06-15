"""Language-agnostic tech-stack fingerprint detection for memory entries.

Parses dependency manifest files at a project root (and its immediate
subdirectories, for monorepos) to extract declared language/runtime versions
and key framework versions. Only values literally present in a manifest are
returned - values resolved via external version catalogs, BOMs, or build
variables are omitted rather than guessed.
"""
from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path
from xml.etree import ElementTree

from icx_engine.graph.parser.detect import _is_noise_dir

# Frameworks of interest per ecosystem: dependency name -> fingerprint key.
_JS_FRAMEWORKS = {
    "react": "react", "vue": "vue", "next": "next", "nuxt": "nuxt",
    "@angular/core": "angular", "svelte": "svelte", "express": "express",
    "fastify": "fastify", "@nestjs/core": "nestjs",
}
_PY_FRAMEWORKS = {
    "django": "django", "flask": "flask", "fastapi": "fastapi",
    "celery": "celery", "sqlalchemy": "sqlalchemy", "pydantic": "pydantic",
}
_RUBY_FRAMEWORKS = {"rails": "rails", "sinatra": "sinatra"}
_PHP_FRAMEWORKS = {
    "laravel/framework": "laravel", "symfony/symfony": "symfony",
}


def _strip_version_spec(spec: str) -> str | None:
    """Extract a literal version from a dependency spec, or None if it's a
    range/wildcard/variable reference that doesn't pin a single version."""
    spec = spec.strip()
    m = re.match(r"^[^0-9]*([0-9]+(?:\.[0-9]+){0,2})", spec)
    return m.group(1) if m else None


def _parse_package_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    result: dict = {}
    languages = {}
    node_spec = (data.get("engines") or {}).get("node")
    if node_spec:
        v = _strip_version_spec(node_spec)
        if v:
            languages["node"] = v
    if languages:
        result["languages"] = languages

    frameworks = {}
    deps = {**(data.get("dependencies") or {}), **(data.get("devDependencies") or {})}
    for dep, key in _JS_FRAMEWORKS.items():
        if dep in deps:
            v = _strip_version_spec(deps[dep])
            if v:
                frameworks[key] = v
    if frameworks:
        result["frameworks"] = frameworks
    result["package_manager"] = "npm"
    return result


def _parse_pyproject_toml(path: Path) -> dict:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    result: dict = {}
    languages = {}
    project = data.get("project") or {}
    requires_python = project.get("requires-python")
    if requires_python:
        v = _strip_version_spec(requires_python)
        if v:
            languages["python"] = v
    if languages:
        result["languages"] = languages

    frameworks = {}
    deps_list = list(project.get("dependencies") or [])
    poetry_deps = (data.get("tool", {}).get("poetry", {}).get("dependencies") or {})
    for dep, version in poetry_deps.items():
        if isinstance(version, str):
            deps_list.append(f"{dep}{version}")
    for entry in deps_list:
        m = re.match(r"^([A-Za-z0-9_.\-]+)\s*([><=!~^]*[0-9][0-9A-Za-z.\-]*)?", entry)
        if not m:
            continue
        name = m.group(1).lower().replace("_", "-")
        if name in _PY_FRAMEWORKS and m.group(2):
            v = _strip_version_spec(m.group(2))
            if v:
                frameworks[_PY_FRAMEWORKS[name]] = v
    if frameworks:
        result["frameworks"] = frameworks
    result["package_manager"] = "poetry" if "poetry" in data.get("tool", {}) else "pip"
    return result


def _parse_requirements_txt(path: Path) -> dict:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return {}
    frameworks = {}
    for line in lines:
        line = line.split("#", 1)[0].strip()
        m = re.match(r"^([A-Za-z0-9_.\-]+)\s*==\s*([0-9][0-9A-Za-z.\-]*)", line)
        if not m:
            continue
        name = m.group(1).lower().replace("_", "-")
        if name in _PY_FRAMEWORKS:
            v = _strip_version_spec(m.group(2))
            if v:
                frameworks[_PY_FRAMEWORKS[name]] = v
    result: dict = {"package_manager": "pip"}
    if frameworks:
        result["frameworks"] = frameworks
    return result


_POM_NS = "{http://maven.apache.org/POM/4.0.0}"


def _parse_pom_xml(path: Path) -> dict:
    try:
        root = ElementTree.parse(path).getroot()
    except Exception:
        return {}
    ns = _POM_NS if root.tag.startswith(_POM_NS) else ""

    result: dict = {"package_manager": "maven"}
    languages = {}
    properties = root.find(f"{ns}properties")
    if properties is not None:
        for key in ("java.version", "maven.compiler.source", "maven.compiler.target"):
            el = properties.find(f"{ns}{key}")
            if el is not None and el.text:
                v = _strip_version_spec(el.text)
                if v:
                    languages["java"] = v
                    break
    if languages:
        result["languages"] = languages

    frameworks = {}
    parent = root.find(f"{ns}parent")
    if parent is not None:
        artifact = parent.find(f"{ns}artifactId")
        version = parent.find(f"{ns}version")
        if artifact is not None and version is not None and artifact.text and version.text:
            if "spring-boot" in artifact.text:
                v = _strip_version_spec(version.text)
                if v:
                    frameworks["spring-boot"] = v
    if frameworks:
        result["frameworks"] = frameworks
    return result


_GRADLE_FRAMEWORK_PATTERNS = {
    "spring-boot": re.compile(
        r"spring-boot[\w.\-]*:([0-9]+(?:\.[0-9]+){1,2})"
        r"|org\.springframework\.boot[\"']?\s+version\s+[\"']([0-9]+(?:\.[0-9]+){1,2})[\"']"
    ),
}


def _parse_build_gradle(path: Path) -> dict:
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return {}
    result: dict = {"package_manager": "gradle"}

    languages = {}
    m = re.search(r"sourceCompatibility\s*[=:]?\s*[\"']?(?:JavaVersion\.VERSION_)?([0-9_.]+)[\"']?", text)
    if m:
        v = m.group(1).replace("_", ".")
        if re.match(r"^[0-9.]+$", v):
            languages["java"] = v
    if languages:
        result["languages"] = languages

    frameworks = {}
    for key, pattern in _GRADLE_FRAMEWORK_PATTERNS.items():
        m = pattern.search(text)
        if m:
            frameworks[key] = next(g for g in m.groups() if g)
    if frameworks:
        result["frameworks"] = frameworks
    return result


def _parse_cargo_toml(path: Path) -> dict:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    result: dict = {"package_manager": "cargo"}
    package = data.get("package") or {}
    edition = package.get("edition")
    if edition:
        result["languages"] = {"rust": str(edition)}
    return result


def _parse_go_mod(path: Path) -> dict:
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return {}
    result: dict = {"package_manager": "go"}
    m = re.search(r"^go\s+([0-9]+(?:\.[0-9]+){1,2})", text, re.MULTILINE)
    if m:
        result["languages"] = {"go": m.group(1)}
    return result


def _parse_gemfile(path: Path) -> dict:
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return {}
    result: dict = {"package_manager": "bundler"}
    languages = {}
    m = re.search(r"^ruby\s+[\"']([0-9][0-9.]*)[\"']", text, re.MULTILINE)
    if m:
        languages["ruby"] = m.group(1)
    if languages:
        result["languages"] = languages

    frameworks = {}
    for dep, key in _RUBY_FRAMEWORKS.items():
        m = re.search(rf"^gem\s+[\"']{re.escape(dep)}[\"']\s*,\s*[\"']([~>=<\s]*[0-9][0-9.]*)[\"']", text, re.MULTILINE)
        if m:
            v = _strip_version_spec(m.group(1))
            if v:
                frameworks[key] = v
    if frameworks:
        result["frameworks"] = frameworks
    return result


def _parse_composer_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    result: dict = {"package_manager": "composer"}
    languages = {}
    php_spec = (data.get("require") or {}).get("php")
    if php_spec:
        v = _strip_version_spec(php_spec)
        if v:
            languages["php"] = v
    if languages:
        result["languages"] = languages

    frameworks = {}
    require = data.get("require") or {}
    for dep, key in _PHP_FRAMEWORKS.items():
        if dep in require:
            v = _strip_version_spec(require[dep])
            if v:
                frameworks[key] = v
    if frameworks:
        result["frameworks"] = frameworks
    return result


def _parse_pubspec_yaml(path: Path) -> dict:
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return {}
    result: dict = {"package_manager": "pub"}
    m = re.search(r"^\s*sdk:\s*[\"']?[\^>=<\s]*([0-9][0-9.]*)", text, re.MULTILINE)
    if m:
        result["languages"] = {"dart": m.group(1)}
    return result


# Manifest filename -> parser. Checked in order; first match per directory wins.
_MANIFEST_PARSERS: tuple[tuple[str, "callable"], ...] = (
    ("package.json", _parse_package_json),
    ("pyproject.toml", _parse_pyproject_toml),
    ("requirements.txt", _parse_requirements_txt),
    ("pom.xml", _parse_pom_xml),
    ("build.gradle", _parse_build_gradle),
    ("build.gradle.kts", _parse_build_gradle),
    ("Cargo.toml", _parse_cargo_toml),
    ("go.mod", _parse_go_mod),
    ("Gemfile", _parse_gemfile),
    ("composer.json", _parse_composer_json),
    ("pubspec.yaml", _parse_pubspec_yaml),
)


def _detect_dir(directory: Path) -> dict:
    for filename, parser in _MANIFEST_PARSERS:
        manifest = directory / filename
        if manifest.is_file():
            try:
                stack = parser(manifest)
            except Exception:
                continue
            if stack:
                return stack
    return {}


def detect_stack(project_path: Path) -> dict:
    """Detect tech-stack fingerprints for a project.

    Returns a dict keyed by relative directory path (`"."` for the project
    root, otherwise a POSIX-style relative path for monorepo sub-projects),
    each mapping to `{"languages": {...}, "frameworks": {...}, "package_manager": "..."}`.
    Returns `{}` if no recognised manifest is found or the path is invalid.
    Never raises.
    """
    try:
        root = Path(project_path)
        if not root.is_dir():
            return {}
    except Exception:
        return {}

    result: dict = {}
    root_stack = _detect_dir(root)
    if root_stack:
        result["."] = root_stack

    try:
        for child in sorted(root.iterdir()):
            if not child.is_dir() or _is_noise_dir(child.name):
                continue
            child_stack = _detect_dir(child)
            if child_stack:
                result[child.name] = child_stack
    except OSError:
        pass

    return result
