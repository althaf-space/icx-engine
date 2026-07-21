"""Benchmark runner: raw vs boosted, injected generator + boost. Pure."""
from __future__ import annotations

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
