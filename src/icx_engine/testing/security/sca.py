"""Native SCA (software composition analysis) - deterministic, offline. Parses dependency manifests
(requirements.txt, package.json, pom.xml, go.mod) and flags unpinned / wildcard versions, then matches
each dependency against an OPTIONAL offline advisory file the user drops in. Honest ceiling: without a
live CVE feed this is best-effort - unpinned/wildcard findings are always emitted; known-vulnerable
findings appear only for advisories present in the offline file."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from icx_engine.testing.security.scan_base import Finding, read_text, rel

# Where the optional offline advisory DB is looked up (first hit wins).
_ADVISORY_ENV = "ICX_SCA_ADVISORY"
_ADVISORY_NAMES = (".icx-advisories.json", ".icx/advisories.json")


def _load_advisories(repo: Path) -> dict:
    """Advisory file shape: {"pypi": {"pkg": [{"lt": "2.0.0", "severity": "high", "id": "CVE-..",
    "title": ".."}]}, "npm": {...}, "maven": {...}, "go": {...}}. Missing/invalid -> {} (no crash)."""
    candidates = []
    env = os.environ.get(_ADVISORY_ENV)
    if env:
        candidates.append(Path(env))
    for n in _ADVISORY_NAMES:
        candidates.append(Path(repo) / n)
    for c in candidates:
        try:
            if c.is_file():
                data = json.loads(c.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
        except (OSError, ValueError):
            continue
    return {}


def _ver_tuple(v: str):
    nums = re.findall(r"\d+", v or "")
    return tuple(int(n) for n in nums[:4]) if nums else ()


def _lt(a: str, b: str) -> bool:
    """True when version a < version b (numeric component compare; unknown -> False)."""
    ta, tb = _ver_tuple(a), _ver_tuple(b)
    if not ta or not tb:
        return False
    return ta < tb


def _advisory_findings(eco: str, name: str, version: str, relp: str, line: int,
                       advisories: dict) -> list[Finding]:
    out: list[Finding] = []
    entries = (advisories.get(eco) or {}).get(name)
    if not isinstance(entries, list):
        return out
    for e in entries:
        if not isinstance(e, dict):
            continue
        lt = e.get("lt")
        # No version, or version below the fixed `lt` bound -> vulnerable.
        vulnerable = (not version) or (lt and _lt(version, str(lt)))
        if vulnerable:
            out.append(Finding(
                scanner="sca", rule="known-vulnerable-dependency",
                severity=str(e.get("severity", "high")).lower(),
                title=f"Vulnerable dependency: {name}",
                file=relp, line=line,
                detail=f"{name} {version or '(unpinned)'} - {e.get('id', 'advisory')}: "
                       f"{e.get('title', 'known vulnerability')}"
                       + (f" (fixed in {lt})" if lt else ""),
                extra={"package": name, "version": version, "advisory": e.get("id", "")}))
    return out


def _unpinned(eco: str, name: str, version: str, spec: str, relp: str, line: int) -> Finding | None:
    if version:
        return None
    return Finding(scanner="sca", rule="unpinned-dependency", severity="low",
                   title=f"Unpinned dependency: {name}",
                   file=relp, line=line,
                   detail=f"{name} pinned as '{spec}' - a floating/wildcard range makes builds "
                          f"non-reproducible and can pull an unvetted version.",
                   extra={"package": name, "ecosystem": eco})


def _parse_requirements(text: str):
    for i, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        m = re.match(r"^([A-Za-z0-9._-]+)\s*(.*)$", line)
        if not m:
            continue
        name, spec = m.group(1).lower(), m.group(2).strip()
        vm = re.match(r"^==\s*([0-9][0-9A-Za-z.\-]*)", spec)
        version = vm.group(1) if vm else ""
        yield name, version, spec or "*", i


def _parse_package_json(text: str):
    try:
        data = json.loads(text)
    except ValueError:
        return
    for key in ("dependencies", "devDependencies"):
        deps = data.get(key)
        if not isinstance(deps, dict):
            continue
        for name, spec in deps.items():
            spec = str(spec)
            vm = re.match(r"^\s*([0-9][0-9A-Za-z.\-]*)\s*$", spec)   # exact only
            version = vm.group(1) if vm else ""
            yield name.lower(), version, spec, 0


def _parse_pom(text: str):
    for m in re.finditer(
            r"<dependency>(.*?)</dependency>", text, re.DOTALL | re.IGNORECASE):
        block = m.group(1)
        aid = re.search(r"<artifactId>\s*([^<]+?)\s*</artifactId>", block, re.IGNORECASE)
        ver = re.search(r"<version>\s*([^<]+?)\s*</version>", block, re.IGNORECASE)
        if not aid:
            continue
        name = aid.group(1).strip().lower()
        vraw = ver.group(1).strip() if ver else ""
        version = vraw if re.match(r"^[0-9]", vraw) else ""
        yield name, version, vraw or "*", 0


def _parse_gomod(text: str):
    for i, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        m = re.match(r"^(?:require\s+)?([A-Za-z0-9./_-]+)\s+v([0-9][0-9A-Za-z.\-+]*)", line)
        if m:
            yield m.group(1).lower(), m.group(2), "v" + m.group(2), i


_MANIFESTS = {
    "requirements.txt": ("pypi", _parse_requirements),
    "package.json": ("npm", _parse_package_json),
    "pom.xml": ("maven", _parse_pom),
    "go.mod": ("go", _parse_gomod),
}


def scan_deps(repo: Path, advisories: dict | None = None) -> list[Finding]:
    """Scan all supported manifests under repo root and one level of common sub-dirs."""
    repo = Path(repo)
    if advisories is None:
        advisories = _load_advisories(repo)
    findings: list[Finding] = []
    seen_files: set = set()
    search_roots = [repo] + [repo / d for d in ("src", "app", "backend", "frontend", "service")]
    for root in search_roots:
        if not root.exists():
            continue
        for fname, (eco, parser) in _MANIFESTS.items():
            mf = root / fname
            try:
                if not mf.is_file() or mf in seen_files:
                    continue
            except OSError:
                continue
            seen_files.add(mf)
            text = read_text(mf)
            if not text:
                continue
            relp = rel(repo, mf)
            for name, version, spec, line in parser(text):
                up = _unpinned(eco, name, version, spec, relp, line)
                if up:
                    findings.append(up)
                findings.extend(_advisory_findings(eco, name, version, relp, line, advisories))
    return findings
