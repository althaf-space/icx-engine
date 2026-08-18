from __future__ import annotations
import json
import httpx
import pytest
import respx

from icx_engine.git.deps import (
    parse_package_json_git_deps, parse_requirements_txt_git_deps, parse_pyproject_toml_git_deps,
    parse_manifest_git_deps, _split_hostpath, DependencyPin, DependencyPinReport,
    resolve_via_local_clone, resolve_via_gitlab, check_dependency_pins, report_to_dict,
    repin_manifest_text,
)
from icx_engine.models.config import GitLabConnection


# -- _split_hostpath -----------------------------------------------------

def test_split_hostpath_https_no_user():
    assert _split_hostpath("gitlab.example.com/group/project") == ("gitlab.example.com", "group/project")


def test_split_hostpath_ssh_with_git_user():
    assert _split_hostpath("git@gitlab.example.com/group/project") == ("gitlab.example.com", "group/project")


def test_split_hostpath_strips_trailing_git_suffix():
    assert _split_hostpath("gitlab.example.com/group/project.git") == ("gitlab.example.com", "group/project")


def test_split_hostpath_no_path_returns_none_path():
    assert _split_hostpath("gitlab.example.com") == ("gitlab.example.com", None)


# -- package.json ----------------------------------------------------------

def test_parse_package_json_git_https_with_sha_ref():
    manifest = json.dumps({
        "dependencies": {"graphs": "git+https://gitlab.example.com/group/graphs.git#58cc063c"},
    })
    pins = parse_package_json_git_deps(manifest)
    assert len(pins) == 1
    pin = pins[0]
    assert pin.name == "graphs"
    assert pin.host == "gitlab.example.com"
    assert pin.project_path == "group/graphs"
    assert pin.ref == "58cc063c"


def test_parse_package_json_git_ssh_with_branch_ref():
    manifest = json.dumps({
        "devDependencies": {"tools": "git+ssh://git@gitlab.example.com/group/tools.git#development"},
    })
    pins = parse_package_json_git_deps(manifest)
    assert len(pins) == 1
    assert pins[0].host == "gitlab.example.com"
    assert pins[0].project_path == "group/tools"
    assert pins[0].ref == "development"


def test_parse_package_json_ignores_plain_semver_deps():
    manifest = json.dumps({"dependencies": {"lodash": "^4.17.21"}})
    assert parse_package_json_git_deps(manifest) == []


def test_parse_package_json_all_three_sections_scanned():
    manifest = json.dumps({
        "dependencies": {"a": "git+https://gitlab.example.com/g/a.git#1"},
        "devDependencies": {"b": "git+https://gitlab.example.com/g/b.git#2"},
        "optionalDependencies": {"c": "git+https://gitlab.example.com/g/c.git#3"},
    })
    pins = parse_package_json_git_deps(manifest)
    assert {p.name for p in pins} == {"a", "b", "c"}


# -- requirements.txt --------------------------------------------------------

def test_parse_requirements_txt_name_at_git_url_with_egg():
    text = "graphs @ git+https://gitlab.example.com/group/graphs.git@58cc063c#egg=graphs\n"
    pins = parse_requirements_txt_git_deps(text)
    assert len(pins) == 1
    pin = pins[0]
    assert pin.name == "graphs"
    assert pin.host == "gitlab.example.com"
    assert pin.project_path == "group/graphs"
    assert pin.ref == "58cc063c"


def test_parse_requirements_txt_bare_url_derives_name_from_egg():
    text = "git+https://gitlab.example.com/group/graphs.git@development#egg=graphs\n"
    pins = parse_requirements_txt_git_deps(text)
    assert pins[0].name == "graphs"
    assert pins[0].ref == "development"


def test_parse_requirements_txt_bare_url_no_egg_derives_name_from_path():
    text = "git+https://gitlab.example.com/group/graphs.git@abc123\n"
    pins = parse_requirements_txt_git_deps(text)
    assert pins[0].name == "graphs"


def test_parse_requirements_txt_editable_install():
    text = "-e git+https://gitlab.example.com/group/graphs.git@abc123#egg=graphs\n"
    pins = parse_requirements_txt_git_deps(text)
    assert len(pins) == 1
    assert pins[0].name == "graphs"


def test_parse_requirements_txt_ignores_comments_and_plain_pins():
    text = "# a comment\nrequests==2.31.0\n\n"
    assert parse_requirements_txt_git_deps(text) == []


