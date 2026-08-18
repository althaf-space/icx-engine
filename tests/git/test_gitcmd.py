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
    stage_files, commit, remote_url, default_remote_head_branch, added_lines_diff, list_ignored,
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


def test_stage_files_stages_an_unstaged_deletion(tmp_git_repo):
    import subprocess
    (tmp_git_repo / "gone.txt").write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "gone.txt"], cwd=str(tmp_git_repo), check=True)
    subprocess.run(["git", "commit", "-m", "add gone.txt"], cwd=str(tmp_git_repo), check=True)
    (tmp_git_repo / "gone.txt").unlink()
    stage_files(tmp_git_repo, ["gone.txt"])
    result = subprocess.run(["git", "diff", "--cached", "--name-status"], cwd=str(tmp_git_repo),
                             check=True, stdout=subprocess.PIPE)
    assert result.stdout.decode().strip() == "D\tgone.txt"


def test_stage_files_tolerates_an_already_fully_staged_deletion(tmp_git_repo):
    """Real bug repro: a path already removed from BOTH the working tree AND the
    index (e.g. re-listed by a caller that already staged its deletion in a prior
    cycle) previously made `git add` fail with 'pathspec did not match any files' -
    failing the ENTIRE batch, including unrelated files also being staged."""
    import subprocess
    (tmp_git_repo / "already_gone.txt").write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "already_gone.txt"], cwd=str(tmp_git_repo), check=True)
    subprocess.run(["git", "commit", "-m", "add already_gone.txt"], cwd=str(tmp_git_repo), check=True)
    subprocess.run(["git", "rm", "--cached", "already_gone.txt"], cwd=str(tmp_git_repo), check=True)
    (tmp_git_repo / "already_gone.txt").unlink()
    # Fully staged deletion now - nothing left in the index or on disk for this path.
    (tmp_git_repo / "new.txt").write_text("new", encoding="utf-8")

    stage_files(tmp_git_repo, ["already_gone.txt", "new.txt"])  # must not raise

    result = subprocess.run(["git", "diff", "--cached", "--name-status"], cwd=str(tmp_git_repo),
                             check=True, stdout=subprocess.PIPE)
    staged = dict(line.split("\t", 1) for line in result.stdout.decode().strip().splitlines())
    assert staged == {"D": "already_gone.txt", "A": "new.txt"}


def test_stage_files_all_already_gone_is_a_noop(tmp_git_repo):
    import subprocess
    (tmp_git_repo / "already_gone.txt").write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "already_gone.txt"], cwd=str(tmp_git_repo), check=True)
    subprocess.run(["git", "commit", "-m", "add already_gone.txt"], cwd=str(tmp_git_repo), check=True)
    subprocess.run(["git", "rm", "--cached", "already_gone.txt"], cwd=str(tmp_git_repo), check=True)
    (tmp_git_repo / "already_gone.txt").unlink()

    stage_files(tmp_git_repo, ["already_gone.txt"])  # must not raise, must not call `git add` at all

    result = subprocess.run(["git", "diff", "--cached", "--name-status"], cwd=str(tmp_git_repo),
                             check=True, stdout=subprocess.PIPE)
    assert result.stdout.decode().strip() == "D\talready_gone.txt"


def test_list_ignored_returns_only_the_gitignored_subset(tmp_git_repo):
    (tmp_git_repo / ".gitignore").write_text("*.gitkeep_ignored\n", encoding="utf-8")
    (tmp_git_repo / "a.gitkeep_ignored").write_text("x", encoding="utf-8")
    (tmp_git_repo / "normal.txt").write_text("x", encoding="utf-8")
    assert list_ignored(tmp_git_repo, ["a.gitkeep_ignored", "normal.txt"]) == ["a.gitkeep_ignored"]


def test_list_ignored_empty_when_nothing_matches(tmp_git_repo):
    (tmp_git_repo / "normal.txt").write_text("x", encoding="utf-8")
    assert list_ignored(tmp_git_repo, ["normal.txt"]) == []


def test_list_ignored_empty_list_input_is_a_noop(tmp_git_repo):
    assert list_ignored(tmp_git_repo, []) == []


def test_list_ignored_rejects_option_like_files(tmp_git_repo):
    with pytest.raises(GitCommandError):
        list_ignored(tmp_git_repo, [_OPTION_LIKE])


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


# Task: changed_files_since_common_ancestor - reverse-direction "what did the OTHER
# side touch since we diverged" (stale-base silent-deletion detection)
from icx_engine.git.gitcmd import changed_files_since_common_ancestor


def test_changed_files_since_common_ancestor_reports_only_other_sides_files(tmp_git_repo):
    (tmp_git_repo / "shared.txt").write_text("base\n", encoding="utf-8")
    stage_files(tmp_git_repo, ["shared.txt"])
    commit(tmp_git_repo, "add shared.txt")

    create_branch_from(tmp_git_repo, "feature/x-ABC-1", "main")
    checkout(tmp_git_repo, "feature/x-ABC-1")
    (tmp_git_repo / "feature_only.txt").write_text("f", encoding="utf-8")
    stage_files(tmp_git_repo, ["feature_only.txt"])
    commit(tmp_git_repo, "feature-only commit")

    checkout(tmp_git_repo, "main")
    (tmp_git_repo / "shared.txt").write_text("changed upstream\n", encoding="utf-8")
    stage_files(tmp_git_repo, ["shared.txt"])
    commit(tmp_git_repo, "upstream touches shared.txt")
    (tmp_git_repo / "main_only.txt").write_text("m", encoding="utf-8")
    stage_files(tmp_git_repo, ["main_only.txt"])
    commit(tmp_git_repo, "upstream-only commit")

    checkout(tmp_git_repo, "feature/x-ABC-1")
    result = changed_files_since_common_ancestor(tmp_git_repo, "main")
    assert set(result) == {"shared.txt", "main_only.txt"}
    assert "feature_only.txt" not in result


