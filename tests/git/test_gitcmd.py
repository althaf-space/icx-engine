from __future__ import annotations
from pathlib import Path

import pytest

from icx_engine.git.gitcmd import (
    GitCommandError, is_git_repo, repo_root, current_branch,
    fetch, remote_branch_exists, local_branch_exists,
)


def test_is_git_repo_true_for_real_repo(tmp_git_repo):
    assert is_git_repo(tmp_git_repo) is True


def test_is_git_repo_false_for_non_repo(tmp_path):
    not_a_repo = tmp_path / "not_a_repo"
    not_a_repo.mkdir()
    assert is_git_repo(not_a_repo) is False


def test_is_git_repo_false_for_missing_path(tmp_path):
    assert is_git_repo(tmp_path / "does_not_exist") is False


def test_repo_root_resolves_to_repo_dir(tmp_git_repo):
    assert repo_root(tmp_git_repo) == tmp_git_repo.resolve()


def test_repo_root_raises_for_non_repo(tmp_path):
    with pytest.raises(GitCommandError):
        repo_root(tmp_path)


def test_repo_root_raises_git_command_error_for_nonexistent_path(tmp_path):
    missing = tmp_path / "does_not_exist_at_all"
    with pytest.raises(GitCommandError):
        repo_root(missing)


def test_current_branch_returns_main(tmp_git_repo):
    assert current_branch(tmp_git_repo) == "main"


def test_fetch_succeeds_against_real_remote(tmp_git_repo_with_remote):
    fetch(tmp_git_repo_with_remote)  # must not raise


def test_fetch_raises_for_unknown_remote(tmp_git_repo):
    with pytest.raises(GitCommandError):
        fetch(tmp_git_repo, remote="does-not-exist")


def test_remote_branch_exists_true_for_pushed_branch(tmp_git_repo_with_remote):
    assert remote_branch_exists(tmp_git_repo_with_remote, "main") is True


def test_remote_branch_exists_false_for_missing_branch(tmp_git_repo_with_remote):
    assert remote_branch_exists(tmp_git_repo_with_remote, "does-not-exist") is False


def test_local_branch_exists_true_for_current_branch(tmp_git_repo):
    assert local_branch_exists(tmp_git_repo, "main") is True


def test_local_branch_exists_false_for_missing_branch(tmp_git_repo):
    assert local_branch_exists(tmp_git_repo, "feature/nope") is False


# Task 2: working tree state and branch operations
from icx_engine.git.gitcmd import (
    is_dirty, dirty_files, stash_push, stash_pop,
    create_branch_from, checkout, fast_forward,
)


def test_is_dirty_false_on_clean_repo(tmp_git_repo):
    assert is_dirty(tmp_git_repo) is False


def test_is_dirty_true_with_untracked_file(tmp_git_repo):
    (tmp_git_repo / "new.txt").write_text("x", encoding="utf-8")
    assert is_dirty(tmp_git_repo) is True


def test_dirty_files_lists_untracked_and_modified(tmp_git_repo):
    (tmp_git_repo / "new.txt").write_text("x", encoding="utf-8")
    (tmp_git_repo / "README.md").write_text("changed\n", encoding="utf-8")
    files = dirty_files(tmp_git_repo)
    assert set(files) == {"new.txt", "README.md"}


def test_dirty_files_empty_on_clean_repo(tmp_git_repo):
    assert dirty_files(tmp_git_repo) == []


def test_dirty_files_unquotes_paths_with_spaces(tmp_git_repo):
    (tmp_git_repo / "space file.txt").write_text("x", encoding="utf-8")
    files = dirty_files(tmp_git_repo)
    assert "space file.txt" in files
    assert '"space file.txt"' not in files


def test_stash_push_and_pop_round_trips(tmp_git_repo):
    (tmp_git_repo / "new.txt").write_text("x", encoding="utf-8")
    assert is_dirty(tmp_git_repo) is True
    stash_push(tmp_git_repo, "icx-test-stash")
    assert is_dirty(tmp_git_repo) is False
    stash_pop(tmp_git_repo)
    assert is_dirty(tmp_git_repo) is True
    assert "new.txt" in dirty_files(tmp_git_repo)


