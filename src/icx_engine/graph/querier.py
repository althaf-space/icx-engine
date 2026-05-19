"""
Graph report generator: reads graph.json, writes:
  GRAPH_REPORT.md       - compact index (god nodes, cluster table, cross-cluster)
  GRAPH_CLUSTERS/*.md   - one file per cluster with role-tagged file list

Also reads cluster_descriptions.json from graph_json_path.parent when present
(written by manager._generate_cluster_descriptions when LLM is configured).
"""
from __future__ import annotations

import json
import logging
import math
import re
from collections import defaultdict
from pathlib import Path

_log = logging.getLogger(__name__)


def _role_tag(filepath: str) -> str:
    """
    Return a role tag like '[controller]' based on filename/path patterns, or '' if none.

    Three layers:
    1. JS/JSX/TS/TSX/Vue/Svelte framework path conventions (React, Redux, Vue Router,
       SvelteKit - directory structure carries meaning that filename alone cannot).
    2. Universal stem-suffix detection across ALL languages: Java, Python, Go, C#,
       Kotlin, Scala, Ruby, PHP, Rust, Swift, Elixir, Dart, Objective-C, etc.
    3. Directory convention detection for languages where the filename has no role
       hint but the containing directory is unambiguous (Rails models/, Vue views/).

    Languages with no established role conventions (C/C++, Lua, Zig, Julia, PS1,
    SQL, Markdown) correctly return '' - blank is the right answer for those.
    """
    fp = filepath.replace("\\", "/")
    stem = Path(fp).stem
    sl = stem.lower()
    fpl = fp.lower()

    # ------------------------------------------------------------------
    # Layer 1: JS/JSX/TS/TSX/Vue/Svelte framework path conventions
    # ------------------------------------------------------------------
    if fp.endswith((".js", ".jsx", ".mjs", ".ts", ".tsx", ".vue", ".svelte")):
        # React hooks / Vue 3 composables: lowercase "use" + uppercase next char
        # Strict check avoids false matches on userList, userActions, etc.
        if "/hooks/" in fpl or "/composables/" in fpl or (
            stem.startswith("use") and len(stem) > 3 and stem[3].isupper()
        ):
            return "[hook]"
        if "redux/actions" in fpl or "/actions/" in fpl:
            return "[action]"
        if "redux/reducers" in fpl or "/reducers/" in fpl:
            return "[reducer]"
        if "/store/" in fpl or "redux/store" in fpl:
            return "[store]"
        if "routeconfig/" in fpl or "/routes/" in fpl:
            return "[route]"
        if "/util/" in fpl or "/utils/" in fpl or "/helpers/" in fpl:
            return "[util]"
        # Stem checks before generic path fallbacks: DeleteModal.jsx in containers/
        # gets [modal], not [container]
        if "modal" in sl:
            return "[modal]"
        if "context" in sl:
            return "[context]"
        if "containers/" in fpl:
            return "[container]"
        if "components/" in fpl:
            return "[component]"
        if "appconfig/" in fpl or "/config/" in fpl:
            return "[config]"
        # Fall through to universal stem layer for remaining cases

    # ------------------------------------------------------------------
    # Layer 2: Universal stem-suffix detection (language-agnostic)
    # Longest suffix first - prevents shorter entries stealing matches
    # (e.g. "serviceimpl" must come before "service").
    # ------------------------------------------------------------------
    _SUFFIX_TAGS: tuple[tuple[str, str], ...] = (
        # Controllers / handlers
        ("restcontroller",  "[controller]"),   # Java Spring
        ("controller",      "[controller]"),   # Java, C#, Ruby, PHP, Go, Dart, Swift
        ("handler",         "[controller]"),   # Go, Python HTTP handlers
        ("views",           "[controller]"),   # Django views.py (plural only - "view"
                                               # alone is ambiguous: Phoenix views are
                                               # presenters, not controllers)
        # Services / use-cases / state management
        ("serviceimpl",     "[service]"),      # Java (must precede "service")
        ("service",         "[service]"),      # Java, C#, Go, Python, Swift
        ("usecase",         "[service]"),      # Clean Architecture
        ("interactor",      "[service]"),      # Clean Architecture
        ("task",            "[service]"),      # Celery tasks, background workers
        ("channel",         "[service]"),      # Elixir Phoenix channels, Django Channels
        ("consumers",       "[service]"),      # Django Channels (consumers.py plural)
        ("consumer",        "[service]"),      # Django Channels consumer
        ("bloc",            "[service]"),      # Flutter BLoC state management
        ("cubit",           "[service]"),      # Flutter Cubit state management
        ("provider",        "[service]"),      # Flutter Provider, DI containers
        ("coordinator",     "[service]"),      # iOS Coordinator navigation pattern
        ("presenter",       "[service]"),      # MVP pattern (Android, iOS)
        ("delegate",        "[service]"),      # iOS/Swift delegate pattern
        # Data access
        ("repositoryimpl",  "[dao]"),          # Java (must precede "repository")
        ("repository",      "[dao]"),          # Java, C#, PHP, Go, Dart, Python
        ("daoimpl",         "[dao]"),          # Java (must precede "dao")
        ("dao",             "[dao]"),          # Java data access object
        ("repo",            "[dao]"),          # Python/Go shorthand
        # Models / data contracts
        ("serializer",      "[model]"),        # Django REST Framework
        ("serializers",     "[model]"),        # Django REST (serializers.py)
        ("schema",          "[model]"),        # Pydantic, GraphQL, Marshmallow
        ("schemas",         "[model]"),        # plural
        ("entity",          "[model]"),        # Java JPA, TypeScript
        ("entities",        "[model]"),        # plural
        ("models",          "[model]"),        # Django models.py
        ("model",           "[model]"),        # general; also catches ViewModel (Swift/Android)
        ("dto",             "[model]"),        # Data Transfer Object
        ("to",              "[model]"),        # Java Transfer Object (ScheduleTO)
        ("bean",            "[model]"),        # Java Bean
        # Configuration
        ("configuration",   "[config]"),       # Java Spring (must precede "config")
        ("settings",        "[config]"),       # Django settings.py, Python configs
        ("configs",         "[config]"),       # plural
        ("config",          "[config]"),       # general
        # UI components / screens / pages
        ("widget",          "[component]"),    # Flutter widgets, Android widgets
        ("screen",          "[container]"),    # Flutter/React Native screens
        ("page",            "[container]"),    # Next.js/SvelteKit pages
        # Middleware / filters
        ("middleware",      "[middleware]"),   # Express, Django, Go, Spring
        ("filter",          "[middleware]"),   # Spring filters, Django middleware
        ("interceptor",     "[middleware]"),   # Spring interceptors
        ("plug",            "[middleware]"),   # Elixir Plug middleware
        # Utilities
        ("decoder",         "[util]"),
        ("encoder",         "[util]"),
        ("helpers",         "[util]"),
        ("helper",          "[util]"),
        ("utils",           "[util]"),
        ("util",            "[util]"),
        # Routing
        ("urls",            "[route]"),        # Django urls.py
        ("url",             "[route]"),
        ("router",          "[route]"),
        ("routes",          "[route]"),
        ("route",           "[route]"),
        # Testing
        ("tests",           "[test]"),
        ("test",            "[test]"),
        ("spec",            "[test]"),         # RSpec, Jest
        # Exceptions
        ("exceptions",      "[exception]"),
        ("exception",       "[exception]"),
        ("error",           "[exception]"),
        ("errors",          "[exception]"),
    )

    for suffix, tag in _SUFFIX_TAGS:
        if sl.endswith(suffix):
            return tag

    # ------------------------------------------------------------------
    # Layer 3: Directory convention detection
    # For languages where the filename carries no role hint but the
    # containing directory is unambiguous (e.g. user.rb in models/).
    # Excludes /services/, /util/ etc. - those appear in Java package
    # paths (com.example.services.util) and would cause false positives.
    # ------------------------------------------------------------------
    _DIR_TAGS: tuple[tuple[str, str], ...] = (
        ("/models/",      "[model]"),       # Rails, Laravel, Express, Rust
        ("/controllers/", "[controller]"),  # Rails, Laravel, Express
        ("/schemas/",     "[model]"),       # FastAPI, Marshmallow, GraphQL
        ("/serializers/", "[model]"),       # Django REST, Rails
        ("/middleware/",  "[middleware]"),  # Go, Express, Gin, Fiber
        ("/policies/",    "[middleware]"),  # Pundit (Rails authorization)
        ("/composables/", "[hook]"),        # Vue 3 composables
        ("/widgets/",     "[component]"),   # Flutter widget directories
        ("/screens/",     "[container]"),   # Flutter/React Native screen dirs
        ("/pages/",       "[container]"),   # Next.js/SvelteKit page dirs
        ("/views/",       "[container]"),   # Vue Router views, Rails view dir
    )
    for dir_pattern, tag in _DIR_TAGS:
        bare = dir_pattern.lstrip("/")
        if dir_pattern in fpl or fpl.startswith(bare):
            return tag

    return ""


