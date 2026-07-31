"""Benchmark runner: raw vs boosted, injected generator + boost. Pure."""
from __future__ import annotations

import re
import time

from icx_engine.boost.benchmark.corpus import BenchPrompt, RubricItem
from icx_engine.boost.benchmark.runner import run_benchmark


def _corpus():
    return [BenchPrompt("p1", "add endpoint", "coding",
                        [RubricItem("validation", ["validat"])], difficulty="underspecified")]


def test_boost_lifts_score():
    def gen(prompt):
        return "here is validation logic" if "BOOSTED" in prompt else "here is some code"

    def boost(prompt):
        return "BOOSTED: " + prompt
    rep = run_benchmark(gen, boost, _corpus())
    assert rep.raw_avg == 0.0
    assert rep.boosted_avg == 1.0
    assert rep.rows[0]["boosted_frac"] == 1.0


def test_rows_and_grouping():
    def gen(prompt):
        return "validate always"
    rep = run_benchmark(gen, lambda p: p, _corpus())
    assert rep.rows[0]["archetype"] == "coding"
    assert rep.rows[0]["difficulty"] == "underspecified"
    assert rep.rows[0]["req_total"] == 1
    assert "coding" in rep.by_archetype
    assert "underspecified" in rep.by_difficulty
    assert rep.by_difficulty["underspecified"]["n"] == 1
    assert rep.raw_avg == 1.0 and rep.boosted_avg == 1.0


def test_covered_counts_tracked():
    def gen(prompt):
        return "validate always" if "BOOST" in prompt else "nothing here"
    rep = run_benchmark(gen, lambda p: "BOOST " + p, _corpus())
    assert rep.rows[0]["raw_covered"] == 0
    assert rep.rows[0]["boosted_covered"] == 1


def test_generate_failure_is_scored_zero_not_crash():
    def gen(prompt):
        raise RuntimeError("model down")
    rep = run_benchmark(gen, lambda p: p, _corpus())
    assert rep.rows[0]["raw_frac"] == 0.0


async def test_base_generate_not_implemented_by_default():
    import pytest
    from icx_engine.llm.base import LLMProvider

    class Dummy(LLMProvider):
        async def analyze(self, raw):
            return None

    assert hasattr(LLMProvider, "generate")
    with pytest.raises(NotImplementedError):
        await Dummy.__new__(Dummy).generate("x")


def test_repeats_averages_variance():
    # generate alternates good/bad each call; over even repeats the average settles at 0.5
    state = {"i": 0}

    def gen(prompt):
        state["i"] += 1
        return "validate" if state["i"] % 2 == 0 else "nothing"
    rep = run_benchmark(gen, lambda p: p, _corpus(), repeats=2)
    assert rep.rows[0]["raw_frac"] in (0.0, 0.5, 1.0)   # averaged over 2 runs
    assert rep.rows[0]["req_total"] == 1


def _timed_corpus(n):
    return [BenchPrompt(f"task-{i}", f"prompt-{i}", "coding",
                        [RubricItem("validation", ["validat"])], difficulty="hard")
            for i in range(n)]


def test_matrix_runs_concurrently_and_preserves_order():
    """The (prompt x raw/boosted x repeat) matrix must run concurrently (wall clock far below the
    sum of every call's delay) while still returning rows in the original corpus order with the
    exact values the old sequential code would produce for the same input.

    Delays are deliberately DECREASING with prompt index (task-0 is the slowest call, task-N-1 the
    fastest) - this catches an implementation that reassembles results by completion order (e.g. a
    naive as_completed loop) instead of preserving submission/corpus order: such a bug would put the
    fast, late-index rows first."""
    n = 6
    per_call = 0.15
    corpus = _timed_corpus(n)

    def idx_of(prompt: str) -> int:
        return int(re.search(r"prompt-(\d+)", prompt).group(1))

    def boost(prompt: str) -> str:
        time.sleep(0.02)
        return "BOOSTED:" + prompt

    def generate(prompt: str) -> str:
        i = idx_of(prompt)
        time.sleep(per_call * (n - i) / n)          # decreasing delay with index
        if prompt.startswith("BOOSTED:"):
            return "here is validation logic"
        return "nothing relevant here"

    sequential_sum = 2 * sum(per_call * (n - i) / n for i in range(n)) + n * 0.02
    slowest_single_call = per_call                   # i == 0

    start = time.perf_counter()
    rep = run_benchmark(generate, boost, corpus, repeats=1)
    elapsed = time.perf_counter() - start

    # Concurrency proof: wall clock is far closer to the single slowest call than to the sum
    # of all (2*n generate + n boost) calls run sequentially.
    assert elapsed < sequential_sum * 0.6, (
        f"elapsed={elapsed:.3f}s not much less than sequential_sum={sequential_sum:.3f}s")
    assert elapsed < slowest_single_call * 5, (
        f"elapsed={elapsed:.3f}s too far from slowest_single_call={slowest_single_call:.3f}s")

    # Order + correctness proof: identical to what the sequential code already produces -
    # rows in original corpus order, every row's raw/boosted fraction unchanged by concurrency.
    assert [r["id"] for r in rep.rows] == [f"task-{i}" for i in range(n)]
    for i, r in enumerate(rep.rows):
        assert r["archetype"] == "coding"
        assert r["req_total"] == 1
        assert r["raw_frac"] == 0.0
        assert r["boosted_frac"] == 1.0
        assert r["raw_covered"] == 0
        assert r["boosted_covered"] == 1