def test_changed_files_since_common_ancestor_empty_when_other_side_unchanged(tmp_git_repo):
    create_branch_from(tmp_git_repo, "feature/x-ABC-1", "main")
    checkout(tmp_git_repo, "feature/x-ABC-1")
    (tmp_git_repo / "feature_only.txt").write_text("f", encoding="utf-8")
    stage_files(tmp_git_repo, ["feature_only.txt"])
    commit(tmp_git_repo, "feature-only commit")
    assert changed_files_since_common_ancestor(tmp_git_repo, "main") == []


def test_changed_files_since_common_ancestor_rejects_option_like_ref(tmp_git_repo):
    with pytest.raises(GitCommandError):
        changed_files_since_common_ancestor(tmp_git_repo, _OPTION_LIKE)


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


# Task: extra_env - GitLab auth-header injection plumbing for fetch/push

def test_safe_git_env_merges_extra_env_on_top_of_hardened_base():
    from icx_engine.git.gitcmd import _safe_git_env, _GIT_SAFE_ENV
    env = _safe_git_env({"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.extraheader"})
    assert env["GIT_CONFIG_COUNT"] == "1"
    assert env["GIT_CONFIG_KEY_0"] == "http.extraheader"
    for key, value in _GIT_SAFE_ENV.items():
        assert env[key] == value


def test_safe_git_env_with_no_extra_env_matches_prior_behavior():
    from icx_engine.git.gitcmd import _safe_git_env
    env = _safe_git_env()
    assert env == _safe_git_env(None)


def test_fetch_forwards_extra_env_to_subprocess(tmp_git_repo_with_remote, monkeypatch):
    from icx_engine.git import gitcmd
    captured = {}
    real_run = gitcmd.subprocess.run

    def _spy(*args, **kwargs):
        captured["env"] = kwargs.get("env")
        return real_run(*args, **kwargs)

    monkeypatch.setattr(gitcmd.subprocess, "run", _spy)
    fetch(tmp_git_repo_with_remote, extra_env={"GIT_CONFIG_COUNT": "0"})
    assert captured["env"]["GIT_CONFIG_COUNT"] == "0"


def test_push_forwards_extra_env_to_subprocess(tmp_git_repo_with_remote, monkeypatch):
    from icx_engine.git import gitcmd
    captured = {}
    real_run = gitcmd.subprocess.run

    def _spy(*args, **kwargs):
        captured["env"] = kwargs.get("env")
        return real_run(*args, **kwargs)

    monkeypatch.setattr(gitcmd.subprocess, "run", _spy)
    create_branch_from(tmp_git_repo_with_remote, "feature/push-with-env", "main")
    checkout(tmp_git_repo_with_remote, "feature/push-with-env")
    push(tmp_git_repo_with_remote, "feature/push-with-env", extra_env={"GIT_CONFIG_COUNT": "0"})
    assert captured["env"]["GIT_CONFIG_COUNT"] == "0"


def test_push_with_no_extra_env_still_works_against_real_remote(tmp_git_repo_with_remote):
    """No regression for the common case - local/SSH remotes never compute an
    auth env at all (manager._gitlab_push_auth_env returns None), so push must
    keep working exactly as before when extra_env is omitted."""
    create_branch_from(tmp_git_repo_with_remote, "feature/push-no-env", "main")
    checkout(tmp_git_repo_with_remote, "feature/push-no-env")
    push(tmp_git_repo_with_remote, "feature/push-no-env")
    assert remote_branch_exists(tmp_git_repo_with_remote, "feature/push-no-env") is True


# Task: diff_worktree - local uncommitted diff (staged/unstaged/combined)
from icx_engine.git.gitcmd import diff_worktree


def test_diff_worktree_staged_reports_only_index_changes(tmp_git_repo):
    (tmp_git_repo / "staged.txt").write_text("staged content\n", encoding="utf-8")
    stage_files(tmp_git_repo, ["staged.txt"])
    (tmp_git_repo / "unstaged.txt").write_text("unstaged content\n", encoding="utf-8")
    result = diff_worktree(tmp_git_repo, mode="staged")
    paths = {f["path"] for f in result["files"]}
    assert paths == {"staged.txt"}


def test_diff_worktree_unstaged_reports_only_worktree_changes(tmp_git_repo):
    (tmp_git_repo / "staged.txt").write_text("staged content\n", encoding="utf-8")
    stage_files(tmp_git_repo, ["staged.txt"])
    (tmp_git_repo / "README.md").write_text("changed but not staged\n", encoding="utf-8")
    result = diff_worktree(tmp_git_repo, mode="unstaged")
    paths = {f["path"] for f in result["files"]}
    assert paths == {"README.md"}


def test_diff_worktree_combined_reports_staged_and_unstaged_together(tmp_git_repo):
    (tmp_git_repo / "staged.txt").write_text("staged content\n", encoding="utf-8")
    stage_files(tmp_git_repo, ["staged.txt"])
    (tmp_git_repo / "README.md").write_text("changed but not staged\n", encoding="utf-8")
    result = diff_worktree(tmp_git_repo, mode="combined")
    paths = {f["path"] for f in result["files"]}
    assert paths == {"staged.txt", "README.md"}


def test_diff_worktree_scopes_to_one_relpath(tmp_git_repo):
    (tmp_git_repo / "a.txt").write_text("a\n", encoding="utf-8")
    (tmp_git_repo / "b.txt").write_text("b\n", encoding="utf-8")
    result = diff_worktree(tmp_git_repo, mode="combined", relpath="a.txt")
    paths = {f["path"] for f in result["files"]}
    assert paths == set()  # untracked files never show up in `git diff` - only tracked changes do


def test_diff_worktree_unstaged_scoped_to_relpath_excludes_other_files(tmp_git_repo):
    (tmp_git_repo / "README.md").write_text("changed\n", encoding="utf-8")
    (tmp_git_repo / "extra.txt").write_text("x", encoding="utf-8")
    stage_files(tmp_git_repo, ["extra.txt"])
    result = diff_worktree(tmp_git_repo, mode="unstaged", relpath="README.md")
    paths = {f["path"] for f in result["files"]}
    assert paths == {"README.md"}


