"""Boost proof benchmark - run a corpus raw vs ICX-boosted, grade deterministically, report the lift."""
from __future__ import annotations

from icx_engine.boost.benchmark.corpus import BenchPrompt, RubricItem, load_corpus
from icx_engine.boost.benchmark.grader import GradeResult, grade
from icx_engine.boost.benchmark.runner import BenchReport, run_benchmark

__all__ = ["BenchPrompt", "RubricItem", "load_corpus", "GradeResult", "grade",
           "BenchReport", "run_benchmark"]
