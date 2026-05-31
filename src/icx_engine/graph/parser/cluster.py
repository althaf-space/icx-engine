# Vendored from github.com/safishamsi/graphify @ 990ac706d823bf92275333433fde4ef4782a9139 (MIT License).
# Copyright (c) 2026 Safi Shamsi. Modified for icx-engine.
"""Community detection on NetworkX graphs. Uses Leiden (graspologic) if available, falls back to Louvain (networkx). Splits oversized communities. Returns cohesion scores."""
from __future__ import annotations
import contextlib
import ctypes
import inspect
import io
import sys
import threading
import networkx as nx


def _suppress_output():
    """Context manager to suppress stdout/stderr during library calls.

    graspologic's leiden() emits ANSI escape sequences (progress bars,
    colored warnings) that corrupt PowerShell 5.1's scroll buffer on
    Windows (see issue #19). Redirecting stdout/stderr to devnull during
    the call prevents this without losing any icx-graph output.
    """
    return contextlib.redirect_stdout(io.StringIO())


def _partition(G: nx.Graph, resolution: float = 1.0) -> dict[str, int]:
    """Run community detection. Returns {node_id: community_id}.

    Tries Leiden (graspologic) first - best quality.
    Falls back to Louvain (built into networkx) if graspologic is not installed.

    resolution > 1.0 → more, smaller communities.
    resolution < 1.0 → fewer, larger communities.

    Output from graspologic is suppressed to prevent ANSI escape codes
    from corrupting terminal scroll buffers on Windows PowerShell 5.1.
    """
    stable = nx.Graph()
    stable.add_nodes_from(sorted(G.nodes(), key=str))
    edge_rows = sorted(
        G.edges(data=True),
        key=lambda row: (str(row[0]), str(row[1])),
    )
    for src, tgt, attrs in edge_rows:
        stable.add_edge(src, tgt, **attrs)

    try:
        from graspologic.partition import leiden
        lsig = inspect.signature(leiden).parameters
        kwargs: dict = {}
        if "random_seed" in lsig:
            kwargs["random_seed"] = 42
        if "trials" in lsig:
            kwargs["trials"] = 1
        if "resolution" in lsig:
            kwargs["resolution"] = resolution
        # Suppress graspologic output to prevent ANSI escape codes from
        # corrupting PowerShell 5.1 scroll buffer (issue #19)
        old_stderr = sys.stderr
        try:
            sys.stderr = io.StringIO()
            with _suppress_output():
                result = leiden(stable, **kwargs)
        finally:
            sys.stderr = old_stderr
        return result
    except ImportError:
        pass

    # Fallback: networkx louvain (available since networkx 2.7).
    # threshold=1e-3 + max_level=5: aggressive stopping to prevent hours-long
    # hangs on large graphs with high-degree hub nodes (barrel index.js files
    # imported by hundreds of components cause louvain to thrash without limits).
    n = stable.number_of_nodes()
    if n > 10_000:
        threshold, max_level = 1e-2, 3
    elif n > 2_000:
        threshold, max_level = 1e-3, 5
    else:
        threshold, max_level = 1e-4, 10
    kwargs: dict = {"seed": 42, "threshold": threshold, "resolution": resolution}
    if "max_level" in inspect.signature(nx.community.louvain_communities).parameters:
        kwargs["max_level"] = max_level
    communities = nx.community.louvain_communities(stable, **kwargs)
    return {node: cid for cid, nodes in enumerate(communities) for node in nodes}


_MAX_COMMUNITY_FRACTION = 0.25   # communities larger than 25% of graph get split
_MIN_SPLIT_SIZE = 10             # only split if community has at least this many nodes
_COHESION_SPLIT_THRESHOLD = 0.05 # re-split communities with cohesion below this
_COHESION_SPLIT_MIN_SIZE = 50    # only cohesion-split if community has at least this many nodes

_DI_RESOLVER_TAGS: frozenset[str] = frozenset({
    "spring", "fastapi", "django", "nestjs", "angular", "celery", "flask",
    "jpa", "jaxrs", "kotlin_spring",
})