def test_diff_worktree_rejects_invalid_mode(tmp_git_repo):
    with pytest.raises(GitCommandError):
        diff_worktree(tmp_git_repo, mode="bogus")


def test_diff_worktree_rejects_path_traversal_relpath(tmp_git_repo):
    with pytest.raises(GitCommandError):
        diff_worktree(tmp_git_repo, mode="combined", relpath="../outside.txt")


# Task: structured_status - rich staged/unstaged/untracked/deleted/renamed/conflicted/ahead/behind
from icx_engine.git.gitcmd import structured_status


def test_structured_status_clean_repo_reports_empty_buckets(tmp_git_repo):
    result = structured_status(tmp_git_repo)
    assert result["staged"] == []
    assert result["unstaged"] == []
    assert result["untracked"] == []
    assert result["deleted"] == []
    assert result["renamed"] == []
    assert result["conflicted"] == []
    assert result["ahead"] == 0
    assert result["behind"] == 0
    assert result["current_branch"] == "main"


def test_structured_status_reports_staged_and_unstaged_separately(tmp_git_repo):
    (tmp_git_repo / "staged.txt").write_text("x", encoding="utf-8")
    stage_files(tmp_git_repo, ["staged.txt"])
    (tmp_git_repo / "README.md").write_text("changed\n", encoding="utf-8")
    result = structured_status(tmp_git_repo)
    staged_paths = {e["path"]: e["status"] for e in result["staged"]}
    unstaged_paths = {e["path"]: e["status"] for e in result["unstaged"]}
    assert staged_paths == {"staged.txt": "added"}
    assert unstaged_paths == {"README.md": "modified"}


def test_structured_status_reports_untracked_file(tmp_git_repo):
    (tmp_git_repo / "new.txt").write_text("x", encoding="utf-8")
    result = structured_status(tmp_git_repo)
    assert result["untracked"] == ["new.txt"]