def test_create_branch_from_creates_and_does_not_switch(tmp_git_repo):
    create_branch_from(tmp_git_repo, "feature/x-ABC-1", "main")
    assert current_branch(tmp_git_repo) == "main"
    assert local_branch_exists(tmp_git_repo, "feature/x-ABC-1") is True


def test_checkout_switches_branch(tmp_git_repo):
    create_branch_from(tmp_git_repo, "feature/x-ABC-1", "main")
    checkout(tmp_git_repo, "feature/x-ABC-1")
    assert current_branch(tmp_git_repo) == "feature/x-ABC-1"


def test_fast_forward_advances_to_remote(tmp_git_repo_with_remote, tmp_path):
    import subprocess
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", str(tmp_git_repo_with_remote), str(clone)], check=True,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=str(clone), check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=str(clone), check=True)
    (tmp_git_repo_with_remote / "new.txt").write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "new.txt"], cwd=str(tmp_git_repo_with_remote), check=True)
    subprocess.run(["git", "commit", "-m", "second"], cwd=str(tmp_git_repo_with_remote), check=True)
    subprocess.run(["git", "push"], cwd=str(tmp_git_repo_with_remote), check=True)
    fetch(clone)
    fast_forward(clone, "main")
    assert (clone / "new.txt").exists()


def test_fast_forward_raises_when_diverged(tmp_git_repo_with_remote, tmp_path):
    import subprocess
    clone = tmp_path / "clone2"
    subprocess.run(["git", "clone", str(tmp_git_repo_with_remote), str(clone)], check=True,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=str(clone), check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=str(clone), check=True)
    (tmp_git_repo_with_remote / "remote_side.txt").write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "remote_side.txt"], cwd=str(tmp_git_repo_with_remote), check=True)
    subprocess.run(["git", "commit", "-m", "remote change"], cwd=str(tmp_git_repo_with_remote), check=True)
    subprocess.run(["git", "push"], cwd=str(tmp_git_repo_with_remote), check=True)
    (clone / "local_side.txt").write_text("y", encoding="utf-8")
    subprocess.run(["git", "add", "local_side.txt"], cwd=str(clone), check=True)
    subprocess.run(["git", "commit", "-m", "local change"], cwd=str(clone), check=True)
    fetch(clone)
    with pytest.raises(GitCommandError):
        fast_forward(clone, "main")


# Task 3: staging, commit, remote info, added-lines diff
from icx_engine.git.gitcmd import (
    stage_files, commit, remote_url, default_remote_head_branch, added_lines_diff,
)


def test_stage_files_only_stages_named_files(tmp_git_repo):
    (tmp_git_repo / "a.txt").write_text("a", encoding="utf-8")
    (tmp_git_repo / "b.txt").write_text("b", encoding="utf-8")
    stage_files(tmp_git_repo, ["a.txt"])
    import subprocess
    result = subprocess.run(["git", "diff", "--cached", "--name-only"], cwd=str(tmp_git_repo),
                             check=True, stdout=subprocess.PIPE)
    staged = result.stdout.decode().strip().splitlines()
    assert staged == ["a.txt"]
    assert "b.txt" in dirty_files(tmp_git_repo)


def test_commit_creates_commit_and_returns_sha(tmp_git_repo):
    (tmp_git_repo / "a.txt").write_text("a", encoding="utf-8")
    stage_files(tmp_git_repo, ["a.txt"])
    sha = commit(tmp_git_repo, "ABC-1 add a.txt")
    assert len(sha) == 40
    assert is_dirty(tmp_git_repo) is False


def test_remote_url_returns_configured_url(tmp_git_repo_with_remote):
    url = remote_url(tmp_git_repo_with_remote)
    assert url  # bare repo path on this machine; just needs to be non-empty and match config
    import subprocess
    expected = subprocess.run(["git", "remote", "get-url", "origin"], cwd=str(tmp_git_repo_with_remote),
                               check=True, stdout=subprocess.PIPE).stdout.decode().strip()
    assert url == expected


def test_default_remote_head_branch_returns_main(tmp_git_repo_with_remote):
    assert default_remote_head_branch(tmp_git_repo_with_remote) == "main"


def test_added_lines_diff_returns_only_plus_lines_per_file(tmp_git_repo):
    (tmp_git_repo / "README.md").write_text("hello\nworld\n", encoding="utf-8")
    (tmp_git_repo / "new.txt").write_text("brand new\n", encoding="utf-8")
    stage_files(tmp_git_repo, ["README.md", "new.txt"])
    diff = added_lines_diff(tmp_git_repo)
    assert diff["README.md"] == ["world"]
    assert diff["new.txt"] == ["brand new"]


