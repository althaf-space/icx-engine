# Vendored from github.com/safishamsi/graphify @ 990ac706d823bf92275333433fde4ef4782a9139 (MIT License).
# Copyright (c) 2026 Safi Shamsi. Modified for icx-engine.
"""Entity deduplication pipeline for icx-graph knowledge graphs.

Pipeline: exact normalization -> entropy gate -> MinHash/LSH blocking ->
Jaro-Winkler verification -> same-community boost -> union-find merge.
"""
from __future__ import annotations
import logging
import math
import re
import unicodedata
from collections import defaultdict

from datasketch import MinHash, MinHashLSH
from rapidfuzz.distance import JaroWinkler

_log = logging.getLogger(__name__)


# -- helpers -------------------------------------------------------------------

def _norm(label: str | None) -> str:
    """Lowercase + collapse non-alphanumeric runs to space (Unicode-aware)."""
    if not label:
        return ""
    label = unicodedata.normalize("NFKC", label)
    return re.sub(r"[\W_]+", " ", label.casefold(), flags=re.UNICODE).strip()


def _entropy(label: str) -> float:
    """Shannon entropy in bits/char of the normalised label."""
    s = _norm(label)
    if not s:
        return 0.0
    freq: dict[str, int] = defaultdict(int)
    for ch in s:
        freq[ch] += 1
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in freq.values())


def _shingles(text: str, k: int = 3) -> set[str]:
    """Return k-gram character shingles of text."""
    if len(text) < k:
        return {text}
    return {text[i : i + k] for i in range(len(text) - k + 1)}


def _make_minhash(text: str, num_perm: int = 128) -> MinHash:
    # Strip spaces so "graph extractor" and "graphextractor" share shingles
    m = MinHash(num_perm=num_perm)
    for shingle in _shingles(text.replace(" ", "")):
        m.update(shingle.encode("utf-8"))
    return m


# Matches labels whose trailing token is a version/variant suffix:
# digits optionally followed by letters (chip SKUs: ASR1603, M1, Cortex-A55)
# or 2+ letters (codename revisions: cranelr vs cranel).
# Requires the stem to end in a letter so plain words don't accidentally match.
_VARIANT_SUFFIX = re.compile(r"^(.*[a-z])([0-9]+[a-z]*|[a-z]{2,})$")


def _is_variant_pair(a: str, b: str) -> bool:
    """True if a and b are sibling model/SKU variants (same stem, different suffix).

    Only applied to short labels (< 12 chars); long labels go through JW normally.
    """
    if a == b:
        return False
    if max(len(a), len(b)) >= 12:
        return False
    ma, mb = _VARIANT_SUFFIX.match(a), _VARIANT_SUFFIX.match(b)
    if not (ma and mb):
        return False
    return ma.group(1) == mb.group(1) and ma.group(2) != mb.group(2)


def _short_label_blocked(a: str, b: str, jw_score: float) -> bool:
    """Block fuzzy merge for short labels unless it's a same-length single-char substitution.

    Insertions/deletions on short strings (cranel/cranelr, M1/M1 Pro) produce
    high Jaro-Winkler scores due to the prefix bonus but are almost never true
    duplicates - they're abbreviations or variants.
    """
    if max(len(a), len(b)) >= 12:
        return False
    from rapidfuzz.distance import DamerauLevenshtein
    # Allow only same-length single-char substitutions (true typos like "Extractor"/"Extractar").
    # Block length-differing pairs regardless of score.
    if jw_score >= 97.0 and len(a) == len(b) and DamerauLevenshtein.distance(a, b) <= 1:
        return False
    return True


# -- union-find ----------------------------------------------------------------

class _UF:
    def __init__(self) -> None:
        self._parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        self._parent.setdefault(x, x)
        while self._parent[x] != x:
            self._parent[x] = self._parent[self._parent[x]]
            x = self._parent[x]
        return x

    def union(self, x: str, y: str) -> None:
        self._parent.setdefault(x, x)
        self._parent.setdefault(y, y)
        rx, ry = self.find(x), self.find(y)
        if rx != ry:
            self._parent[ry] = rx

    def components(self) -> dict[str, list[str]]:
        groups: dict[str, list[str]] = defaultdict(list)
        for x in self._parent:
            groups[self.find(x)].append(x)
        return dict(groups)


# -- constants -----------------------------------------------------------------

