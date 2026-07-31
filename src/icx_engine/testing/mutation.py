"""Mutation-testing filter for AI-drafted unit tests.

An AI-drafted unit test can hit lines and pass while asserting nothing meaningful ("coverage lies").
Mutation testing is the deterministic filter: mutate the code under test; if the draft STILL passes,
it caught nothing and is rejected as worthless; if it FAILS on a mutant, it genuinely verifies.

Tools per language: mutmut (Python), Stryker (JS/TS/C#), PIT (Java/Kotlin), Infection (PHP).

This module: selects the tool, builds the command, parses each tool's report into a normalized
MutationResult, and evaluates the gate. Real mutation runs happen in the executor; parsing + the
gate logic are pure and tested here.
"""
from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from icx_engine.testing.runners.base import RunSpec

# Hard floor: a draft that kills nothing is always rejected. Quality bar on top is configurable.
DEFAULT_MIN_MUTATION_SCORE = 0.6

_MUTATION_TOOLS = {
    "python": "mutmut",
    "js-ts": "stryker",
    "javascript": "stryker",
    "typescript": "stryker",
    "csharp": "stryker",
    "java": "pit",
    "kotlin": "pit",
    "php": "infection",
}


@dataclass
class MutationResult:
    tool: str
    total: int = 0
    killed: int = 0
    survived: int = 0

    @property
    def score(self) -> float:
        return round(self.killed / self.total, 3) if self.total else 0.0

    @property
    def meaningful(self) -> bool:
        """A draft is meaningful only if it killed at least one mutant."""
        return self.killed > 0


def select_mutation_tool(lang: str) -> str | None:
    return _MUTATION_TOOLS.get(lang.lower())


def build_mutation_command(lang: str, repo, runtime_path: str | None, target: str | None = None) -> RunSpec | None:
    """Build the mutation-testing command for a language, or None if unsupported. Report path is
    where the tool writes its machine-readable result for parsing."""
    tool = select_mutation_tool(lang)
    if tool is None:
        return None
    repo = str(repo)
    if tool == "mutmut":
        return RunSpec(command=["mutmut", "run"] + (["--paths-to-mutate", target] if target else []),
                       cwd=repo, report_path="mutmut-results",
                       note="parse via 'mutmut results'; no native file report")
    if tool == "stryker":
        report = str(Path(repo) / "reports" / "mutation" / "mutation-report.json")
        return RunSpec(command=["npx", "stryker", "run", "--reporters", "json"],
                       cwd=repo, report_path=report,
                       note="Stryker JSON report")
    if tool == "pit":
        env = {"JAVA_HOME": runtime_path} if runtime_path else {}
        return RunSpec(command=["mvn", "-q", "org.pitest:pitest-maven:mutationCoverage"],
                       cwd=repo, report_path=str(Path(repo) / "target" / "pit-reports" / "mutations.xml"),
                       env=env, note="PIT mutations.xml")
    if tool == "infection":
        report = str(Path(repo) / "infection.json")
        return RunSpec(command=["vendor/bin/infection", "--logger-json=infection.json"],
                       cwd=repo, report_path=report,
                       note="Infection JSON log")
    return None


def parse_mutmut(text: str) -> MutationResult:
    """Parse a mutmut summary like 'killed: 8, survived: 2' (order-independent)."""
    killed = _int(re.search(r"killed[^0-9]*([0-9]+)", text, re.IGNORECASE))
    survived = _int(re.search(r"survived[^0-9]*([0-9]+)", text, re.IGNORECASE))
    return MutationResult(tool="mutmut", killed=killed, survived=survived, total=killed + survived)


def parse_stryker(json_text: str) -> MutationResult:
    """Parse a Stryker JSON report; sum Killed / Survived across all files' mutants."""
    killed = survived = 0
    try:
        data = json.loads(json_text)
    except json.JSONDecodeError:
        return MutationResult(tool="stryker")
    files = (data.get("files") or {})
    for f in files.values():
        for m in (f.get("mutants") or []):
            status = str(m.get("status", "")).lower()
            if status == "killed":
                killed += 1
            elif status == "survived":
                survived += 1
    return MutationResult(tool="stryker", killed=killed, survived=survived, total=killed + survived)


def parse_pit(xml_text: str) -> MutationResult:
    """Parse a PIT mutations.xml; <mutation detected='true' status='KILLED'>."""
    killed = survived = 0
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return MutationResult(tool="pit")
    for m in root.iter("mutation"):
        status = (m.get("status") or "").upper()
        detected = (m.get("detected") or "").lower() == "true"
        if status == "KILLED" or detected:
            killed += 1
        elif status in ("SURVIVED", "NO_COVERAGE"):
            survived += 1
    return MutationResult(tool="pit", killed=killed, survived=survived, total=killed + survived)


def evaluate_mutation(result: MutationResult, min_score: float = DEFAULT_MIN_MUTATION_SCORE) -> tuple[bool, str]:
    """Gate an AI-draft test by its mutation result.

    Rejected if it killed zero mutants (verifies nothing) or its score is below min_score.
    Returns (passed, reason).
    """
    if result.total == 0:
        return False, "no mutants generated - cannot verify the draft catches anything"
    if not result.meaningful:
        return False, "draft killed 0 mutants - it verifies nothing (rejected)"
    if result.score < min_score:
        return False, f"mutation score {result.score} below minimum {min_score}"
    return True, f"mutation score {result.score} (killed {result.killed}/{result.total})"


def _int(m) -> int:
    try:
        return int(m.group(1)) if m else 0
    except (ValueError, AttributeError):
        return 0