def test_added_lines_diff_includes_content_line_starting_with_plus_plus_plus(tmp_git_repo):
    (tmp_git_repo / "b.txt").write_text("+++ another new plus line\n", encoding="utf-8")
    stage_files(tmp_git_repo, ["b.txt"])
    diff = added_lines_diff(tmp_git_repo)
    assert diff["b.txt"] == ["+++ another new plus line"]


def test_added_lines_diff_handles_multiple_files_correctly(tmp_git_repo):
    (tmp_git_repo / "one.txt").write_text("first file line\n", encoding="utf-8")
    (tmp_git_repo / "two.txt").write_text("second file line\n", encoding="utf-8")
    stage_files(tmp_git_repo, ["one.txt", "two.txt"])
    diff = added_lines_diff(tmp_git_repo)
    assert diff["one.txt"] == ["first file line"]
    assert diff["two.txt"] == ["second file line"]


# Task 1: merge and conflict detection primitives
from icx_engine.git.gitcmd import merge_ref, merge_abort, conflicted_files, conflict_versions


def _make_diverging_clone(tmp_git_repo_with_remote, tmp_path, name="clone"):
    """Push a commit on the 'remote' repo and a DIFFERENT, conflicting commit
    (same line of the same file) on a fresh clone, without fetching either
    into the other - sets up a real merge conflict for the next fetch+merge."""
    import subprocess
    clone = tmp_path / name
    subprocess.run(["git", "clone", str(tmp_git_repo_with_remote), str(clone)], check=True,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=str(clone), check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=str(clone), check=True)

    (tmp_git_repo_with_remote / "README.md").write_text("remote version\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=str(tmp_git_repo_with_remote), check=True)
    subprocess.run(["git", "commit", "-m", "remote change"], cwd=str(tmp_git_repo_with_remote), check=True)
    subprocess.run(["git", "push"], cwd=str(tmp_git_repo_with_remote), check=True)

    (clone / "README.md").write_text("local version\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=str(clone), check=True)
    subprocess.run(["git", "commit", "-m", "local change"], cwd=str(clone), check=True)
    return clone


def test_merge_ref_succeeds_cleanly_when_no_conflict(tmp_git_repo_with_remote, tmp_path):
    import subprocess
    clone = tmp_path / "clone_clean"
    subprocess.run(["git", "clone", str(tmp_git_repo_with_remote), str(clone)], check=True,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=str(clone), check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=str(clone), check=True)
    (tmp_git_repo_with_remote / "new.txt").write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "new.txt"], cwd=str(tmp_git_repo_with_remote), check=True)
    subprocess.run(["git", "commit", "-m", "remote change"], cwd=str(tmp_git_repo_with_remote), check=True)
    subprocess.run(["git", "push"], cwd=str(tmp_git_repo_with_remote), check=True)
    (clone / "other.txt").write_text("y", encoding="utf-8")
    subprocess.run(["git", "add", "other.txt"], cwd=str(clone), check=True)
    subprocess.run(["git", "commit", "-m", "local change"], cwd=str(clone), check=True)

    fetch(clone)
    merge_ref(clone, "origin/main")
    assert (clone / "new.txt").exists()
    assert (clone / "other.txt").exists()
    assert conflicted_files(clone) == []


def test_merge_ref_raises_on_conflict(tmp_git_repo_with_remote, tmp_path):
    clone = _make_diverging_clone(tmp_git_repo_with_remote, tmp_path)
    fetch(clone)
    with pytest.raises(GitCommandError):
        merge_ref(clone, "origin/main")


def test_conflicted_files_lists_the_conflicting_path(tmp_git_repo_with_remote, tmp_path):
    clone = _make_diverging_clone(tmp_git_repo_with_remote, tmp_path)
    fetch(clone)
    with pytest.raises(GitCommandError):
        merge_ref(clone, "origin/main")
    assert conflicted_files(clone) == ["README.md"]