_ENTROPY_THRESHOLD = 2.5
_LSH_THRESHOLD = 0.7
_MERGE_THRESHOLD = 92.0     # rapidfuzz normalized_similarity * 100
_COMMUNITY_BOOST = 5.0      # score bonus when both nodes share community
_NUM_PERM = 128
_CHUNK_SUFFIX = re.compile(r"_c\d+$")


# -- main entry point ----------------------------------------------------------

def deduplicate_entities(
    nodes: list[dict],
    edges: list[dict],
    *,
    communities: dict[str, int],
    dedup_llm_backend: str | None = None,
) -> tuple[list[dict], list[dict]]:
    """Deduplicate near-identical entities in a knowledge graph.

    Args:
        nodes: list of node dicts with at minimum {"id": str, "label": str}
        edges: list of edge dicts with {"source": str, "target": str, ...}
        communities: mapping of node_id -> community_id (from cluster())
        dedup_llm_backend: if set, use LLM to resolve ambiguous pairs

    Returns:
        (deduped_nodes, deduped_edges) with edges rewired to survivors
    """
    # Guard: cross-project dedup is not supported - nodes from different repos
    # share label names by coincidence and must never be merged by string similarity.
    # If you need to dedup a global graph, run deduplicate_entities per-repo first.
    repos_seen = {n.get("repo") for n in nodes if n.get("repo")}
    if len(repos_seen) > 1:
        raise ValueError(
            f"deduplicate_entities: nodes span multiple repos {sorted(repos_seen)!r}. "
            f"Cross-project dedup is disabled - run dedup per-repo before merging."
        )

    if len(nodes) <= 1:
        return nodes, edges

    # Pre-deduplicate: keep first occurrence of each id
    seen_ids: dict[str, dict] = {}
    for node in nodes:
        nid = node.get("id", "")
        if nid and nid not in seen_ids:
            seen_ids[nid] = node
    unique_nodes = list(seen_ids.values())

    if len(unique_nodes) <= 1:
        return unique_nodes, edges

    # -- pass 1: exact normalization -------------------------------------------
    norm_to_nodes: dict[str, list[dict]] = defaultdict(list)
    for node in unique_nodes:
        key = _norm(node.get("label", node.get("id", "")))
        if key:
            norm_to_nodes[key].append(node)

    uf = _UF()
    exact_merges = 0
    for key, group in norm_to_nodes.items():
        if len(group) <= 1:
            continue
        # Partition by source_file - only merge within the same file in Pass 1.
        # Cross-file matches fall through to Pass 2 fuzzy matching.
        by_file: dict[str, list[dict]] = defaultdict(list)
        for node in group:
            sf = node.get("source_file") or ""
            by_file[sf].append(node)
        for file_group in by_file.values():
            if len(file_group) > 1:
                winner = _pick_winner(file_group)
                for node in file_group:
                    uf.union(winner["id"], node["id"])
                exact_merges += len(file_group) - 1

    # -- pass 2: MinHash/LSH + Jaro-Winkler (high-entropy nodes only) ---------
    candidates: list[dict] = []
    seen_norms: set[str] = set()
    for node in unique_nodes:
        key = _norm(node.get("label", node.get("id", "")))
        if key and key not in seen_norms:
            seen_norms.add(key)
            if _entropy(node.get("label", "")) >= _ENTROPY_THRESHOLD:
                candidates.append(node)

    fuzzy_merges = 0
    lsh: MinHashLSH | None = None
    minhashes: dict[str, MinHash] = {}
    if len(candidates) >= 2:
        lsh = MinHashLSH(threshold=_LSH_THRESHOLD, num_perm=_NUM_PERM)

        for node in candidates:
            norm_label = _norm(node.get("label", node.get("id", "")))
            m = _make_minhash(norm_label)
            minhashes[node["id"]] = m
            try:
                lsh.insert(node["id"], m)
            except ValueError:
                pass  # duplicate key in LSH - already inserted

        candidates_by_id = {n["id"]: n for n in candidates}
        for node in candidates:
            node_id = node["id"]
            norm_label = _norm(node.get("label", node.get("id", "")))
            neighbors = lsh.query(minhashes[node_id])

            for neighbor_id in neighbors:
                if neighbor_id == node_id:
                    continue
                if uf.find(node_id) == uf.find(neighbor_id):
                    continue

                neighbor = candidates_by_id.get(neighbor_id)
                if neighbor is None:
                    continue

                neighbor_norm = _norm(neighbor.get("label", neighbor.get("id", "")))
                score = JaroWinkler.normalized_similarity(norm_label, neighbor_norm) * 100

                if _is_variant_pair(norm_label, neighbor_norm):
                    continue
                if _short_label_blocked(norm_label, neighbor_norm, score):
                    continue

                c1 = communities.get(node_id)
                c2 = communities.get(neighbor_id)
                if (c1 is not None and c2 is not None and c1 == c2
                        and min(len(norm_label), len(neighbor_norm)) >= 12):
                    score += _COMMUNITY_BOOST

                if score >= _MERGE_THRESHOLD:
                    all_group = norm_to_nodes.get(norm_label, [node]) + \
                                norm_to_nodes.get(neighbor_norm, [neighbor])
                    winner = _pick_winner(all_group)
                    uf.union(winner["id"], node_id)
                    uf.union(winner["id"], neighbor_id)
                    fuzzy_merges += 1

    # -- pass 3: LLM tiebreaker for ambiguous pairs (opt-in) ------------------
    if dedup_llm_backend is not None:
        _llm_tiebreak(candidates, uf, communities, minhashes, lsh, backend=dedup_llm_backend)

    # -- build remap table from union-find components --------------------------
    components = uf.components()
    remap: dict[str, str] = {}

    for root, members in components.items():
        if len(members) == 1:
            continue
        group_nodes = [n for n in unique_nodes if n["id"] in members]
        winner = _pick_winner(group_nodes) if group_nodes else {"id": root}
        winner_id = winner["id"]
        for member in members:
            if member != winner_id:
                remap[member] = winner_id

    # -- apply remap -----------------------------------------------------------
    if not remap:
        return unique_nodes, edges

    total = len(remap)
    msg = f"[icx graph] Deduplicated {total} node(s)"
    if exact_merges:
        msg += f" ({exact_merges} exact"
        if fuzzy_merges:
            msg += f", {fuzzy_merges} fuzzy"
        msg += ")"
    _log.info("%s.", msg)

    deduped_nodes = [n for n in unique_nodes if n["id"] not in remap]
    deduped_edges = []
    for edge in edges:
        e = dict(edge)
        # Tolerate "from"/"to" keys from LLM backends that don't follow the
        # schema exactly - build_from_json normalises later but dedup runs
        # first so bracket access would KeyError here (#803).
        # Use explicit key presence check (not `or`) so empty-string src/tgt
        # aren't silently replaced by the fallback key.
        src = e["source"] if "source" in e else e.get("from")
        tgt = e["target"] if "target" in e else e.get("to")
        if src is None or tgt is None:
            continue
        e["source"] = remap.get(src, src)
        e["target"] = remap.get(tgt, tgt)
        # Remove legacy keys so they don't leak into edge attrs in graph.json.
        e.pop("from", None)
        e.pop("to", None)
        if e["source"] != e["target"]:
            deduped_edges.append(e)

    return deduped_nodes, deduped_edges


