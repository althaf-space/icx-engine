"""C/C++ (ctest) and SQL stored-routine runner adapters."""
from __future__ import annotations

from pathlib import Path

from icx_engine.testing.runners.systems import _Ctest, _SqlRoutines


# -- C/C++ ctest ---------------------------------------------------------------

def test_ctest_detects_cmake_with_gtest(tmp_path):
    (tmp_path / "CMakeLists.txt").write_text(
        "project(x)\nenable_testing()\nadd_test(NAME t COMMAND t)\n# uses gtest\n", encoding="utf-8")
    assert _Ctest().detect(tmp_path) is True


def test_ctest_no_cmake_no_detect(tmp_path):
    (tmp_path / "main.cpp").write_text("int main(){return 0;}", encoding="utf-8")
    assert _Ctest().detect(tmp_path) is False


def test_ctest_detects_configured_build_tree(tmp_path):
    (tmp_path / "CMakeLists.txt").write_text("project(x)\n", encoding="utf-8")
    bd = tmp_path / "build"
    bd.mkdir()
    (bd / "CTestTestfile.cmake").write_text("add_test(t t)\n", encoding="utf-8")
    r = _Ctest()
    assert r.detect(tmp_path) is True
    spec = r.build_command(tmp_path, None)
    assert spec.command[0] == "ctest"
    assert "--output-junit" in spec.command
    assert str(bd) in spec.command                      # runs against the build dir
    assert spec.report_path.endswith(".icx-ctest-junit.xml")


def test_ctest_command_shape(tmp_path):
    (tmp_path / "CMakeLists.txt").write_text("enable_testing()\n", encoding="utf-8")
    spec = _Ctest().build_command(tmp_path, None)
    assert spec.command[0] == "ctest" and "--test-dir" in spec.command
    assert not getattr(_Ctest(), "requires", None)      # ctest is a discovered SDK, not ICX-installed


# -- SQL routines --------------------------------------------------------------

def _write_sql(tmp_path):
    (tmp_path / "test_pkg.sql").write_text(
        "-- %suite\n-- %test\nbegin ut.expect(1).to_equal(1); end;\n", encoding="utf-8")


def test_sql_no_detect_without_config(tmp_path, monkeypatch):
    monkeypatch.delenv("ICX_SQL_TEST_CMD", raising=False)
    monkeypatch.delenv("ICX_SQL_DIALECT", raising=False)
    monkeypatch.delenv("ICX_SQL_CONN", raising=False)
    _write_sql(tmp_path)
    # test files present but no DB config -> not applicable (never guesses a database)
    assert _SqlRoutines().detect(tmp_path) is False


def test_sql_detects_with_explicit_cmd(tmp_path, monkeypatch):
    _write_sql(tmp_path)
    monkeypatch.setenv("ICX_SQL_TEST_CMD", "pg_prove -r tests/")
    r = _SqlRoutines()
    assert r.detect(tmp_path) is True
    spec = r.build_command(tmp_path, None)
    assert spec.command[:2] == ["pg_prove", "-r"]
    assert spec.env["ICX_SQL_REPORT"] == spec.report_path
    assert spec.report_path.endswith(".icx-sql-junit.xml")


def test_sql_utplsql_convenience(tmp_path, monkeypatch):
    _write_sql(tmp_path)
    monkeypatch.delenv("ICX_SQL_TEST_CMD", raising=False)
    monkeypatch.setenv("ICX_SQL_DIALECT", "oracle")
    monkeypatch.setenv("ICX_SQL_CONN", "u/p@db")
    r = _SqlRoutines()
    assert r.detect(tmp_path) is True
    spec = r.build_command(tmp_path, None)
    assert spec.command[0] == "utplsql" and "u/p@db" in spec.command
    assert any("-f=ju" in c for c in spec.command)       # JUnit output format


def test_sql_config_without_testfiles_no_detect(tmp_path, monkeypatch):
    monkeypatch.setenv("ICX_SQL_TEST_CMD", "x")
    # no SQL test files -> not applicable
    assert _SqlRoutines().detect(tmp_path) is False


def test_all_systems_runners_registered():
    from icx_engine.testing.runners.base import list_runners
    names = {r.name for r in list_runners()}
    assert {"ctest", "sql-routines", "grpc", "terraform"} <= names


# -- gRPC ----------------------------------------------------------------------

def test_grpc_no_detect_without_cmd(tmp_path, monkeypatch):
    monkeypatch.delenv("ICX_GRPC_TEST_CMD", raising=False)
    (tmp_path / "svc.proto").write_text('service S { rpc Get(Req) returns (Res); }', encoding="utf-8")
    from icx_engine.testing.runners.systems import _Grpc
    assert _Grpc().detect(tmp_path) is False


def test_grpc_detects_with_cmd_and_proto(tmp_path, monkeypatch):
    (tmp_path / "svc.proto").write_text('service S { rpc Get(Req) returns (Res); }', encoding="utf-8")
    monkeypatch.setenv("ICX_GRPC_TEST_CMD", "ghz --insecure --proto svc.proto")
    from icx_engine.testing.runners.systems import _Grpc
    r = _Grpc()
    assert r.detect(tmp_path) is True
    spec = r.build_command(tmp_path, None)
    assert spec.command[0] == "ghz"
    assert spec.env["ICX_GRPC_REPORT"] == spec.report_path


# -- Terraform -----------------------------------------------------------------

def test_terraform_no_detect_without_cmd(tmp_path, monkeypatch):
    monkeypatch.delenv("ICX_IAC_TEST_CMD", raising=False)
    (tmp_path / "main.tf").write_text('resource "null_resource" "x" {}', encoding="utf-8")
    from icx_engine.testing.runners.systems import _Terraform
    assert _Terraform().detect(tmp_path) is False


def test_terraform_detects_with_cmd(tmp_path, monkeypatch):
    (tmp_path / "main.tf").write_text('resource "null_resource" "x" {}', encoding="utf-8")
    monkeypatch.setenv("ICX_IAC_TEST_CMD", "checkov -d . -o junitxml")
    from icx_engine.testing.runners.systems import _Terraform
    r = _Terraform()
    assert r.detect(tmp_path) is True
    spec = r.build_command(tmp_path, None)
    assert spec.command[0] == "checkov"
    assert spec.env["ICX_IAC_REPORT"] == spec.report_path
