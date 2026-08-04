"""Native SCA: manifest parsing, unpinned/wildcard flagging, and offline-advisory matching."""
from __future__ import annotations

import json

from icx_engine.testing.security.sca import scan_deps


def test_unpinned_requirements_flagged(tmp_path):
    (tmp_path / "requirements.txt").write_text("flask\nrequests==2.31.0\n", encoding="utf-8")
    f = scan_deps(tmp_path)
    rules = [(x.rule, x.extra.get("package")) for x in f]
    assert ("unpinned-dependency", "flask") in rules
    # pinned exact version is not unpinned
    assert not any(x.extra.get("package") == "requests" and x.rule == "unpinned-dependency" for x in f)


def test_advisory_match_below_fixed_version(tmp_path):
    (tmp_path / "requirements.txt").write_text("requests==2.5.0\n", encoding="utf-8")
    (tmp_path / ".icx-advisories.json").write_text(
        json.dumps({"pypi": {"requests": [
            {"lt": "2.20.0", "severity": "high", "id": "CVE-2018-18074", "title": "creds leak"}]}}),
        encoding="utf-8")
    f = scan_deps(tmp_path)
    vuln = [x for x in f if x.rule == "known-vulnerable-dependency"]
    assert vuln and vuln[0].severity == "high"
    assert "CVE-2018-18074" in vuln[0].detail


def test_advisory_not_matched_when_patched(tmp_path):
    (tmp_path / "requirements.txt").write_text("requests==2.31.0\n", encoding="utf-8")
    (tmp_path / ".icx-advisories.json").write_text(
        json.dumps({"pypi": {"requests": [{"lt": "2.20.0", "severity": "high", "id": "X"}]}}),
        encoding="utf-8")
    assert not any(x.rule == "known-vulnerable-dependency" for x in scan_deps(tmp_path))


def test_package_json_parsed(tmp_path):
    (tmp_path / "package.json").write_text(
        json.dumps({"dependencies": {"lodash": "^4.17.0", "react": "18.2.0", "left-pad": "*"}}),
        encoding="utf-8")
    f = scan_deps(tmp_path)
    # caret range is a bounded, intentional pin -> low-noise "ranged", not "unpinned"
    assert any(x.extra.get("package") == "lodash" and x.rule == "ranged-dependency" for x in f)
    assert not any(x.extra.get("package") == "lodash" and x.rule == "unpinned-dependency" for x in f)
    # exact version -> no finding at all
    assert not any(x.extra.get("package") == "react" for x in f)
    # a true wildcard is still "unpinned"
    assert any(x.extra.get("package") == "left-pad" and x.rule == "unpinned-dependency" for x in f)


def test_go_mod_parsed_and_advisory(tmp_path):
    (tmp_path / "go.mod").write_text(
        "module x\n\nrequire github.com/foo/bar v1.2.0\n", encoding="utf-8")
    (tmp_path / ".icx-advisories.json").write_text(
        json.dumps({"go": {"github.com/foo/bar": [{"lt": "1.5.0", "severity": "critical", "id": "G1"}]}}),
        encoding="utf-8")
    f = scan_deps(tmp_path)
    assert any(x.rule == "known-vulnerable-dependency" and x.severity == "critical" for x in f)


def test_empty_and_missing_manifests_no_crash(tmp_path):
    assert scan_deps(tmp_path) == []
    (tmp_path / "package.json").write_text("{not valid json", encoding="utf-8")
    assert isinstance(scan_deps(tmp_path), list)


def test_requirements_semver_range_is_ranged_not_unpinned(tmp_path):
    (tmp_path / "requirements.txt").write_text(
        "requests~=2.31\nflask>=2.0,<3.0\ndjango\n", encoding="utf-8")
    f = scan_deps(tmp_path)
    assert any(x.extra.get("package") == "requests" and x.rule == "ranged-dependency" for x in f)
    assert any(x.extra.get("package") == "flask" and x.rule == "ranged-dependency" for x in f)
    assert not any(x.extra.get("package") in ("requests", "flask") and x.rule == "unpinned-dependency" for x in f)
    # bare package name (no spec at all) is still a true unpinned wildcard
    assert any(x.extra.get("package") == "django" and x.rule == "unpinned-dependency" for x in f)


def test_pom_bracket_range_is_ranged_latest_release_still_unpinned(tmp_path):
    (tmp_path / "pom.xml").write_text(
        "<project>"
        "<dependency><artifactId>a</artifactId><version>[1.0,2.0)</version></dependency>"
        "<dependency><artifactId>b</artifactId><version>LATEST</version></dependency>"
        "<dependency><artifactId>c</artifactId><version>1.2.3</version></dependency>"
        "</project>", encoding="utf-8")
    f = scan_deps(tmp_path)
    assert any(x.extra.get("package") == "a" and x.rule == "ranged-dependency" for x in f)
    assert any(x.extra.get("package") == "b" and x.rule == "unpinned-dependency" for x in f)
    assert not any(x.extra.get("package") == "c" for x in f)


def test_advisory_env_override(tmp_path, monkeypatch):
    (tmp_path / "requirements.txt").write_text("requests==1.0.0\n", encoding="utf-8")
    adv = tmp_path / "ext_adv.json"
    adv.write_text(json.dumps({"pypi": {"requests": [{"lt": "2.0.0", "severity": "high", "id": "E"}]}}),
                   encoding="utf-8")
    monkeypatch.setenv("ICX_SCA_ADVISORY", str(adv))
    assert any(x.rule == "known-vulnerable-dependency" for x in scan_deps(tmp_path))