def _pick_winner(nodes: list[dict]) -> dict:
    """Pick the canonical survivor: prefer no chunk suffix, then shorter ID."""
    if not nodes:
        raise ValueError("Cannot pick winner from empty list")

    def _score(n: dict) -> tuple[int, int]:
        has_suffix = bool(_CHUNK_SUFFIX.search(n["id"]))
        return (1 if has_suffix else 0, len(n["id"]))

    return min(nodes, key=_score)


def _llm_tiebreak(
    candidates: list[dict],
    uf: _UF,
    communities: dict[str, int],
    minhashes: dict[str, MinHash],
    lsh: MinHashLSH | None,
    *,
    backend: str,
    batch_size: int = 30,
    low: float = 75.0,
    high: float = 92.0,
) -> None:
    """Batch-resolve ambiguous pairs (score in [low, high)) via LLM.

    Only LSH-neighbor pairs are considered (mirrors pass 2's blocking strategy),
    avoiding an O(n^2) cross-product over all candidates.
    """
    try:
        from icx_engine.graph.parser.llm import BACKENDS, format_backend_env_keys, get_backend_api_key
        if backend not in BACKENDS:
            _log.warning("[icx graph] --dedup-llm: unknown backend %r, skipping LLM tiebreaker.", backend)
            return
        if not get_backend_api_key(backend):
            env_keys = format_backend_env_keys(backend)
            _log.warning("[icx graph] --dedup-llm: %s not set, skipping LLM tiebreaker.", env_keys)
            return
    except ImportError:
        return

    if lsh is None or len(candidates) < 2:
        return

    candidates_by_id = {n["id"]: n for n in candidates}
    seen_pairs: set[tuple[str, str]] = set()
    ambiguous: list[tuple[dict, dict, float]] = []

    for node in candidates:
        node_id = node["id"]
        norm_i = _norm(node.get("label", node.get("id", "")))
        for neighbor_id in lsh.query(minhashes[node_id]):
            if neighbor_id == node_id:
                continue
            pair = (node_id, neighbor_id) if node_id < neighbor_id else (neighbor_id, node_id)
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)

            if uf.find(node_id) == uf.find(neighbor_id):
                continue

            neighbor = candidates_by_id.get(neighbor_id)
            if neighbor is None:
                continue

            norm_j = _norm(neighbor.get("label", neighbor.get("id", "")))
            score = JaroWinkler.normalized_similarity(norm_i, norm_j) * 100
            if _is_variant_pair(norm_i, norm_j):
                continue
            if _short_label_blocked(norm_i, norm_j, score):
                continue

            c1 = communities.get(node_id)
            c2 = communities.get(neighbor_id)
            if (c1 is not None and c2 is not None and c1 == c2
                    and min(len(norm_i), len(norm_j)) >= 12):
                score += _COMMUNITY_BOOST

            if low <= score < high:
                ambiguous.append((node, neighbor, score))

    if not ambiguous:
        return

    try:
        from icx_engine.graph.parser.llm import _call_llm
    except ImportError as exc:
        # F-038: previously this silent fallback hid the fact that `_call_llm`
        # didn't exist in `graphify.llm` at all, so `--dedup-llm` was a no-op.
        # Surface the import failure so future regressions are visible.
        _log.warning("[icx graph] --dedup-llm: cannot import _call_llm (%s); skipping LLM tiebreaker.", exc)
        return

    for batch_start in range(0, len(ambiguous), batch_size):
        batch = ambiguous[batch_start : batch_start + batch_size]
        pairs_text = "\n".join(
            f"{i+1}. \"{a['label']}\" vs \"{b['label']}\""
            for i, (a, b, _) in enumerate(batch)
        )
        prompt = (
            "For each pair below, answer only 'yes' or 'no': are they the same real-world concept?\n\n"
            f"{pairs_text}\n\n"
            "Reply with one line per pair: '1. yes', '2. no', etc."
        )
        try:
            response = _call_llm(prompt, backend=backend, max_tokens=200)
            lines = response.strip().splitlines()
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                parts = line.split(".", 1)
                if len(parts) != 2:
                    continue
                try:
                    idx = int(parts[0].strip()) - 1
                except ValueError:
                    continue
                if 0 <= idx < len(batch):
                    answer = parts[1].strip().lower()
                    if answer.startswith("yes"):
                        a, b, _ = batch[idx]
                        winner = _pick_winner([a, b])
                        uf.union(winner["id"], a["id"])
                        uf.union(winner["id"], b["id"])
        except Exception as exc:
            _log.warning("[icx graph] --dedup-llm batch failed: %s", exc)


