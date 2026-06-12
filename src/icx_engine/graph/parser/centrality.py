"""
Graph centrality computation for ICX knowledge graph.

Computes at build time (stored in graph.json as node attributes):
  pagerank          -- influence via link analysis (Power Iteration, 20 rounds)
  degree_centrality -- normalized in+out degree
  betweenness       -- approximate (BFS sample of min(50, n) source nodes)
  importance        -- weighted combination: 0.50*PR + 0.30*degree + 0.20*between

All pure Python, no networkx dependency. Scales to ~5000 nodes.
"""
from __future__ import annotations

import random
from collections import defaultdict

_BETWEENNESS_SAMPLE_SIZE = 50


def compute_centrality(nodes: list[dict], edges: list[dict]) -> dict[str, dict]:
    """Compute PageRank, degree_centrality, betweenness, importance for all nodes.

    Returns: {node_id: {"pagerank": float, "degree_centrality": float,
                         "betweenness": float, "importance": float}}
    """
    if not nodes:
        return {}
    if not edges:
        # No edges: every node is dangling - PageRank converges to uniform 1/n.
        valid_ids = [x["id"] for x in nodes if x.get("id")]
        if not valid_ids:
            return {}
        return {
            nid: {
                "pagerank": 1.0,
                "degree_centrality": 0.0,
                "betweenness": 0.0,
                "importance": round(0.50 * 1.0, 6),
            }
            for nid in valid_ids
        }

    ids = [n["id"] for n in nodes if n.get("id")]
    if not ids:
        return {}
    n = len(ids)
    idx = {nid: i for i, nid in enumerate(ids)}

    out_e: dict[int, list[int]] = defaultdict(list)
    in_e:  dict[int, list[int]] = defaultdict(list)
    for e in edges:
        si = idx.get(e.get("source", ""), -1)
        ti = idx.get(e.get("target", ""), -1)
        if si >= 0 and ti >= 0 and si != ti:
            out_e[si].append(ti)
            in_e[ti].append(si)

    # PageRank - Power Iteration
    # Dangling nodes (no outlinks) redistribute their rank evenly to all nodes.
    # Collect dangling_sum once per iteration: O(N) instead of O(D*N) per iteration.
    d = 0.85
    pr = [1.0 / n] * n
    for _ in range(20):
        dangling_sum = sum(pr[i] for i in range(n) if not out_e[i])
        base = (1.0 - d) / n + d * dangling_sum / n
        new_pr = [base] * n
        for i in range(n):
            if out_e[i]:
                contrib = d * pr[i] / len(out_e[i])
                for j in out_e[i]:
                    new_pr[j] += contrib
        pr = new_pr

    max_pr = max(pr) or 1.0
    pr_norm = [p / max_pr for p in pr]

    # Degree centrality
    max_deg = max((len(out_e[i]) + len(in_e[i]) for i in range(n)), default=1) or 1
    deg = [(len(out_e[i]) + len(in_e[i])) / max_deg for i in range(n)]

    # Approximate betweenness (BFS from sample of nodes)
    sample_size = min(_BETWEENNESS_SAMPLE_SIZE, n)
    sample = random.Random(42).sample(range(n), sample_size) if n > sample_size else list(range(n))
    btw = [0.0] * n
    for src in sample:
        dist = [-1] * n
        dist[src] = 0
        queue = [src]
        order: list[int] = []
        npaths = [0] * n
        npaths[src] = 1
        preds: dict[int, list[int]] = defaultdict(list)
        head = 0
        while head < len(queue):
            v = queue[head]; head += 1; order.append(v)
            for w in out_e[v]:
                if dist[w] < 0:
                    dist[w] = dist[v] + 1; queue.append(w)
                if dist[w] == dist[v] + 1:
                    npaths[w] += npaths[v]; preds[w].append(v)
        dep = [0.0] * n
        for w in reversed(order):
            for v in preds[w]:
                if npaths[w]:
                    dep[v] += (npaths[v] / npaths[w]) * (1.0 + dep[w])
            if w != src:
                btw[w] += dep[w]

    max_btw = max(btw) or 1.0
    btw_norm = [b / max_btw for b in btw]

    return {
        ids[i]: {
            "pagerank":           round(pr_norm[i], 6),
            "degree_centrality":  round(deg[i], 6),
            "betweenness":        round(btw_norm[i], 6),
            "importance":         round(
                0.50 * pr_norm[i] + 0.30 * deg[i] + 0.20 * btw_norm[i], 6
            ),
        }
        for i in range(n)
    }