def test_parse_requirements_txt_multiple_lines():
    text = (
        "a @ git+https://gitlab.example.com/g/a.git@sha1\n"
        "requests==2.31.0\n"
        "b @ git+https://gitlab.example.com/g/b.git@sha2\n"
    )
    pins = parse_requirements_txt_git_deps(text)
    assert {p.name for p in pins} == {"a", "b"}


# -- pyproject.toml ----------------------------------------------------------

def test_parse_pyproject_toml_poetry_inline_table_with_rev():
    text = 'graphs = { git = "https://gitlab.example.com/group/graphs.git", rev = "58cc063c" }\n'
    pins = parse_pyproject_toml_git_deps(text)
    assert len(pins) == 1
    pin = pins[0]
    assert pin.name == "graphs"
    assert pin.host == "gitlab.example.com"
    assert pin.project_path == "group/graphs"
    assert pin.ref == "58cc063c"


def test_parse_pyproject_toml_poetry_inline_table_with_branch():
    text = 'tools = { git = "https://gitlab.example.com/group/tools.git", branch = "development" }\n'
    pins = parse_pyproject_toml_git_deps(text)
    assert pins[0].ref == "development"


def test_parse_pyproject_toml_pep508_style_in_dependencies_array():
    text = 'dependencies = [\n  "graphs @ git+https://gitlab.example.com/group/graphs.git@58cc063c",\n]\n'
    pins = parse_pyproject_toml_git_deps(text)
    assert len(pins) == 1
    assert pins[0].host == "gitlab.example.com"
    assert pins[0].ref == "58cc063c"


def test_parse_pyproject_toml_no_git_deps_returns_empty():
    text = '[tool.poetry.dependencies]\npython = "^3.11"\nrequests = "^2.31"\n'
    assert parse_pyproject_toml_git_deps(text) == []


# -- repin_manifest_text ------------------------------------------------------

def test_repin_package_json_replaces_only_the_ref():
    text = json.dumps({
        "name": "app",
        "dependencies": {
            "graphs": "git+https://gitlab.example.com/group/graphs.git#58cc063c",
            "requests": "^2.31.0",
        },
    }, indent=2)
    new_text, old_ref = repin_manifest_text("package.json", text, "graphs", "a1b2c3d4")
    assert old_ref == "58cc063c"
    assert "git+https://gitlab.example.com/group/graphs.git#a1b2c3d4" in new_text
    # everything else in the file is untouched, including formatting
    assert json.loads(new_text)["dependencies"]["requests"] == "^2.31.0"
    assert new_text.count("\n") == text.count("\n")


def test_repin_package_json_unknown_dependency_raises():
    text = json.dumps({"dependencies": {"graphs": "git+https://gitlab.example.com/g/graphs.git#sha1"}})
    with pytest.raises(ValueError):
        repin_manifest_text("package.json", text, "nope", "newsha")


def test_repin_requirements_txt_replaces_only_the_target_line():
    text = (
        "a @ git+https://gitlab.example.com/g/a.git@sha1\n"
        "requests==2.31.0\n"
        "b @ git+https://gitlab.example.com/g/b.git@sha2\n"
    )
    new_text, old_ref = repin_manifest_text("requirements.txt", text, "b", "sha2-new")
    assert old_ref == "sha2"
    lines = new_text.splitlines()
    assert lines[0] == "a @ git+https://gitlab.example.com/g/a.git@sha1"  # untouched
    assert lines[1] == "requests==2.31.0"  # untouched
    assert lines[2] == "b @ git+https://gitlab.example.com/g/b.git@sha2-new"


def test_repin_requirements_txt_editable_egg_form():
    text = "-e git+https://gitlab.example.com/group/graphs.git@abc123#egg=graphs\n"
    new_text, old_ref = repin_manifest_text("requirements-dev.txt", text, "graphs", "def456")
    assert old_ref == "abc123"
    assert new_text == "-e git+https://gitlab.example.com/group/graphs.git@def456#egg=graphs\n"


def test_repin_requirements_txt_unknown_dependency_raises():
    with pytest.raises(ValueError):
        repin_manifest_text("requirements.txt", "a @ git+https://gitlab.example.com/g/a.git@sha1\n", "nope", "x")


def test_repin_pyproject_toml_pep508_style():
    text = 'dependencies = [\n  "graphs @ git+https://gitlab.example.com/group/graphs.git@58cc063c",\n]\n'
    new_text, old_ref = repin_manifest_text("pyproject.toml", text, "graphs", "a1b2c3d4")
    assert old_ref == "58cc063c"
    assert new_text == 'dependencies = [\n  "graphs @ git+https://gitlab.example.com/group/graphs.git@a1b2c3d4",\n]\n'