# ---------------------------------------------------------------------------
# Edge fusion: multi-source confidence boosting
# ---------------------------------------------------------------------------

# Edges in the same FAMILY for the same (source_file, target_file) pair get fused.
# Edges in different families are NEVER fused - they represent different relationships.
_EDGE_FAMILIES: dict[str, str] = {
    # Structural import/reference family - fuse when multiple resolvers agree
    "scip_reference":       "import",
    "pyright_import":       "import",
    "jedi_import":          "import",
    "tsserver_import":      "import",
    "java_symbol_import":   "import",
    "java_symbol_call":     "import",
    "go_import":            "import",

    # Structural call family
    "go_calls":             "call",

    # Implementation family
    "go_implements":        "implements",
    "java_interface_impl":  "implements",
    "proto_implements":     "implements",

    # Framework edges - each is its OWN family, never fused with others
    "spring_bean":          "spring_bean",
    "spring_component":     "spring_component",
    "react_import":         "react_import",
    "django_url":           "django_url",
    "fastapi_route":        "fastapi_route",
    "flask_route":          "flask_route",
    "jsp_forward":          "jsp_forward",
    "jsp_include":          "jsp_include",
    "servlet_mapping":      "servlet_mapping",
    "el_binding":           "el_binding",
    "taglib_import":        "taglib_import",
    "rails_view":           "rails_view",
    "rails_route":          "rails_route",
    "rails_model_controller": "rails_model_controller",
    "rails_ar_usage":       "rails_ar_usage",
    "rails_concern":        "rails_concern",
    "rails_service":        "rails_service",
    "proto_generated":      "proto_generated",
    "proto_import":         "proto_import",
    "grpc_client":          "grpc_client",
    "tf_module":            "tf_module",
    "tf_var_ref":           "tf_var_ref",
    "tf_data_ref":          "tf_data_ref",
    "tf_resource_dep":      "tf_resource_dep",
    "tf_output":            "tf_output",

    # Event edges - each broker direction is its own family
    "kafka_publish":        "kafka_publish",
    "kafka_subscribe":      "kafka_subscribe",
    "rabbitmq_publish":     "rabbitmq_publish",
    "rabbitmq_subscribe":   "rabbitmq_subscribe",
    "redis_publish":        "redis_publish",
    "redis_subscribe":      "redis_subscribe",
    "sqs_publish":          "sqs_publish",
    "sqs_subscribe":        "sqs_subscribe",
    "sns_publish":          "sns_publish",
    "nats_publish":         "nats_publish",
    "nats_subscribe":       "nats_subscribe",
    "event_channel":        "event_channel",
    "openapi_impl":         "openapi_impl",
    "asyncapi_impl":        "asyncapi_impl",

    # Co-change - its own family, never fused with structural edges
    "co_changed":           "co_changed",
}

