from __future__ import annotations

from pathlib import PurePosixPath

_FUNC_KEYWORDS = ("create", "edit", "update", "modify", "delete", "search", "view", "list")


def _screen_name(path: str) -> str:
    return PurePosixPath(path.replace("\\", "/")).stem


def _functionality(name: str) -> str:
    low = name.lower()
    for kw in _FUNC_KEYWORDS:
        if kw in low:
            return "modify" if kw in ("edit", "update", "modify") else kw
    return "view"


def build_profile_markdown(classified: list[dict], project_name: str, base_url: str) -> str:
    lines: list[str] = [
        f"# Project: {project_name}",
        "",
        "## Description",
        f"Auto-generated profile for {project_name} testing scope.",
        "",
        "## Base URL",
        base_url,
        "",
        "## Screens",
        "",
    ]
    for fc in classified:
        if fc.get("layer") != "frontend":
            continue
        if "component" not in fc.get("artifacts", []) and fc.get("role") not in ("container", "component"):
            continue
        name = _screen_name(fc["path"])
        lines.append(f"### Screen: {name}")
        lines.append(f"- functionality: {_functionality(name)}")
        lines.append(f"- source: {fc['path']}")
        lines.append("")
    return "\n".join(lines)