def test_merge_abort_restores_clean_working_tree(tmp_git_repo_with_remote, tmp_path):
    clone = _make_diverging_clone(tmp_git_repo_with_remote, tmp_path)
    fetch(clone)
    with pytest.raises(GitCommandError):
        merge_ref(clone, "origin/main")
    merge_abort(clone)
    assert conflicted_files(clone) == []
    assert not (clone / ".git" / "MERGE_HEAD").exists()
    assert (clone / "README.md").read_text(encoding="utf-8") == "local version\n"


def test_conflict_versions_returns_ours_and_theirs(tmp_git_repo_with_remote, tmp_path):
    clone = _make_diverging_clone(tmp_git_repo_with_remote, tmp_path)
    fetch(clone)
    with pytest.raises(GitCommandError):
        merge_ref(clone, "origin/main")
    ours, theirs = conflict_versions(clone, "README.md")
    assert ours == "local version\n"
    assert theirs == "remote version\n"


# Task 2: branch delete, generic fast-forward, on-disk marker scan
from icx_engine.git.gitcmd import delete_branch, fast_forward_ref, find_conflict_markers


def test_delete_branch_removes_it(tmp_git_repo):
    create_branch_from(tmp_git_repo, "feature/throwaway-ABC-1", "main")
    assert local_branch_exists(tmp_git_repo, "feature/throwaway-ABC-1") is True
    delete_branch(tmp_git_repo, "feature/throwaway-ABC-1")
    assert local_branch_exists(tmp_git_repo, "feature/throwaway-ABC-1") is False


def test_delete_branch_force_removes_unmerged_branch(tmp_git_repo):
    create_branch_from(tmp_git_repo, "feature/unmerged-ABC-1", "main")
    checkout(tmp_git_repo, "feature/unmerged-ABC-1")
    (tmp_git_repo / "only_here.txt").write_text("x", encoding="utf-8")
    stage_files(tmp_git_repo, ["only_here.txt"])
    commit(tmp_git_repo, "ABC-1 unmerged commit")
    checkout(tmp_git_repo, "main")
    delete_branch(tmp_git_repo, "feature/unmerged-ABC-1", force=True)
    assert local_branch_exists(tmp_git_repo, "feature/unmerged-ABC-1") is False


def test_fast_forward_ref_advances_to_arbitrary_ref(tmp_git_repo):
    create_branch_from(tmp_git_repo, "feature/x-ABC-1", "main")
    checkout(tmp_git_repo, "feature/x-ABC-1")
    (tmp_git_repo / "new.txt").write_text("x", encoding="utf-8")
    stage_files(tmp_git_repo, ["new.txt"])
    commit(tmp_git_repo, "ABC-1 add new.txt")
    checkout(tmp_git_repo, "main")
    fast_forward_ref(tmp_git_repo, "feature/x-ABC-1")
    assert (tmp_git_repo / "new.txt").exists()
    assert current_branch(tmp_git_repo) == "main"


def test_fast_forward_ref_raises_when_not_an_ancestor(tmp_git_repo):
    create_branch_from(tmp_git_repo, "feature/unrelated-ABC-1", "main")
    checkout(tmp_git_repo, "feature/unrelated-ABC-1")
    (tmp_git_repo / "a.txt").write_text("a", encoding="utf-8")
    stage_files(tmp_git_repo, ["a.txt"])
    commit(tmp_git_repo, "ABC-1 a")
    checkout(tmp_git_repo, "main")
    (tmp_git_repo / "b.txt").write_text("b", encoding="utf-8")
    stage_files(tmp_git_repo, ["b.txt"])
    commit(tmp_git_repo, "ABC-1 b")
    with pytest.raises(GitCommandError):
        fast_forward_ref(tmp_git_repo, "feature/unrelated-ABC-1")


def test_fast_forward_still_works_against_remote_after_refactor(tmp_git_repo_with_remote, tmp_path):
    import subprocess
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", str(tmp_git_repo_with_remote), str(clone)], check=True,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=str(clone), check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=str(clone), check=True)
    (tmp_git_repo_with_remote / "new.txt").write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "new.txt"], cwd=str(tmp_git_repo_with_remote), check=True)
    subprocess.run(["git", "commit", "-m", "second"], cwd=str(tmp_git_repo_with_remote), check=True)
    subprocess.run(["git", "push"], cwd=str(tmp_git_repo_with_remote), check=True)
    fetch(clone)
    fast_forward(clone, "main")
    assert (clone / "new.txt").exists()


