"""Normalized test-report model + unit-runner plugin registry.

Mirrors ICX's connector/provider registry pattern (`register_connector`/`register_provider`): a
language runner is registered by name; adding a language never requires editing core.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass
class TestCase:
    name: str
    classname: str = ""
    status: str = "passed"      # passed | failed | error | skipped
    message: str = ""
    time: float = 0.0


@dataclass
class TestReport:
    """Normalized result of any runner, derived from JUnit XML (the universal spine)."""
    total: int = 0
    passed: int = 0
    failures: int = 0
    errors: int = 0
    skipped: int = 0
    time: float = 0.0
    cases: list[TestCase] = field(default_factory=list)
    raw_path: str = ""

    @property
    def ok(self) -> bool:
        """A report is a pass only when nothing failed or errored and at least one test ran."""
        return self.total > 0 and self.failures == 0 and self.errors == 0


@dataclass
class RunSpec:
    """How to run a language's unit tests so they emit JUnit XML."""
    command: list[str]
    cwd: str
    report_path: str            # where the JUnit XML will be written
    env: dict = field(default_factory=dict)
    note: str = ""              # optional caveat (e.g. requires a JUnit-XML bridge tool)


@runtime_checkable
class UnitRunner(Protocol):
    lang: str
    name: str
    def detect(self, repo: Path) -> bool: ...
    def build_command(self, repo: Path, runtime_path: str | None) -> RunSpec: ...


_RUNNERS: dict[str, UnitRunner] = {}


def register_runner(runner: UnitRunner) -> None:
    """Register (or override) a unit runner by its unique name."""
    _RUNNERS[runner.name] = runner


def get_runner(name: str) -> UnitRunner | None:
    return _RUNNERS.get(name)


def list_runners() -> list[UnitRunner]:
    return list(_RUNNERS.values())


def detect_runners(repo, category: str | None = None) -> list[UnitRunner]:
    """Return every registered runner whose detect() matches this repo (order-stable).

    Optional category filter ("unit" | "api" | "security"); runners without an explicit category
    attribute default to "unit", so existing adapters are unaffected.
    """
    repo = Path(repo)
    out: list[UnitRunner] = []
    for r in _RUNNERS.values():
        if category is not None and getattr(r, "category", "unit") != category:
            continue
        try:
            if r.detect(repo):
                out.append(r)
        except Exception:
            pass
    return out