def _sanitize_cluster_filename(label: str) -> str:
    """Convert a cluster label to a safe filename (no .md extension)."""
    safe = re.sub(r"[^\w\-]", "_", label)
    safe = re.sub(r"_+", "_", safe).strip("_")
    return safe or "cluster"


def generate_graph_report(graph_json_path: Path, output_path: Path) -> None:
    """
    Read graph.json and write:
      GRAPH_REPORT.md       - compact index at output_path
      GRAPH_CLUSTERS/*.md   - one per cluster in output_path.parent/GRAPH_CLUSTERS/
    Reads cluster_descriptions.json from graph_json_path.parent if present.
    """
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
    edges: list[dict] = graph_data.get("links") or graph_data.get("edges", [])

    if not nodes:
        output_path.write_text(
            "# Project Graph Report\n\nNo nodes found in graph.\n",
            encoding="utf-8",
        )
        return

    # Load optional LLM cluster descriptions keyed by community id string
    descriptions: dict[str, str] = {}
    desc_path = graph_json_path.parent / "cluster_descriptions.json"
    if desc_path.exists():
        try:
            descriptions = json.loads(desc_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    # -----------------------------------------------------------------------
    # Step 1: Build node_id -> source_file mapping (skip nodes without one)
    # -----------------------------------------------------------------------
    _ARCHIVE_SUFFIXES = frozenset({".war", ".jar", ".ear", ".zip", ".tar"})

    def _is_archive_path(src: str) -> bool:
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

    file_degree: dict[str, int] = {}
    for nid, degree in node_degree.items():
        file_path = node_to_file.get(nid)
        if file_path:
            if file_path not in file_degree or degree > file_degree[file_path]:
                file_degree[file_path] = degree

    all_files: set[str] = set(node_to_file.values())
    for f in all_files:
        if f not in file_degree:
            file_degree[f] = 0

    # -----------------------------------------------------------------------
    # Step 3: Determine community assignments (3-priority system)
    # -----------------------------------------------------------------------
    communities_raw = graph_data.get("communities")
    node_community: dict[str, str] = {}

    if communities_raw and isinstance(communities_raw, dict):
        for comm_id, member_ids in communities_raw.items():
            for nid in member_ids:
                node_community[str(nid)] = str(comm_id)
    else:
        has_node_community = any(node.get("community") is not None for node in nodes)
        if has_node_community:
            for node in nodes:
                nid = node.get("id") or node.get("label") or ""
                comm = node.get("community")
                if nid and comm is not None:
                    node_community[nid] = str(comm)
        else:
            for node in nodes:
                nid = node.get("id") or node.get("label") or ""
                src = node.get("source_file")
                if nid and src:
                    parent = str(Path(src.replace("\\", "/")).parent)
                    node_community[nid] = parent

    # -----------------------------------------------------------------------
    # Step 4: Group source files by community
    # -----------------------------------------------------------------------
    file_community: dict[str, str] = {}
    for nid, src in node_to_file.items():
        comm = node_community.get(nid)
        if comm is not None:
            if src not in file_community:
                file_community[src] = comm

    for f in all_files:
        if f not in file_community:
            file_community[f] = "0"

    community_files: dict[str, list[str]] = defaultdict(list)
    for f, comm in file_community.items():
        community_files[comm].append(f)

    # Fallback: AST-only Louvain assigns each node its own community when there are no
    # cross-file edges, so every community ends up single-file. Re-derive using parent
    # directory so the report is still useful for navigation.
    if all(len(fs) < 2 for fs in community_files.values()):
        file_community = {}
        for f in all_files:
            parent = str(Path(f.replace("\\", "/")).parent)
            file_community[f] = parent
        community_files = defaultdict(list)
        for f, comm in file_community.items():
            community_files[comm].append(f)

    sorted_communities = sorted(community_files.items(), key=lambda x: -len(x[1]))

    def _community_label(comm_id: str, files: list[str], index: int) -> str:
        _SKIP_PARTS = frozenset({
            ".", "..", "src", "lib", "app", "main", "java", "kotlin",
            "com", "org", "net", "io", "resources", "webapp",
            # Java structural package directories - no semantic value as cluster names
            "services", "service", "dao", "impl", "model", "models",
            "controller", "controllers", "util", "utils", "helper", "helpers",
            "dto", "entity", "entities", "repository", "repositories",
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

            # Strategy 1: common prefix of file stems
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

            # Strategy 3: depth-weighted directory segments
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
        god_nodes = [(f, d) for f, d in file_degree.items() if d > threshold]
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
    # Step 7: Build deduplicated cluster filenames
    # -----------------------------------------------------------------------
    shown_communities = [(cid, fs) for cid, fs in sorted_communities if len(fs) >= 2]
    hidden_count = len(sorted_communities) - len(shown_communities)
    has_any_degree = any(d > 0 for d in file_degree.values())

    used_filenames: set[str] = set()  # lowercase for case-insensitive fs compatibility
    cluster_filenames: dict[str, str] = {}  # comm_id -> filename without .md
    for comm_id, _ in shown_communities:
        base = _sanitize_cluster_filename(comm_labels[comm_id])
        candidate = base
        counter = 2
        while candidate.lower() in used_filenames:
            candidate = f"{base}_{counter}"
            counter += 1
        used_filenames.add(candidate.lower())
        cluster_filenames[comm_id] = candidate

    # -----------------------------------------------------------------------
    # Step 8: Write GRAPH_CLUSTERS/<name>.md per-cluster files
    # -----------------------------------------------------------------------
    clusters_dir = output_path.parent / "GRAPH_CLUSTERS"
    clusters_dir.mkdir(parents=True, exist_ok=True)

    written_names: set[str] = set()

    for comm_id, files in shown_communities:
        filename = cluster_filenames[comm_id]
        label = comm_labels[comm_id]
        desc = descriptions.get(str(comm_id), "")
        cluster_path = clusters_dir / f"{filename}.md"

        sorted_files = sorted(files, key=lambda f: (-file_degree.get(f, 0), f))
        core_files = sorted_files[:10]

        clines: list[str] = [f"# Cluster: {label} ({len(files)} files)", ""]
        if desc:
            clines.append(f"> {desc}")
            clines.append("")

        clines.append("## Core files (read first)")
        for f in core_files:
            deg = file_degree.get(f, 0)
            tag = _role_tag(f)
            tag_str = f"  {tag}" if tag else ""
            deg_str = f"  degree:{deg}" if has_any_degree else ""
            clines.append(f"  - {f}{tag_str}{deg_str}")
        clines.append("")

        if len(files) > 10:
            clines.append(f"## All files ({len(files)} total)")
            for f in sorted_files:
                tag = _role_tag(f)
                tag_str = f"  {tag}" if tag else ""
                clines.append(f"  - {f}{tag_str}")
            clines.append("")

        cluster_path.write_text("\n".join(clines), encoding="utf-8")
        written_names.add(cluster_path.name)

    # Remove stale cluster files from previous builds (avoids shutil.rmtree TOCTOU).
    for stale in clusters_dir.glob("*.md"):
        if stale.name not in written_names:
            try:
                stale.unlink(missing_ok=True)
            except Exception:
                pass

    # -----------------------------------------------------------------------
    # Step 9: Write compact GRAPH_REPORT.md index
    # -----------------------------------------------------------------------
    has_descriptions = bool(descriptions)
    lines: list[str] = ["# Project Graph Report", ""]

    lines.append("## God Nodes (high connectivity - check these for cross-cutting concerns)")
    if god_nodes:
        for f, deg in god_nodes:
            lines.append(f"  - {f}  ({deg} connections)")
    else:
        lines.append("  (none identified)")
    lines.append("")

    lines.append("## Community Clusters")
    lines.append(f"Cluster detail files: `{clusters_dir}`")
    lines.append("")

    if has_descriptions:
        lines.append("| Cluster | Files | Top file | Description |")
        lines.append("|---------|-------|----------|-------------|")
    else:
        lines.append("| Cluster | Files | Top file |")
        lines.append("|---------|-------|----------|")

    for comm_id, files in shown_communities:
        label = comm_labels[comm_id]
        sorted_files = sorted(files, key=lambda f: (-file_degree.get(f, 0), f))
        top_file = sorted_files[0] if sorted_files else ""
        top_deg = file_degree.get(top_file, 0) if top_file else 0
        top_str = f"{top_file} [{top_deg}]" if has_any_degree and top_file else top_file
        desc = descriptions.get(str(comm_id), "")

        if has_descriptions:
            lines.append(f"| {label} | {len(files)} | {top_str} | {desc} |")
        else:
            lines.append(f"| {label} | {len(files)} | {top_str} |")

    lines.append("")
    if hidden_count > 0:
        lines.append(f"*({hidden_count} single-file module(s) not listed)*")
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
        "*Generated by ICX graph engine. Read GRAPH_CLUSTERS/<name>.md for full cluster file lists.*"
    )
    lines.append("")

    report = "\n".join(lines)
    output_path.write_text(report, encoding="utf-8")