def test_find_conflict_markers_detects_all_three_marker_lines(tmp_git_repo):
    conflicted = tmp_git_repo / "conflicted.py"
    conflicted.write_text(
        "line one\n<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> origin/main\nline last\n",
        encoding="utf-8",
    )
    clean = tmp_git_repo / "clean.py"
    clean.write_text("no markers here\n", encoding="utf-8")
    result = find_conflict_markers(tmp_git_repo, ["conflicted.py", "clean.py"])
    assert result == {"conflicted.py": ["<<<<<<< HEAD", "=======", ">>>>>>> origin/main"]}


def test_find_conflict_markers_empty_dict_when_all_clean(tmp_git_repo):
    (tmp_git_repo / "clean.py").write_text("nothing to see\n", encoding="utf-8")
    result = find_conflict_markers(tmp_git_repo, ["clean.py"])
    assert result == {}


# Task 6: commit/file listing since a base ref, for MR description building
from icx_engine.git.gitcmd import commits_since, changed_files_since


def test_commits_since_lists_new_commits_on_current_branch(tmp_git_repo):
    create_branch_from(tmp_git_repo, "feature/x-ABC-1", "main")
    checkout(tmp_git_repo, "feature/x-ABC-1")
    (tmp_git_repo / "a.txt").write_text("a", encoding="utf-8")
    stage_files(tmp_git_repo, ["a.txt"])
    sha1 = commit(tmp_git_repo, "ABC-1 add a.txt")
    (tmp_git_repo / "b.txt").write_text("b", encoding="utf-8")
    stage_files(tmp_git_repo, ["b.txt"])
    sha2 = commit(tmp_git_repo, "ABC-1 add b.txt")
    result = commits_since(tmp_git_repo, "main")
    assert len(result) == 2
    assert any(sha2[:7] in line and "add b.txt" in line for line in result)
    assert any(sha1[:7] in line and "add a.txt" in line for line in result)


def test_commits_since_empty_when_no_new_commits(tmp_git_repo):
    create_branch_from(tmp_git_repo, "feature/x-ABC-1", "main")
    checkout(tmp_git_repo, "feature/x-ABC-1")
    assert commits_since(tmp_git_repo, "main") == []


def test_changed_files_since_lists_touched_paths(tmp_git_repo):
    create_branch_from(tmp_git_repo, "feature/x-ABC-1", "main")
    checkout(tmp_git_repo, "feature/x-ABC-1")
    (tmp_git_repo / "src" / "app.py").parent.mkdir(parents=True, exist_ok=True)
    (tmp_git_repo / "src" / "app.py").write_text("x", encoding="utf-8")
    stage_files(tmp_git_repo, ["src/app.py"])
    commit(tmp_git_repo, "ABC-1 add app.py")
    assert changed_files_since(tmp_git_repo, "main") == ["src/app.py"]


from icx_engine.git.gitcmd import file_exists_at_ref


def test_file_exists_at_ref_true_for_tracked_file(tmp_git_repo):
    assert file_exists_at_ref(tmp_git_repo, "main", "README.md") is True


def test_file_exists_at_ref_false_for_missing_file(tmp_git_repo):
    assert file_exists_at_ref(tmp_git_repo, "main", "does_not_exist.txt") is False


# Security: reject ref/branch/remote values that could be parsed as a git option
# (argument/flag injection defense - values may originate from untrusted MCP input)

_OPTION_LIKE = "--upload-pack=/tmp/x"


def test_checkout_rejects_option_like_branch(tmp_git_repo):
    with pytest.raises(GitCommandError):
        checkout(tmp_git_repo, _OPTION_LIKE)


def test_create_branch_from_rejects_option_like_new_branch(tmp_git_repo):
    with pytest.raises(GitCommandError):
        create_branch_from(tmp_git_repo, _OPTION_LIKE, "main")


def test_create_branch_from_rejects_option_like_start_point(tmp_git_repo):
    with pytest.raises(GitCommandError):
        create_branch_from(tmp_git_repo, "feature/x-ABC-1", _OPTION_LIKE)


def test_fast_forward_ref_rejects_option_like_ref(tmp_git_repo):
    with pytest.raises(GitCommandError):
        fast_forward_ref(tmp_git_repo, _OPTION_LIKE)


