from __future__ import annotations

from dataclasses import dataclass, field

from icx_engine.testing.classify import FileClass

_UI_MODES = {"ui", "agent"}


@dataclass
class CompatVerdict:
    path: str
    compatible: bool
    reasons: list[str] = field(default_factory=list)
    required_changes: list[str] = field(default_factory=list)


def check_compat(fc: FileClass, mode: str) -> CompatVerdict:
    reasons: list[str] = []
    changes: list[str] = []

    if mode in _UI_MODES:
        if fc.layer == "backend":
            reasons.append(f"{fc.path} is a backend file; not testable in a UI run")
            changes.append("Drop this file, or run it under API mode instead")
            return CompatVerdict(fc.path, False, reasons, changes)
        if fc.layer not in ("frontend", "shared"):
            reasons.append(f"{fc.path} is not a renderable UI file")
            changes.append("Drop this file, or point the run at a UI component")
            return CompatVerdict(fc.path, False, reasons, changes)
        t = fc.testability
        if not t.get("renderable", False) and fc.layer == "shared":
            reasons.append(f"{fc.path} has no renderable UI markup")
            changes.append("Confirm this file renders UI, or drop it")
            return CompatVerdict(fc.path, False, reasons, changes)
        advisory: list[str] = []
        if not t.get("has_route", False):
            advisory.append(f"{fc.path} is not wired to a route; add a route so Playwright can reach it")
        if not t.get("has_stable_selector", False):
            changes.append(f"{fc.path} lacks stable selectors; add a data-testid to interactive controls")
        return CompatVerdict(fc.path, len(changes) == 0, reasons, advisory + changes)

    if mode == "api":
        if fc.layer == "frontend":
            reasons.append(f"{fc.path} is a frontend file; not testable in an API run")
            changes.append("Drop this file, or run it under UI mode instead")
            return CompatVerdict(fc.path, False, reasons, changes)
        t = fc.testability
        if not t.get("exposes_endpoint", False):
            reasons.append(f"{fc.path} exposes no HTTP endpoint")
            changes.append(f"Expose a method+path on {fc.path}, or drop it")
            return CompatVerdict(fc.path, False, reasons, changes)
        if not t.get("has_request_schema", False):
            changes.append(f"{fc.path} has no derivable request schema; add a typed request body")
        return CompatVerdict(fc.path, len(changes) == 0, reasons, changes)

    return CompatVerdict(fc.path, False, [f"unknown mode {mode!r}"], [])


def build_report(classified: list[FileClass], mode: str) -> list[CompatVerdict]:
    return [check_compat(fc, mode) for fc in classified]
