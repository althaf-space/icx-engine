"""Graph query API for AI agents.

GraphQuerier loads graph.json once and exposes score-ranked retrieval:
  find_context(task)     - ranked file list by relevance score
  get_call_chain(node_id) - upstream callers + downstream callees
  get_impact(node_id)    - all dependents, by confidence tier
  get_subsystem(file_path) - cluster containing this file
"""
from __future__ import annotations

import json
import logging
import math
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path

_log = logging.getLogger(__name__)


@dataclass
class ContextResult:
    file: str
    node_id: str
    score: float
    role_tag: str
    degree: int
    reason: str


@dataclass
class ChainNode:
    node_id: str
    label: str
    file: str
    edge_type: str
    confidence: float
    resolver_tag: str
    depth: int


@dataclass
class CallChain:
    upstream: list[ChainNode] = field(default_factory=list)
    downstream: list[ChainNode] = field(default_factory=list)


@dataclass
class ImpactResult:
    direct: list[str] = field(default_factory=list)
    transitive: list[str] = field(default_factory=list)
    total: int = 0
    by_confidence: dict = field(default_factory=lambda: {"high": [], "medium": [], "low": []})


@dataclass
class SubsystemResult:
    cluster_label: str
    files: list[str]
    top_files: list[str]
    cross_cluster_files: list[str]


