"""Build-system dependency resolver for multi-module projects.

Detects inter-module dependency edges in:
  * Maven multi-module projects (parent pom.xml with <modules>, child pom.xml
    with <dependency> referencing sibling modules)
  * Gradle multi-project builds (settings.gradle/settings.gradle.kts with
    include directives, build.gradle/build.gradle.kts with project(':...')
    dependencies)

Emits "module_depends" edges between modules that depend on each other
within the same project.
"""
from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Iterable

from icx_engine.graph.parser.confidence import (
    FRAMEWORK_RESOLVED, annotate_edge,
)

_log = logging.getLogger(__name__)

# Gradle patterns for project dependencies:
#   implementation project(':module-name')
#   api project(':sub:module')
#   testImplementation project(":module")
#   compileOnly project(':module')
_GRADLE_PROJECT_DEP_RE = re.compile(
    r"""(?:implementation|api|compileOnly|runtimeOnly|"""
    r"""testImplementation|testRuntimeOnly|testCompileOnly)"""
    r"""\s*(?:\(\s*)?project\(\s*['"]:([\w:.-]+)['"]\s*\)""",
    re.VERBOSE,
)

# settings.gradle include patterns:
#   include ':module-a', ':module-b'
#   include(':module-a')
_GRADLE_INCLUDE_RE = re.compile(
    r"""include\s*\(?\s*['"]:([\w:.-]+)['"]""",
)

# Maven namespace
_MVN_NS = {"m": "http://maven.apache.org/POM/4.0.0"}


def extract_build_dep_edges(
    files: Iterable[Path],
    project_root: Path,
    ast_extraction: dict,
) -> list[dict]:
    """Extract inter-module dependency edges from Maven/Gradle build files."""
    project_root = project_root.resolve()
    node_index = _build_node_index(ast_extraction.get("nodes", []), project_root)

    edges: list[dict] = []

    # Attempt both Maven and Gradle detection; a project may use either or both.
    edges.extend(_extract_maven_edges(project_root, node_index))
    edges.extend(_extract_gradle_edges(project_root, node_index))

    return edges


# ---------------------------------------------------------------------------
# Maven multi-module detection
# ---------------------------------------------------------------------------

def _extract_maven_edges(project_root: Path, node_index: dict) -> list[dict]:
    """Parse Maven pom.xml files to find inter-module dependencies."""
    root_pom = project_root / "pom.xml"
    if not root_pom.is_file():
        return []

    # Discover declared modules from the root pom.
    modules = _parse_maven_modules(root_pom)
    if not modules:
        return []

    # Build a mapping of (groupId, artifactId) -> module_dir for each module.
    module_coords: dict[tuple[str, str], str] = {}
    parent_group_id = _parse_maven_group_id(root_pom)

    for mod_name in modules:
        mod_pom = project_root / mod_name / "pom.xml"
        if not mod_pom.is_file():
            continue
        group_id, artifact_id = _parse_maven_module_coords(mod_pom, parent_group_id)
        if artifact_id:
            module_coords[(group_id, artifact_id)] = mod_name

    if len(module_coords) < 2:
        return []

    # For each module, find dependencies on other modules in the same project.
    edges: list[dict] = []
    seen: set[tuple[str, str]] = set()

    for mod_name in modules:
        mod_pom = project_root / mod_name / "pom.xml"
        if not mod_pom.is_file():
            continue

        deps = _parse_maven_dependencies(mod_pom)
        source_node = _resolve_module_node(mod_name, project_root, node_index)
        if not source_node:
            continue

        for dep_group, dep_artifact in deps:
            target_mod = module_coords.get((dep_group, dep_artifact))
            if target_mod is None or target_mod == mod_name:
                continue
            target_node = _resolve_module_node(target_mod, project_root, node_index)
            if not target_node:
                continue

            key = (source_node, target_node)
            if key in seen:
                continue
            seen.add(key)

            rel_path = (Path(mod_name) / "pom.xml").as_posix()
            edge = {
                "relation": "module_depends",
                "source": source_node,
                "target": target_node,
                "source_file": rel_path,
                "source_location": "",
                "weight": 1.0,
            }
            annotate_edge(edge, FRAMEWORK_RESOLVED, "build_deps_resolver")
            edges.append(edge)

    return edges


def _parse_maven_modules(pom_path: Path) -> list[str]:
    """Extract <module> entries from a parent pom.xml."""
    try:
        tree = ET.parse(pom_path)
    except (ET.ParseError, OSError):
        return []

    root = tree.getroot()
    modules: list[str] = []

    # Try with namespace first, then without.
    for mod_el in root.findall(".//m:modules/m:module", _MVN_NS):
        text = (mod_el.text or "").strip()
        if text:
            modules.append(text)

    if not modules:
        for mod_el in root.findall(".//modules/module"):
            text = (mod_el.text or "").strip()
            if text:
                modules.append(text)

    return modules


