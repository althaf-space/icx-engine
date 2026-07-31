"""Safe git subprocess wrapper for the git-workflow lifecycle tool.

Every function here shells out to a real `git` process using the same
hardened subprocess settings as `graph/paths.py` - credential helpers
disabled, no terminal prompts, explicit timeouts, no inherited handles on
Windows. No rebase, no force-push - those operations do not exist in this
module by design - see the design spec's safety doctrine
(docs/superpowers/specs/2026-07-26-icx-git-workflow-design.md, Section 2).
A real, conflict-capable merge exists here (`merge_ref`), but this module
never interprets or resolves a conflict itself - a caller that gets a
conflict must either abort (`merge_abort`) or hand it to the
conflict-quarantine flow in `manager.py`; `gitcmd.py` only ever reports raw
git state, never makes a resolution decision. `fast_forward`/`fast_forward_ref`
are `--ff-only` and cannot conflict or create a merge commit - they are not
"real" merges in this sense.
"""
from __future__ import annotations

import json
import os
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


def _safe_git_env() -> dict[str, str]:
    env = dict(os.environ)
    env.update(_GIT_SAFE_ENV)
    return env


def _run_git(
    repo: Path,
    args: list[str],
    timeout: float = _DEFAULT_TIMEOUT,
    allowed_returncodes: set[int] | None = None,
) -> subprocess.CompletedProcess:
    """Run a git command in `repo`. Raises GitCommandError unless the return
    code is 0 or explicitly listed in `allowed_returncodes` (used by callers
    like `remote_branch_exists` where a non-zero code is a meaningful result,
    not a failure)."""
    ok_codes = {0} | (allowed_returncodes or set())
    try:
        result = subprocess.run(
            [*_GIT_BASE_CMD, *args],
            cwd=str(repo), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL, timeout=timeout, env=_safe_git_env(),
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


def fetch(repo: Path, remote: str = "origin") -> None:
    _reject_option_like(remote, "remote")
    _run_git(repo, ["fetch", remote], timeout=60.0)


def remote_branch_exists(repo: Path, branch: str, remote: str = "origin") -> bool:
    _reject_option_like(branch, "branch")
    _reject_option_like(remote, "remote")
    result = _run_git(
        repo, ["ls-remote", "--exit-code", "--heads", remote, branch],
        timeout=30.0, allowed_returncodes={2},
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


def stash_pop(repo: Path) -> None:
    _run_git(repo, ["stash", "pop"])


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


def stage_files(repo: Path, files: list[str]) -> None:
    """Stage exactly these files. Never called with a wildcard or '.' - callers
    always pass an explicit list (design spec Section 6.3)."""
    if not files:
        return
    _run_git(repo, ["add", "--", *files])


def head_sha(repo: Path) -> str:
    result = _run_git(repo, ["rev-parse", "HEAD"])
    return _stdout(result)


def commit(repo: Path, message: str) -> str:
    _run_git(repo, ["commit", "-m", message])
    return head_sha(repo)


def push(repo: Path, branch: str, remote: str = "origin", set_upstream: bool = True) -> None:
    """Plain, non-force push of `branch` to `remote`. `set_upstream` passes `-u`
    to set the tracking branch on first push - harmless to repeat on subsequent
    pushes."""
    _reject_option_like(branch, "branch")
    _reject_option_like(remote, "remote")
    args = ["push"]
    if set_upstream:
        args += ["-u"]
    args += [remote, branch]
    _run_git(repo, args, timeout=60.0)


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


def conflicted_files(repo: Path) -> list[str]:
    """Paths with unresolved merge conflicts (unmerged index entries)."""
    result = _run_git(repo, ["diff", "--name-only", "--diff-filter=U"])
    out = _stdout(result)
    return [_unquote_git_path(line) for line in out.splitlines() if line]


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


def file_exists_at_ref(repo: Path, ref: str, relpath: str) -> bool:
    _reject_option_like(ref, "ref")
    result = _run_git(repo, ["cat-file", "-e", f"{ref}:{relpath}"], allowed_returncodes={1, 128})
    return result.returncode == 0


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


def diff_between(repo: Path, ref_a: str, ref_b: str) -> dict:
    """Per-file status plus insertions/deletions between two refs. Combines
    `--numstat` (counts) and `--name-status` (status codes) - numstat alone has
    no status field. Rename detection is disabled on both calls so paths line up
    1:1 between the two outputs. Binary files report `insertions`/`deletions` as
    None (numstat prints '-' for them, not a number)."""
    _reject_option_like(ref_a, "ref_a")
    _reject_option_like(ref_b, "ref_b")
    numstat_result = _run_git(repo, ["diff", "--no-renames", "--numstat", ref_a, ref_b])
    namestatus_result = _run_git(repo, ["diff", "--no-renames", "--name-status", ref_a, ref_b])
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