def test_repin_pyproject_toml_poetry_inline_table_rev():
    text = 'graphs = { git = "https://gitlab.example.com/group/graphs.git", rev = "58cc063c" }\n'
    new_text, old_ref = repin_manifest_text("pyproject.toml", text, "graphs", "a1b2c3d4")
    assert old_ref == "58cc063c"
    assert new_text == 'graphs = { git = "https://gitlab.example.com/group/graphs.git", rev = "a1b2c3d4" }\n'


def test_repin_pyproject_toml_poetry_inline_table_branch():
    text = 'tools = { git = "https://gitlab.example.com/group/tools.git", branch = "development" }\n'
    new_text, old_ref = repin_manifest_text("pyproject.toml", text, "tools", "main")
    assert old_ref == "development"
    assert new_text == 'tools = { git = "https://gitlab.example.com/group/tools.git", branch = "main" }\n'


def test_repin_pyproject_toml_only_touches_matching_dependency_among_several():
    text = (
        'graphs = { git = "https://gitlab.example.com/group/graphs.git", rev = "sha1" }\n'
        'tools = { git = "https://gitlab.example.com/group/tools.git", rev = "sha2" }\n'
    )
    new_text, old_ref = repin_manifest_text("pyproject.toml", text, "tools", "sha2-new")
    assert old_ref == "sha2"
    assert 'graphs = { git = "https://gitlab.example.com/group/graphs.git", rev = "sha1" }' in new_text
    assert 'tools = { git = "https://gitlab.example.com/group/tools.git", rev = "sha2-new" }' in new_text


def test_repin_pyproject_toml_unknown_dependency_raises():
    with pytest.raises(ValueError):
        repin_manifest_text("pyproject.toml", 'python = "^3.11"\n', "nope", "x")


def test_repin_manifest_text_raises_for_unsupported_filename():
    with pytest.raises(ValueError):
        repin_manifest_text("Cargo.toml", "", "graphs", "x")


# -- parse_manifest_git_deps dispatch ----------------------------------------

def test_parse_manifest_git_deps_dispatches_by_basename():
    manifest = json.dumps({"dependencies": {"graphs": "git+https://gitlab.example.com/g/graphs.git#sha1"}})
    pins = parse_manifest_git_deps("subdir/package.json", manifest)
    assert pins[0].manifest == "subdir/package.json"


def test_parse_manifest_git_deps_matches_requirements_variants():
    pins = parse_manifest_git_deps("requirements-dev.txt", "a @ git+https://gitlab.example.com/g/a.git@sha1\n")
    assert len(pins) == 1
    assert pins[0].manifest == "requirements-dev.txt"


def test_parse_manifest_git_deps_raises_for_unsupported_filename():
    with pytest.raises(ValueError):
        parse_manifest_git_deps("Cargo.toml", "")


# -- resolve_via_local_clone --------------------------------------------------

def _pin(ref="58cc063c") -> DependencyPin:
    return DependencyPin(
        manifest="requirements.txt", name="graphs", url="git+https://gitlab.example.com/group/graphs.git",
        ref=ref, host="gitlab.example.com", project_path="group/graphs",
    )


def test_resolve_via_local_clone_up_to_date(tmp_git_repo):
    from icx_engine.git.gitcmd import head_sha
    sha = head_sha(tmp_git_repo)
    pin = _pin(ref=sha)
    report = resolve_via_local_clone(pin, "main", tmp_git_repo)
    assert report.resolved is True
    assert report.status == "UP_TO_DATE"
    assert report.commits_behind == 0
    assert report.pinned_commit == sha
    assert report.target_commit == sha


def test_resolve_via_local_clone_behind(tmp_git_repo):
    from icx_engine.git.gitcmd import head_sha, stage_files, commit
    pinned_sha = head_sha(tmp_git_repo)
    (tmp_git_repo / "new.txt").write_text("x", encoding="utf-8")
    stage_files(tmp_git_repo, ["new.txt"])
    commit(tmp_git_repo, "advance main")
    pin = _pin(ref=pinned_sha)
    report = resolve_via_local_clone(pin, "main", tmp_git_repo)
    assert report.status == "BEHIND"
    assert report.commits_behind == 1