def _parse_maven_group_id(pom_path: Path) -> str:
    """Extract the groupId from a pom.xml (project-level or parent-level)."""
    try:
        tree = ET.parse(pom_path)
    except (ET.ParseError, OSError):
        return ""

    root = tree.getroot()

    # Direct groupId
    for tag in ("m:groupId", "groupId"):
        ns = _MVN_NS if tag.startswith("m:") else {}
        el = root.find(tag, ns) if ns else root.find(tag)
        if el is not None and el.text:
            return el.text.strip()

    # Inherited from parent
    for prefix, ns in [("m:", _MVN_NS), ("", {})]:
        parent_el = root.find(f"{prefix}parent", ns) if ns else root.find("parent")
        if parent_el is not None:
            gid_el = parent_el.find(f"{prefix}groupId", ns) if ns else parent_el.find("groupId")
            if gid_el is not None and gid_el.text:
                return gid_el.text.strip()

    return ""


def _parse_maven_module_coords(
    pom_path: Path, parent_group_id: str
) -> tuple[str, str]:
    """Return (groupId, artifactId) for a module pom.xml."""
    try:
        tree = ET.parse(pom_path)
    except (ET.ParseError, OSError):
        return ("", "")

    root = tree.getroot()
    group_id = ""
    artifact_id = ""

    # Try namespaced, then plain.
    for prefix, ns in [("m:", _MVN_NS), ("", {})]:
        if not artifact_id:
            el = root.find(f"{prefix}artifactId", ns) if ns else root.find("artifactId")
            if el is not None and el.text:
                artifact_id = el.text.strip()
        if not group_id:
            el = root.find(f"{prefix}groupId", ns) if ns else root.find("groupId")
            if el is not None and el.text:
                group_id = el.text.strip()

    # Inherit groupId from parent element in this pom, or from the parent_group_id arg.
    if not group_id:
        for prefix, ns in [("m:", _MVN_NS), ("", {})]:
            parent_el = root.find(f"{prefix}parent", ns) if ns else root.find("parent")
            if parent_el is not None:
                gid_el = parent_el.find(f"{prefix}groupId", ns) if ns else parent_el.find("groupId")
                if gid_el is not None and gid_el.text:
                    group_id = gid_el.text.strip()
                    break

    if not group_id:
        group_id = parent_group_id

    return (group_id, artifact_id)


def _parse_maven_dependencies(pom_path: Path) -> list[tuple[str, str]]:
    """Return list of (groupId, artifactId) for all <dependency> elements."""
    try:
        tree = ET.parse(pom_path)
    except (ET.ParseError, OSError):
        return []

    root = tree.getroot()
    deps: list[tuple[str, str]] = []

    for prefix, ns in [("m:", _MVN_NS), ("", {})]:
        dep_path = f".//{prefix}dependencies/{prefix}dependency"
        found = root.findall(dep_path, ns) if ns else root.findall(dep_path)
        for dep_el in found:
            gid_el = dep_el.find(f"{prefix}groupId", ns) if ns else dep_el.find("groupId")
            aid_el = dep_el.find(f"{prefix}artifactId", ns) if ns else dep_el.find("artifactId")
            gid = (gid_el.text or "").strip() if gid_el is not None else ""
            aid = (aid_el.text or "").strip() if aid_el is not None else ""
            if gid and aid:
                deps.append((gid, aid))
        if deps:
            break  # Found with this namespace variant, no need to retry.

    return deps


# ---------------------------------------------------------------------------
# Gradle multi-project detection
# ---------------------------------------------------------------------------