def _confidence_weight(confidence_score: float, resolver_tag: str) -> float:
    """Map edge confidence_score + resolver_tag to a Leiden edge weight.

    Range: [1.0, 4.5]. LSP edges top out near 3.9; DI-tagged edges get a 1.5x
    multiplier because framework injection edges are stronger semantic signal
    than raw import counts.
    """
    base = 1.0 + 2.0 * max(0.0, min(1.0, confidence_score))
    if resolver_tag in _DI_RESOLVER_TAGS:
        base *= 1.5
    return base


def _apply_confidence_weights(G: nx.Graph) -> nx.Graph:
    """Return a new graph with 'weight' set on every edge from confidence data.

    Does not mutate G. The returned graph has the same topology but weight
    attributes reflect confidence_score + resolver_tag via _confidence_weight.
    """
    H = G.copy()
    for u, v, data in H.edges(data=True):
        cs = data.get("confidence_score", 0.55)
        tag = data.get("confidence_source") or data.get("resolver_tag") or ""
        H[u][v]["weight"] = _confidence_weight(cs, tag)
    return H


_PARTITION_TIMEOUT = 90  # seconds before injecting SystemExit into stuck louvain thread


def _partition_safe(G: nx.Graph, resolution: float = 1.0) -> dict[str, int] | None:
    """Run _partition with a timeout. Returns None on timeout (caller falls back).

    networkx louvain has an unbounded inner `while nb_moves > 0:` loop that can
    run forever on graphs with oscillating community assignments (high-degree barrel
    files cycling between communities). PyThreadState_SetAsyncExc injects SystemExit
    between Python bytecodes to interrupt pure-Python code reliably.

    Python 3.14 note: threading.Event.wait() may raise KeyboardInterrupt during
    interpreter shutdown in subprocess contexts. All wait() calls are guarded.
    """
    result: list[dict[str, int] | None] = [None]
    error: list[BaseException | None] = [None]
    finished = threading.Event()

    def _run() -> None:
        try:
            result[0] = _partition(G, resolution)
        except BaseException as exc:
            error[0] = exc
        finally:
            finished.set()

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    try:
        finished.wait(_PARTITION_TIMEOUT)
    except (KeyboardInterrupt, SystemExit):
        return None

    if finished.is_set():
        if error[0] is not None and not isinstance(error[0], SystemExit):
            raise error[0]
        return result[0]

    # Timed out: inject SystemExit into the stuck thread
    tid = t.ident
    if tid is not None:
        ctypes.pythonapi.PyThreadState_SetAsyncExc(
            ctypes.c_ulong(tid), ctypes.py_object(SystemExit)
        )
    try:
        finished.wait(5)  # Give thread 5 s to acknowledge
    except (KeyboardInterrupt, SystemExit):
        pass
    return None  # Caller will use connected-components fallback


