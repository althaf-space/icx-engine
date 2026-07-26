"""Systems & data runner adapters: C/C++ (ctest) and SQL stored-routine testing.

These execute the output of the C/C++ and SQL analyzer census prompts. Both register under the
"unit" category (the routine/function test surface), so `test_type=unit` picks them up alongside the
language unit runners - each adapter's `detect()` gates on project markers so only the right one
fires.

C/C++  -> ctest (CMake >= 3.21 `--output-junit`), which is the umbrella for GoogleTest AND Catch2
          tests registered via CMake `add_test`. The project must already be built; ctest runs the
          registered tests. ctest is a user SDK (discovered, never ICX-installed) - no `requires`.
SQL    -> DB-routine frameworks (utPLSQL, tSQLt, pgTAP). ICX cannot own DB credentials, so this
          activates ONLY when the user provides connection config via env:
            ICX_SQL_TEST_CMD   - explicit runner command; ICX sets ICX_SQL_REPORT to the JUnit path
                                 the command must write (works for ANY framework), OR
            ICX_SQL_DIALECT=oracle + ICX_SQL_CONN=user/pass@dsn - utPLSQL convenience.
          With neither set, `detect()` returns False and the SQL layer reports cleanly as
          "no runner detected" rather than guessing at a database.
"""
from __future__ import annotations

import os
import shlex
from dataclasses import dataclass
from pathlib import Path

from icx_engine.testing.runners.base import RunSpec, register_runner


# -- C / C++ (ctest) -----------------------------------------------------------

_CTEST_MARKERS = ("gtest", "catch2", "catch.hpp", "catch_amalgamated", "add_test(", "enable_testing(")


@dataclass
class _Ctest:
    lang: str = "cpp"
    name: str = "ctest"
    category: str = "unit"

    def _build_dir(self, repo: Path) -> Path:
        for d in ("build", "cmake-build-debug", "out/build"):
            p = repo / d
            if (p / "CTestTestfile.cmake").exists() or p.is_dir():
                return p
        return repo

    def detect(self, repo: Path) -> bool:
        cml = repo / "CMakeLists.txt"
        if not cml.exists():
            return False
        try:
            txt = cml.read_text(encoding="utf-8", errors="ignore").lower()
        except OSError:
            txt = ""
        if any(m in txt for m in _CTEST_MARKERS):
            return True
        # a configured build tree with registered tests also qualifies
        return (self._build_dir(repo) / "CTestTestfile.cmake").exists()

    def build_command(self, repo: Path, runtime_path: str | None) -> RunSpec:
        report = str(repo / ".icx-ctest-junit.xml")
        test_dir = str(self._build_dir(repo))
        return RunSpec(
            command=["ctest", "--test-dir", test_dir, "--output-junit", report,
                     "--output-on-failure", "--no-tests=error"],
            cwd=str(repo), report_path=report,
            note=("ctest --output-junit (CMake >= 3.21) runs the GoogleTest/Catch2 tests registered "
                  "via add_test; the project must already be built. ctest is a discovered user SDK."),
        )


# -- SQL stored routines (utPLSQL / tSQLt / pgTAP) -----------------------------

_SQL_TEST_MARKERS = ("--%test", "%test", "tsqlt.", "pg_prove", "plan(", "ut.run", "utplsql")


def _sql_configured() -> bool:
    return bool(os.environ.get("ICX_SQL_TEST_CMD") or
                (os.environ.get("ICX_SQL_DIALECT") and os.environ.get("ICX_SQL_CONN")))