def _extract_gradle_edges(project_root: Path, node_index: dict) -> list[dict]:
    """Parse Gradle settings and build files to find inter-module dependencies."""
    # Find settings file.
    settings_file = None
    for name in ("settings.gradle.kts", "settings.gradle"):
        candidate = project_root / name
        if candidate.is_file():
            settings_file = candidate
            break

    if settings_file is None:
        return []

    # Parse included modules from settings.
    modules = _parse_gradle_modules(settings_file)
    if not modules:
        return []

    # Build mapping from module name (last segment) to directory path.
    # Gradle uses ':' as separator for nested modules, e.g. ':sub:module' -> sub/module
    module_dirs: dict[str, str] = {}
    for mod in modules:
        # Convert ':sub:module' style to path 'sub/module'
        dir_path = mod.replace(":", "/")
        module_dirs[mod] = dir_path

    if len(module_dirs) < 2:
        return []

    edges: list[dict] = []
    seen: set[tuple[str, str]] = set()

    for mod_name, mod_dir in module_dirs.items():
        # Look for build.gradle or build.gradle.kts in the module directory.
        build_file = None
        for name in ("build.gradle.kts", "build.gradle"):
            candidate = project_root / mod_dir / name
            if candidate.is_file():
                build_file = candidate
                break

        if build_file is None:
            continue

        try:
            content = build_file.read_text(encoding="utf-8")
        except OSError:
            continue

        source_node = _resolve_module_node(mod_dir, project_root, node_index)
        if not source_node:
            continue

        # Find all project(':...') dependencies.
        for match in _GRADLE_PROJECT_DEP_RE.finditer(content):
            dep_ref = match.group(1)  # e.g. 'module-name' or 'sub:module'
            if dep_ref not in module_dirs:
                continue
            if dep_ref == mod_name:
                continue

            target_dir = module_dirs[dep_ref]
            target_node = _resolve_module_node(target_dir, project_root, node_index)
            if not target_node:
                continue

            key = (source_node, target_node)
            if key in seen:
                continue
            seen.add(key)

            rel_path = (Path(mod_dir) / build_file.name).as_posix()
            # Compute line number for source_location.
            line_num = content[:match.start()].count("\n") + 1
            edge = {
                "relation": "module_depends",
                "source": source_node,
                "target": target_node,
                "source_file": rel_path,
                "source_location": f"L{line_num}",
                "weight": 1.0,
            }
            annotate_edge(edge, FRAMEWORK_RESOLVED, "build_deps_resolver")
            edges.append(edge)

    return edges


def _parse_gradle_modules(settings_file: Path) -> list[str]:
    """Extract module names from settings.gradle(.kts)."""
    try:
        content = settings_file.read_text(encoding="utf-8")
    except OSError:
        return []

    modules: list[str] = []
    for match in _GRADLE_INCLUDE_RE.finditer(content):
        mod_name = match.group(1)
        if mod_name:
            modules.append(mod_name)

    return modules


# ---------------------------------------------------------------------------
# Node resolution helpers
# ---------------------------------------------------------------------------

def _resolve_module_node(
    module_dir: str, project_root: Path, node_index: dict
) -> str | None:
    """Map a module directory to the best matching node in the graph.

    Strategy:
      1. Look for a file node that matches the module's build file (pom.xml,
         build.gradle, build.gradle.kts).
      2. Look for any file node whose path starts with the module directory.
      3. Fall back to the directory name as a symbol lookup.
    """
    by_file = node_index["by_file"]
    by_symbol = node_index["by_symbol"]

    # Normalize path separators.
    mod_dir_posix = module_dir.replace("\\", "/")

    # Try build file nodes directly.
    for build_name in ("pom.xml", "build.gradle.kts", "build.gradle"):
        candidate = f"{mod_dir_posix}/{build_name}" if mod_dir_posix else build_name
        if candidate in by_file:
            return by_file[candidate]

    # Try any file in that module directory.
    for file_path, node_id in by_file.items():
        if file_path.startswith(mod_dir_posix + "/"):
            return node_id

    # Try symbol lookup using the last directory segment.
    last_segment = mod_dir_posix.rsplit("/", 1)[-1] if "/" in mod_dir_posix else mod_dir_posix
    for (path, sym), nid in by_symbol.items():
        if sym == last_segment.lower():
            return nid

    return None


def _build_node_index(nodes: list[dict], project_root: Path) -> dict[str, dict]:
    """Build an index of nodes by file path and by symbol name.

    Mirrors the pattern used by other resolvers (jaxrs, spring, etc.).
    """
    project_str = str(project_root).replace("\\", "/")
    by_file: dict[str, str] = {}
    by_symbol: dict[tuple[str, str], str] = {}

    for n in nodes:
        nid = n.get("id") or n.get("label")
        if not nid:
            continue
        src_file = (n.get("source_file") or "").replace("\\", "/").strip()
        label = (n.get("label") or "").strip()
        if not src_file:
            continue

        # Compute relative path from project root.
        if src_file.startswith(project_str + "/"):
            rel = src_file[len(project_str) + 1:]
        elif src_file.startswith(project_str):
            rel = src_file[len(project_str):].lstrip("/")
        else:
            rel = src_file

        # File-level nodes: label matches filename.
        fname = Path(rel).name
        if label == fname or label.lower() == fname.lower():
            by_file.setdefault(rel, nid)
            continue

        # Symbol-level nodes.
        symbol = label
        if symbol.endswith("()"):
            symbol = symbol[:-2]
        symbol = symbol.lstrip(".").lower()
        if symbol:
            by_symbol.setdefault((rel, symbol), nid)

    return {"by_file": by_file, "by_symbol": by_symbol}
