"""
CO_CHANGED edge resolver.

Files that co-appear in git commits >= min_strength fraction of the time
get a co_changed edge. Reveals architectural coupling that static analysis cannot detect.

Edge type: co_changed
Confidence: 0.50 + (strength * 0.50) capped at 0.90
  (strength=0.30 -> confidence=0.65, strength=1.0 -> confidence=0.90)

Activation: git must be available and project must be a git repo.
Graceful fallback: returns empty list if git unavailable, timeout, or non-git project.
"""
import subprocess
from pathlib import Path
from collections import defaultdict
from itertools import combinations

_GIT_BASE = ["git", "-c", "core.quotepath=false"]


def resolve_cochange(
    files: list,
    project_path,
    extraction: dict,
    max_commits: int = 200,
    min_strength: float = 0.30,
    min_cooccurrences: int = 3,
) -> list:
    nodes = extraction.get("nodes", []) if isinstance(extraction, dict) else []
    project_path_str = str(project_path)

    # Verify git is available and project is a repo
    try:
        r = subprocess.run(
            _GIT_BASE + ["rev-parse", "--git-dir"],
            cwd=project_path_str, capture_output=True, timeout=5,
        )
        if r.returncode != 0:
            return []
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return []

    # Get commit history
    try:
        log = subprocess.run(
            _GIT_BASE + [
                "log", f"-{max_commits}",
                "--name-only", "--pretty=format:__COMMIT__",
                "--diff-filter=ACMRT",
            ],
            cwd=project_path_str, capture_output=True, text=True,
            timeout=30, errors="replace",
        )
        if log.returncode != 0:
            return []
    except (subprocess.TimeoutExpired, OSError):
        return []

    # Build set of tracked relative file paths
    file_strs = [str(f).replace("\\", "/") for f in files]
    root_posix = Path(project_path_str).as_posix()
    rel_set: set[str] = set()
    abs_to_rel: dict[str, str] = {}
    for f in file_strs:
        if f.startswith(root_posix + "/"):
            rel = f[len(root_posix) + 1:]
        else:
            rel = f
        rel_set.add(rel)
        abs_to_rel[f] = rel

    # Parse commits
    commits: list[list[str]] = []
    current: list[str] = []
    for line in log.stdout.splitlines():
        line = line.strip()
        if line == "__COMMIT__":
            if current:
                commits.append(current)
            current = []
        elif line and (norm := line.replace("\\", "/")) in rel_set:
            current.append(norm)
    if current:
        commits.append(current)

    # Count co-occurrences
    cooccur: dict[tuple, int] = defaultdict(int)
    file_count: dict[str, int] = defaultdict(int)
    for commit_files in commits:
        unique = list(set(commit_files))
        for f in unique:
            file_count[f] += 1
        for f1, f2 in combinations(sorted(unique), 2):
            cooccur[(f1, f2)] += 1

    # Build node lookup (both relative and absolute paths)
    node_by_file: dict[str, list] = defaultdict(list)
    for n in nodes:
        sf = (n.get("source_file") or n.get("file") or "").replace("\\", "/")
        if sf:
            node_by_file[sf].append(n)
            if sf.startswith(root_posix + "/"):
                node_by_file[sf[len(root_posix) + 1:]].append(n)
            else:
                node_by_file[f"{root_posix}/{sf}"].append(n)

    edges = []
    seen: set[tuple] = set()

    for (f1, f2), count in cooccur.items():
        if count < min_cooccurrences:
            continue
        strength = count / max(file_count.get(f1, 1), file_count.get(f2, 1))
        if strength < min_strength:
            continue
        pair = (min(f1, f2), max(f1, f2))
        if pair in seen:
            continue
        seen.add(pair)

        n1 = node_by_file.get(f1, [])
        n2 = node_by_file.get(f2, [])
        if not (n1 and n2):
            continue

        confidence = round(min(0.90, 0.50 + strength * 0.50), 4)
        for src, tgt, sf_node, tf_node in [(f1, f2, n1[0], n2[0]), (f2, f1, n2[0], n1[0])]:
            edges.append({
                "source": sf_node["id"], "target": tf_node["id"],
                "source_file": src, "target_file": tgt,
                "relation": "co_changed", "type": "co_changed", "confidence": confidence,
                "co_change_strength": round(strength, 4),
                "co_occurrences": count,
                "resolver": "cochange",
                "fix_confidence_delta": 0.0, "resolution_weight": 0.0,
            })

    return edges
