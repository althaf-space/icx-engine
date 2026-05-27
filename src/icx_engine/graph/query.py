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
        return kw_score * deg_weight * conf_bonus

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
