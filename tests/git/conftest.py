from __future__ import annotations
import subprocess
from pathlib import Path
import pytest


def _run(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(repo), check=True,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)


@pytest.fixture
def tmp_git_repo(tmp_path: Path) -> Path:
    """A real, throwaway git repo on branch 'main' with one commit - for testing gitcmd.py
    against real git behavior rather than mocked subprocess output."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _run(repo, "init", "-b", "main")
    _run(repo, "config", "user.email", "test@example.com")
    _run(repo, "config", "user.name", "Test User")
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    _run(repo, "add", "README.md")
    _run(repo, "commit", "-m", "initial commit")
    return repo


@pytest.fixture
def tmp_git_repo_with_remote(tmp_git_repo: Path, tmp_path: Path) -> Path:
    """tmp_git_repo, but with a bare 'origin' remote it has already pushed 'main' to -
    for testing functions that need a real origin (fetch, ls-remote)."""
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", str(bare)], check=True,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    # Set the bare repo's HEAD to point to main before pushing
    subprocess.run(["git", "symbolic-ref", "HEAD", "refs/heads/main"], cwd=str(bare),
                    check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    _run(tmp_git_repo, "remote", "add", "origin", str(bare))
    _run(tmp_git_repo, "push", "-u", "origin", "main")
    # Fetch to discover and cache the remote's HEAD symbolic-ref
    _run(tmp_git_repo, "fetch", "origin")
    return tmp_git_repo