def cluster(
    G: nx.Graph,
    resolution: float = 1.0,
    exclude_hubs_percentile: float | None = None,
) -> dict[int, list[str]]:
    """Run Leiden community detection. Returns {community_id: [node_ids]}.

    Community IDs are stable across runs: 0 = largest community after splitting.
    Oversized communities (> 25% of graph nodes, min 10) are split by running
    a second Leiden pass on the subgraph.

    Accepts directed or undirected graphs. DiGraphs are converted to undirected
    internally since Louvain/Leiden require undirected input.

    resolution: passed to Leiden/Louvain. >1.0 = more smaller communities,
        <1.0 = fewer larger communities. Default 1.0.
    exclude_hubs_percentile: if set (0-100), nodes whose degree exceeds this
        percentile are excluded from partitioning and reattached to their
        majority-vote neighbour community afterwards. Useful for staging/utility
        super-hubs that inflate god-node rankings (#919).
    """
    if G.number_of_nodes() == 0:
        return {}
    if G.is_directed():
        G = G.to_undirected()
    if G.number_of_edges() == 0:
        return {i: [n] for i, n in enumerate(sorted(G.nodes))}

    # Compute hub exclusion set before removing anything so degree is based on full graph
    hub_nodes: set[str] = set()
    if exclude_hubs_percentile is not None:
        degrees = sorted(d for _, d in G.degree())
        if degrees:
            idx = max(0, int(len(degrees) * exclude_hubs_percentile / 100) - 1)
            threshold = degrees[idx]
            hub_nodes = {n for n, d in G.degree() if d > threshold}

    # Leiden warns and drops isolates - handle them separately
    # Also exclude hub nodes from partitioning so they don't pull unrelated
    # subsystems into the same community
    excluded = hub_nodes
    isolates = [n for n in G.nodes() if G.degree(n) == 0 and n not in excluded]
    connected_nodes = [n for n in G.nodes() if G.degree(n) > 0 and n not in excluded]
    connected = _apply_confidence_weights(G.subgraph(connected_nodes))

    raw: dict[int, list[str]] = {}
    if connected.number_of_nodes() > 0:
        partition = _partition_safe(connected, resolution=resolution)
        if partition is None:
            # Louvain timed out (oscillating assignments): fall back to connected components
            _log = __import__("logging").getLogger(__name__)
            _log.warning("community detection timed out after %ds; using connected-components fallback", _PARTITION_TIMEOUT)
            partition = {}
            for cid, component in enumerate(nx.connected_components(connected)):
                for node in component:
                    partition[node] = cid
        for node, cid in partition.items():
            raw.setdefault(cid, []).append(node)

    # Each isolate becomes its own single-node community
    next_cid = max(raw.keys(), default=-1) + 1
    for node in isolates:
        raw[next_cid] = [node]
        next_cid += 1

    # Reattach excluded hubs by majority-vote neighbour community
    if hub_nodes:
        node_community: dict[str, int] = {n: cid for cid, nodes in raw.items() for n in nodes}
        for hub in sorted(hub_nodes):
            votes: dict[int, int] = {}
            for nb in G.neighbors(hub):
                cid = node_community.get(nb)
                if cid is not None:
                    votes[cid] = votes.get(cid, 0) + 1
            if votes:
                best = min(votes, key=lambda c: (-votes[c], c))
                raw.setdefault(best, []).append(hub)
                node_community[hub] = best
            else:
                raw[next_cid] = [hub]
                node_community[hub] = next_cid
                next_cid += 1

    # Split oversized communities
    max_size = max(_MIN_SPLIT_SIZE, int(G.number_of_nodes() * _MAX_COMMUNITY_FRACTION))
    final_communities: list[list[str]] = []
    for nodes in raw.values():
        if len(nodes) > max_size:
            final_communities.extend(_split_community(G, nodes))
        else:
            final_communities.append(nodes)

    # Second pass: re-split low-cohesion communities caused by doc-hub nodes
    # that bridge otherwise-unrelated subsystems (e.g. CLAUDE.md connected to everything).
    second_pass: list[list[str]] = []
    for nodes in final_communities:
        if len(nodes) >= _COHESION_SPLIT_MIN_SIZE and cohesion_score(G, nodes) < _COHESION_SPLIT_THRESHOLD:
            splits = _split_community(G, nodes)
            second_pass.extend(splits if len(splits) > 1 else [nodes])
        else:
            second_pass.append(nodes)
    final_communities = second_pass

    # Re-index by size descending for deterministic ordering
    final_communities.sort(key=len, reverse=True)
    return {i: sorted(nodes) for i, nodes in enumerate(final_communities)}


def _split_community(G: nx.Graph, nodes: list[str]) -> list[list[str]]:
    """Run a second Leiden pass on a community subgraph to split it further."""
    subgraph = _apply_confidence_weights(G.subgraph(nodes))
    if subgraph.number_of_edges() == 0:
        # No edges - split into individual nodes
        return [[n] for n in sorted(nodes)]
    try:
        sub_partition = _partition_safe(subgraph)
        if sub_partition is None:
            return [sorted(nodes)]
        sub_communities: dict[int, list[str]] = {}
        for node, cid in sub_partition.items():
            sub_communities.setdefault(cid, []).append(node)
        if len(sub_communities) <= 1:
            return [sorted(nodes)]
        return [sorted(v) for v in sub_communities.values()]
    except Exception:
        return [sorted(nodes)]


def cohesion_score(G: nx.Graph, community_nodes: list[str]) -> float:
    """Ratio of actual intra-community edges to maximum possible."""
    n = len(community_nodes)
    if n <= 1:
        return 1.0
    subgraph = G.subgraph(community_nodes)
    actual = subgraph.number_of_edges()
    possible = n * (n - 1) / 2
    return actual / possible if possible > 0 else 0.0


def score_all(G: nx.Graph, communities: dict[int, list[str]]) -> dict[int, float]:
    return {cid: cohesion_score(G, nodes) for cid, nodes in communities.items()}