class GraphQuerier:
    """Read graph.json once, answer AI agent queries efficiently."""

    def __init__(self, graph_json_path: Path) -> None:
        self._path = Path(graph_json_path)
        data = json.loads(self._path.read_text(encoding="utf-8"))

        raw_nodes: list[dict] = data.get("nodes", [])
        raw_edges: list[dict] = data.get("links") or data.get("edges", [])

        self._nodes: dict[str, dict] = {
            n["id"]: n for n in raw_nodes if n.get("id")
        }
        self._out_edges: dict[str, list[dict]] = defaultdict(list)
        self._in_edges: dict[str, list[dict]] = defaultdict(list)
        for e in raw_edges:
            src, tgt = e.get("source"), e.get("target")
            if src and tgt:
                self._out_edges[src].append(e)
                self._in_edges[tgt].append(e)

        self._file_nodes: dict[str, list[str]] = defaultdict(list)
        for nid, nd in self._nodes.items():
            src = nd.get("source_file")
            if src:
                self._file_nodes[src].append(nid)

        communities_raw = data.get("communities", {})
        self._node_community: dict[str, str] = {}
        if isinstance(communities_raw, dict):
            for cid, members in communities_raw.items():
                for m in members:
                    self._node_community[str(m)] = str(cid)
        else:
            for nd in raw_nodes:
                nid = nd.get("id")
                comm = nd.get("community")
                if nid and comm is not None:
                    self._node_community[nid] = str(comm)

        self._community_files: dict[str, list[str]] = defaultdict(list)
        for nid, nd in self._nodes.items():
            src = nd.get("source_file")
            comm = self._node_community.get(nid)
            if src and comm is not None and src not in self._community_files[comm]:
                self._community_files[comm].append(src)

        self._degree: dict[str, int] = defaultdict(int)
        for e in raw_edges:
            src, tgt = e.get("source"), e.get("target")
            if src:
                self._degree[src] += 1
            if tgt:
                self._degree[tgt] += 1

    @property
    def node_count(self) -> int:
        return len(self._nodes)

    @property
    def edge_count(self) -> int:
        return sum(len(v) for v in self._out_edges.values())

    @staticmethod
    def _edge_confidence(e: dict) -> float:
        """Numeric edge confidence in [0,1].

        `confidence_score` is the canonical float (export guarantees it on every
        edge). `confidence` is a STRING enum in a real graph.json
        ("EXTRACTED"/"INFERRED"/"AMBIGUOUS", set by build), so it must never be
        compared numerically. Test fixtures sometimes carry only a float
        `confidence`; accept that as a fallback but ignore the string form.
        """
        cs = e.get("confidence_score")
        if isinstance(cs, (int, float)):
            return float(cs)
        c = e.get("confidence")
        return float(c) if isinstance(c, (int, float)) else 0.0

    def _max_edge_confidence(self, node_id: str) -> float:
        best = 0.0
        for e in self._out_edges.get(node_id, []) + self._in_edges.get(node_id, []):
            cs = e.get("confidence_score", 0.0)
            if cs > best:
                best = cs
        return best

    def _score_node(self, node_id: str, terms: list[str]) -> float:
        nd = self._nodes.get(node_id, {})
        label = (nd.get("label") or "").lower()
        src = (nd.get("source_file") or "").lower()
        rtag = (nd.get("role_tag") or "").lower()
        text = f"{label} {src} {rtag}"
        matched = sum(1 for t in terms if t in text)
        kw_score = matched / max(len(terms), 1)
        deg_weight = math.log(1 + self._degree.get(node_id, 0))
        conf_bonus = 0.5 + 0.5 * self._max_edge_confidence(node_id)
        base = kw_score * deg_weight * conf_bonus
        importance = nd.get("importance", 0.0)
        # Importance boosts the base score proportionally; when base is 0 but
        # keyword matched, add a small importance-only contribution so high-
        # centrality isolated nodes (no edges yet) still rank above low-centrality.
        if base > 0:
            return base * (1.0 + 0.2 * importance)
        return kw_score * 0.1 * importance

    def find_context(
        self,
        task: str,
        token_budget: int | None = None,
        min_confidence: float = 0.0,
        source_root: Path | None = None,
    ) -> list[ContextResult]:
        """Return all files ranked by relevance to task, sorted by score descending.

        token_budget, min_confidence, and source_root are accepted for backward
        compatibility but unused. Results are all scored files; agent reads in
        score order.
        """
        terms = [t.lower() for t in task.split() if t]
        if not terms:
            return []

        scored: list[tuple[float, str]] = []
        for nid in self._nodes:
            s = self._score_node(nid, terms)
            if s > 0:
                scored.append((s, nid))
        scored.sort(key=lambda x: -x[0])

        best_per_file: dict[str, tuple[float, str]] = {}
        for score, nid in scored:
            nd = self._nodes[nid]
            src = nd.get("source_file")
            if not src:
                continue
            if src not in best_per_file or score > best_per_file[src][0]:
                best_per_file[src] = (score, nid)

        file_ranked = sorted(best_per_file.items(), key=lambda kv: -kv[1][0])

        results: list[ContextResult] = []
        for src, (score, nid) in file_ranked:
            nd = self._nodes[nid]
            matched_terms = [t for t in terms if t in f"{nd.get('label', '')} {src}".lower()]
            conf = self._max_edge_confidence(nid)
            reason = (
                f"matched {'+'.join(repr(t) for t in matched_terms)} "
                f"in {nd.get('label', nid)} "
                f"{nd.get('role_tag', '')} "
                f"degree:{self._degree.get(nid, 0)} "
                f"conf:{conf:.2f}"
            ).strip()
            results.append(ContextResult(
                file=src,
                node_id=nid,
                score=round(score, 4),
                role_tag=nd.get("role_tag", ""),
                degree=self._degree.get(nid, 0),
                reason=reason,
            ))

        return results

    def get_call_chain(
        self,
        node_id: str,
        depth: int = 3,
        min_confidence: float = 0.5,
    ) -> CallChain:
        """BFS outward (downstream) and inward (upstream) from node_id."""
        def _bfs(start: str, edge_map: dict, going_out: bool) -> list[ChainNode]:
            visited: set[str] = {start}
            queue: deque[tuple[str, int]] = deque([(start, 0)])
            result: list[ChainNode] = []
            while queue:
                current, d = queue.popleft()
                if d >= depth:
                    continue
                for e in edge_map.get(current, []):
                    cs = e.get("confidence_score", 0.0)
                    if cs < min_confidence:
                        continue
                    next_id = e.get("target") if going_out else e.get("source")
                    if not next_id or next_id in visited:
                        continue
                    visited.add(next_id)
                    nd = self._nodes.get(next_id, {})
                    result.append(ChainNode(
                        node_id=next_id,
                        label=nd.get("label", next_id),
                        file=nd.get("source_file", ""),
                        edge_type=e.get("relation", ""),
                        confidence=cs,
                        resolver_tag=e.get("resolver_tag", e.get("confidence_source", "")),
                        depth=d + 1,
                    ))
                    queue.append((next_id, d + 1))
            return result

        return CallChain(
            upstream=_bfs(node_id, self._in_edges, going_out=False),
            downstream=_bfs(node_id, self._out_edges, going_out=True),
        )

    def get_impact(
        self,
        node_id: str,
        min_confidence: float = 0.5,
    ) -> ImpactResult:
        """Who depends on node_id? Reverse BFS through incoming edges."""
        direct: list[str] = []
        transitive: list[str] = []
        by_conf: dict[str, list[str]] = {"high": [], "medium": [], "low": []}

        visited: set[str] = {node_id}
        queue: deque[tuple[str, int]] = deque([(node_id, 0)])
        while queue:
            current, d = queue.popleft()
            for e in self._in_edges.get(current, []):
                cs = e.get("confidence_score", 0.0)
                if cs < min_confidence:
                    continue
                src = e.get("source")
                if not src or src in visited:
                    continue
                visited.add(src)
                if d == 0:
                    direct.append(src)
                else:
                    transitive.append(src)
                tier = "high" if cs >= 0.85 else ("medium" if cs >= 0.60 else "low")
                by_conf[tier].append(src)
                queue.append((src, d + 1))

        return ImpactResult(
            direct=direct,
            transitive=transitive,
            total=len(direct) + len(transitive),
            by_confidence=by_conf,
        )

    def get_subsystem(self, file_path: str) -> SubsystemResult:
        """Return the cluster containing file_path and all files in that cluster."""
        file_path = file_path.replace("\\", "/")
        node_ids = self._file_nodes.get(file_path, [])
        comm: str | None = None
        for nid in node_ids:
            comm = self._node_community.get(nid)
            if comm is not None:
                break

        if comm is None:
            return SubsystemResult(
                cluster_label="unknown",
                files=[file_path],
                top_files=[file_path],
                cross_cluster_files=[],
            )

        cluster_files = list(self._community_files.get(comm, [file_path]))

        def _file_max_degree(f: str) -> int:
            return max((self._degree.get(nid, 0) for nid in self._file_nodes.get(f, [])), default=0)

        sorted_files = sorted(cluster_files, key=lambda f: -_file_max_degree(f))
        top_files = sorted_files[:10]

        cross_set: set[str] = set()
        for f in cluster_files:
            for nid in self._file_nodes.get(f, []):
                for e in self._out_edges.get(nid, []) + self._in_edges.get(nid, []):
                    other_id = e.get("target") if e.get("source") == nid else e.get("source")
                    if not other_id:
                        continue
                    other_comm = self._node_community.get(other_id)
                    if other_comm and other_comm != comm:
                        nd = self._nodes.get(other_id, {})
                        other_file = nd.get("source_file")
                        if other_file:
                            cross_set.add(other_file)

        return SubsystemResult(
            cluster_label=comm,
            files=cluster_files,
            top_files=top_files,
            cross_cluster_files=sorted(cross_set),
        )

    def get_cochange_partners(self, file_path: str) -> list[dict]:
        """Return files that co-change with file_path, sorted by strength descending."""
        file_path = file_path.replace("\\", "/")
        result = []
        all_edges = []
        for nid in self._file_nodes.get(file_path, []):
            all_edges.extend(self._out_edges.get(nid, []))
        for e in all_edges:
            if e.get("type") == "co_changed":
                tgt_file = e.get("target_file", "")
                result.append({
                    "file": tgt_file,
                    "strength": e.get("co_change_strength", 0.0),
                    "co_occurrences": e.get("co_occurrences", 0),
                })
        return sorted(result, key=lambda x: x["strength"], reverse=True)

    def get_blast_radius(
        self,
        changed_files: list[str],
        max_depth: int = 5,
        min_confidence: float = 0.3,
    ) -> dict:
        """Compute blast radius for a set of changed files.

        Returns:
            changed_files: the input files
            direct_dependents: files with direct edges to/from changed files
            transitive_dependents: all files reachable within max_depth hops
            risk_score: 0.0-1.0 (fraction of important nodes in blast radius)
            missing_changes: files that co-change with affected files but not in changed_files
            total_affected: total unique affected file count
        """
        changed_set = {f.replace("\\", "/") for f in changed_files}

        # Collect all node IDs for changed files
        changed_node_ids: set[str] = set()
        for f in changed_set:
            for nid in self._file_nodes.get(f, []):
                changed_node_ids.add(nid)

        # BFS for dependents (files that depend on changed files)
        direct_files: set[str] = set()
        transitive_files: set[str] = set()
        visited: set[str] = set(changed_node_ids)
        frontier: set[str] = set(changed_node_ids)

        for depth in range(max_depth):
            next_frontier: set[str] = set()
            for nid in frontier:
                # Incoming edges: files that use/import this node
                for e in self._in_edges.get(nid, []):
                    conf = self._edge_confidence(e)
                    if conf < min_confidence:
                        continue
                    src = e.get("source")
                    if src and src not in visited:
                        visited.add(src)
                        next_frontier.add(src)
                        nd = self._nodes.get(src, {})
                        dep_file = nd.get("source_file", "")
                        if dep_file and dep_file not in changed_set:
                            if depth == 0:
                                direct_files.add(dep_file)
                            else:
                                transitive_files.add(dep_file)
                # Outgoing edges: files this node uses (could be changed by ripple)
                for e in self._out_edges.get(nid, []):
                    conf = self._edge_confidence(e)
                    if conf < min_confidence:
                        continue
                    tgt = e.get("target")
                    if tgt and tgt not in visited:
                        visited.add(tgt)
                        next_frontier.add(tgt)
                        nd = self._nodes.get(tgt, {})
                        dep_file = nd.get("source_file", "")
                        if dep_file and dep_file not in changed_set:
                            if depth == 0:
                                direct_files.add(dep_file)
                            else:
                                transitive_files.add(dep_file)
            if not next_frontier:
                break
            frontier = next_frontier

        all_affected = direct_files | transitive_files

        # Risk score: fraction of high-importance nodes in blast radius
        important_total = sum(1 for n in self._nodes.values() if n.get("importance", 0) > 0.5)
        if important_total > 0:
            important_affected = sum(
                1 for nid in visited
                if self._nodes.get(nid, {}).get("importance", 0) > 0.5
                and self._nodes.get(nid, {}).get("source_file", "") not in changed_set
            )
            risk_score = round(min(1.0, important_affected / important_total), 4)
        else:
            # Fallback: use proportion of total nodes affected
            risk_score = round(min(1.0, len(all_affected) / max(len(self._nodes), 1)), 4)

        # Missing changes: files that co-change with any affected file but not in changed_set
        missing: set[str] = set()
        for f in changed_set | all_affected:
            for partner_data in self.get_cochange_partners(f):
                partner_file = partner_data.get("file", "")
                if partner_file and partner_file not in changed_set:
                    missing.add(partner_file)

        return {
            "changed_files": list(changed_set),
            "direct_dependents": sorted(direct_files),
            "transitive_dependents": sorted(transitive_files - direct_files),
            "risk_score": risk_score,
            "missing_changes": sorted(missing),
            "total_affected": len(all_affected),
        }

    def get_important_nodes(self, top_k: int = 10) -> list[dict]:
        """Return top-k nodes by importance score (PageRank + betweenness). Added in Phase 11."""
        nodes_with_importance = [
            n for n in self._nodes.values() if n.get("importance", 0.0) > 0
        ]
        return sorted(nodes_with_importance,
                      key=lambda n: n.get("importance", 0.0), reverse=True)[:top_k]

    def get_cycles(self, max_cycles: int = 20) -> list[list[str]]:
        """Detect circular dependency chains using DFS.

        Returns a list of cycles where each cycle is a list of file paths.
        Each cycle is the shortest simple cycle found. Limited to max_cycles results.
        """
        # Build file-level dependency graph (ignore co_changed and event edges)
        _SKIP_TYPES = frozenset({
            "co_changed", "kafka_publish", "kafka_subscribe",
            "rabbitmq_publish", "rabbitmq_subscribe", "redis_publish", "redis_subscribe",
            "sqs_publish", "sqs_subscribe", "sns_publish", "nats_publish", "nats_subscribe",
            "event_channel", "openapi_impl", "asyncapi_impl",
        })
        file_deps: dict[str, set[str]] = defaultdict(set)
        for nid, edges in self._out_edges.items():
            src_nd = self._nodes.get(nid, {})
            src_file = src_nd.get("source_file", "")
            if not src_file:
                continue
            for e in edges:
                if e.get("type", "") in _SKIP_TYPES:
                    continue
                tgt_nd = self._nodes.get(e.get("target", ""), {})
                tgt_file = tgt_nd.get("source_file", "")
                if tgt_file and tgt_file != src_file:
                    file_deps[src_file].add(tgt_file)

        # DFS cycle detection (Johnson's algorithm simplified)
        cycles: list[list[str]] = []
        visited: set[str] = set()
        in_stack: set[str] = set()
        seen_cycles: set[frozenset] = set()

        for start in list(file_deps.keys()):
            if start in visited or len(cycles) >= max_cycles:
                continue
            path_stack: list[str] = [start]
            work: list[tuple] = [(start, iter(file_deps.get(start, set())))]
            visited.add(start)
            in_stack.add(start)
            while work and len(cycles) < max_cycles:
                node, nbr_iter = work[-1]
                try:
                    neighbor = next(nbr_iter)
                    if len(cycles) >= max_cycles:
                        break
                    if neighbor in in_stack:
                        try:
                            idx = path_stack.index(neighbor)
                            cycle = path_stack[idx:]
                        except ValueError:
                            continue
                        key = frozenset(cycle)
                        if key not in seen_cycles:
                            seen_cycles.add(key)
                            cycles.append(cycle + [neighbor])
                    elif neighbor not in visited:
                        visited.add(neighbor)
                        in_stack.add(neighbor)
                        path_stack.append(neighbor)
                        work.append((neighbor, iter(file_deps.get(neighbor, set()))))
                except StopIteration:
                    work.pop()
                    path_stack.pop()
                    in_stack.discard(node)

        return cycles

    def get_dead_code(self) -> list[dict]:
        """Detect files with zero incoming edges (potential dead code).

        Excludes known entry points (main.py, app.py, server.py, index.*, test files, etc.)
        Returns list of {"file": str, "node_count": int} dicts.
        """
        _ENTRY_POINT_PATTERNS = frozenset({
            "main.py", "app.py", "server.py", "application.py",
            "main.ts", "app.ts", "server.ts", "index.ts", "index.js",
            "main.go", "server.go",
            "App.java", "Application.java",
            "__main__.py",
        })
        _ENTRY_POINT_SUFFIXES = ("_test.go", "test.py", "_spec.rb")
        _ENTRY_POINT_PREFIXES = ("test_", "spec_")

        def _is_entry_point(filepath: str) -> bool:
            name = Path(filepath).name
            if name in _ENTRY_POINT_PATTERNS:
                return True
            if any(name.endswith(s) for s in _ENTRY_POINT_SUFFIXES):
                return True
            if any(name.startswith(p) for p in _ENTRY_POINT_PREFIXES):
                return True
            if name.startswith("conftest"):
                return True
            return False

        # Build file-level incoming edge count
        file_incoming: dict[str, int] = defaultdict(int)
        for nid, edges in self._in_edges.items():
            nd = self._nodes.get(nid, {})
            tgt_file = nd.get("source_file", "")
            if tgt_file:
                file_incoming[tgt_file] += len(edges)

        # Files that appear in the graph but have zero incoming edges
        all_files: set[str] = set()
        file_node_count: dict[str, int] = defaultdict(int)
        for nid, nd in self._nodes.items():
            sf = nd.get("source_file", "")
            if sf:
                all_files.add(sf)
                file_node_count[sf] += 1

        dead = []
        for f in all_files:
            if file_incoming.get(f, 0) == 0 and not _is_entry_point(f):
                dead.append({"file": f, "node_count": file_node_count[f]})

        return sorted(dead, key=lambda x: x["file"])

    def get_ownership(
        self,
        file_path: str,
        project_path: str,
    ) -> dict:
        """Return ownership information for a file path.

        Loads CODEOWNERS from the project and returns:
          owners: list of @owner strings for this file
          owned_files: all files in the graph owned by the same owners
          cross_owner_dependencies: edges crossing ownership boundaries
        """
        from icx_engine.graph.parser.ownership import load_codeowners, find_owners

        file_path = file_path.replace("\\", "/")
        rules = load_codeowners(project_path)
        if not rules:
            return {"owners": [], "owned_files": [], "cross_owner_dependencies": [], "codeowners_found": False}

        owners = find_owners(file_path, rules)

        # Find all files in the graph owned by the same set of owners
        all_files: set[str] = set()
        for nid, nd in self._nodes.items():
            sf = nd.get("source_file", "")
            if sf:
                all_files.add(sf.replace("\\", "/"))

        same_owner_files = [
            f for f in all_files
            if set(find_owners(f, rules)) & set(owners)
        ] if owners else []

        # Find cross-owner dependencies: edges from owned files to files with different owners
        cross_owner = []
        for f in same_owner_files:
            for nid in self._file_nodes.get(f, []):
                for e in self._out_edges.get(nid, []):
                    tgt_nd = self._nodes.get(e.get("target", ""), {})
                    tgt_file = tgt_nd.get("source_file", "").replace("\\", "/")
                    if not tgt_file or tgt_file in same_owner_files:
                        continue
                    tgt_owners = find_owners(tgt_file, rules)
                    if set(tgt_owners) - set(owners):
                        cross_owner.append({
                            "from": f,
                            "to": tgt_file,
                            "to_owners": tgt_owners,
                            "edge_type": e.get("type", ""),
                            "confidence": e.get("confidence", 0.0),
                        })

        return {
            "owners": owners,
            "owned_files": sorted(same_owner_files),
            "cross_owner_dependencies": cross_owner,
            "codeowners_found": True,
        }
