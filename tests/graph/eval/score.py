"""
Graph extractor evaluation scorer.

Builds a graph over a fixture project, compares the extracted edges against
hand-annotated ground truth, and reports precision/recall per edge kind.

Usage:
    python -m tests.graph.eval.score <fixture_name>

Example:
    python -m tests.graph.eval.score fastapi_sample

Normalization
-------------
The parser emits its own node ID conventions (e.g. `app_main_list_users`).
Ground-truth files express edges in the more human form
`relative_path:symbol`. The scorer builds a node_id -> (rel_path, symbol_lc)
lookup from the produced graph, then converts every predicted edge into that
same `rel_path:symbol_lc` form before comparison. Ground-truth IDs are also
lowercased so the match is case-insensitive on symbol names.

Edge-kind mapping
-----------------
Parser relation names are translated to the ground-truth taxonomy:
    contains, method     -> intra-file structural; ignored for scoring
    calls                -> call
    inherits             -> inherit
    uses                 -> reference
    imports              -> import   (currently never emitted; gap)
The `di` and `route` kinds are produced by future resolvers (FastAPI, Spring,
etc.) and only emitted once those resolvers ship.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

EVAL_ROOT = Path(__file__).parent
FIXTURES_DIR = EVAL_ROOT / "fixtures"
GROUND_TRUTH_DIR = EVAL_ROOT / "ground_truth"

# Parser relation -> ground-truth kind. Relations not in this map are
# treated as intra-file structural noise and dropped before scoring.
_RELATION_KIND_MAP: dict[str, str] = {
    "calls": "call",
    "inherits": "inherit",
    "uses": "reference",
    "imports": "import",
    "depends_on": "di",
    "routes": "route",
    "has_relation": "relation",
    "dao": "dao",
    "implements": "inherit",
    "renders": "renders",
    "listens": "listens",
    "scheduled": "scheduled",
    "queries": "queries",
    "module_depends": "module_depends",
    "provides": "provides",
    "advises": "advises",
    "calls_service": "calls_service",
    "calls_type": "calls_type",
}

# Relations the scorer always discards because ground-truth never asserts them.
_DROPPED_RELATIONS: frozenset[str] = frozenset({
    "contains", "method", "defines", "exports",
})


@dataclass(frozen=True)
class Edge:
    source: str
    target: str
    kind: str


def _norm_path(s: str) -> str:
    return s.replace("\\", "/").strip()


def _split_symbol(s: str) -> tuple[str, str | None]:
    """Split 'path/file.py:Symbol.method' into ('path/file.py', 'symbol.method' lowercased)."""
    s = _norm_path(s)
    if ":" in s:
        path, sym = s.split(":", 1)
        return path, sym.strip().lower()
    return s, None


def _ground_truth_to_edge(d: dict) -> Edge:
    src_path, src_sym = _split_symbol(d["source"])
    tgt_path, tgt_sym = _split_symbol(d["target"])
    src = f"{src_path}:{src_sym}" if src_sym else src_path
    tgt = f"{tgt_path}:{tgt_sym}" if tgt_sym else tgt_path
    kind = str(d.get("kind", "")).lower().strip() or "reference"
    return Edge(source=src, target=tgt, kind=kind)


def load_ground_truth(name: str) -> list[Edge]:
    path = GROUND_TRUTH_DIR / f"{name}.json"
    if not path.exists():
        raise FileNotFoundError(f"Ground truth file not found: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    return [_ground_truth_to_edge(e) for e in data.get("edges", [])]


def build_fixture_graph(fixture_dir: Path) -> tuple[list[Edge], dict]:
    """
    Build a graph over the fixture directory using the same pipeline as
    `icx graph build`, then convert the result into the normalized Edge form.

    Returns (edges, raw_graph_json).
    """
    from icx_engine.graph.builder import _build_project_isolated

    fixture_dir = fixture_dir.resolve()

    with tempfile.TemporaryDirectory(prefix="icx-eval-", ignore_cleanup_errors=True) as tmp:
        tmp_path = Path(tmp)
        graph_tmp = tmp_path / "graph.json.tmp"
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)

        result = _build_project_isolated(
            project_path_str=str(fixture_dir),
            graph_tmp_path_str=str(graph_tmp),
            icx_cache_path_str=str(cache_dir),
            llm_backend=None,
            llm_api_key=None,
            llm_base_url=None,
        )

        if result.get("error"):
            raise RuntimeError(f"Graph build failed: {result['error']}")
        if not graph_tmp.exists():
            raise RuntimeError("Graph build produced no output file.")
        graph = json.loads(graph_tmp.read_text(encoding="utf-8"))

    nodes_raw = graph.get("nodes", [])
    edges_raw = graph.get("links") or graph.get("edges") or []

    node_to_rel: dict[str, tuple[str, str | None]] = {}
    fixture_str = str(fixture_dir).replace("\\", "/")
    for n in nodes_raw:
        nid = n.get("id") or n.get("label") or ""
        if not nid:
            continue
        src_file = _norm_path(n.get("source_file") or "")
        label = (n.get("label") or "").strip()
        symbol_lc = _label_to_symbol(label)

        if src_file:
            if src_file.startswith(fixture_str + "/"):
                rel = src_file[len(fixture_str) + 1 :]
            elif src_file.startswith(fixture_str):
                rel = src_file[len(fixture_str) :].lstrip("/")
            else:
                rel = src_file
            # If the node represents the file itself (label is the
            # filename), drop the redundant `:filename` suffix so the
            # address normalizes to bare `app/main.py`.
            fname_lc = Path(rel).name.lower()
            if symbol_lc and (
                fname_lc == symbol_lc
                or (
                    "/" in symbol_lc
                    and (
                        symbol_lc.endswith("/" + fname_lc)
                        or symbol_lc.endswith("/" + fname_lc.rsplit(".", 1)[0])
                    )
                )
            ):
                symbol_lc = None
            node_to_rel[nid] = (rel, symbol_lc)
        else:
            # External / unresolved node: keep no path, symbol only.
            node_to_rel[nid] = ("", symbol_lc)

    edges_out: list[Edge] = []
    for e in edges_raw:
        rel = str(e.get("relation") or e.get("kind") or "").lower().strip()
        if rel in _DROPPED_RELATIONS:
            continue
        kind = _RELATION_KIND_MAP.get(rel, rel)
        src_id = e.get("source", "")
        tgt_id = e.get("target", "")
        src_norm = _node_id_to_address(src_id, node_to_rel)
        tgt_norm = _node_id_to_address(tgt_id, node_to_rel)
        if not src_norm or not tgt_norm:
            continue
        edges_out.append(Edge(source=src_norm, target=tgt_norm, kind=kind))

    return edges_out, graph


def _label_to_symbol(label: str) -> str | None:
    """Strip parentheses + leading method-marker dot from a node label.

    Parser labels methods with a leading dot (e.g. `.list_users()`) so they
    can be distinguished from module-level functions in cluster reports.
    Strip both the `()` and the leading dots before comparison so a
    ground-truth `app/x.py:list_users` matches the parser node.
    """
    if not label:
        return None
    s = label.strip()
    if s.endswith("()"):
        s = s[:-2]
    s = s.lstrip(".")
    return s.lower() or None


def _node_id_to_address(node_id: str, node_to_rel: dict) -> str | None:
    """Resolve a parser node ID to 'rel_path:symbol' or 'rel_path' or ':symbol'."""
    if not node_id:
        return None
    entry = node_to_rel.get(node_id)
    if entry is None:
        # Unknown node ID. Fall back to the raw ID lowercased so a downstream
        # ground-truth entry can still potentially match by name.
        return f":{node_id.lower()}"
    rel, sym = entry
    if rel and sym:
        return f"{rel}:{sym}"
    if rel:
        return rel
    if sym:
        return f":{sym}"
    return None


def score(predicted: Iterable[Edge], truth: Iterable[Edge]) -> dict:
    """
    Compute overall + per-kind precision and recall.

    Match policy:
      - Direction matters.
      - Source/target address must match exactly after normalization.
      - Kind must agree, except ground-truth 'reference' accepts any kind
        (catch-all bucket for loose semantic links).
      - Targets that ground truth names by symbol only (e.g. external ':base')
        also match a predicted edge whose target file lives inside the fixture
        but ends in the same symbol - this avoids false negatives when the
        parser resolves a base class and ground truth left it unresolved.
    """
    pred_set = set(predicted)
    truth_set = set(truth)

    pred_index: dict[tuple[str, str], set[Edge]] = defaultdict(set)
    for p in pred_set:
        pred_index[(p.source, p.target)].add(p)
        # Also index target by symbol-only suffix for soft external match.
        if ":" in p.target:
            _, sym = p.target.rsplit(":", 1)
            pred_index[(p.source, f":{sym}")].add(p)

    matched_pred: set[Edge] = set()
    matched_truth: set[Edge] = set()

    for t in truth_set:
        candidates = pred_index.get((t.source, t.target), set())
        # Try soft match: same source, target symbol matches anywhere
        if not candidates and ":" in t.target:
            _, tsym = t.target.rsplit(":", 1)
            candidates = pred_index.get((t.source, f":{tsym}"), set())
        if not candidates:
            continue
        for p in candidates:
            if t.kind == "reference" or t.kind == p.kind:
                matched_pred.add(p)
                matched_truth.add(t)
                break

    overall_p = len(matched_pred) / len(pred_set) if pred_set else 0.0
    overall_r = len(matched_truth) / len(truth_set) if truth_set else 0.0

    per_kind_truth: dict[str, set[Edge]] = defaultdict(set)
    per_kind_matched: dict[str, set[Edge]] = defaultdict(set)
    for e in truth_set:
        per_kind_truth[e.kind].add(e)
    for e in matched_truth:
        per_kind_matched[e.kind].add(e)

    per_kind: dict[str, dict] = {}
    for kind, truth_edges in per_kind_truth.items():
        matched = per_kind_matched.get(kind, set())
        per_kind[kind] = {
            "truth_count": len(truth_edges),
            "matched_count": len(matched),
            "recall": len(matched) / len(truth_edges) if truth_edges else 0.0,
        }

    return {
        "predicted_total": len(pred_set),
        "truth_total": len(truth_set),
        "matched": len(matched_pred),
        "precision": overall_p,
        "recall": overall_r,
        "per_kind": per_kind,
        "missed_truth": sorted(
            ((e.source, e.target, e.kind) for e in truth_set - matched_truth),
        ),
    }


def _print_report(fixture: str, result: dict, verbose: bool) -> None:
    print(f"=== Eval: {fixture} ===")
    print(f"Predicted edges : {result['predicted_total']}")
    print(f"Ground truth    : {result['truth_total']}")
    print(f"Matched         : {result['matched']}")
    print(f"Precision       : {result['precision']:.3f}")
    print(f"Recall          : {result['recall']:.3f}")
    if result["per_kind"]:
        print()
        print("Per-kind recall:")
        for kind, stats in sorted(result["per_kind"].items()):
            print(
                f"  {kind:<12} {stats['matched_count']}/{stats['truth_count']} "
                f"(recall {stats['recall']:.3f})"
            )
    if verbose and result["missed_truth"]:
        print()
        print("Missed ground-truth edges:")
        for src, tgt, kind in result["missed_truth"]:
            print(f"  [{kind}] {src} -> {tgt}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("fixture", help="Fixture name (subdir of fixtures/, ground_truth/<name>.json)")
    parser.add_argument("-v", "--verbose", action="store_true", help="Print missed ground-truth edges")
    args = parser.parse_args(argv)

    fixture_dir = FIXTURES_DIR / args.fixture
    if not fixture_dir.exists():
        print(f"Fixture directory not found: {fixture_dir}", file=sys.stderr)
        return 2

    truth = load_ground_truth(args.fixture)
    predicted, _raw = build_fixture_graph(fixture_dir)
    result = score(predicted, truth)
    _print_report(args.fixture, result, verbose=args.verbose)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