def test_resolve_via_local_clone_incompatible_when_diverged(tmp_git_repo):
    from icx_engine.git.gitcmd import create_branch_from, checkout, stage_files, commit
    create_branch_from(tmp_git_repo, "side", "main")
    checkout(tmp_git_repo, "side")
    (tmp_git_repo / "side.txt").write_text("x", encoding="utf-8")
    stage_files(tmp_git_repo, ["side.txt"])
    side_sha = commit(tmp_git_repo, "side commit")
    checkout(tmp_git_repo, "main")
    (tmp_git_repo / "main.txt").write_text("y", encoding="utf-8")
    stage_files(tmp_git_repo, ["main.txt"])
    commit(tmp_git_repo, "main commit")
    pin = _pin(ref=side_sha)
    report = resolve_via_local_clone(pin, "main", tmp_git_repo)
    assert report.status == "INCOMPATIBLE"
    assert report.commits_behind is None


def test_resolve_via_local_clone_missing_paths_marks_incompatible(tmp_git_repo):
    from icx_engine.git.gitcmd import head_sha
    sha = head_sha(tmp_git_repo)
    pin = _pin(ref=sha)
    report = resolve_via_local_clone(pin, "main", tmp_git_repo, check_paths=["does_not_exist.txt"])
    assert report.status == "INCOMPATIBLE"
    assert report.missing_paths == ["does_not_exist.txt"]


def test_resolve_via_local_clone_unresolvable_pinned_ref(tmp_git_repo):
    pin = _pin(ref="not-a-real-ref")
    report = resolve_via_local_clone(pin, "main", tmp_git_repo)
    assert report.resolved is False
    assert "does not resolve" in report.reason


def test_resolve_via_local_clone_unresolvable_target_ref(tmp_git_repo):
    from icx_engine.git.gitcmd import head_sha
    pin = _pin(ref=head_sha(tmp_git_repo))
    report = resolve_via_local_clone(pin, "not-a-real-branch", tmp_git_repo)
    assert report.resolved is False


def test_resolve_via_local_clone_no_ref_pinned(tmp_git_repo):
    pin = _pin(ref=None)
    report = resolve_via_local_clone(pin, "main", tmp_git_repo)
    assert report.resolved is False
    assert "No ref pinned" in report.reason


def test_resolve_via_local_clone_not_a_git_repo(tmp_path):
    not_a_repo = tmp_path / "plain_dir"
    not_a_repo.mkdir()
    pin = _pin()
    report = resolve_via_local_clone(pin, "main", not_a_repo)
    assert report.resolved is False
    assert "not a git repository" in report.reason


# -- check_dependency_pins orchestrator --------------------------------------

async def test_check_dependency_pins_routes_to_local_clone_when_given(tmp_git_repo):
    from icx_engine.git.gitcmd import head_sha
    sha = head_sha(tmp_git_repo)
    manifest = json.dumps({"dependencies": {"graphs": f"git+https://gitlab.example.com/group/graphs.git#{sha}"}})
    reports = await check_dependency_pins(
        {"package.json": manifest}, target_ref="main", dependency_name="graphs", dep_repo_path=tmp_git_repo,
    )
    assert len(reports) == 1
    assert reports[0].resolved is True
    assert reports[0].status == "UP_TO_DATE"


async def test_check_dependency_pins_no_match_reports_clear_reason():
    manifest = json.dumps({"dependencies": {"graphs": "git+https://github.com/group/graphs.git#abc123"}})
    reports = await check_dependency_pins({"package.json": manifest}, target_ref="main", gitlab_connections=[])
    assert len(reports) == 1
    assert reports[0].resolved is False
    # Assert against the structured, already-parsed host field - not a substring/URL
    # check on the free-text reason, which CodeQL flags as incomplete URL sanitization
    # (a domain substring can appear anywhere in an untrusted string).
    assert reports[0].pin.host == "github.com"
    assert "does not match any active GitLab connection" in reports[0].reason


async def test_check_dependency_pins_filters_by_dependency_name():
    manifest = json.dumps({
        "dependencies": {
            "graphs": "git+https://github.com/group/graphs.git#abc123",
            "other": "git+https://github.com/group/other.git#def456",
        },
    })
    reports = await check_dependency_pins(
        {"package.json": manifest}, target_ref="main", dependency_name="graphs", gitlab_connections=[],
    )
    assert len(reports) == 1
    assert reports[0].pin.name == "graphs"


async def test_check_dependency_pins_ignores_unsupported_manifest_types():
    reports = await check_dependency_pins({"Cargo.toml": "not real toml"}, target_ref="main")
    assert reports == []


