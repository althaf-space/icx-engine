"""API-layer runners: Schemathesis (schema-driven fuzz, deterministic - no AI) and Hurl (scripted
HTTP). Registered on import with category='api'. They test THROUGH the HTTP interface, so they are
identical across every backend language.

Adapters build the base command + report path; the executor injects the target base URL (confirmed
by the user at the LangGraph config gate) and runs it. No AI decides pass/fail - the schema and the
HTTP responses do.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from icx_engine.testing.runners.base import RunSpec, register_runner

_SCHEMA_NAMES = (
    "openapi.json", "openapi.yaml", "openapi.yml",
    "swagger.json", "swagger.yaml", "swagger.yml",
)


def _find_schema(repo: Path) -> str | None:
    for name in _SCHEMA_NAMES:
        p = repo / name
        if p.exists():
            return str(p)
    # common nested spots
    for sub in ("api", "docs", "openapi", "spec"):
        d = repo / sub
        if d.is_dir():
            for name in _SCHEMA_NAMES:
                p = d / name
                if p.exists():
                    return str(p)
    return None


@dataclass
class _Schemathesis:
    lang: str = "http"
    name: str = "schemathesis"
    category: str = "api"

    def detect(self, repo: Path) -> bool:
        return _find_schema(repo) is not None

    def build_command(self, repo: Path, runtime_path: str | None) -> RunSpec:
        schema = _find_schema(repo) or "openapi.json"
        report = str(repo / ".icx-api-junit.xml")
        return RunSpec(
            command=["schemathesis", "run", schema, f"--junit-xml={report}", "--checks", "all"],
            cwd=str(repo), report_path=report,
            note="executor appends --base-url=<user-confirmed target>; deterministic schema fuzz, no AI",
        )


@dataclass
class _Hurl:
    lang: str = "http"
    name: str = "hurl"
    category: str = "api"

    def _files(self, repo: Path) -> list[str]:
        return [str(p) for p in sorted(repo.rglob("*.hurl"))]

    def detect(self, repo: Path) -> bool:
        return bool(self._files(repo))

    def build_command(self, repo: Path, runtime_path: str | None) -> RunSpec:
        report = str(repo / ".icx-hurl-junit.xml")
        files = self._files(repo)
        return RunSpec(
            command=["hurl", "--test", "--report-junit", report, *files],
            cwd=str(repo), report_path=report,
            note="scripted HTTP; executor may pass --variable base=<target>",
        )


for _r in (_Schemathesis(), _Hurl()):
    register_runner(_r)