_FUSABLE_FAMILIES = frozenset({"import", "call", "implements"})


def _num_confidence(e: dict) -> float:
    """Numeric confidence for fusion math.

    `confidence` is numeric on most resolver edges, but some (e.g.
    java_symbols validation, LLM extraction) set it to a STRING enum
    ("EXTRACTED"/"INFERRED"/"AMBIGUOUS"). Comparing/summing that string against
    a float raises TypeError, aborting the whole build. Prefer the numeric
    `confidence`; when it is a string, fall back to the canonical
    `confidence_score` float; otherwise 0.0.
    """
    c = e.get("confidence")
    if isinstance(c, (int, float)):
        return float(c)
    cs = e.get("confidence_score")
    return float(cs) if isinstance(cs, (int, float)) else 0.0


def fuse_and_dedup(edges: list[dict]) -> list[dict]:
    """Deduplicate and fuse cross-resolver edges.

    FUSION RULE (import, call, implements families only):
      When multiple resolvers produce edges for the same (source, target) NODE
      pair in the same family, sum their confidence scores capped at 0.98.
      Store all contributing resolver names in resolver_sources[].
      Keep all other fields from the highest-confidence source edge.

    NO FUSION (all other families):
      Keep the highest-confidence edge per (source, target, type) NODE pair.

    Grouping is by NODE pair, not file pair: distinct node-level edges that
    share a (source_file, target_file) - e.g. three route handlers in main.py
    each depending on get_db in db.py, or several functions in A calling
    different methods in B - are genuinely different edges and must all survive.
    An earlier file-pair grouping collapsed them to one, silently dropping real
    call/DI/route/event edges (major recall loss on every framework project).
    True cross-resolver duplicates (identical node pair, multiple resolvers)
    still fuse, because they share the same node-pair key.

    0.98 cap: never exactly 1.0 - reserved for verified ground truth only.
    """
    # Group by (source_node, target_node, family)
    grouped: dict[tuple, list[dict]] = defaultdict(list)
    for edge in edges:
        if not isinstance(edge, dict):
            continue
        src = edge.get("source", "")
        tgt = edge.get("target", "")
        edge_type = edge.get("type", edge.get("relation", ""))
        family = _EDGE_FAMILIES.get(edge_type, edge_type)  # unknown type = own family
        grouped[(src, tgt, family)].append(edge)

    result = []
    for (src, tgt, family), group in grouped.items():
        if len(group) == 1:
            result.append(group[0])
            continue

        if family in _FUSABLE_FAMILIES:
            base = max(group, key=_num_confidence)
            total = sum(_num_confidence(e) for e in group)
            fused = dict(base)
            fused["confidence"] = round(min(0.98, total), 4)
            fused["resolver"] = "fused"
            fused["resolver_sources"] = sorted(
                {e.get("resolver", "unknown") for e in group}
            )
            result.append(fused)
        else:
            result.append(max(group, key=_num_confidence))

    return result