def classify_god_nodes(
    G: nx.Graph,
    structural_sigma: float = 2.0,
    di_sigma: float = 1.5,
) -> dict[str, list[str]]:
    """Identify god nodes in two tiers.

    structural: nodes with total degree > mean + structural_sigma * std.
                Usually utility files imported everywhere.
    di: nodes with DI-injection edge count > mean + di_sigma * std.
        Usually central services/repositories - higher priority for AI agents.

    Returns {"structural": [node_ids], "di": [node_ids]}.
    """
    _DI_RELATIONS = frozenset({"injects", "depends_on", "autowired", "provides"})

    all_nodes = list(G.nodes())
    if len(all_nodes) < 4:
        return {"structural": [], "di": []}

    degrees = {n: G.degree(n) for n in all_nodes}
    deg_vals = list(degrees.values())
    mean_d = sum(deg_vals) / len(deg_vals)
    std_d = (sum((d - mean_d) ** 2 for d in deg_vals) / len(deg_vals)) ** 0.5
    structural_threshold = mean_d + structural_sigma * std_d

    di_degrees: dict[str, int] = {n: 0 for n in all_nodes}
    for u, v, data in G.edges(data=True):
        rel = (data.get("relation") or "").lower()
        src = data.get("confidence_source") or data.get("resolver_tag") or ""
        if rel in _DI_RELATIONS or src in _DI_RESOLVER_TAGS:
            di_degrees[u] = di_degrees.get(u, 0) + 1
            di_degrees[v] = di_degrees.get(v, 0) + 1

    di_vals = list(di_degrees.values())
    mean_di = sum(di_vals) / max(len(di_vals), 1)
    std_di = (sum((d - mean_di) ** 2 for d in di_vals) / max(len(di_vals), 1)) ** 0.5
    di_threshold = mean_di + di_sigma * std_di

    structural_gods = sorted(
        [n for n, d in degrees.items() if d > structural_threshold],
        key=lambda n: -degrees[n],
    )
    di_gods = sorted(
        [n for n, d in di_degrees.items() if d > di_threshold and d > 0],
        key=lambda n: -di_degrees[n],
    )

    return {"structural": structural_gods, "di": di_gods}


def remap_communities_to_previous(
    communities: dict[int, list[str]],
    previous_node_community: dict[str, int],
) -> dict[int, list[str]]:
    """Remap community IDs to maximize overlap with a previous assignment.

    Uses greedy one-to-one matching by intersection size, then assigns fresh IDs
    to unmatched communities in deterministic order (size desc, lexical tie-break).
    """
    if not communities:
        return {}

    new_sets = {cid: set(nodes) for cid, nodes in communities.items()}
    old_sets: dict[int, set[str]] = {}
    for node, old_cid in previous_node_community.items():
        old_sets.setdefault(old_cid, set()).add(node)

    overlaps: list[tuple[int, int, int]] = []
    for old_cid, old_nodes in old_sets.items():
        for new_cid, new_nodes in new_sets.items():
            overlap = len(old_nodes & new_nodes)
            if overlap > 0:
                overlaps.append((overlap, old_cid, new_cid))
    overlaps.sort(key=lambda x: (-x[0], x[1], x[2]))

    new_to_final: dict[int, int] = {}
    used_old_ids: set[int] = set()
    matched_new_ids: set[int] = set()
    for _overlap, old_cid, new_cid in overlaps:
        if old_cid in used_old_ids or new_cid in matched_new_ids:
            continue
        new_to_final[new_cid] = old_cid
        used_old_ids.add(old_cid)
        matched_new_ids.add(new_cid)

    unmatched = [cid for cid in communities if cid not in matched_new_ids]
    unmatched.sort(key=lambda cid: (-len(communities[cid]), tuple(sorted(communities[cid]))))
    next_id = 0
    for new_cid in unmatched:
        while next_id in used_old_ids:
            next_id += 1
        new_to_final[new_cid] = next_id
        used_old_ids.add(next_id)
        next_id += 1

    remapped: dict[int, list[str]] = {}
    for new_cid, nodes in communities.items():
        remapped[new_to_final[new_cid]] = sorted(nodes)
    return dict(sorted(remapped.items(), key=lambda kv: kv[0]))