def test_fast_forward_rejects_option_like_branch(tmp_git_repo):
    with pytest.raises(GitCommandError):
        fast_forward(tmp_git_repo, _OPTION_LIKE)


def test_fast_forward_rejects_option_like_remote(tmp_git_repo):
    with pytest.raises(GitCommandError):
        fast_forward(tmp_git_repo, "main", remote=_OPTION_LIKE)


def test_merge_ref_rejects_option_like_ref(tmp_git_repo):
    with pytest.raises(GitCommandError):
        merge_ref(tmp_git_repo, _OPTION_LIKE)


def test_delete_branch_rejects_option_like_branch(tmp_git_repo):
    with pytest.raises(GitCommandError):
        delete_branch(tmp_git_repo, _OPTION_LIKE)


def test_remote_branch_exists_rejects_option_like_branch(tmp_git_repo):
    with pytest.raises(GitCommandError):
        remote_branch_exists(tmp_git_repo, _OPTION_LIKE)


def test_remote_branch_exists_rejects_option_like_remote(tmp_git_repo):
    with pytest.raises(GitCommandError):
        remote_branch_exists(tmp_git_repo, "main", remote=_OPTION_LIKE)


def test_local_branch_exists_rejects_option_like_branch(tmp_git_repo):
    with pytest.raises(GitCommandError):
        local_branch_exists(tmp_git_repo, _OPTION_LIKE)


def test_fetch_rejects_option_like_remote(tmp_git_repo):
    with pytest.raises(GitCommandError):
        fetch(tmp_git_repo, remote=_OPTION_LIKE)


def test_commits_since_rejects_option_like_base_ref(tmp_git_repo):
    with pytest.raises(GitCommandError):
        commits_since(tmp_git_repo, _OPTION_LIKE)


def test_changed_files_since_rejects_option_like_base_ref(tmp_git_repo):
    with pytest.raises(GitCommandError):
        changed_files_since(tmp_git_repo, _OPTION_LIKE)


def test_file_exists_at_ref_rejects_option_like_ref(tmp_git_repo):
    with pytest.raises(GitCommandError):
        file_exists_at_ref(tmp_git_repo, _OPTION_LIKE, "README.md")


def test_option_like_rejection_happens_before_any_subprocess_spawn(tmp_git_repo, monkeypatch):
    """Proves the validator short-circuits before git is ever invoked - not just
    that git itself would later reject the flag-shaped value."""
    from icx_engine.git import gitcmd

    def _boom(*args, **kwargs):
        raise AssertionError("subprocess.run must not be called when validation rejects the input")

    monkeypatch.setattr(gitcmd.subprocess, "run", _boom)
    with pytest.raises(GitCommandError):
        checkout(tmp_git_repo, _OPTION_LIKE)
    with pytest.raises(GitCommandError):
        merge_ref(tmp_git_repo, "--force")
    with pytest.raises(GitCommandError):
        delete_branch(tmp_git_repo, "-D")


# Task: blame, log, show_commit, diff_between - read-only history inspection

from icx_engine.git.gitcmd import blame, log, show_commit, diff_between


def test_blame_attributes_correct_commit_and_author_per_line(tmp_git_repo):
    target = tmp_git_repo / "target.txt"
    target.write_text("line one\nline two\nline three\n", encoding="utf-8")
    stage_files(tmp_git_repo, ["target.txt"])
    sha1 = commit(tmp_git_repo, "ABC-1 add target")

    import subprocess
    subprocess.run(["git", "config", "user.name", "Second Author"], cwd=str(tmp_git_repo), check=True)
    subprocess.run(["git", "config", "user.email", "second@example.com"], cwd=str(tmp_git_repo), check=True)
    target.write_text("line one\nCHANGED two\nline three\n", encoding="utf-8")
    stage_files(tmp_git_repo, ["target.txt"])
    sha2 = commit(tmp_git_repo, "ABC-1 change line two")

    result = blame(tmp_git_repo, "target.txt")
    assert len(result) == 3
    assert result[0]["commit_sha"] == sha1
    assert result[0]["content"] == "line one"
    assert result[1]["commit_sha"] == sha2
    assert result[1]["content"] == "CHANGED two"
    assert result[1]["author"] == "Second Author"
    assert result[1]["author_email"] == "second@example.com"
    assert result[2]["commit_sha"] == sha1
    assert [entry["line_no"] for entry in result] == [1, 2, 3]