def test_structured_status_reports_staged_deletion(tmp_git_repo):
    import subprocess
    subprocess.run(["git", "rm", "README.md"], cwd=str(tmp_git_repo), check=True,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    result = structured_status(tmp_git_repo)
    assert result["deleted"] == ["README.md"]
    staged_paths = {e["path"]: e["status"] for e in result["staged"]}
    assert staged_paths == {"README.md": "deleted"}


def test_structured_status_reports_staged_rename(tmp_git_repo):
    import subprocess
    subprocess.run(["git", "mv", "README.md", "RENAMED.md"], cwd=str(tmp_git_repo), check=True,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    result = structured_status(tmp_git_repo)
    assert result["renamed"] == [{"from": "README.md", "to": "RENAMED.md"}]


def test_structured_status_reports_conflicted_file_during_merge(tmp_git_repo_with_remote, tmp_path):
    clone = _make_diverging_clone(tmp_git_repo_with_remote, tmp_path, name="status_conflict_clone")
    fetch(clone)
    with pytest.raises(GitCommandError):
        merge_ref(clone, "origin/main")
    result = structured_status(clone)
    assert result["conflicted"] == ["README.md"]


def test_structured_status_reports_upstream_and_ahead_behind(tmp_git_repo_with_remote):
    (tmp_git_repo_with_remote / "local.txt").write_text("x", encoding="utf-8")
    stage_files(tmp_git_repo_with_remote, ["local.txt"])
    commit(tmp_git_repo_with_remote, "ABC-1 local only commit")
    result = structured_status(tmp_git_repo_with_remote)
    assert result["upstream"] == "origin/main"
    assert result["ahead"] == 1
    assert result["behind"] == 0


def test_structured_status_no_upstream_defaults_ahead_behind_to_zero(tmp_git_repo):
    result = structured_status(tmp_git_repo)
    assert result["upstream"] is None
    assert result["ahead"] == 0
    assert result["behind"] == 0


# Task: read_file_at_ref - read-only file content at any ref
from icx_engine.git.gitcmd import read_file_at_ref


def test_read_file_at_ref_returns_content_at_head(tmp_git_repo):
    content = read_file_at_ref(tmp_git_repo, "HEAD", "README.md")
    assert content == "hello\n"


def test_read_file_at_ref_returns_content_at_branch(tmp_git_repo):
    create_branch_from(tmp_git_repo, "feature/x-ABC-1", "main")
    checkout(tmp_git_repo, "feature/x-ABC-1")
    (tmp_git_repo / "README.md").write_text("changed on feature\n", encoding="utf-8")
    stage_files(tmp_git_repo, ["README.md"])
    commit(tmp_git_repo, "ABC-1 change readme")
    assert read_file_at_ref(tmp_git_repo, "main", "README.md") == "hello\n"
    assert read_file_at_ref(tmp_git_repo, "feature/x-ABC-1", "README.md") == "changed on feature\n"


def test_read_file_at_ref_raises_for_missing_path(tmp_git_repo):
    with pytest.raises(GitCommandError):
        read_file_at_ref(tmp_git_repo, "HEAD", "does_not_exist.txt")


def test_read_file_at_ref_raises_for_unresolvable_ref(tmp_git_repo):
    with pytest.raises(GitCommandError):
        read_file_at_ref(tmp_git_repo, "not-a-real-ref", "README.md")


def test_read_file_at_ref_rejects_option_like_ref(tmp_git_repo):
    with pytest.raises(GitCommandError):
        read_file_at_ref(tmp_git_repo, _OPTION_LIKE, "README.md")


def test_read_file_at_ref_rejects_path_traversal(tmp_git_repo):
    with pytest.raises(GitCommandError):
        read_file_at_ref(tmp_git_repo, "HEAD", "../outside.txt")


# Task: stash_list/apply/drop, stash_pop(ref=...) - full stash API
from icx_engine.git.gitcmd import stash_list, stash_apply, stash_drop


def test_stash_list_empty_on_clean_repo(tmp_git_repo):
    assert stash_list(tmp_git_repo) == []


def test_stash_list_reports_message_and_ref_newest_first(tmp_git_repo):
    (tmp_git_repo / "a.txt").write_text("a", encoding="utf-8")
    stash_push(tmp_git_repo, "first stash")
    (tmp_git_repo / "b.txt").write_text("b", encoding="utf-8")
    stash_push(tmp_git_repo, "second stash")
    stashes = stash_list(tmp_git_repo)
    assert len(stashes) == 2
    assert stashes[0]["ref"] == "stash@{0}"
    assert stashes[0]["message"].endswith("second stash")
    assert stashes[1]["ref"] == "stash@{1}"
    assert stashes[1]["message"].endswith("first stash")


def test_stash_apply_restores_changes_without_removing_stash(tmp_git_repo):
    (tmp_git_repo / "new.txt").write_text("x", encoding="utf-8")
    stash_push(tmp_git_repo, "keep me")
    assert is_dirty(tmp_git_repo) is False
    stash_apply(tmp_git_repo)
    assert is_dirty(tmp_git_repo) is True
    assert len(stash_list(tmp_git_repo)) == 1  # still there - apply never removes it


def test_stash_apply_by_explicit_ref(tmp_git_repo):
    (tmp_git_repo / "a.txt").write_text("a", encoding="utf-8")
    stash_push(tmp_git_repo, "older")
    (tmp_git_repo / "b.txt").write_text("b", encoding="utf-8")
    stash_push(tmp_git_repo, "newer")
    stash_apply(tmp_git_repo, "stash@{1}")
    assert (tmp_git_repo / "a.txt").exists()
    assert not (tmp_git_repo / "b.txt").exists()


def test_stash_pop_with_explicit_ref_removes_only_that_stash(tmp_git_repo):
    (tmp_git_repo / "a.txt").write_text("a", encoding="utf-8")
    stash_push(tmp_git_repo, "older")
    (tmp_git_repo / "b.txt").write_text("b", encoding="utf-8")
    stash_push(tmp_git_repo, "newer")
    stash_pop(tmp_git_repo, "stash@{1}")
    assert (tmp_git_repo / "a.txt").exists()
    remaining = stash_list(tmp_git_repo)
    assert len(remaining) == 1
    assert remaining[0]["message"].endswith("newer")


def test_stash_pop_default_still_pops_top_stash(tmp_git_repo):
    (tmp_git_repo / "new.txt").write_text("x", encoding="utf-8")
    stash_push(tmp_git_repo, "top")
    stash_pop(tmp_git_repo)
    assert (tmp_git_repo / "new.txt").exists()
    assert stash_list(tmp_git_repo) == []


def test_stash_drop_removes_stash_permanently(tmp_git_repo):
    (tmp_git_repo / "new.txt").write_text("x", encoding="utf-8")
    stash_push(tmp_git_repo, "to drop")
    stash_drop(tmp_git_repo)
    assert stash_list(tmp_git_repo) == []
    assert not (tmp_git_repo / "new.txt").exists()


def test_stash_apply_rejects_option_like_ref(tmp_git_repo):
    with pytest.raises(GitCommandError):
        stash_apply(tmp_git_repo, _OPTION_LIKE)


def test_stash_drop_rejects_option_like_ref(tmp_git_repo):
    with pytest.raises(GitCommandError):
        stash_drop(tmp_git_repo, _OPTION_LIKE)


def test_stash_pop_rejects_option_like_ref(tmp_git_repo):
    with pytest.raises(GitCommandError):
        stash_pop(tmp_git_repo, _OPTION_LIKE)


# Task: fetch() ref/prune options
def test_fetch_with_ref_updates_that_branchs_remote_tracking_ref(tmp_git_repo_with_remote, tmp_path):
    import subprocess
    bare_origin = tmp_path / "origin.git"
    clone = tmp_path / "clone_fetch_ref"
    subprocess.run(["git", "clone", str(bare_origin), str(clone)], check=True,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    create_branch_from(tmp_git_repo_with_remote, "feature/only-remote", "main")
    checkout(tmp_git_repo_with_remote, "feature/only-remote")
    (tmp_git_repo_with_remote / "extra.txt").write_text("x", encoding="utf-8")
    stage_files(tmp_git_repo_with_remote, ["extra.txt"])
    commit(tmp_git_repo_with_remote, "ABC-1 extra")
    subprocess.run(["git", "push", "origin", "feature/only-remote"], cwd=str(tmp_git_repo_with_remote), check=True)
    fetch(clone, ref="feature/only-remote")
    result = subprocess.run(["git", "rev-parse", "--verify", "origin/feature/only-remote"],
                             cwd=str(clone), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert result.returncode == 0


def test_fetch_with_prune_removes_deleted_remote_tracking_refs(tmp_git_repo_with_remote, tmp_path):
    import subprocess
    # Clone from the shared BARE origin (tmp_path / "origin.git", set up by the
    # tmp_git_repo_with_remote fixture) - not from tmp_git_repo_with_remote's own
    # working copy, which would make this clone's "origin" a different, unrelated
    # non-bare remote that a push to the real bare origin never touches.
    bare_origin = tmp_path / "origin.git"
    clone = tmp_path / "clone_fetch_prune"
    subprocess.run(["git", "clone", str(bare_origin), str(clone)], check=True,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    create_branch_from(tmp_git_repo_with_remote, "feature/to-be-deleted", "main")
    subprocess.run(["git", "push", "origin", "feature/to-be-deleted"], cwd=str(tmp_git_repo_with_remote), check=True)
    fetch(clone)
    result_before = subprocess.run(["git", "branch", "-r"], cwd=str(clone), check=True, stdout=subprocess.PIPE)
    assert "feature/to-be-deleted" in result_before.stdout.decode()
    subprocess.run(["git", "push", "origin", "--delete", "feature/to-be-deleted"], cwd=str(tmp_git_repo_with_remote), check=True)
    fetch(clone, prune=True)
    result_after = subprocess.run(["git", "branch", "-r"], cwd=str(clone), check=True, stdout=subprocess.PIPE)
    assert "feature/to-be-deleted" not in result_after.stdout.decode()


def test_fetch_rejects_option_like_ref(tmp_git_repo):
    with pytest.raises(GitCommandError):
        fetch(tmp_git_repo, ref=_OPTION_LIKE)


# Task: is_ancestor/unique_commit_count/delete_remote_branch - branch-delete safety primitives
from icx_engine.git.gitcmd import is_ancestor, unique_commit_count, delete_remote_branch


def test_is_ancestor_true_when_fully_merged(tmp_git_repo):
    create_branch_from(tmp_git_repo, "feature/merged-ABC-1", "main")
    assert is_ancestor(tmp_git_repo, "feature/merged-ABC-1", "main") is True


def test_is_ancestor_false_when_branch_has_unique_commits(tmp_git_repo):
    create_branch_from(tmp_git_repo, "feature/unmerged-ABC-1", "main")
    checkout(tmp_git_repo, "feature/unmerged-ABC-1")
    (tmp_git_repo / "only_here.txt").write_text("x", encoding="utf-8")
    stage_files(tmp_git_repo, ["only_here.txt"])
    commit(tmp_git_repo, "ABC-1 unique commit")
    checkout(tmp_git_repo, "main")
    assert is_ancestor(tmp_git_repo, "feature/unmerged-ABC-1", "main") is False


def test_is_ancestor_rejects_option_like_refs(tmp_git_repo):
    with pytest.raises(GitCommandError):
        is_ancestor(tmp_git_repo, _OPTION_LIKE, "main")
    with pytest.raises(GitCommandError):
        is_ancestor(tmp_git_repo, "main", _OPTION_LIKE)


from icx_engine.git.gitcmd import check_merge_tree


def test_check_merge_tree_clean_when_no_overlapping_changes(tmp_git_repo):
    create_branch_from(tmp_git_repo, "target-clean", "main")
    create_branch_from(tmp_git_repo, "source-clean", "main")
    checkout(tmp_git_repo, "source-clean")
    (tmp_git_repo / "only_here.txt").write_text("x", encoding="utf-8")
    stage_files(tmp_git_repo, ["only_here.txt"])
    commit(tmp_git_repo, "unrelated addition")
    checkout(tmp_git_repo, "main")
    result = check_merge_tree(tmp_git_repo, "target-clean", "source-clean")
    assert result == {"has_conflicts": False, "conflicting_files": []}


def test_check_merge_tree_reports_conflicting_files(tmp_git_repo):
    readme = tmp_git_repo / "README.md"
    readme.write_text("base\n", encoding="utf-8")
    stage_files(tmp_git_repo, ["README.md"])
    commit(tmp_git_repo, "base content")

    create_branch_from(tmp_git_repo, "target-conflict", "main")
    checkout(tmp_git_repo, "target-conflict")
    readme.write_text("target change\n", encoding="utf-8")
    stage_files(tmp_git_repo, ["README.md"])
    commit(tmp_git_repo, "target edits readme")

    create_branch_from(tmp_git_repo, "source-conflict", "main")
    checkout(tmp_git_repo, "source-conflict")
    readme.write_text("source change\n", encoding="utf-8")
    stage_files(tmp_git_repo, ["README.md"])
    commit(tmp_git_repo, "source edits readme")
    checkout(tmp_git_repo, "main")

    result = check_merge_tree(tmp_git_repo, "target-conflict", "source-conflict")
    assert result == {"has_conflicts": True, "conflicting_files": ["README.md"]}


def test_check_merge_tree_never_touches_working_tree_or_index(tmp_git_repo):
    create_branch_from(tmp_git_repo, "target-noop", "main")
    create_branch_from(tmp_git_repo, "source-noop", "main")
    checkout(tmp_git_repo, "main")
    status_before = structured_status(tmp_git_repo)
    check_merge_tree(tmp_git_repo, "target-noop", "source-noop")
    assert current_branch(tmp_git_repo) == "main"
    assert structured_status(tmp_git_repo) == status_before


def test_check_merge_tree_rejects_option_like_refs(tmp_git_repo):
    with pytest.raises(GitCommandError):
        check_merge_tree(tmp_git_repo, _OPTION_LIKE, "main")
    with pytest.raises(GitCommandError):
        check_merge_tree(tmp_git_repo, "main", _OPTION_LIKE)


def test_unique_commit_count_zero_when_fully_merged(tmp_git_repo):
    create_branch_from(tmp_git_repo, "feature/merged-ABC-1", "main")
    assert unique_commit_count(tmp_git_repo, "feature/merged-ABC-1", "main") == 0


def test_unique_commit_count_counts_unreachable_commits(tmp_git_repo):
    create_branch_from(tmp_git_repo, "feature/unmerged-ABC-1", "main")
    checkout(tmp_git_repo, "feature/unmerged-ABC-1")
    for i in range(3):
        (tmp_git_repo / f"f{i}.txt").write_text("x", encoding="utf-8")
        stage_files(tmp_git_repo, [f"f{i}.txt"])
        commit(tmp_git_repo, f"ABC-1 commit {i}")
    assert unique_commit_count(tmp_git_repo, "feature/unmerged-ABC-1", "main") == 3


def test_unique_commit_count_rejects_option_like_refs(tmp_git_repo):
    with pytest.raises(GitCommandError):
        unique_commit_count(tmp_git_repo, _OPTION_LIKE, "main")
    with pytest.raises(GitCommandError):
        unique_commit_count(tmp_git_repo, "main", _OPTION_LIKE)


def test_delete_remote_branch_removes_it_from_remote(tmp_git_repo_with_remote):
    create_branch_from(tmp_git_repo_with_remote, "feature/remote-delete-me", "main")
    checkout(tmp_git_repo_with_remote, "feature/remote-delete-me")
    push(tmp_git_repo_with_remote, "feature/remote-delete-me")
    assert remote_branch_exists(tmp_git_repo_with_remote, "feature/remote-delete-me") is True
    delete_remote_branch(tmp_git_repo_with_remote, "feature/remote-delete-me")
    assert remote_branch_exists(tmp_git_repo_with_remote, "feature/remote-delete-me") is False


def test_delete_remote_branch_rejects_option_like_branch(tmp_git_repo):
    with pytest.raises(GitCommandError):
        delete_remote_branch(tmp_git_repo, _OPTION_LIKE)


def test_delete_remote_branch_rejects_option_like_remote(tmp_git_repo):
    with pytest.raises(GitCommandError):
        delete_remote_branch(tmp_git_repo, "main", remote=_OPTION_LIKE)


# Task: list_local_branches - branch discovery for stale-branch cleanup
from icx_engine.git.gitcmd import list_local_branches


def test_list_local_branches_reports_every_local_branch_with_tip_info(tmp_git_repo):
    create_branch_from(tmp_git_repo, "feature/x-ABC-1", "main")
    branches = {b["branch"]: b for b in list_local_branches(tmp_git_repo)}
    assert set(branches) == {"main", "feature/x-ABC-1"}
    assert branches["main"]["author"] == "Test User"
    assert branches["main"]["sha"] == head_sha(tmp_git_repo)
    assert branches["feature/x-ABC-1"]["sha"] == head_sha(tmp_git_repo)


# Task: conflict_stage/parse_conflict_hunks/checkout_conflict_side/conflict_state/
# abort_in_progress_operation - line-level conflict inspection + gated resolution primitives
from icx_engine.git.gitcmd import (
    conflict_stage, parse_conflict_hunks, checkout_conflict_side, conflict_state,
    abort_in_progress_operation,
)


def test_conflict_stage_returns_base_ours_and_theirs(tmp_git_repo_with_remote, tmp_path):
    clone = _make_diverging_clone(tmp_git_repo_with_remote, tmp_path, name="stage_clone")
    fetch(clone)
    with pytest.raises(GitCommandError):
        merge_ref(clone, "origin/main")
    assert conflict_stage(clone, "README.md", 1) == "hello\n"
    assert conflict_stage(clone, "README.md", 2) == "local version\n"
    assert conflict_stage(clone, "README.md", 3) == "remote version\n"


def test_conflict_stage_returns_none_for_missing_base_on_add_add_conflict(tmp_git_repo_with_remote, tmp_path):
    import subprocess
    clone = tmp_path / "add_add_clone"
    subprocess.run(["git", "clone", str(tmp_git_repo_with_remote), str(clone)], check=True,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=str(clone), check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=str(clone), check=True)

    (tmp_git_repo_with_remote / "new_both.txt").write_text("remote side\n", encoding="utf-8")
    stage_files(tmp_git_repo_with_remote, ["new_both.txt"])
    commit(tmp_git_repo_with_remote, "PARENT-1 add new_both remote side")
    subprocess.run(["git", "push"], cwd=str(tmp_git_repo_with_remote), check=True)

    (clone / "new_both.txt").write_text("local side\n", encoding="utf-8")
    stage_files(clone, ["new_both.txt"])
    commit(clone, "ABC-1 add new_both local side")

    fetch(clone)
    with pytest.raises(GitCommandError):
        merge_ref(clone, "origin/main")
    assert conflict_stage(clone, "new_both.txt", 1) is None  # no common ancestor for this path
    assert conflict_stage(clone, "new_both.txt", 2) == "local side\n"
    assert conflict_stage(clone, "new_both.txt", 3) == "remote side\n"


def test_conflict_stage_rejects_invalid_stage_number(tmp_git_repo):
    with pytest.raises(GitCommandError):
        conflict_stage(tmp_git_repo, "README.md", 4)


def test_conflict_stage_rejects_path_traversal(tmp_git_repo):
    with pytest.raises(GitCommandError):
        conflict_stage(tmp_git_repo, "../outside.txt", 1)


def test_parse_conflict_hunks_single_hunk(tmp_git_repo_with_remote, tmp_path):
    clone = _make_diverging_clone(tmp_git_repo_with_remote, tmp_path, name="hunk_clone")
    fetch(clone)
    with pytest.raises(GitCommandError):
        merge_ref(clone, "origin/main")
    hunks = parse_conflict_hunks(clone, "README.md")
    assert len(hunks) == 1
    assert hunks[0]["ours"] == "local version"
    assert hunks[0]["theirs"] == "remote version"
    assert hunks[0]["start_line"] == 1
    on_disk = (clone / "README.md").read_text(encoding="utf-8")
    on_disk_lines = on_disk.splitlines()
    assert on_disk_lines[hunks[0]["start_line"] - 1].startswith("<<<<<<<")
    assert on_disk_lines[hunks[0]["end_line"] - 1].startswith(">>>>>>>")


def test_parse_conflict_hunks_multiple_hunks_in_one_file(tmp_git_repo_with_remote, tmp_path):
    import subprocess
    lines = [f"line{i}" for i in range(1, 13)]
    (tmp_git_repo_with_remote / "multi.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    stage_files(tmp_git_repo_with_remote, ["multi.txt"])
    commit(tmp_git_repo_with_remote, "PARENT-1 add multi.txt")
    subprocess.run(["git", "push"], cwd=str(tmp_git_repo_with_remote), check=True)

    clone = tmp_path / "multi_hunk_clone"
    subprocess.run(["git", "clone", str(tmp_git_repo_with_remote), str(clone)], check=True,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=str(clone), check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=str(clone), check=True)

    local_lines = list(lines)
    local_lines[1] = "local-line2"
    local_lines[9] = "local-line10"
    (clone / "multi.txt").write_text("\n".join(local_lines) + "\n", encoding="utf-8")
    stage_files(clone, ["multi.txt"])
    commit(clone, "ABC-1 local edits")

    remote_lines = list(lines)
    remote_lines[1] = "remote-line2"
    remote_lines[9] = "remote-line10"
    (tmp_git_repo_with_remote / "multi.txt").write_text("\n".join(remote_lines) + "\n", encoding="utf-8")
    stage_files(tmp_git_repo_with_remote, ["multi.txt"])
    commit(tmp_git_repo_with_remote, "PARENT-2 remote edits")
    subprocess.run(["git", "push"], cwd=str(tmp_git_repo_with_remote), check=True)

    fetch(clone)
    with pytest.raises(GitCommandError):
        merge_ref(clone, "origin/main")
    hunks = parse_conflict_hunks(clone, "multi.txt")
    assert len(hunks) == 2
    assert hunks[0]["ours"] == "local-line2"
    assert hunks[0]["theirs"] == "remote-line2"
    assert hunks[1]["ours"] == "local-line10"
    assert hunks[1]["theirs"] == "remote-line10"
    assert hunks[0]["end_line"] < hunks[1]["start_line"]


def test_parse_conflict_hunks_empty_for_clean_file(tmp_git_repo):
    assert parse_conflict_hunks(tmp_git_repo, "README.md") == []


def test_parse_conflict_hunks_rejects_path_traversal(tmp_git_repo):
    with pytest.raises(GitCommandError):
        parse_conflict_hunks(tmp_git_repo, "../outside.txt")


def test_checkout_conflict_side_ours_resolves_to_local_version(tmp_git_repo_with_remote, tmp_path):
    clone = _make_diverging_clone(tmp_git_repo_with_remote, tmp_path, name="take_ours_clone")
    fetch(clone)
    with pytest.raises(GitCommandError):
        merge_ref(clone, "origin/main")
    checkout_conflict_side(clone, "README.md", "ours")
    assert (clone / "README.md").read_text(encoding="utf-8") == "local version\n"
    # still unmerged in the index until staged
    assert "README.md" in conflicted_files(clone)


def test_checkout_conflict_side_theirs_resolves_to_remote_version(tmp_git_repo_with_remote, tmp_path):
    clone = _make_diverging_clone(tmp_git_repo_with_remote, tmp_path, name="take_theirs_clone")
    fetch(clone)
    with pytest.raises(GitCommandError):
        merge_ref(clone, "origin/main")
    checkout_conflict_side(clone, "README.md", "theirs")
    assert (clone / "README.md").read_text(encoding="utf-8") == "remote version\n"


def test_checkout_conflict_side_rejects_invalid_side(tmp_git_repo):
    with pytest.raises(GitCommandError):
        checkout_conflict_side(tmp_git_repo, "README.md", "bogus")


def test_checkout_conflict_side_rejects_path_traversal(tmp_git_repo):
    with pytest.raises(GitCommandError):
        checkout_conflict_side(tmp_git_repo, "../outside.txt", "ours")


def test_conflict_state_clean_on_fresh_repo(tmp_git_repo):
    assert conflict_state(tmp_git_repo) == "CLEAN"


def test_conflict_state_conflict_detected_mid_merge(tmp_git_repo_with_remote, tmp_path):
    clone = _make_diverging_clone(tmp_git_repo_with_remote, tmp_path, name="state_conflict_clone")
    fetch(clone)
    with pytest.raises(GitCommandError):
        merge_ref(clone, "origin/main")
    assert conflict_state(clone) == "CONFLICT_DETECTED"


def test_conflict_state_staged_after_resolving_and_staging(tmp_git_repo_with_remote, tmp_path):
    clone = _make_diverging_clone(tmp_git_repo_with_remote, tmp_path, name="state_staged_clone")
    fetch(clone)
    with pytest.raises(GitCommandError):
        merge_ref(clone, "origin/main")
    checkout_conflict_side(clone, "README.md", "ours")
    stage_files(clone, ["README.md"])
    assert conflict_state(clone) == "STAGED"


def test_conflict_state_clean_after_abort(tmp_git_repo_with_remote, tmp_path):
    clone = _make_diverging_clone(tmp_git_repo_with_remote, tmp_path, name="state_abort_clone")
    fetch(clone)
    with pytest.raises(GitCommandError):
        merge_ref(clone, "origin/main")
    merge_abort(clone)
    assert conflict_state(clone) == "CLEAN"


def test_abort_in_progress_operation_aborts_merge(tmp_git_repo_with_remote, tmp_path):
    clone = _make_diverging_clone(tmp_git_repo_with_remote, tmp_path, name="abort_merge_clone")
    fetch(clone)
    with pytest.raises(GitCommandError):
        merge_ref(clone, "origin/main")
    result = abort_in_progress_operation(clone)
    assert result == "merge"
    assert conflicted_files(clone) == []
    assert not (clone / ".git" / "MERGE_HEAD").exists()


def test_abort_in_progress_operation_raises_when_nothing_in_progress(tmp_git_repo):
    with pytest.raises(GitCommandError):
        abort_in_progress_operation(tmp_git_repo)


def test_abort_in_progress_operation_aborts_cherry_pick(tmp_git_repo):
    create_branch_from(tmp_git_repo, "feature/cp-source", "main")
    checkout(tmp_git_repo, "feature/cp-source")
    (tmp_git_repo / "README.md").write_text("cherry-pick version\n", encoding="utf-8")
    stage_files(tmp_git_repo, ["README.md"])
    sha = commit(tmp_git_repo, "ABC-1 cherry-pick change")
    checkout(tmp_git_repo, "main")
    (tmp_git_repo / "README.md").write_text("main version\n", encoding="utf-8")
    stage_files(tmp_git_repo, ["README.md"])
    commit(tmp_git_repo, "PARENT-1 main change")

    import subprocess
    result = subprocess.run(["git", "cherry-pick", sha], cwd=str(tmp_git_repo),
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert result.returncode != 0  # real conflict
    assert (tmp_git_repo / ".git" / "CHERRY_PICK_HEAD").exists()

    aborted = abort_in_progress_operation(tmp_git_repo)
    assert aborted == "cherry-pick"
    assert not (tmp_git_repo / ".git" / "CHERRY_PICK_HEAD").exists()
    assert conflicted_files(tmp_git_repo) == []


def test_abort_in_progress_operation_aborts_rebase(tmp_git_repo):
    create_branch_from(tmp_git_repo, "feature/rebase-source", "main")
    checkout(tmp_git_repo, "feature/rebase-source")
    (tmp_git_repo / "README.md").write_text("rebase version\n", encoding="utf-8")
    stage_files(tmp_git_repo, ["README.md"])
    commit(tmp_git_repo, "ABC-1 rebase change")
    checkout(tmp_git_repo, "main")
    (tmp_git_repo / "README.md").write_text("main version\n", encoding="utf-8")
    stage_files(tmp_git_repo, ["README.md"])
    commit(tmp_git_repo, "PARENT-1 main change")
    checkout(tmp_git_repo, "feature/rebase-source")

    import subprocess
    result = subprocess.run(["git", "rebase", "main"], cwd=str(tmp_git_repo),
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert result.returncode != 0  # real conflict
    rebase_in_progress = (tmp_git_repo / ".git" / "rebase-merge").exists() or \
                          (tmp_git_repo / ".git" / "rebase-apply").exists()
    assert rebase_in_progress

    aborted = abort_in_progress_operation(tmp_git_repo)
    assert aborted == "rebase"
    assert not (tmp_git_repo / ".git" / "rebase-merge").exists()
    assert not (tmp_git_repo / ".git" / "rebase-apply").exists()
    assert conflicted_files(tmp_git_repo) == []


# Task: resolve_ref - tolerant ref-to-sha resolution for dependency-pin analysis
from icx_engine.git.gitcmd import resolve_ref, head_sha


def test_resolve_ref_resolves_branch_name(tmp_git_repo):
    assert resolve_ref(tmp_git_repo, "main") == head_sha(tmp_git_repo)


def test_resolve_ref_resolves_full_sha(tmp_git_repo):
    sha = head_sha(tmp_git_repo)
    assert resolve_ref(tmp_git_repo, sha) == sha


def test_resolve_ref_resolves_short_sha(tmp_git_repo):
    sha = head_sha(tmp_git_repo)
    assert resolve_ref(tmp_git_repo, sha[:8]) == sha


def test_resolve_ref_returns_none_for_unresolvable_ref(tmp_git_repo):
    assert resolve_ref(tmp_git_repo, "not-a-real-ref") is None


def test_resolve_ref_rejects_option_like_ref(tmp_git_repo):
    with pytest.raises(GitCommandError):
        resolve_ref(tmp_git_repo, _OPTION_LIKE)


# Task: restore_files - file-level discard, worktree/staged/both modes
from icx_engine.git.gitcmd import restore_files, structured_status


def _stage_then_dirty_readme(tmp_git_repo):
    (tmp_git_repo / "README.md").write_text("staged\n", encoding="utf-8")
    stage_files(tmp_git_repo, ["README.md"])
    (tmp_git_repo / "README.md").write_text("unstaged\n", encoding="utf-8")


def test_restore_files_worktree_mode_restores_from_index_leaves_staged(tmp_git_repo):
    _stage_then_dirty_readme(tmp_git_repo)
    restore_files(tmp_git_repo, ["README.md"], mode="worktree")
    assert (tmp_git_repo / "README.md").read_text(encoding="utf-8") == "staged\n"
    status = structured_status(tmp_git_repo)
    assert status["unstaged"] == []
    staged_paths = {e["path"]: e["status"] for e in status["staged"]}
    assert staged_paths == {"README.md": "modified"}


def test_restore_files_staged_mode_unstages_leaves_worktree(tmp_git_repo):
    _stage_then_dirty_readme(tmp_git_repo)
    restore_files(tmp_git_repo, ["README.md"], mode="staged")
    assert (tmp_git_repo / "README.md").read_text(encoding="utf-8") == "unstaged\n"
    status = structured_status(tmp_git_repo)
    assert status["staged"] == []
    unstaged_paths = {e["path"]: e["status"] for e in status["unstaged"]}
    assert unstaged_paths == {"README.md": "modified"}


def test_restore_files_both_mode_fully_reverts_to_head(tmp_git_repo):
    _stage_then_dirty_readme(tmp_git_repo)
    restore_files(tmp_git_repo, ["README.md"], mode="both")
    assert (tmp_git_repo / "README.md").read_text(encoding="utf-8") == "hello\n"
    status = structured_status(tmp_git_repo)
    assert status["staged"] == []
    assert status["unstaged"] == []


def test_restore_files_multiple_files(tmp_git_repo):
    (tmp_git_repo / "a.txt").write_text("a", encoding="utf-8")
    (tmp_git_repo / "b.txt").write_text("b", encoding="utf-8")
    stage_files(tmp_git_repo, ["a.txt", "b.txt"])
    commit(tmp_git_repo, "add a and b")
    (tmp_git_repo / "a.txt").write_text("a-changed", encoding="utf-8")
    (tmp_git_repo / "b.txt").write_text("b-changed", encoding="utf-8")
    restore_files(tmp_git_repo, ["a.txt", "b.txt"], mode="worktree")
    assert (tmp_git_repo / "a.txt").read_text(encoding="utf-8") == "a"
    assert (tmp_git_repo / "b.txt").read_text(encoding="utf-8") == "b"


def test_restore_files_empty_list_is_noop(tmp_git_repo):
    restore_files(tmp_git_repo, [])  # must not raise


def test_restore_files_rejects_invalid_mode(tmp_git_repo):
    with pytest.raises(GitCommandError):
        restore_files(tmp_git_repo, ["README.md"], mode="bogus")


def test_restore_files_rejects_option_like_file(tmp_git_repo):
    with pytest.raises(GitCommandError):
        restore_files(tmp_git_repo, [_OPTION_LIKE])


def test_restore_files_rejects_option_like_source(tmp_git_repo):
    with pytest.raises(GitCommandError):
        restore_files(tmp_git_repo, ["README.md"], source=_OPTION_LIKE)
