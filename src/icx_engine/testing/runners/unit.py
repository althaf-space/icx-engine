"""Wave-1 per-language unit-runner adapters. Registered on import.

Each adapter: detect(repo) -> is this the language's project? and build_command(repo, runtime) ->
a RunSpec whose command emits JUnit XML. Real subprocess execution is the executor's job (later
phase); adapters only decide WHAT to run. Where a language needs a JUnit-XML bridge tool, RunSpec.note
records it.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from icx_engine.testing.runners.base import RunSpec, register_runner


def _pkg_json(repo: Path) -> dict:
    try:
        return json.loads((repo / "package.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _all_deps(pkg: dict) -> dict:
    out = {}
    out.update(pkg.get("dependencies") or {})
    out.update(pkg.get("devDependencies") or {})
    return out


@dataclass
class _Pytest:
    lang: str = "python"
    name: str = "pytest"

    def detect(self, repo: Path) -> bool:
        if (repo / "pytest.ini").exists() or (repo / "conftest.py").exists():
            return True
        py = ""
        try:
            py = (repo / "pyproject.toml").read_text(encoding="utf-8")
        except OSError:
            pass
        if "[tool.pytest" in py or "pytest" in py:
            return True
        return (repo / "tests").is_dir() and any(repo.glob("**/test_*.py"))

    def build_command(self, repo: Path, runtime_path: str | None) -> RunSpec:
        report = str(repo / ".icx-junit.xml")
        py = runtime_path or "python"
        return RunSpec(
            command=[py, "-m", "pytest", f"--junitxml={report}", "-q"],
            cwd=str(repo), report_path=report,
        )


@dataclass
class _Vitest:
    lang: str = "js-ts"
    name: str = "vitest"

    def detect(self, repo: Path) -> bool:
        return "vitest" in _all_deps(_pkg_json(repo))

    def build_command(self, repo: Path, runtime_path: str | None) -> RunSpec:
        report = str(repo / ".icx-junit.xml")
        return RunSpec(
            command=["npx", "vitest", "run", "--reporter=junit", f"--outputFile={report}"],
            cwd=str(repo), report_path=report,
        )


@dataclass
class _Jest:
    lang: str = "js-ts"
    name: str = "jest"

    def detect(self, repo: Path) -> bool:
        pkg = _pkg_json(repo)
        return "jest" in _all_deps(pkg) or "jest" in pkg

    def build_command(self, repo: Path, runtime_path: str | None) -> RunSpec:
        report_dir = str(repo)
        return RunSpec(
            command=["npx", "jest", "--reporters=default", "--reporters=jest-junit"],
            cwd=str(repo), report_path=str(repo / "junit.xml"),
            env={"JEST_JUNIT_OUTPUT_DIR": report_dir, "JEST_JUNIT_OUTPUT_NAME": "junit.xml"},
            note="requires the jest-junit reporter package",
        )


@dataclass
class _MavenJUnit:
    lang: str = "java"
    name: str = "junit-maven"

    def detect(self, repo: Path) -> bool:
        return (repo / "pom.xml").exists()

    def build_command(self, repo: Path, runtime_path: str | None) -> RunSpec:
        env = {"JAVA_HOME": runtime_path} if runtime_path else {}
        return RunSpec(
            command=["mvn", "-q", "test"],
            cwd=str(repo),
            report_path=str(repo / "target" / "surefire-reports"),
            env=env,
            note="Surefire writes target/surefire-reports/*.xml (JUnit XML)",
        )


@dataclass
class _GradleJUnit:
    """Covers both Java and Kotlin Gradle projects."""
    lang: str = "java"
    name: str = "junit-gradle"

    def detect(self, repo: Path) -> bool:
        return (repo / "build.gradle").exists() or (repo / "build.gradle.kts").exists()

    def build_command(self, repo: Path, runtime_path: str | None) -> RunSpec:
        env = {"JAVA_HOME": runtime_path} if runtime_path else {}
        wrapper = "./gradlew"
        if not (repo / "gradlew").exists():
            wrapper = "gradle"
        return RunSpec(
            command=[wrapper, "test"],
            cwd=str(repo),
            report_path=str(repo / "build" / "test-results" / "test"),
            env=env,
            note="Gradle writes build/test-results/test/*.xml (JUnit XML)",
        )


@dataclass
class _Go:
    lang: str = "go"
    name: str = "go"

    def detect(self, repo: Path) -> bool:
        return (repo / "go.mod").exists()

    def build_command(self, repo: Path, runtime_path: str | None) -> RunSpec:
        report = str(repo / ".icx-junit.xml")
        return RunSpec(
            command=["gotestsum", f"--junitfile={report}", "--", "./..."],
            cwd=str(repo), report_path=report,
            note="go test has no native JUnit XML; gotestsum bridges it",
        )


@dataclass
class _Cargo:
    lang: str = "rust"
    name: str = "cargo"

    def detect(self, repo: Path) -> bool:
        return (repo / "Cargo.toml").exists()

    def build_command(self, repo: Path, runtime_path: str | None) -> RunSpec:
        report = str(repo / ".icx-junit.xml")
        return RunSpec(
            command=["cargo", "nextest", "run", "--profile", "ci"],
            cwd=str(repo), report_path=report,
            note="cargo nextest (or cargo2junit) produces JUnit XML; plain cargo test does not",
        )


for _r in (_Pytest(), _Vitest(), _Jest(), _MavenJUnit(), _GradleJUnit(), _Go(), _Cargo()):
    register_runner(_r)