def test_blame_line_range_narrows_results(tmp_git_repo):
    target = tmp_git_repo / "target.txt"
    target.write_text("one\ntwo\nthree\nfour\nfive\n", encoding="utf-8")
    stage_files(tmp_git_repo, ["target.txt"])
    commit(tmp_git_repo, "ABC-1 add target")

    result = blame(tmp_git_repo, "target.txt", line_range=(2, 3))
    assert [entry["line_no"] for entry in result] == [2, 3]
    assert [entry["content"] for entry in result] == ["two", "three"]


def test_blame_rejects_path_traversal(tmp_git_repo):
    with pytest.raises(GitCommandError):
        blame(tmp_git_repo, "../outside.txt")


def test_log_orders_newest_first(tmp_git_repo):
    (tmp_git_repo / "a.txt").write_text("a", encoding="utf-8")
    stage_files(tmp_git_repo, ["a.txt"])
    sha1 = commit(tmp_git_repo, "ABC-1 first")
    (tmp_git_repo / "b.txt").write_text("b", encoding="utf-8")
    stage_files(tmp_git_repo, ["b.txt"])
    sha2 = commit(tmp_git_repo, "ABC-1 second")

    result = log(tmp_git_repo)
    shas = [entry["sha"] for entry in result]
    assert shas.index(sha2) < shas.index(sha1)
    assert result[0]["subject"] == "ABC-1 second"


def test_log_limit_truncates(tmp_git_repo):
    for i in range(5):
        (tmp_git_repo / f"f{i}.txt").write_text(str(i), encoding="utf-8")
        stage_files(tmp_git_repo, [f"f{i}.txt"])
        commit(tmp_git_repo, f"ABC-1 commit {i}")

    result = log(tmp_git_repo, limit=3)
    assert len(result) == 3


def test_log_relpath_scopes_to_file(tmp_git_repo):
    (tmp_git_repo / "only_a.txt").write_text("a", encoding="utf-8")
    stage_files(tmp_git_repo, ["only_a.txt"])
    sha_a = commit(tmp_git_repo, "ABC-1 touch only_a")
    (tmp_git_repo / "only_b.txt").write_text("b", encoding="utf-8")
    stage_files(tmp_git_repo, ["only_b.txt"])
    commit(tmp_git_repo, "ABC-1 touch only_b")

    result = log(tmp_git_repo, relpath="only_a.txt")
    shas = [entry["sha"] for entry in result]
    assert shas == [sha_a]


def test_log_author_filters(tmp_git_repo):
    import subprocess
    (tmp_git_repo / "a.txt").write_text("a", encoding="utf-8")
    stage_files(tmp_git_repo, ["a.txt"])
    sha1 = commit(tmp_git_repo, "ABC-1 by default author")

    subprocess.run(["git", "config", "user.name", "Other Author"], cwd=str(tmp_git_repo), check=True)
    subprocess.run(["git", "config", "user.email", "other@example.com"], cwd=str(tmp_git_repo), check=True)
    (tmp_git_repo / "b.txt").write_text("b", encoding="utf-8")
    stage_files(tmp_git_repo, ["b.txt"])
    sha2 = commit(tmp_git_repo, "ABC-1 by other author")

    result = log(tmp_git_repo, author="Other Author")
    shas = [entry["sha"] for entry in result]
    assert shas == [sha2]
    assert sha1 not in shas


def test_show_commit_returns_message_author_and_files(tmp_git_repo):
    (tmp_git_repo / "new.txt").write_text("x", encoding="utf-8")
    stage_files(tmp_git_repo, ["new.txt"])
    sha = commit(tmp_git_repo, "ABC-1 add new.txt")

    result = show_commit(tmp_git_repo, sha)
    assert result["sha"] == sha
    assert result["subject"] == "ABC-1 add new.txt"
    assert result["author_email"] == "test@example.com"
    assert result["files"] == [{"path": "new.txt", "status": "A"}]


def test_show_commit_raises_for_bogus_sha(tmp_git_repo):
    with pytest.raises(GitCommandError):
        show_commit(tmp_git_repo, "0" * 40)


def test_show_commit_rejects_option_like_sha(tmp_git_repo):
    with pytest.raises(GitCommandError):
        show_commit(tmp_git_repo, _OPTION_LIKE)


