"""Tests for the context-completeness engine (pure module: fan-out, fuse/rank, coverage, miss-check)."""
from __future__ import annotations

from icx_engine.context_completeness import (
    Candidate, ScoredFile, fan_out, fuse_rank, coverage, miss_check,
)


# -- fan_out: merges signals, records reasons, guarded -----------------------------

def test_fan_out_merges_signals_and_seeds():
    cands = fan_out(
        ["src/Login.jsx"],
        graph=lambda: [("src/auth.js", "imported by Login.jsx")],
        grep=lambda: [("src/auth.js", "matches token"), ("config/routes.yaml", "route /login")],
        memory=lambda: [("src/session.js", "prior fix ICX-1")],
    )
    by = {c.path: c for c in cands}
    assert by["src/Login.jsx"].signals == {"seed"}
    assert by["src/auth.js"].signals == {"graph", "grep"}          # hit by two -> merged
    assert "imported by Login.jsx" in by["src/auth.js"].reasons
    assert "config/routes.yaml" in by                              # grep-only config file
    assert by["src/session.js"].signals == {"memory"}


def test_fan_out_guards_bad_signal():
    def _boom():
        raise RuntimeError("graph down")
    cands = fan_out(["a.py"], graph=_boom, grep=lambda: [("b.py", "x")])
    paths = {c.path for c in cands}
    assert paths == {"a.py", "b.py"}                               # boom ignored, grep survives


def test_fan_out_empty_signal_contributes_nothing():
    cands = fan_out(["a.py"], graph=lambda: [], memory=None)
    assert {c.path for c in cands} == {"a.py"}


# -- fuse_rank: scoring, tiers, ranking --------------------------------------------

def test_multi_signal_ranks_above_single():
    cands = fan_out(
        ["seed.py"],
        graph=lambda: [("multi.py", "importer")],
        grep=lambda: [("multi.py", "symbol"), ("lonely.py", "same-name match")],
    )
    scored = fuse_rank(cands)
    order = [s.path for s in scored]
    assert order[0] == "seed.py"                                   # seed first
    assert order.index("multi.py") < order.index("lonely.py")      # 2-signal above 1-signal


def test_tiers_high_medium_low():
    cands = fan_out(
        ["seed.py"],
        graph=lambda: [("dep.py", "imported by seed")],            # structural single -> high
        grep=lambda: [("lonely.py", "same-name")],                 # grep-only -> low
        semantic=lambda: [("concept.py", "related")],              # semantic single -> medium
    )
    scored = {s.path: s for s in fuse_rank(cands)}
    assert scored["dep.py"].tier == "high"
    assert scored["concept.py"].tier == "medium"
    assert scored["lonely.py"].tier == "low"


def test_prior_fix_boosts_to_high():
    cands = fan_out(["seed.py"], grep=lambda: [("x.py", "match")])
    scored = {s.path: s for s in fuse_rank(cands, prior_fix={"x.py"})}
    assert scored["x.py"].tier == "high"                           # prior-fix -> high, top boost
    assert scored["x.py"].score >= 4.0


def test_centrality_and_recency_add_score():
    cands = fan_out(["seed.py"], graph=lambda: [("a.py", "dep"), ("b.py", "dep")])
    scored = {s.path: s for s in fuse_rank(cands, centrality={"a.py": 1.0}, recency={"a.py": 1.0})}
    assert scored["a.py"].score > scored["b.py"].score


# -- coverage + miss_check ---------------------------------------------------------

def _scored_high(paths):
    return [ScoredFile(path=p, score=9, tier="high", signals=["graph", "grep"], reasons=["x"]) for p in paths]


def test_miss_check_blocks_unresolved_high_tier():
    scored = _scored_high(["a.py", "b.py"])
    r = miss_check(chosen=["a.py"], scored=scored)
    assert r["ok"] is False
    assert r["coverage"] == 0.5
    assert [m["path"] for m in r["blocking_missed"]] == ["b.py"]


def test_miss_check_justify_or_include_passes():
    scored = _scored_high(["a.py", "b.py"])
    r = miss_check(chosen=["a.py"], scored=scored, justifications={"b.py": "unrelated dupe name"})
    assert r["ok"] is True and r["coverage"] == 1.0


def test_miss_check_medium_low_advisory_not_blocking():
    scored = [
        ScoredFile("m.py", 3, "medium", ["semantic"], []),
        ScoredFile("l.py", 1, "low", ["grep"], []),
    ]
    r = miss_check(chosen=[], scored=scored)
    assert r["ok"] is True                                         # nothing high-tier -> not blocked
    assert {m["path"] for m in r["missed"]} == {"m.py", "l.py"}    # surfaced as advisory
    assert r["blocking_missed"] == []


def test_coverage_full_when_no_high_tier():
    assert coverage(chosen=[], scored=[ScoredFile("x", 1, "low", ["grep"], [])]) == 1.0


def test_miss_check_windows_paths_normalized():
    scored = _scored_high(["src/a.py"])
    r = miss_check(chosen=[r"src\a.py"], scored=scored)            # backslash chosen matches
    assert r["ok"] is True and r["coverage"] == 1.0
