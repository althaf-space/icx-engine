"""Safe git subprocess wrapper for the git-workflow lifecycle tool.

Every function here shells out to a real `git` process using the same
hardened subprocess settings as `graph/paths.py` - credential helpers
disabled, no terminal prompts, explicit timeouts, no inherited handles on
Windows. No rebase, no force-push - those operations do not exist in this
module by design - see the design spec's safety doctrine
(docs/superpowers/specs/2026-07-26-icx-git-workflow-design.md, Section 2).
A real, conflict-capable merge exists here (`merge_ref`), but this module
never interprets or resolves a conflict itself - a caller that gets a
conflict must either abort (`merge_abort`, or the operation-agnostic
`abort_in_progress_operation`) or hand it to the conflict-quarantine flow in
`manager.py`; `gitcmd.py` only ever reports raw git state, never makes a
resolution decision. `fast_forward`/`fast_forward_ref` are `--ff-only` and
cannot conflict or create a merge commit - they are not "real" merges in
this sense.

ONE explicit, narrow exception to "no rebase": `abort_in_progress_operation`
may run `git rebase --abort` - but ONLY to back out of a rebase a human
started outside ICX, never to start, continue, or otherwise drive one. An
abort restores the pre-rebase state; it does not rewrite history the way a
real rebase does. This module still never initiates, continues, or skips a
rebase step anywhere.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

_GIT_SAFE_ENV: dict[str, str] = {
    "GIT_TERMINAL_PROMPT": "0",
    "GCM_INTERACTIVE": "never",
    "GCM_CREDENTIAL_STORE": "cache",
}

_GIT_BASE_CMD: tuple[str, ...] = (
    "git", "--no-pager",
    "-c", "credential.helper=",
    "-c", "gc.auto=0",
    "-c", "maintenance.auto=false",
    "-c", "core.quotePath=false",
)

_GIT_POPEN_KW: dict = {
    "close_fds": True,
    **({"creationflags": 0x08000000} if os.name == "nt" else {}),  # CREATE_NO_WINDOW
}

_DEFAULT_TIMEOUT = 10.0


class GitCommandError(RuntimeError):
    """Raised when a git subprocess exits with an unexpected return code."""

    def __init__(self, message: str, stderr: str = "", returncode: int | None = None):
        super().__init__(message)
        self.stderr = stderr
        self.returncode = returncode


def _reject_option_like(name: str, param: str) -> None:
    """Raise GitCommandError if `name` could be parsed as a git command-line option
    (starts with '-') rather than a plain ref/branch/remote name - defense against
    argument/flag injection when the value originates from untrusted input (e.g. an
    MCP tool argument). Real branch/ref/remote names never start with '-'."""
    if name.startswith("-"):
        raise GitCommandError(f"{param} must not start with '-': {name!r}")


def _reject_path_traversal(relpath: str, param: str) -> None:
    """Raise GitCommandError if `relpath` contains a '..' path segment, which
    could otherwise be used to read/blame a file outside the repo working tree."""
    if ".." in Path(relpath).parts:
        raise GitCommandError(f"{param} must not contain '..' segments: {relpath!r}")


def _safe_git_env(extra_env: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ)
    env.update(_GIT_SAFE_ENV)
    if extra_env:
        env.update(extra_env)
    return env


def _run_git(
    repo: Path,
    args: list[str],
    timeout: float = _DEFAULT_TIMEOUT,
    allowed_returncodes: set[int] | None = None,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """Run a git command in `repo`. Raises GitCommandError unless the return
    code is 0 or explicitly listed in `allowed_returncodes` (used by callers
    like `remote_branch_exists` where a non-zero code is a meaningful result,
    not a failure). `extra_env` (if given) is merged in on top of the hardened
    base env - used by `fetch`/`push` to inject a GitLab auth header for
    network calls that need one, without touching the credential.helper/
    terminal-prompt hardening itself."""
    ok_codes = {0} | (allowed_returncodes or set())
    try:
        result = subprocess.run(
            [*_GIT_BASE_CMD, *args],
            cwd=str(repo), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL, timeout=timeout, env=_safe_git_env(extra_env),
            **_GIT_POPEN_KW,
        )
    except subprocess.TimeoutExpired as exc:
        raise GitCommandError(f"git {' '.join(args)} timed out after {timeout}s") from exc
    except OSError as exc:
        raise GitCommandError(f"git {' '.join(args)} could not run in '{repo}': {exc}") from exc
    if result.returncode not in ok_codes:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        raise GitCommandError(
            f"git {' '.join(args)} failed (exit {result.returncode}): {stderr}",
            stderr=stderr, returncode=result.returncode,
        )
    return result


def _stdout(result: subprocess.CompletedProcess) -> str:
    return result.stdout.decode("utf-8", errors="replace").strip()


def is_git_repo(path: Path) -> bool:
    try:
        result = subprocess.run(
            [*_GIT_BASE_CMD, "rev-parse", "--is-inside-work-tree"],
            cwd=str(path), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, timeout=_DEFAULT_TIMEOUT, env=_safe_git_env(),
            **_GIT_POPEN_KW,
        )
        return result.returncode == 0
    except Exception:
        return False


def repo_root(path: Path) -> Path:
    result = _run_git(path, ["rev-parse", "--show-toplevel"])
    return Path(_stdout(result)).resolve()


def current_branch(repo: Path) -> str:
    result = _run_git(repo, ["rev-parse", "--abbrev-ref", "HEAD"])
    return _stdout(result)


def fetch(
    repo: Path, remote: str = "origin", ref: str | None = None, prune: bool = False,
    extra_env: dict[str, str] | None = None,
) -> None:
    _reject_option_like(remote, "remote")
    args = ["fetch", remote]
    if ref is not None:
        _reject_option_like(ref, "ref")
        args.append(ref)
    if prune:
        args.append("--prune")
    _run_git(repo, args, timeout=60.0, extra_env=extra_env)


def remote_branch_exists(
    repo: Path, branch: str, remote: str = "origin", extra_env: dict[str, str] | None = None,
) -> bool:
    _reject_option_like(branch, "branch")
    _reject_option_like(remote, "remote")
    result = _run_git(
        repo, ["ls-remote", "--exit-code", "--heads", remote, branch],
        timeout=30.0, allowed_returncodes={2}, extra_env=extra_env,
    )
    return result.returncode == 0


def local_branch_exists(repo: Path, branch: str) -> bool:
    _reject_option_like(branch, "branch")
    result = _run_git(
        repo, ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        allowed_returncodes={1},
    )
    return result.returncode == 0


def is_dirty(repo: Path) -> bool:
    return len(dirty_files(repo)) > 0


def _unquote_git_path(path: str) -> str:
    """Unquote a C-style quoted path from porcelain output. Git quotes paths
    containing spaces/tabs/quotes for disambiguation regardless of
    core.quotePath - that setting only affects escaping of non-ASCII bytes.
    If the path is quoted, parse it as JSON; otherwise return as-is."""
    if path.startswith('"') and path.endswith('"'):
        try:
            return json.loads(path)
        except (json.JSONDecodeError, ValueError):
            return path
    return path


def dirty_files(repo: Path) -> list[str]:
    """Modified + untracked file paths, relative to repo root. Ignored files excluded."""
    result = _run_git(repo, ["status", "--porcelain"])
    out = _stdout(result)
    if not out:
        return []
    files = []
    for line in out.splitlines():
        # porcelain format: "XY path" or "XY path -> newpath" for renames
        # Note: _stdout() strips the output, which can remove the leading space
        # from the first line if the output starts with a space. Handle this by
        # splitting on whitespace and taking the part after status codes.
        parts = line.split(None, 1)
        if len(parts) >= 2:
            path = parts[1]
            if " -> " in path:
                path = path.split(" -> ", 1)[1]
            path = _unquote_git_path(path)
            files.append(path)
    return files


def stash_push(repo: Path, message: str) -> None:
    """-u includes untracked files - matches the design spec's carry/stash decision,
    which must be able to stash brand-new files, not just modified ones."""
    _run_git(repo, ["stash", "push", "-u", "-m", message])


def stash_pop(repo: Path, ref: str | None = None) -> None:
    """Pops `ref` (a `stash@{N}` string) if given, else the most recent stash
    (`stash@{0}`) - backward compatible with every existing no-ref call."""
    args = ["stash", "pop"]
    if ref is not None:
        _reject_option_like(ref, "ref")
        args.append(ref)
    _run_git(repo, args)


def stash_list(repo: Path) -> list[dict]:
    """Every stash, newest first (stash@{0} is always the most recent).
    `ref` is the exact `stash@{N}` string to pass to stash_apply/stash_pop/
    stash_drop; `message` is the stash's own label (the part after
    'On <branch>: ' or 'WIP on <branch>: ')."""
    result = _run_git(repo, ["stash", "list", "--format=%gd%x1f%s"])
    out = _stdout(result)
    stashes = []
    for i, line in enumerate(out.splitlines()):
        if not line:
            continue
        ref, _, message = line.partition("\x1f")
        stashes.append({"index": i, "ref": ref, "message": message})
    return stashes


def stash_apply(repo: Path, ref: str = "stash@{0}") -> None:
    """Applies `ref`'s changes to the working tree WITHOUT removing it from
    the stash list - use stash_pop to apply-and-remove in one step."""
    _reject_option_like(ref, "ref")
    _run_git(repo, ["stash", "apply", ref])


def stash_drop(repo: Path, ref: str = "stash@{0}") -> None:
    """Permanently discards `ref` from the stash list - never call this
    without the human's explicit confirmation, this is not recoverable
    through any ICX tool."""
    _reject_option_like(ref, "ref")
    _run_git(repo, ["stash", "drop", ref])


def create_branch_from(repo: Path, new_branch: str, start_point: str) -> None:
    """Creates new_branch pointing at start_point WITHOUT switching to it."""
    _reject_option_like(new_branch, "new_branch")
    _reject_option_like(start_point, "start_point")
    _run_git(repo, ["branch", new_branch, start_point])


def checkout(repo: Path, branch: str) -> None:
    _reject_option_like(branch, "branch")
    _run_git(repo, ["checkout", branch])


def fast_forward_ref(repo: Path, ref: str) -> None:
    """Fast-forward-only merge of an arbitrary ref into the currently checked-out
    branch. Raises GitCommandError (never falls back to a real merge) if a
    fast-forward is not possible."""
    _reject_option_like(ref, "ref")
    _run_git(repo, ["merge", "--ff-only", ref])


def fast_forward(repo: Path, branch: str, remote: str = "origin") -> None:
    """Fast-forward-only merge of <remote>/<branch> into the currently checked-out
    branch. Thin wrapper over fast_forward_ref for the common remote-tracking case."""
    _reject_option_like(branch, "branch")
    _reject_option_like(remote, "remote")
    fast_forward_ref(repo, f"{remote}/{branch}")


def delete_branch(repo: Path, branch: str, force: bool = False) -> None:
    _reject_option_like(branch, "branch")
    _run_git(repo, ["branch", "-D" if force else "-d", branch])


def delete_remote_branch(
    repo: Path, branch: str, remote: str = "origin", extra_env: dict[str, str] | None = None,
) -> None:
    """Deletes `branch` on `remote` via a real push (`--delete`) - routes
    through the same extra_env auth plumbing as push()/fetch(), rather than a
    second, separately-authenticated code path."""
    _reject_option_like(branch, "branch")
    _reject_option_like(remote, "remote")
    _run_git(repo, ["push", remote, "--delete", branch], timeout=60.0, extra_env=extra_env)


def list_local_branches(repo: Path) -> list[dict]:
    """Every local branch with its tip commit's sha/author/date - one
    `for-each-ref` call, no per-branch subprocess needed. `date` is
    ISO-8601 (author date, not commit date) for direct comparison/sorting.
    Fields are tab-separated - unlike `log --format`, for-each-ref's format
    spec does not support arbitrary `%xHH` hex escapes (`%x1f` prints
    literally, verified), only a handful of named ones like `%09` (tab)."""
    result = _run_git(repo, [
        "for-each-ref", "refs/heads/",
        "--format=%(refname:short)%09%(objectname)%09%(authorname)%09%(authordate:iso-strict)",
    ])
    out = _stdout(result)
    branches = []
    for line in out.splitlines():
        if not line:
            continue
        name, sha, author, date = line.split("\t")
        branches.append({"branch": name, "sha": sha, "author": author, "date": date})
    return branches


def is_ancestor(repo: Path, ancestor_ref: str, descendant_ref: str) -> bool:
    """True if every commit on ancestor_ref already exists on descendant_ref
    - i.e. ancestor_ref could be deleted without losing any commit reachable
    only from it."""
    _reject_option_like(ancestor_ref, "ancestor_ref")
    _reject_option_like(descendant_ref, "descendant_ref")
    result = _run_git(
        repo, ["merge-base", "--is-ancestor", ancestor_ref, descendant_ref], allowed_returncodes={1},
    )
    return result.returncode == 0


_MERGE_TREE_CONFLICT_LINE = re.compile(r"^\d+ [0-9a-f]+ [123]\t(.+)$")


def check_merge_tree(repo: Path, target_ref: str, source_ref: str) -> dict:
    """Read-only merge simulation via `git merge-tree` (git >=2.38) - answers
    whether merging source_ref into target_ref would conflict, and which
    files, without touching the working tree, the index, or either ref.
    Exit code 0 means a clean merge (stdout is just the resulting tree OID,
    nothing to parse); exit code 1 means conflicts, and stdout's leading
    block is `<mode> <oid> <stage 1|2|3>\\t<path>` lines - distinct paths
    across those lines are the conflicting files. Any other exit code is a
    real failure (e.g. an unresolvable ref), not a conflict result, and
    raises GitCommandError via `_run_git`."""
    _reject_option_like(target_ref, "target_ref")
    _reject_option_like(source_ref, "source_ref")
    result = _run_git(repo, ["merge-tree", target_ref, source_ref], allowed_returncodes={1})
    if result.returncode == 0:
        return {"has_conflicts": False, "conflicting_files": []}
    conflicting: list[str] = []
    for line in _stdout(result).splitlines():
        match = _MERGE_TREE_CONFLICT_LINE.match(line)
        if match and match.group(1) not in conflicting:
            conflicting.append(match.group(1))
    return {"has_conflicts": True, "conflicting_files": conflicting}


def unique_commit_count(repo: Path, branch: str, target: str) -> int:
    """Count of commits reachable from branch but not from target - what
    would become unreachable (lost) if branch were deleted right now. Zero
    means branch is fully merged into target (equivalent to
    is_ancestor(branch, target) being True)."""
    _reject_option_like(branch, "branch")
    _reject_option_like(target, "target")
    result = _run_git(repo, ["rev-list", "--count", f"{target}..{branch}"])
    return int(_stdout(result))


def resolve_ref(repo: Path, ref: str) -> str | None:
    """Resolves ref (branch/tag/full or short sha/anything git understands)
    to its full commit sha - tolerant of an unresolvable ref (returns None
    rather than raising), used by dependency-pin analysis to check a pinned
    ref against a target branch without assuming either exists."""
    _reject_option_like(ref, "ref")
    result = _run_git(repo, ["rev-parse", "--verify", f"{ref}^{{commit}}"], allowed_returncodes={128})
    if result.returncode != 0:
        return None
    return _stdout(result)


def find_conflict_markers(repo: Path, relpaths: list[str]) -> dict[str, list[str]]:
    """Scan each given file's ON-DISK content (not git-tracked state) for
    literal conflict-marker lines. Universal, language-agnostic check (design
    spec Section 7.2 step 5) - returns only files that still have markers,
    empty dict means everything is clean."""
    markers = ("<<<<<<<", "=======", ">>>>>>>")
    found: dict[str, list[str]] = {}
    for relpath in relpaths:
        path = repo / relpath
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        hits = [line for line in text.splitlines() if line.startswith(markers)]
        if hits:
            found[relpath] = hits
    return found


def list_ignored(repo: Path, files: list[str]) -> list[str]:
    """Which of `files` .gitignore would silently exclude from `git add` - checked
    BEFORE staging so a caller can warn/refuse instead of reporting a commit as
    successful while some listed files were never actually staged at all (the
    real failure mode: `git_stage_and_commit` listing .gitkeep files that
    .gitignore silently swallowed, with no error and no sign anything was
    skipped). `git check-ignore` prints only the matching subset of the given
    paths to stdout, one per line - exit 1 means none matched, a normal
    result here, not a failure."""
    if not files:
        return []
    for f in files:
        _reject_option_like(f, "files")
    result = _run_git(repo, ["check-ignore", "--", *files], allowed_returncodes={1})
    return [line for line in _stdout(result).splitlines() if line]


def stage_files(repo: Path, files: list[str]) -> None:
    """Stage exactly these files. Never called with a wildcard or '.' - callers
    always pass an explicit list (design spec Section 6.3).

    A path missing from the working tree but STILL in the index (an
    unstaged deletion) stages fine via plain `git add`. A path missing from
    BOTH the working tree AND the index (already fully staged as deleted in
    a prior stage/commit cycle - e.g. the caller re-lists a file it already
    handled) has nothing left to change - `git add` refuses it with a fatal
    'pathspec did not match any files', which previously failed the ENTIRE
    batched add for every other file in the same call. Such paths are
    detected (one extra `ls-files` check, only for paths absent from disk -
    zero extra cost for the common case) and silently skipped instead."""
    if not files:
        return
    missing = [f for f in files if not (repo / f).exists()]
    already_gone: set[str] = set()
    for f in missing:
        _reject_option_like(f, "files")
        result = _run_git(repo, ["ls-files", "--cached", "--", f])
        if not _stdout(result):
            already_gone.add(f)
    to_add = [f for f in files if f not in already_gone]
    if to_add:
        _run_git(repo, ["add", "--", *to_add])


def restore_files(repo: Path, files: list[str], mode: str = "worktree", source: str | None = None) -> None:
    """Discards changes to exactly these files - never a wildcard or '.',
    same discipline as stage_files. mode='worktree' (default) restores the
    working tree only (`git restore <file>` - unstaged changes discarded,
    staged changes untouched). mode='staged' unstages only (`git restore
    --staged <file>` - working tree untouched, only the index entry
    reverts). mode='both' restores both (file fully reverts to source,
    discarding staged AND unstaged changes). source (default None - git's
    own default: index if staged, else HEAD) forces restoring from a
    specific ref instead."""
    if not files:
        return
    if mode not in ("worktree", "staged", "both"):
        raise GitCommandError(f"mode must be 'worktree', 'staged', or 'both', got {mode!r}")
    for f in files:
        _reject_option_like(f, "files")
    args = ["restore"]
    if source is not None:
        _reject_option_like(source, "source")
        args.append(f"--source={source}")
    if mode in ("staged", "both"):
        args.append("--staged")
    if mode in ("worktree", "both"):
        args.append("--worktree")
    args += ["--", *files]
    _run_git(repo, args)


def head_sha(repo: Path) -> str:
    result = _run_git(repo, ["rev-parse", "HEAD"])
    return _stdout(result)


def commit(repo: Path, message: str) -> str:
    _run_git(repo, ["commit", "-m", message])
    return head_sha(repo)


def push(
    repo: Path, branch: str, remote: str = "origin", set_upstream: bool = True,
    extra_env: dict[str, str] | None = None,
) -> None:
    """Plain, non-force push of `branch` to `remote`. `set_upstream` passes `-u`
    to set the tracking branch on first push - harmless to repeat on subsequent
    pushes. `extra_env` (see `manager._gitlab_push_auth_env`) injects a GitLab
    auth header for hosts that require one - omitted entirely, this behaves
    exactly as before (relies on whatever git credential already exists)."""
    _reject_option_like(branch, "branch")
    _reject_option_like(remote, "remote")
    args = ["push"]
    if set_upstream:
        args += ["-u"]
    args += [remote, branch]
    _run_git(repo, args, timeout=60.0, extra_env=extra_env)


def remote_url(repo: Path, remote: str = "origin") -> str:
    result = _run_git(repo, ["remote", "get-url", remote])
    return _stdout(result)


def default_remote_head_branch(repo: Path, remote: str = "origin") -> str | None:
    """The remote's default branch (e.g. 'main' or 'development'), read from the
    local symbolic-ref cache set at clone/fetch time. Returns None if unknown -
    callers must handle that (design spec Section 3.3: never assume a default
    exists)."""
    result = _run_git(
        repo, ["symbolic-ref", f"refs/remotes/{remote}/HEAD"], allowed_returncodes={1, 128},
    )
    if result.returncode != 0:
        return None
    ref = _stdout(result)  # e.g. "refs/remotes/origin/main"
    prefix = f"refs/remotes/{remote}/"
    return ref[len(prefix):] if ref.startswith(prefix) else None


def added_lines_diff(repo: Path) -> dict[str, list[str]]:
    """Staged diff, filename -> list of added (not context) line contents.
    Used only for the debug-leftover scan (design spec Section 6.1) - never
    inspects unstaged or pre-existing lines."""
    result = _run_git(repo, ["diff", "--cached", "--unified=0", "--no-color"])
    out = result.stdout.decode("utf-8", errors="replace")
    diff: dict[str, list[str]] = {}
    current_file: str | None = None
    in_hunk = False
    for line in out.splitlines():
        if line.startswith("diff --git "):
            in_hunk = False
            current_file = None
            continue
        if not in_hunk and line.startswith("+++ b/"):
            current_file = line[len("+++ b/"):]
            diff.setdefault(current_file, [])
            continue
        if not in_hunk and line.startswith("+++ /dev/null"):
            current_file = None
            continue
        if line.startswith("@@"):
            in_hunk = True
            continue
        if in_hunk and current_file is not None and line.startswith("+"):
            diff[current_file].append(line[1:])
    return diff


def merge_ref(repo: Path, ref: str) -> None:
    """Real merge (not fast-forward-only) of `ref` into the currently checked-out
    branch. Raises GitCommandError on ANY non-zero exit, including a conflict -
    callers must check conflicted_files() after catching to distinguish a real
    conflict from some other failure."""
    _reject_option_like(ref, "ref")
    _run_git(repo, ["merge", "--no-edit", ref])


def merge_abort(repo: Path) -> None:
    _run_git(repo, ["merge", "--abort"])


def abort_in_progress_operation(repo: Path) -> str:
    """Aborts whichever conflict-capable operation is actually in progress -
    merge, cherry-pick, or rebase - detected from real on-disk state markers
    (MERGE_HEAD/CHERRY_PICK_HEAD/rebase-merge or rebase-apply), never assumed
    to be one specific flow. Returns which one was aborted. Raises
    GitCommandError if none is in progress - nothing to abort. See this
    module's docstring for why a rebase abort here does not violate the
    no-rebase doctrine."""
    git_dir = repo / ".git"
    if (git_dir / "MERGE_HEAD").exists():
        merge_abort(repo)
        return "merge"
    if (git_dir / "CHERRY_PICK_HEAD").exists():
        _run_git(repo, ["cherry-pick", "--abort"])
        return "cherry-pick"
    if (git_dir / "rebase-merge").exists() or (git_dir / "rebase-apply").exists():
        _run_git(repo, ["rebase", "--abort"])
        return "rebase"
    raise GitCommandError("No merge, cherry-pick, or rebase is currently in progress - nothing to abort.")


def conflicted_files(repo: Path) -> list[str]:
    """Paths with unresolved merge conflicts (unmerged index entries) -
    reflects the CURRENT repo's real index state, regardless of whether ICX,
    a manual `git merge`/`git pull`, a rebase, or a cherry-pick produced it."""
    result = _run_git(repo, ["diff", "--name-only", "--diff-filter=U"])
    out = _stdout(result)
    return [_unquote_git_path(line) for line in out.splitlines() if line]


def conflict_state(repo: Path) -> str:
    """Live, derived conflict-workflow state label - CLEAN/CONFLICT_DETECTED/
    STAGED - computed fresh from real git state every call, never stored or
    tracked separately (a stored tracker could drift from the real repo
    state; this can't, since it IS the real repo state). STAGED means every
    conflicted path has been staged (no more unmerged index entries) but an
    operation is still in progress - the merge/cherry-pick/rebase itself has
    not been committed/continued yet."""
    if conflicted_files(repo):
        return "CONFLICT_DETECTED"
    git_dir = repo / ".git"
    if (git_dir / "MERGE_HEAD").exists() or (git_dir / "CHERRY_PICK_HEAD").exists() or \
            (git_dir / "rebase-merge").exists() or (git_dir / "rebase-apply").exists():
        return "STAGED"
    return "CLEAN"


def conflict_stage(repo: Path, relpath: str, stage: int) -> str | None:
    """Reads one index stage of a conflicted path - 1=base (common ancestor),
    2=ours, 3=theirs. Tolerant of a missing stage (an add/add conflict has no
    base; a delete/modify conflict has no ours or theirs on the deleting
    side) - returns None rather than raising, unlike conflict_versions()
    which assumes both ours/theirs exist (the only shape ICX's own
    scratch-branch merge flow ever produces)."""
    if stage not in (1, 2, 3):
        raise GitCommandError(f"stage must be 1, 2, or 3, got {stage!r}")
    _reject_path_traversal(relpath, "relpath")
    result = _run_git(repo, ["show", f":{stage}:{relpath}"], allowed_returncodes={128})
    if result.returncode != 0:
        return None
    return result.stdout.decode("utf-8", errors="replace")


def parse_conflict_hunks(repo: Path, relpath: str) -> list[dict]:
    """Parses relpath's ON-DISK conflict markers into structured hunks -
    1-indexed start_line/end_line spanning the full `<<<<<<<` to `>>>>>>>`
    block, plus ours/theirs text for each - exactly what a human would see
    open in an editor. Assumes the standard 2-way marker style (`<<<<<<<` /
    `=======` / `>>>>>>>`) - ICX never enables diff3-style base markers
    (`|||||||`) for its own merges, so a per-hunk base section never has to
    be parsed here (use conflict_stage(repo, relpath, 1) for the whole
    file's base content instead)."""
    _reject_path_traversal(relpath, "relpath")
    text = (repo / relpath).read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    hunks: list[dict] = []
    i, n = 0, len(lines)
    while i < n:
        if not lines[i].startswith("<<<<<<<"):
            i += 1
            continue
        start = i
        i += 1
        ours_lines: list[str] = []
        while i < n and not lines[i].startswith("======="):
            ours_lines.append(lines[i])
            i += 1
        i += 1  # skip the ======= separator
        theirs_lines: list[str] = []
        while i < n and not lines[i].startswith(">>>>>>>"):
            theirs_lines.append(lines[i])
            i += 1
        hunks.append({
            "start_line": start + 1,
            "end_line": i + 1,
            "ours": "\n".join(ours_lines),
            "theirs": "\n".join(theirs_lines),
        })
        i += 1  # skip the >>>>>>> marker
    return hunks


def checkout_conflict_side(repo: Path, relpath: str, side: str) -> None:
    """Resolves relpath's ON-DISK content to one side of an in-progress
    conflict via `git checkout --ours`/`--theirs` - does NOT stage the
    result (still shows as unmerged in the index until stage_files is
    called) and does not touch any other file."""
    if side not in ("ours", "theirs"):
        raise GitCommandError(f"side must be 'ours' or 'theirs', got {side!r}")
    _reject_path_traversal(relpath, "relpath")
    _run_git(repo, ["checkout", f"--{side}", "--", relpath])


def commits_since(repo: Path, base_ref: str) -> list[str]:
    """One-line `<short-sha> <subject>` per commit on the current branch not
    on base_ref, newest first."""
    _reject_option_like(base_ref, "base_ref")
    result = _run_git(repo, ["log", f"{base_ref}..HEAD", "--oneline"])
    out = _stdout(result)
    return out.splitlines() if out else []


def changed_files_since(repo: Path, base_ref: str) -> list[str]:
    """Paths touched by any commit on the current branch not on base_ref."""
    _reject_option_like(base_ref, "base_ref")
    result = _run_git(repo, ["diff", "--name-only", f"{base_ref}...HEAD"])
    out = _stdout(result)
    return [_unquote_git_path(line) for line in out.splitlines() if line]


def changed_files_since_common_ancestor(repo: Path, ref: str) -> list[str]:
    """Paths touched on `ref` since ITS merge-base with the current branch -
    the reverse direction from changed_files_since() (which reports paths
    the CURRENT branch touched instead). Used to detect the "stale base"
    silent-deletion class of bug: a file this branch is about to commit that
    the parent branch also modified since the branch point - the pending
    commit could silently drop that upstream change on merge. Uses git's own
    triple-dot (symmetric-difference-from-merge-base) diff form, just with
    the two sides swapped relative to changed_files_since()."""
    _reject_option_like(ref, "ref")
    result = _run_git(repo, ["diff", "--name-only", f"HEAD...{ref}"])
    out = _stdout(result)
    return [_unquote_git_path(line) for line in out.splitlines() if line]


def file_exists_at_ref(repo: Path, ref: str, relpath: str) -> bool:
    _reject_option_like(ref, "ref")
    result = _run_git(repo, ["cat-file", "-e", f"{ref}:{relpath}"], allowed_returncodes={1, 128})
    return result.returncode == 0


def read_file_at_ref(repo: Path, ref: str, relpath: str) -> str:
    """Read relpath's exact content at ref - HEAD, MERGE_HEAD (mid-conflict),
    a branch, a remote-tracking ref, or a commit sha. Read-only, local, no
    network. Raises GitCommandError if ref does not resolve or relpath does
    not exist there."""
    _reject_option_like(ref, "ref")
    _reject_path_traversal(relpath, "relpath")
    result = _run_git(repo, ["show", f"{ref}:{relpath}"])
    return result.stdout.decode("utf-8", errors="replace")


def conflict_versions(repo: Path, relpath: str) -> tuple[str, str]:
    """Return (ours, theirs) content for a conflicted file, read from the
    merge index stages (stage 2 = ours, stage 3 = theirs) - works only while
    a merge conflict is actually in progress for this path."""
    ours_result = _run_git(repo, ["show", f":2:{relpath}"])
    theirs_result = _run_git(repo, ["show", f":3:{relpath}"])
    ours = ours_result.stdout.decode("utf-8", errors="replace")
    theirs = theirs_result.stdout.decode("utf-8", errors="replace")
    return ours, theirs


def blame(repo: Path, relpath: str, line_range: tuple[int, int] | None = None) -> list[dict]:
    """Per-line blame for `relpath` via `git blame --line-porcelain`, which repeats
    full commit metadata for every line (unlike --porcelain's first-occurrence-only
    metadata) - simpler and unambiguous to parse. `line_range` narrows to `-L
    start,end` but the returned `line_no` is always the file's real line number."""
    _reject_path_traversal(relpath, "relpath")
    args = ["blame", "--line-porcelain"]
    if line_range is not None:
        start, end = line_range
        args += ["-L", f"{start},{end}"]
    args += ["--", relpath]
    result = _run_git(repo, args)
    out = result.stdout.decode("utf-8", errors="replace")
    lines = out.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    entries: list[dict] = []
    i = 0
    n = len(lines)
    while i < n:
        header_parts = lines[i].split(" ")
        sha = header_parts[0]
        final_line_no = int(header_parts[2])
        i += 1
        meta: dict[str, str] = {}
        while i < n and not lines[i].startswith("\t"):
            line = lines[i]
            if line.startswith("author "):
                meta["author"] = line[len("author "):]
            elif line.startswith("author-mail "):
                meta["author_email"] = line[len("author-mail "):].strip("<>")
            elif line.startswith("author-time "):
                ts = int(line[len("author-time "):])
                meta["author_time"] = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
            elif line.startswith("summary "):
                meta["summary"] = line[len("summary "):]
            i += 1
        content = lines[i][1:] if i < n else ""
        i += 1
        entries.append({
            "line_no": final_line_no,
            "content": content,
            "commit_sha": sha,
            "author": meta.get("author", ""),
            "author_email": meta.get("author_email", ""),
            "author_time": meta.get("author_time", ""),
            "summary": meta.get("summary", ""),
        })
    return entries


_LOG_FORMAT = "%H%x1f%an%x1f%ae%x1f%aI%x1f%s%x1e"


def log(
    repo: Path,
    relpath: str | None = None,
    limit: int = 20,
    author: str | None = None,
    since: str | None = None,
) -> list[dict]:
    """Commit history, newest first (git log's default order). `limit` is clamped
    to 1-500 rather than raising, matching this module's tolerant-on-simple-params
    stance. `relpath` scopes history to commits touching that file."""
    limit = max(1, min(limit, 500))
    args = ["log", f"--format={_LOG_FORMAT}", "-n", str(limit)]
    if author is not None:
        args.append(f"--author={author}")
    if since is not None:
        args.append(f"--since={since}")
    if relpath is not None:
        _reject_path_traversal(relpath, "relpath")
        args += ["--", relpath]
    result = _run_git(repo, args)
    out = result.stdout.decode("utf-8", errors="replace")
    commits = []
    for record in out.split("\x1e"):
        record = record.strip("\n")
        if not record:
            continue
        sha, an, ae, ai, subject = record.split("\x1f")
        commits.append({
            "sha": sha,
            "author": an,
            "author_email": ae,
            "date": ai,
            "subject": subject,
        })
    return commits


_SHOW_FORMAT = "%H%x1f%an%x1f%ae%x1f%aI%x1f%s%x1f%b%x1e"


def show_commit(repo: Path, sha: str) -> dict:
    """Single commit's message plus its changed files (`--name-status`, so status
    is git's code: A/M/D/R100 etc). Raises GitCommandError for an unresolvable sha
    via _run_git's normal non-zero-exit handling."""
    _reject_option_like(sha, "sha")
    result = _run_git(repo, ["show", f"--format={_SHOW_FORMAT}", "--name-status", sha])
    out = result.stdout.decode("utf-8", errors="replace")
    header, _, rest = out.partition("\x1e")
    sha_, an, ae, ai, subject, body = header.split("\x1f", 5)
    files = []
    for line in rest.strip("\n").splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        status = parts[0]
        path = _unquote_git_path(parts[-1])
        files.append({"path": path, "status": status})
    return {
        "sha": sha_,
        "author": an,
        "author_email": ae,
        "date": ai,
        "subject": subject,
        "body": body.strip("\n"),
        "files": files,
    }


def _diff_stats(repo: Path, base_args: list[str], relpath: str | None) -> dict:
    """Shared implementation for diff_between/diff_worktree - per-file status
    plus insertions/deletions. Combines `--numstat` (counts) and
    `--name-status` (status codes) - numstat alone has no status field. Rename
    detection is disabled on both calls so paths line up 1:1 between the two
    outputs. Binary files report `insertions`/`deletions` as None (numstat
    prints '-' for them, not a number)."""
    path_args: list[str] = []
    if relpath is not None:
        _reject_path_traversal(relpath, "relpath")
        path_args = ["--", relpath]
    numstat_result = _run_git(repo, [*base_args, "--numstat", *path_args])
    namestatus_result = _run_git(repo, [*base_args, "--name-status", *path_args])
    numstat_out = _stdout(numstat_result)
    namestatus_out = _stdout(namestatus_result)

    stats: dict[str, tuple[int | None, int | None]] = {}
    for line in numstat_out.splitlines():
        if not line:
            continue
        parts = line.split("\t")
        ins_str, del_str, path = parts[0], parts[1], parts[-1]
        path = _unquote_git_path(path)
        insertions = None if ins_str == "-" else int(ins_str)
        deletions = None if del_str == "-" else int(del_str)
        stats[path] = (insertions, deletions)

    statuses: dict[str, str] = {}
    order: list[str] = []
    for line in namestatus_out.splitlines():
        if not line:
            continue
        parts = line.split("\t")
        status = parts[0]
        path = _unquote_git_path(parts[-1])
        statuses[path] = status
        order.append(path)

    files = []
    for path in order:
        insertions, deletions = stats.get(path, (None, None))
        files.append({
            "path": path,
            "status": statuses[path],
            "insertions": insertions,
            "deletions": deletions,
        })
    return {"files": files}


def diff_between(repo: Path, ref_a: str, ref_b: str) -> dict:
    """Per-file status plus insertions/deletions between two refs (both
    committed - use diff_worktree for anything involving the working tree or
    the index)."""
    _reject_option_like(ref_a, "ref_a")
    _reject_option_like(ref_b, "ref_b")
    return _diff_stats(repo, ["diff", "--no-renames", ref_a, ref_b], relpath=None)


def diff_worktree(repo: Path, mode: str = "combined", relpath: str | None = None) -> dict:
    """Local, uncommitted diff - diff_between only ever compares two existing
    refs, never the working tree or index. mode='staged' is index vs HEAD
    (what the next commit would contain), 'unstaged' is working tree vs index
    (changes not yet staged), 'combined' is working tree vs HEAD (every
    uncommitted change, staged or not). relpath scopes to one file; omitted
    means every changed file. Same per-file shape as diff_between."""
    if mode == "staged":
        base_args = ["diff", "--no-renames", "--cached"]
    elif mode == "unstaged":
        base_args = ["diff", "--no-renames"]
    elif mode == "combined":
        base_args = ["diff", "--no-renames", "HEAD"]
    else:
        raise GitCommandError(f"mode must be 'staged', 'unstaged', or 'combined', got {mode!r}")
    return _diff_stats(repo, base_args, relpath)


_STATUS_CODE_NAMES = {
    "M": "modified", "A": "added", "D": "deleted",
    "R": "renamed", "C": "copied", "T": "type_changed", "U": "unmerged",
}


def structured_status(repo: Path) -> dict:
    """Full working-tree status via `git status --porcelain=v2 --branch` -
    v2's fixed-width XY codes and dedicated line-type prefixes (1/2/u/?) let
    staged vs unstaged vs conflicted be told apart unambiguously, unlike v1's
    single ambiguous leading-space format (dirty_files() above). branch.ab is
    only present when an upstream is configured - ahead/behind default to 0
    and upstream to None otherwise."""
    result = _run_git(repo, ["status", "--porcelain=v2", "--branch"])
    out = _stdout(result)

    branch: str | None = None
    upstream: str | None = None
    ahead = 0
    behind = 0
    staged: list[dict] = []
    unstaged: list[dict] = []
    untracked: list[str] = []
    renamed: list[dict] = []
    conflicted: list[str] = []
    deleted: set[str] = set()

    for line in out.splitlines():
        if line.startswith("# branch.head "):
            branch = line[len("# branch.head "):]
        elif line.startswith("# branch.upstream "):
            upstream = line[len("# branch.upstream "):]
        elif line.startswith("# branch.ab "):
            for token in line[len("# branch.ab "):].split():
                if token.startswith("+"):
                    ahead = int(token[1:])
                elif token.startswith("-"):
                    behind = int(token[1:])
        elif line.startswith("1 "):
            fields = line.split(" ", 8)
            xy, path = fields[1], _unquote_git_path(fields[8])
            staged_code, unstaged_code = xy[0], xy[1]
            if staged_code != ".":
                staged.append({"path": path, "status": _STATUS_CODE_NAMES.get(staged_code, staged_code)})
                if staged_code == "D":
                    deleted.add(path)
            if unstaged_code != ".":
                unstaged.append({"path": path, "status": _STATUS_CODE_NAMES.get(unstaged_code, unstaged_code)})
                if unstaged_code == "D":
                    deleted.add(path)
        elif line.startswith("2 "):
            fields = line.split(" ", 9)
            staged_code, unstaged_code = fields[1][0], fields[1][1]
            new_path, _, old_path = fields[9].partition("\t")
            new_path, old_path = _unquote_git_path(new_path), _unquote_git_path(old_path)
            renamed.append({"from": old_path, "to": new_path})
            if staged_code != ".":
                staged.append({"path": new_path, "status": _STATUS_CODE_NAMES.get(staged_code, staged_code)})
            if unstaged_code != ".":
                unstaged.append({"path": new_path, "status": _STATUS_CODE_NAMES.get(unstaged_code, unstaged_code)})
        elif line.startswith("u "):
            fields = line.split(" ", 10)
            conflicted.append(_unquote_git_path(fields[10]))
        elif line.startswith("? "):
            untracked.append(_unquote_git_path(line[2:]))

    return {
        "current_branch": branch,
        "upstream": upstream,
        "ahead": ahead,
        "behind": behind,
        "staged": staged,
        "unstaged": unstaged,
        "untracked": untracked,
        "deleted": sorted(deleted),
        "renamed": renamed,
        "conflicted": conflicted,
    }