def test_diff_between_reports_added_modified_deleted(tmp_git_repo):
    (tmp_git_repo / "to_delete.txt").write_text("will be removed\n", encoding="utf-8")
    stage_files(tmp_git_repo, ["to_delete.txt"])
    commit(tmp_git_repo, "ABC-1 add to_delete.txt")

    create_branch_from(tmp_git_repo, "feature/diff-b", "main")
    create_branch_from(tmp_git_repo, "feature/diff-a", "main")
    checkout(tmp_git_repo, "feature/diff-a")

    (tmp_git_repo / "added.txt").write_text("brand new\nsecond line\n", encoding="utf-8")
    stage_files(tmp_git_repo, ["added.txt"])
    commit(tmp_git_repo, "ABC-1 add added.txt")

    (tmp_git_repo / "README.md").write_text("changed\n", encoding="utf-8")
    stage_files(tmp_git_repo, ["README.md"])
    commit(tmp_git_repo, "ABC-1 modify README")

    import subprocess
    subprocess.run(["git", "rm", "to_delete.txt"], cwd=str(tmp_git_repo), check=True,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    commit(tmp_git_repo, "ABC-1 remove to_delete.txt")

    result = diff_between(tmp_git_repo, "feature/diff-b", "feature/diff-a")
    by_path = {entry["path"]: entry for entry in result["files"]}

    assert by_path["added.txt"]["status"] == "A"
    assert by_path["added.txt"]["insertions"] == 2
    assert by_path["added.txt"]["deletions"] == 0

    assert by_path["README.md"]["status"] == "M"
    assert by_path["README.md"]["insertions"] == 1
    assert by_path["README.md"]["deletions"] == 1

    assert by_path["to_delete.txt"]["status"] == "D"
    assert by_path["to_delete.txt"]["insertions"] == 0
    assert by_path["to_delete.txt"]["deletions"] == 1


def test_diff_between_rejects_option_like_ref_a(tmp_git_repo):
    with pytest.raises(GitCommandError):
        diff_between(tmp_git_repo, _OPTION_LIKE, "main")


def test_diff_between_rejects_option_like_ref_b(tmp_git_repo):
    with pytest.raises(GitCommandError):
        diff_between(tmp_git_repo, "main", _OPTION_LIKE)


def test_read_only_functions_reject_option_like_before_subprocess_spawn(tmp_git_repo, monkeypatch):
    from icx_engine.git import gitcmd

    def _boom(*args, **kwargs):
        raise AssertionError("subprocess.run must not be called when validation rejects the input")

    monkeypatch.setattr(gitcmd.subprocess, "run", _boom)
    with pytest.raises(GitCommandError):
        show_commit(tmp_git_repo, _OPTION_LIKE)
    with pytest.raises(GitCommandError):
        diff_between(tmp_git_repo, _OPTION_LIKE, "main")
    with pytest.raises(GitCommandError):
        blame(tmp_git_repo, "../outside.txt")


# Task: push() - plain, non-force push to a remote

from icx_engine.git.gitcmd import push


def test_push_puts_branch_on_remote(tmp_git_repo_with_remote):
    create_branch_from(tmp_git_repo_with_remote, "feature/push-me", "main")
    checkout(tmp_git_repo_with_remote, "feature/push-me")
    assert remote_branch_exists(tmp_git_repo_with_remote, "feature/push-me") is False
    push(tmp_git_repo_with_remote, "feature/push-me")
    assert remote_branch_exists(tmp_git_repo_with_remote, "feature/push-me") is True


def test_push_rejects_option_like_branch(tmp_git_repo):
    with pytest.raises(GitCommandError):
        push(tmp_git_repo, _OPTION_LIKE)


def test_push_rejects_option_like_remote(tmp_git_repo):
    with pytest.raises(GitCommandError):
        push(tmp_git_repo, "main", remote=_OPTION_LIKE)


def test_push_rejects_option_like_before_subprocess_spawn(tmp_git_repo, monkeypatch):
    from icx_engine.git import gitcmd

    def _boom(*args, **kwargs):
        raise AssertionError("subprocess.run must not be called when validation rejects the input")

    monkeypatch.setattr(gitcmd.subprocess, "run", _boom)
    with pytest.raises(GitCommandError):
        push(tmp_git_repo, _OPTION_LIKE)
