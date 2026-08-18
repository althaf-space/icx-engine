"""Dependency-pin analysis - finds git-VCS dependency references in common
manifest formats (package.json, requirements.txt, pyproject.toml) and
reports whether the pinned commit is stale/diverged relative to that
dependency's own target branch.

Resolving the DEPENDENCY's own repository (never the consuming project's -
that's just where the manifest lives) is supported two ways, no new
external client added:
  1. A local git checkout of the dependency's own repo (dep_repo_path) -
     reuses gitcmd.py directly, real ancestor/distance checks via
     is_ancestor/unique_commit_count.
  2. The dependency's GitLab project, if its parsed URL host matches an
     ACTIVE ICX GitLab connection - reuses gitlab/client.py, checked across
     every configured connection, not just the active one (a dependency can
     live on a different GitLab host than the consuming project).
A dependency hosted anywhere else (GitHub, Bitbucket, an unauthenticated
bare host) is reported resolved=False with a clear reason - never guessed,
never silently skipped. Regex-based parsing throughout, deliberately narrow
(no TOML/JSON-schema-level parsing beyond stdlib json for package.json) -
see parse_manifest_git_deps for exactly which real-world dependency spec
shapes are recognized.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class DependencyPin:
    manifest: str
    name: str
    url: str
    ref: str | None
    host: str | None
    project_path: str | None


@dataclass
class DependencyPinReport:
    pin: DependencyPin
    resolved: bool
    pinned_commit: str | None = None
    target_ref: str | None = None
    target_commit: str | None = None
    commits_behind: int | None = None
    status: str = "UNRESOLVED"  # UP_TO_DATE | BEHIND | INCOMPATIBLE | UNRESOLVED
    missing_paths: list[str] = field(default_factory=list)
    reason: str | None = None


def report_to_dict(report: DependencyPinReport) -> dict:
    return asdict(report)


# npm dependency spec form (package.json): (git+)?(https|ssh|git)://host/path(.git)?(#ref)?
_NPM_GIT_URL_RE = re.compile(
    r'^(?:git\+)?(?P<scheme>https?|ssh|git)://(?P<hostpath>[^#]+?)(?:\.git)?/?(?:#(?P<ref>.+))?$'
)
# pip/poetry PEP 508 direct-URL form: git+(https|ssh)://host/path(.git)?@ref
# ref excludes quote/comma too, so this also matches cleanly when embedded inside a
# TOML string literal (pyproject.toml's `dependencies = ["name @ git+...@ref"]` array).
_PIP_GIT_URL_RE = re.compile(
    r'git\+(?P<scheme>https?|ssh)://(?P<hostpath>[^@\s#]+?)(?:\.git)?@(?P<ref>[^#\s",]+)'
)
_PIP_NAME_AT_URL_RE = re.compile(r'^(?P<name>[A-Za-z0-9_.\-]+)\s*@\s*(?P<url>.+)$')
_EGG_NAME_RE = re.compile(r'#egg=(?P<name>[A-Za-z0-9_.\-]+)')
# poetry inline-table form (pyproject.toml): name = { git = "url", rev/branch/tag = "ref" }
_POETRY_DEP_RE = re.compile(r'^(?P<name>[A-Za-z0-9_.\-]+)\s*=\s*\{(?P<body>[^}]*)\}', re.MULTILINE)
_POETRY_GIT_RE = re.compile(r'git\s*=\s*"(?P<url>[^"]+)"')
_POETRY_REF_RE = re.compile(r'(?:rev|branch|tag)\s*=\s*"(?P<ref>[^"]+)"')


def _split_hostpath(hostpath: str) -> tuple[str | None, str | None]:
    """'gitlab.example.com/group/project' or 'git@gitlab.example.com/group/project'
    -> (host, 'group/project'). Never raises - an unparseable hostpath just
    yields (hostpath or None, None), surfaced later as an unresolvable pin,
    not a crash."""
    first_segment = hostpath.split("/", 1)[0]
    if "@" in first_segment:
        hostpath = hostpath.split("@", 1)[1]
    parts = hostpath.split("/", 1)
    if len(parts) != 2 or not parts[1]:
        return (parts[0] or None) if parts else None, None
    host, path = parts
    path = path.rstrip("/")
    if path.endswith(".git"):
        path = path[:-4]
    return host, path or None


def _pin_from_url_match(name: str, raw_spec: str, match: re.Match, manifest: str = "") -> DependencyPin:
    host, project_path = _split_hostpath(match["hostpath"])
    ref = match.groupdict().get("ref")
    return DependencyPin(manifest=manifest, name=name, url=raw_spec, ref=ref, host=host, project_path=project_path)


def parse_package_json_git_deps(text: str) -> list[DependencyPin]:
    data = json.loads(text)
    pins: list[DependencyPin] = []
    for section in ("dependencies", "devDependencies", "optionalDependencies"):
        for name, spec in (data.get(section) or {}).items():
            if not isinstance(spec, str):
                continue
            match = _NPM_GIT_URL_RE.match(spec.strip())
            if not match:
                continue
            pins.append(_pin_from_url_match(name, spec.strip(), match))
    return pins


def parse_requirements_txt_git_deps(text: str) -> list[DependencyPin]:
    pins: list[DependencyPin] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("-e "):
            line = line[3:].strip()
        name = None
        url_part = line
        name_match = _PIP_NAME_AT_URL_RE.match(line)
        if name_match:
            name = name_match["name"]
            url_part = name_match["url"]
        match = _PIP_GIT_URL_RE.search(url_part)
        if not match:
            continue
        if name is None:
            egg_match = _EGG_NAME_RE.search(url_part)
            if egg_match:
                name = egg_match["name"]
            else:
                _, project_path = _split_hostpath(match["hostpath"])
                name = (project_path or match["hostpath"]).rsplit("/", 1)[-1]
        pins.append(_pin_from_url_match(name, url_part, match))
    return pins


def parse_pyproject_toml_git_deps(text: str) -> list[DependencyPin]:
    pins: list[DependencyPin] = []
    for match in _PIP_GIT_URL_RE.finditer(text):
        _, project_path = _split_hostpath(match["hostpath"])
        name = (project_path or match["hostpath"]).rsplit("/", 1)[-1]
        pins.append(_pin_from_url_match(name, match.group(0), match))
    for dep_match in _POETRY_DEP_RE.finditer(text):
        body = dep_match["body"]
        git_match = _POETRY_GIT_RE.search(body)
        if not git_match:
            continue
        ref_match = _POETRY_REF_RE.search(body)
        url = git_match["url"]
        host, project_path = _split_hostpath(re.sub(r'^\w+://', '', url))
        pins.append(DependencyPin(
            manifest="", name=dep_match["name"], url=url,
            ref=ref_match["ref"] if ref_match else None, host=host, project_path=project_path,
        ))
    return pins


_MANIFEST_PARSERS = {
    "package.json": parse_package_json_git_deps,
    "pyproject.toml": parse_pyproject_toml_git_deps,
}


def parse_manifest_git_deps(filename: str, text: str) -> list[DependencyPin]:
    """Dispatches by filename (basename, either slash style) to the right
    parser. requirements*.txt matches any requirements.txt/requirements-
    dev.txt/etc, not just the exact name. Raises ValueError for anything
    else - callers scanning a directory should catch this and skip, not
    treat every file as a manifest."""
    basename = filename.replace("\\", "/").rsplit("/", 1)[-1]
    if basename in _MANIFEST_PARSERS:
        pins = _MANIFEST_PARSERS[basename](text)
    elif basename.startswith("requirements") and basename.endswith(".txt"):
        pins = parse_requirements_txt_git_deps(text)
    else:
        raise ValueError(
            f"Unsupported manifest filename: {filename!r} - supported: package.json, "
            "requirements*.txt, pyproject.toml"
        )
    for pin in pins:
        pin.manifest = filename
    return pins


def _repin_package_json(text: str, dependency_name: str, new_ref: str) -> tuple[str, str | None]:
    data = json.loads(text)
    for section in ("dependencies", "devDependencies", "optionalDependencies"):
        spec = (data.get(section) or {}).get(dependency_name)
        if not isinstance(spec, str):
            continue
        match = _NPM_GIT_URL_RE.match(spec.strip())
        if not match or not match.group("ref"):
            continue
        quoted = f'"{spec.strip()}"'
        pos = text.find(quoted)
        if pos == -1:
            continue
        old_ref = match["ref"]
        ref_start = pos + 1 + match.start("ref")  # +1 skips the opening quote
        ref_end = pos + 1 + match.end("ref")
        return text[:ref_start] + new_ref + text[ref_end:], old_ref
    raise ValueError(f"No git-pinned dependency named '{dependency_name}' with a #ref found in package.json.")


def _repin_requirements_txt(text: str, dependency_name: str, new_ref: str) -> tuple[str, str | None]:
    lines = text.splitlines(keepends=True)
    offset = 0
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            body = stripped[3:].strip() if stripped.startswith("-e ") else stripped
            name = None
            url_part = body
            name_match = _PIP_NAME_AT_URL_RE.match(body)
            if name_match:
                name = name_match["name"]
                url_part = name_match["url"]
            match = _PIP_GIT_URL_RE.search(url_part)
            if match:
                if name is None:
                    egg_match = _EGG_NAME_RE.search(url_part)
                    if egg_match:
                        name = egg_match["name"]
                    else:
                        _, project_path = _split_hostpath(match["hostpath"])
                        name = (project_path or match["hostpath"]).rsplit("/", 1)[-1]
                if name == dependency_name:
                    line_pos_in_body = line.index(url_part)
                    old_ref = match["ref"]
                    ref_start = offset + line_pos_in_body + match.start("ref")
                    ref_end = offset + line_pos_in_body + match.end("ref")
                    return text[:ref_start] + new_ref + text[ref_end:], old_ref
        offset += len(line)
    raise ValueError(f"No git-pinned dependency named '{dependency_name}' found in requirements.txt.")


def _repin_pyproject_toml(text: str, dependency_name: str, new_ref: str) -> tuple[str, str | None]:
    for match in _PIP_GIT_URL_RE.finditer(text):
        _, project_path = _split_hostpath(match["hostpath"])
        name = (project_path or match["hostpath"]).rsplit("/", 1)[-1]
        if name == dependency_name:
            old_ref = match["ref"]
            return text[:match.start("ref")] + new_ref + text[match.end("ref"):], old_ref
    for dep_match in _POETRY_DEP_RE.finditer(text):
        if dep_match["name"] != dependency_name:
            continue
        body = dep_match["body"]
        if not _POETRY_GIT_RE.search(body):
            continue
        ref_match = _POETRY_REF_RE.search(body)
        if not ref_match:
            continue
        body_start = dep_match.start("body")
        old_ref = ref_match["ref"]
        ref_start = body_start + ref_match.start("ref")
        ref_end = body_start + ref_match.end("ref")
        return text[:ref_start] + new_ref + text[ref_end:], old_ref
    raise ValueError(f"No git-pinned dependency named '{dependency_name}' with a rev/branch/tag found in pyproject.toml.")


def repin_manifest_text(filename: str, text: str, dependency_name: str, new_ref: str) -> tuple[str, str | None]:
    """Rewrites exactly the pinned ref/SHA for `dependency_name` in `text`, leaving
    everything else in the manifest byte-for-byte unchanged (no JSON/TOML
    re-serialization - a targeted splice at the ref's exact character span,
    found by re-running the same matching logic parse_manifest_git_deps uses).
    Returns (new_text, old_ref). Raises ValueError if dependency_name isn't
    found as a git-pinned dependency with a resolvable ref in this manifest."""
    basename = filename.replace("\\", "/").rsplit("/", 1)[-1]
    if basename == "package.json":
        return _repin_package_json(text, dependency_name, new_ref)
    if basename == "pyproject.toml":
        return _repin_pyproject_toml(text, dependency_name, new_ref)
    if basename.startswith("requirements") and basename.endswith(".txt"):
        return _repin_requirements_txt(text, dependency_name, new_ref)
    raise ValueError(
        f"Unsupported manifest filename: {filename!r} - supported: package.json, "
        "requirements*.txt, pyproject.toml"
    )


def resolve_via_local_clone(
    pin: DependencyPin, target_ref: str, dep_repo_path: Path, check_paths: list[str] | None = None,
) -> DependencyPinReport:
    from icx_engine.git import gitcmd

    if not gitcmd.is_git_repo(dep_repo_path):
        return DependencyPinReport(pin=pin, resolved=False, reason=f"'{dep_repo_path}' is not a git repository.")

    pinned_commit = gitcmd.resolve_ref(dep_repo_path, pin.ref) if pin.ref else None
    if pin.ref and pinned_commit is None:
        return DependencyPinReport(
            pin=pin, resolved=False,
            reason=f"Pinned ref '{pin.ref}' does not resolve in '{dep_repo_path}' - fetch first?",
        )
    target_commit = gitcmd.resolve_ref(dep_repo_path, target_ref)
    if target_commit is None:
        return DependencyPinReport(
            pin=pin, resolved=False, pinned_commit=pinned_commit,
            reason=f"target_ref '{target_ref}' does not resolve in '{dep_repo_path}'.",
        )
    if pinned_commit is None:
        return DependencyPinReport(
            pin=pin, resolved=False, target_ref=target_ref, target_commit=target_commit,
            reason="No ref pinned in the manifest - nothing to compare.",
        )

    missing_paths = [p for p in (check_paths or []) if not gitcmd.file_exists_at_ref(dep_repo_path, target_commit, p)]

    if pinned_commit == target_commit:
        status = "INCOMPATIBLE" if missing_paths else "UP_TO_DATE"
        return DependencyPinReport(
            pin=pin, resolved=True, pinned_commit=pinned_commit, target_ref=target_ref,
            target_commit=target_commit, commits_behind=0, status=status, missing_paths=missing_paths,
        )

    ancestor = gitcmd.is_ancestor(dep_repo_path, pinned_commit, target_commit)
    commits_behind = gitcmd.unique_commit_count(dep_repo_path, target_commit, pinned_commit) if ancestor else None
    status = "BEHIND" if (ancestor and not missing_paths) else "INCOMPATIBLE"
    return DependencyPinReport(
        pin=pin, resolved=True, pinned_commit=pinned_commit, target_ref=target_ref,
        target_commit=target_commit, commits_behind=commits_behind, status=status, missing_paths=missing_paths,
    )


async def resolve_via_gitlab(
    pin: DependencyPin, target_ref: str, conn, check_paths: list[str] | None = None,
) -> DependencyPinReport:
    from icx_engine.gitlab.client import GitLabClient, GitLabError

    if pin.project_path is None:
        return DependencyPinReport(
            pin=pin, resolved=False, reason="Could not parse a project path from this dependency's URL.",
        )
    async with GitLabClient(conn.url, conn.token, conn.verify_tls) as client:
        try:
            pinned_commits = await client.list_commits(pin.project_path, ref=pin.ref, limit=1)
        except GitLabError as exc:
            return DependencyPinReport(
                pin=pin, resolved=False,
                reason=f"Could not resolve pinned ref on {pin.project_path}: {exc}",
            )
        pinned_commit = pinned_commits[0]["id"] if pinned_commits else None

        try:
            target_commits = await client.list_commits(pin.project_path, ref=target_ref, limit=1)
        except GitLabError as exc:
            return DependencyPinReport(
                pin=pin, resolved=False, pinned_commit=pinned_commit,
                reason=f"Could not resolve target_ref '{target_ref}' on {pin.project_path}: {exc}",
            )
        target_commit = target_commits[0]["id"] if target_commits else None

        if pinned_commit is None or target_commit is None:
            return DependencyPinReport(
                pin=pin, resolved=False, pinned_commit=pinned_commit, target_ref=target_ref,
                target_commit=target_commit, reason="Pinned ref or target_ref did not resolve to any commit.",
            )

        missing_paths: list[str] = []
        for path in (check_paths or []):
            try:
                await client.get_repository_file(pin.project_path, path, target_commit)
            except GitLabError:
                missing_paths.append(path)

        if pinned_commit == target_commit:
            status = "INCOMPATIBLE" if missing_paths else "UP_TO_DATE"
            return DependencyPinReport(
                pin=pin, resolved=True, pinned_commit=pinned_commit, target_ref=target_ref,
                target_commit=target_commit, commits_behind=0, status=status, missing_paths=missing_paths,
            )

        try:
            compare = await client.compare(pin.project_path, pinned_commit, target_commit)
            commits_behind = len(compare.get("commits") or [])
        except GitLabError:
            commits_behind = None
        status = "BEHIND" if not missing_paths else "INCOMPATIBLE"
        return DependencyPinReport(
            pin=pin, resolved=True, pinned_commit=pinned_commit, target_ref=target_ref,
            target_commit=target_commit, commits_behind=commits_behind, status=status, missing_paths=missing_paths,
        )


def find_matching_gitlab_connection(host: str | None, connections: list):
    if not host:
        return None
    from urllib.parse import urlparse
    for conn in connections:
        if urlparse(conn.url).netloc.lower() == host.lower():
            return conn
    return None


async def check_dependency_pins(
    manifests: dict[str, str], target_ref: str, dependency_name: str | None = None,
    dep_repo_path: Path | None = None, check_paths: list[str] | None = None,
    gitlab_connections: list | None = None,
) -> list[DependencyPinReport]:
    """Parses every manifest, optionally filters to one dependency_name (required
    to make dep_repo_path/check_paths unambiguous when more than one pin is
    found), then resolves each via a local clone (if dep_repo_path given) or the
    first configured GitLab connection whose host matches - never both for the
    same pin, and never a guess when neither is available."""
    all_pins: list[DependencyPin] = []
    for filename, text in manifests.items():
        try:
            all_pins.extend(parse_manifest_git_deps(filename, text))
        except ValueError:
            continue
    if dependency_name:
        all_pins = [p for p in all_pins if p.name == dependency_name]

    reports: list[DependencyPinReport] = []
    for pin in all_pins:
        if dep_repo_path is not None:
            reports.append(resolve_via_local_clone(pin, target_ref, dep_repo_path, check_paths=check_paths))
            continue
        conn = find_matching_gitlab_connection(pin.host, gitlab_connections or [])
        if conn is None:
            reports.append(DependencyPinReport(
                pin=pin, resolved=False,
                reason=(
                    f"No local clone path given and host '{pin.host}' does not match any active "
                    "GitLab connection - configure one (icx gitlab --add) or pass dep_repo_path."
                ),
            ))
            continue
        reports.append(await resolve_via_gitlab(pin, target_ref, conn, check_paths=check_paths))
    return reports