@dataclass
class _SqlRoutines:
    lang: str = "sql"
    name: str = "sql-routines"
    category: str = "unit"

    def _test_files(self, repo: Path) -> list[Path]:
        found = []
        for ext in ("*.sql", "*.pks", "*.pkb", "*.tsql"):
            for p in repo.rglob(ext):
                try:
                    t = p.read_text(encoding="utf-8", errors="ignore").lower()
                except OSError:
                    continue
                if any(m in t for m in _SQL_TEST_MARKERS):
                    found.append(p)
                    if len(found) >= 50:
                        return found
        return found

    def detect(self, repo: Path) -> bool:
        # Only when the user has supplied DB connection config - ICX never guesses a database.
        return _sql_configured() and bool(self._test_files(repo))

    def build_command(self, repo: Path, runtime_path: str | None) -> RunSpec:
        report = str(repo / ".icx-sql-junit.xml")
        env = {"ICX_SQL_REPORT": report}
        explicit = os.environ.get("ICX_SQL_TEST_CMD")
        if explicit:
            # The user's command writes JUnit to $ICX_SQL_REPORT - works for any framework.
            cmd = shlex.split(explicit, posix=(os.name != "nt"))
            note = "user ICX_SQL_TEST_CMD; must write JUnit XML to $ICX_SQL_REPORT"
        else:
            # utPLSQL convenience for Oracle: `utplsql run <conn> -f=ju -o=<report>`.
            conn = os.environ.get("ICX_SQL_CONN", "")
            cmd = ["utplsql", "run", conn, "-f=ju", f"-o={report}"]
            note = "utPLSQL-cli JUnit format; set ICX_SQL_TEST_CMD to use tSQLt/pgTAP instead"
        return RunSpec(command=cmd, cwd=str(repo), report_path=report, env=env, note=note)


# -- gRPC services (grpcurl / ghz / buf) ---------------------------------------
#
# gRPC functional testing calls a RUNNING service, so - like SQL - ICX cannot own the connection.
# Activates only when the user supplies ICX_GRPC_TEST_CMD (writes JUnit to $ICX_GRPC_REPORT), so a
# bare proto tree never fires a runner that has nothing to talk to.
@dataclass
class _Grpc:
    lang: str = "grpc"
    name: str = "grpc"
    category: str = "unit"

    def _protos(self, repo: Path) -> list[Path]:
        return list(repo.rglob("*.proto"))[:50]

    def detect(self, repo: Path) -> bool:
        return bool(os.environ.get("ICX_GRPC_TEST_CMD")) and bool(self._protos(repo))

    def build_command(self, repo: Path, runtime_path: str | None) -> RunSpec:
        report = str(repo / ".icx-grpc-junit.xml")
        cmd = shlex.split(os.environ.get("ICX_GRPC_TEST_CMD", ""), posix=(os.name != "nt"))
        return RunSpec(command=cmd, cwd=str(repo), report_path=report,
                       env={"ICX_GRPC_REPORT": report},
                       note="user ICX_GRPC_TEST_CMD (grpcurl/ghz/buf) must write JUnit to $ICX_GRPC_REPORT")


# -- Terraform / IaC (checkov / tflint / terraform validate) -------------------
#
# IaC validation frameworks and their JUnit emitters vary, so ICX takes the runner command from
# ICX_IAC_TEST_CMD (writing JUnit to $ICX_IAC_REPORT). Detected only when *.tf files exist AND the
# command is configured - never guesses a tool chain.
@dataclass
class _Terraform:
    lang: str = "terraform"
    name: str = "terraform"
    category: str = "unit"

    def detect(self, repo: Path) -> bool:
        return bool(os.environ.get("ICX_IAC_TEST_CMD")) and any(repo.rglob("*.tf"))

    def build_command(self, repo: Path, runtime_path: str | None) -> RunSpec:
        report = str(repo / ".icx-iac-junit.xml")
        cmd = shlex.split(os.environ.get("ICX_IAC_TEST_CMD", ""), posix=(os.name != "nt"))
        return RunSpec(command=cmd, cwd=str(repo), report_path=report,
                       env={"ICX_IAC_REPORT": report},
                       note="user ICX_IAC_TEST_CMD (checkov -o junitxml / tflint --format junit) writes to $ICX_IAC_REPORT")


for _r in (_Ctest(), _SqlRoutines(), _Grpc(), _Terraform()):
    register_runner(_r)
