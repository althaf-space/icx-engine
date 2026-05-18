"""
Graph report generator: reads graph.json, writes GRAPH_REPORT.md navigation map.
The report is read directly by AI agents to navigate the codebase.
"""
from __future__ import annotations

import json
import logging
import math
import re
from collections import Counter, defaultdict
from pathlib import Path

_log = logging.getLogger(__name__)


def generate_graph_report(graph_json_path: Path, output_path: Path) -> None:
    """Read graph.json and write GRAPH_REPORT.md navigation map."""
    try:
        graph_data = json.loads(graph_json_path.read_text(encoding="utf-8"))
    except Exception as exc:
        _log.debug("generate_graph_report: failed to read graph (%s)", type(exc).__name__)
        output_path.write_text(
            "# Project Graph Report\n\nGraph data unavailable.\n",
            encoding="utf-8",
        )
        return

    nodes: list[dict] = graph_data.get("nodes", [])
    # NetworkX serializes edges as "links"; fall back to "edges" for older graphs.
    edges: list[dict] = graph_data.get("links") or graph_data.get("edges", [])

    if not nodes:
        output_path.write_text(
            "# Project Graph Report\n\nNo nodes found in graph.\n",
            encoding="utf-8",
        )
        return

    # -----------------------------------------------------------------------
    # Step 1: Build node_id -> source_file mapping (skip nodes without one)
    # -----------------------------------------------------------------------
    _ARCHIVE_SUFFIXES = frozenset({".war", ".jar", ".ear", ".zip", ".tar"})

    def _is_archive_path(src: str) -> bool:
        """Skip files that live inside expanded archive dirs (e.g. Work_Order_UI.war/)."""
        return any(
            Path(part).suffix.lower() in _ARCHIVE_SUFFIXES
            for part in Path(src.replace("\\", "/")).parts[:-1]
        )

    node_to_file: dict[str, str] = {}
    for node in nodes:
        nid = node.get("id") or node.get("label") or ""
        src = node.get("source_file")
        if nid and src and not _is_archive_path(src):
            node_to_file[nid] = src

    # -----------------------------------------------------------------------
    # Step 2: Per-file degree (connection count) via edges
    # -----------------------------------------------------------------------
    node_degree: dict[str, int] = defaultdict(int)
    for edge in edges:
        src = edge.get("source", "")
        tgt = edge.get("target", "")
        if src:
            node_degree[src] += 1
        if tgt:
            node_degree[tgt] += 1

    # Map node degree to file degree: file degree = max node degree for that file
    file_degree: dict[str, int] = {}
    for nid, degree in node_degree.items():
        file_path = node_to_file.get(nid)
        if file_path:
            if file_path not in file_degree or degree > file_degree[file_path]:
                file_degree[file_path] = degree

    # Files with no edges get degree 0
    all_files: set[str] = set(node_to_file.values())
    for f in all_files:
        if f not in file_degree:
            file_degree[f] = 0

    # -----------------------------------------------------------------------
    # Step 3: Determine community assignments
    # -----------------------------------------------------------------------
    # Priority 1: top-level "communities" key
    communities_raw = graph_data.get("communities")
    node_community: dict[str, str] = {}

    if communities_raw and isinstance(communities_raw, dict):
        for comm_id, member_ids in communities_raw.items():
            for nid in member_ids:
                node_community[str(nid)] = str(comm_id)
    else:
        # Priority 2: community attribute on each node
        has_node_community = any(node.get("community") is not None for node in nodes)
        if has_node_community:
            for node in nodes:
                nid = node.get("id") or node.get("label") or ""
                comm = node.get("community")
                if nid and comm is not None:
                    node_community[nid] = str(comm)
        else:
            # Priority 3: group by parent directory of source_file
            for node in nodes:
                nid = node.get("id") or node.get("label") or ""
                src = node.get("source_file")
                if nid and src:
                    parent = str(Path(src.replace("\\", "/")).parent)
                    node_community[nid] = parent

    # -----------------------------------------------------------------------
    # Step 4: Group source files by community
    # -----------------------------------------------------------------------
    # file -> community string
    file_community: dict[str, str] = {}
    for nid, src in node_to_file.items():
        comm = node_community.get(nid)
        if comm is not None:
            if src not in file_community:
                file_community[src] = comm

    # Files without community assignment go to "0"
    for f in all_files:
        if f not in file_community:
            file_community[f] = "0"

    # community -> list of files
    community_files: dict[str, list[str]] = defaultdict(list)
    for f, comm in file_community.items():
        community_files[comm].append(f)

    # Fallback: AST-only mode produces 0 cross-file edges, so Louvain assigns each
    # node its own community. Every community ends up single-file and the 2-file
    # filter would hide everything. Re-derive using parent directory so the report
    # is still useful for navigation.
    if all(len(fs) < 2 for fs in community_files.values()):
        file_community = {}
        for f in all_files:
            parent = str(Path(f.replace("\\", "/")).parent)
            file_community[f] = parent
        community_files = defaultdict(list)
        for f, comm in file_community.items():
            community_files[comm].append(f)

    # Sort communities by size descending for stable ordering
    sorted_communities = sorted(community_files.items(), key=lambda x: -len(x[1]))

    def _community_label(comm_id: str, files: list[str], index: int) -> str:
        _SKIP_PARTS = frozenset({
            ".", "..", "src", "lib", "app", "main", "java", "kotlin",
            "com", "org", "net", "io", "resources", "webapp",
        })
        _GENERIC_STEMS = frozenset({
            "impl", "base", "util", "utils", "helper", "abstract", "default",
            "index", "main", "config", "test", "spec", "controller", "service",
            "dao", "dto", "model", "entity", "repository", "manager", "handler",
            "factory", "adapter", "component", "page", "screen", "view",
            "hook", "reducer", "action", "selector", "slice", "store",
            "router", "route", "enum", "type", "constants",
        })
        try:
            stems = [Path(f.replace("\\", "/")).stem for f in files]

            # Strategy 1: common prefix of file stems - works for Java feature clusters
            # e.g. WorkOrderConfigController+Dao+Service → "WorkOrderConfig"
            if stems:
                prefix = stems[0]
                for s in stems[1:]:
                    i = 0
                    while i < len(prefix) and i < len(s) and prefix[i] == s[i]:
                        i += 1
                    prefix = prefix[:i]
                if len(prefix) >= 4:
                    return prefix

            # Strategy 2: most common non-generic word across CamelCase-split stems
            # requires the word to appear in at least 2 files (or 1 for single-file clusters)
            word_counts: dict[str, int] = {}
            for stem in stems:
                parts = re.findall(r'[A-Z]?[a-z]+|[A-Z]+(?=[A-Z][a-z]|\d|$)', stem)
                if not parts:
                    parts = re.split(r'[_\-\s]+', stem.lower())
                for w in parts:
                    wl = w.lower()
                    if len(wl) >= 3 and wl not in _GENERIC_STEMS:
                        word_counts[wl] = word_counts.get(wl, 0) + 1
            if word_counts:
                best = max(word_counts, key=lambda k: (word_counts[k], len(k)))
                if word_counts[best] >= min(2, len(stems)):
                    return best.capitalize()

            # Strategy 3: depth-weighted directory segments - deeper = more specific
            # e.g. pages/WorkOrder/ scores higher than project-root cms-re-wo-ui/
            seg_weights: dict[str, float] = {}
            for f in files:
                parts = Path(f.replace("\\", "/")).parts
                n = max(len(parts), 1)
                for depth, part in enumerate(parts[:-1]):
                    if part in _SKIP_PARTS or part.startswith("."):
                        continue
                    seg_weights[part] = seg_weights.get(part, 0.0) + (depth + 1) / n
            if seg_weights:
                return max(seg_weights, key=lambda k: seg_weights[k])

        except Exception:
            pass
        return f"cluster_{index}"

    # -----------------------------------------------------------------------
    # Step 5: Identify god nodes
    # -----------------------------------------------------------------------
    degrees = list(file_degree.values())
    god_nodes: list[tuple[str, int]] = []
    if len(degrees) >= 2:
        mean_deg = sum(degrees) / len(degrees)
        variance = sum((d - mean_deg) ** 2 for d in degrees) / len(degrees)
        std_dev = math.sqrt(variance)
        threshold = mean_deg + 2 * std_dev
        god_nodes = [
            (f, d) for f, d in file_degree.items() if d > threshold
        ]
        god_nodes.sort(key=lambda x: -x[1])
        god_nodes = god_nodes[:10]

    # -----------------------------------------------------------------------
    # Step 6: Cross-cluster connections
    # -----------------------------------------------------------------------
    cross_cluster_counts: dict[tuple[str, str], int] = defaultdict(int)
    if len(sorted_communities) > 1:
        for edge in edges:
            src_nid = edge.get("source", "")
            tgt_nid = edge.get("target", "")
            src_file = node_to_file.get(src_nid)
            tgt_file = node_to_file.get(tgt_nid)
            if not src_file or not tgt_file:
                continue
            src_comm = file_community.get(src_file)
            tgt_comm = file_community.get(tgt_file)
            if src_comm and tgt_comm and src_comm != tgt_comm:
                pair = tuple(sorted([src_comm, tgt_comm]))
                cross_cluster_counts[pair] += 1  # type: ignore[index]

    cross_cluster_pairs = sorted(cross_cluster_counts.items(), key=lambda x: -x[1])[:20]

    comm_labels: dict[str, str] = {}
    for idx, (comm_id, files) in enumerate(sorted_communities):
        comm_labels[comm_id] = _community_label(comm_id, files, idx)

    # -----------------------------------------------------------------------
    # Step 7: Write GRAPH_REPORT.md
    # -----------------------------------------------------------------------
    lines: list[str] = ["# Project Graph Report", ""]

    # Only show clusters with 2+ source files - single-file clusters are noise.
    shown_communities = [(cid, fs) for cid, fs in sorted_communities if len(fs) >= 2]
    hidden_count = len(sorted_communities) - len(shown_communities)
    has_any_degree = any(d > 0 for d in file_degree.values())

    lines.append("## Community Clusters")
    lines.append("")
    for idx, (comm_id, files) in enumerate(shown_communities):
        label = comm_labels[comm_id]
        n = len(files)
        lines.append(f"### {label} ({n} files)")
        lines.append("Core files (read first):")
        sorted_files = sorted(files, key=lambda f: -file_degree.get(f, 0))
        for f in sorted_files[:10]:
            deg = file_degree.get(f, 0)
            deg_str = f"  [degree: {deg}]" if has_any_degree else ""
            lines.append(f"  - {f}{deg_str}")
        lines.append("")

    if hidden_count > 0:
        lines.append(f"*({hidden_count} single-file module(s) not listed above)*")
        lines.append("")

    lines.append("## God Nodes (high connectivity - check these for cross-cutting concerns)")
    if god_nodes:
        for f, deg in god_nodes:
            lines.append(f"  - {f}  ({deg} connections)")
    else:
        lines.append("  (none identified)")
    lines.append("")

    if len(sorted_communities) > 1:
        lines.append("## Cross-Cluster Connections")
        if cross_cluster_pairs:
            for (comm_a, comm_b), count in cross_cluster_pairs:
                label_a = comm_labels.get(comm_a, comm_a)
                label_b = comm_labels.get(comm_b, comm_b)
                lines.append(f"  - {label_a} <-> {label_b}  ({count} edges)")
        else:
            lines.append("  (none detected)")
        lines.append("")

    lines.append("---")
    lines.append(
        "*Generated by ICX graph engine. Read this file to understand project structure, "
        "then read core files from relevant clusters.*"
    )
    lines.append("")

    report = "\n".join(lines)
    output_path.write_text(report, encoding="utf-8")