def test_report_to_dict_serializes_nested_pin():
    pin = _pin()
    report = DependencyPinReport(pin=pin, resolved=True, status="UP_TO_DATE", commits_behind=0)
    d = report_to_dict(report)
    assert d["status"] == "UP_TO_DATE"
    assert d["pin"]["name"] == "graphs"


# -- resolve_via_gitlab -------------------------------------------------------

def _conn() -> GitLabConnection:
    return GitLabConnection(name="gitlab.example.com", url="https://gitlab.example.com", token="glpat-x")


@respx.mock
async def test_resolve_via_gitlab_up_to_date():
    pin = _pin(ref="58cc063c")
    respx.get("https://gitlab.example.com/api/v4/projects/group%2Fgraphs/repository/commits").mock(
        return_value=httpx.Response(200, json=[{"id": "58cc063c" + "0" * 33}])
    )
    report = await resolve_via_gitlab(pin, "development", _conn())
    assert report.resolved is True
    assert report.status == "UP_TO_DATE"
    assert report.commits_behind == 0


@respx.mock
async def test_resolve_via_gitlab_behind():
    pin = _pin(ref="58cc063c")
    pinned_sha = "58cc063c" + "0" * 33
    target_sha = "abc12345" + "0" * 33

    call_count = {"n": 0}

    def _commits_side_effect(request):
        call_count["n"] += 1
        ref = request.url.params.get("ref_name")
        if ref == "58cc063c":
            return httpx.Response(200, json=[{"id": pinned_sha}])
        return httpx.Response(200, json=[{"id": target_sha}])

    respx.get("https://gitlab.example.com/api/v4/projects/group%2Fgraphs/repository/commits").mock(
        side_effect=_commits_side_effect
    )
    respx.get("https://gitlab.example.com/api/v4/projects/group%2Fgraphs/repository/compare").mock(
        return_value=httpx.Response(200, json={"commits": [{"id": "x"}, {"id": "y"}]})
    )
    report = await resolve_via_gitlab(pin, "development", _conn())
    assert report.status == "BEHIND"
    assert report.commits_behind == 2
    assert report.pinned_commit == pinned_sha
    assert report.target_commit == target_sha


@respx.mock
async def test_resolve_via_gitlab_missing_check_path_marks_incompatible():
    pin = _pin(ref="58cc063c")
    sha = "58cc063c" + "0" * 33
    respx.get("https://gitlab.example.com/api/v4/projects/group%2Fgraphs/repository/commits").mock(
        return_value=httpx.Response(200, json=[{"id": sha}])
    )
    respx.get("https://gitlab.example.com/api/v4/projects/group%2Fgraphs/repository/files/graphs/raw").mock(
        return_value=httpx.Response(404, json={"message": "404 Not Found"})
    )
    report = await resolve_via_gitlab(pin, "development", _conn(), check_paths=["graphs"])
    assert report.status == "INCOMPATIBLE"
    assert report.missing_paths == ["graphs"]


@respx.mock
async def test_resolve_via_gitlab_unresolvable_pinned_ref_reports_reason():
    pin = _pin(ref="not-a-real-ref")
    respx.get("https://gitlab.example.com/api/v4/projects/group%2Fgraphs/repository/commits").mock(
        return_value=httpx.Response(404, json={"message": "404 Not Found"})
    )
    report = await resolve_via_gitlab(pin, "development", _conn())
    assert report.resolved is False
    assert "Could not resolve pinned ref" in report.reason


async def test_resolve_via_gitlab_no_project_path_reports_reason():
    pin = DependencyPin(manifest="package.json", name="x", url="git+https://gitlab.example.com",
                         ref="abc", host="gitlab.example.com", project_path=None)
    report = await resolve_via_gitlab(pin, "development", _conn())
    assert report.resolved is False
    assert "project path" in report.reason


@respx.mock
async def test_check_dependency_pins_routes_to_matching_gitlab_connection():
    manifest = json.dumps({"dependencies": {"graphs": "git+https://gitlab.example.com/group/graphs.git#58cc063c"}})
    sha = "58cc063c" + "0" * 33
    respx.get("https://gitlab.example.com/api/v4/projects/group%2Fgraphs/repository/commits").mock(
        return_value=httpx.Response(200, json=[{"id": sha}])
    )
    reports = await check_dependency_pins(
        {"package.json": manifest}, target_ref="development", gitlab_connections=[_conn()],
    )
    assert len(reports) == 1
    assert reports[0].resolved is True
    assert reports[0].status == "UP_TO_DATE"
