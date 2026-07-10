"""Tests for the Runtime Manager (per-repo runtime detection + registry, never installs)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from icx_engine import runtime_manager as rm


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    monkeypatch.setattr(rm, "_home", lambda: tmp_path)
    return tmp_path


# -- Task 1: per-language detection --------------------------------------------

def test_detect_java_java_version_file(tmp_path):
    (tmp_path / ".java-version").write_text("17\n", encoding="utf-8")
    assert rm.detect_required_runtime("java", tmp_path) == "17"


def test_detect_java_sdkmanrc(tmp_path):
    (tmp_path / ".sdkmanrc").write_text("java=17.0.9-tem\n", encoding="utf-8")
    assert rm.detect_required_runtime("java", tmp_path) == "17.0.9-tem"


def test_detect_java_maven_release(tmp_path):
    (tmp_path / "pom.xml").write_text(
        "<project><properties><maven.compiler.release>21</maven.compiler.release>"
        "</properties></project>", encoding="utf-8")
    assert rm.detect_required_runtime("java", tmp_path) == "21"


def test_detect_node_nvmrc(tmp_path):
    (tmp_path / ".nvmrc").write_text("v20.11.0\n", encoding="utf-8")
    assert rm.detect_required_runtime("node", tmp_path) == "20.11.0"


def test_detect_node_package_engines(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"engines": {"node": ">=18"}}), encoding="utf-8")
    assert rm.detect_required_runtime("node", tmp_path) == ">=18"


def test_detect_python_version_file(tmp_path):
    (tmp_path / ".python-version").write_text("3.11.4\n", encoding="utf-8")
    assert rm.detect_required_runtime("python", tmp_path) == "3.11.4"


def test_detect_python_pyproject_requires(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project]\nrequires-python = ">=3.10"\n', encoding="utf-8")
    assert rm.detect_required_runtime("python", tmp_path) == ">=3.10"


def test_detect_go_mod(tmp_path):
    (tmp_path / "go.mod").write_text("module x\n\ngo 1.22\n", encoding="utf-8")
    assert rm.detect_required_runtime("go", tmp_path) == "1.22"


def test_detect_rust_toolchain(tmp_path):
    (tmp_path / "rust-toolchain.toml").write_text('[toolchain]\nchannel = "1.75.0"\n', encoding="utf-8")
    assert rm.detect_required_runtime("rust", tmp_path) == "1.75.0"


def test_detect_dotnet_global_json(tmp_path):
    (tmp_path / "global.json").write_text(json.dumps({"sdk": {"version": "8.0.100"}}), encoding="utf-8")
    assert rm.detect_required_runtime("dotnet", tmp_path) == "8.0.100"


def test_detect_ruby_version(tmp_path):
    (tmp_path / ".ruby-version").write_text("3.2.2\n", encoding="utf-8")
    assert rm.detect_required_runtime("ruby", tmp_path) == "3.2.2"


def test_detect_php_composer(tmp_path):
    (tmp_path / "composer.json").write_text(json.dumps({"require": {"php": "^8.2"}}), encoding="utf-8")
    assert rm.detect_required_runtime("php", tmp_path) == "^8.2"


def test_detect_none_when_absent(tmp_path):
    assert rm.detect_required_runtime("java", tmp_path) is None
    assert rm.detect_required_runtime("unknownlang", tmp_path) is None


# -- Task 2: registry ----------------------------------------------------------

def test_registry_roundtrip(fake_home):
    real = fake_home / "jdk17"
    real.mkdir()
    rm.remember_runtime("java", "17", str(real))
    assert rm.lookup_runtime("java", "17") == str(real)


def test_registry_miss(fake_home):
    assert rm.lookup_runtime("java", "17") is None


def test_registry_prunes_stale_path(fake_home):
    rm.remember_runtime("java", "17", str(fake_home / "gone"))
    assert rm.lookup_runtime("java", "17") is None  # path missing -> pruned


# -- Task 3/4: discovery, validation, resolution -------------------------------

def test_resolve_not_required(fake_home, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    res = rm.resolve_runtime("java", repo)
    assert res.status == "not_required"


def test_resolve_reuse_from_registry(fake_home, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".java-version").write_text("17", encoding="utf-8")
    jdk = fake_home / "jdk17"
    jdk.mkdir()
    rm.remember_runtime("java", "17", str(jdk))
    res = rm.resolve_runtime("java", repo)
    assert res.status == "resolved" and res.path == str(jdk)


def test_resolve_single_discovered_match_is_remembered(fake_home, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".java-version").write_text("17", encoding="utf-8")
    real_java = tmp_path / "jdk17" / "bin" / "java"
    real_java.parent.mkdir(parents=True)
    real_java.write_text("", encoding="utf-8")  # real path so lookup does not prune
    monkeypatch.setattr(rm, "discover_runtimes",
                        lambda lang: [rm.RuntimeCandidate(str(real_java), "17.0.9", "discovered")])
    res = rm.resolve_runtime("java", repo)
    assert res.status == "resolved"
    assert res.path == str(real_java)
    assert rm.lookup_runtime("java", "17") == str(real_java)  # remembered + reused


def test_resolve_multiple_matches_needs_choose(fake_home, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".java-version").write_text("17", encoding="utf-8")
    monkeypatch.setattr(rm, "discover_runtimes", lambda lang: [
        rm.RuntimeCandidate("/a/java", "17.0.9", "discovered"),
        rm.RuntimeCandidate("/b/java", "17.0.2", "discovered"),
    ])
    res = rm.resolve_runtime("java", repo)
    assert res.status == "choose" and len(res.candidates) == 2


def test_resolve_no_match_needs_ask(fake_home, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".java-version").write_text("17", encoding="utf-8")
    monkeypatch.setattr(rm, "discover_runtimes",
                        lambda lang: [rm.RuntimeCandidate("/opt/jdk8/bin/java", "1.8.0", "discovered")])
    res = rm.resolve_runtime("java", repo)
    assert res.status == "ask"


def test_version_matches():
    assert rm._version_matches("17", "17.0.9") is True
    assert rm._version_matches("3.11", "3.11.4") is True
    assert rm._version_matches("17", "1.8.0") is False
    assert rm._version_matches("stable", "1.75.0") is True  # non-numeric -> accept
