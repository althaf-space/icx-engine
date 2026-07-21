"""Built-in benchmark corpus + rubric + difficulty tags."""
from __future__ import annotations

from icx_engine.boost.benchmark.corpus import load_corpus, BenchPrompt, RubricItem


def test_corpus_nonempty_and_typed():
    corpus = load_corpus()
    assert len(corpus) >= 8
    assert all(isinstance(p, BenchPrompt) for p in corpus)


def test_every_prompt_has_rubric_and_archetype():
    for p in load_corpus():
        assert p.prompt.strip()
        assert p.archetype
        assert p.rubric and all(isinstance(r, RubricItem) and r.any_of for r in p.rubric)


def test_ids_unique():
    ids = [p.id for p in load_corpus()]
    assert len(ids) == len(set(ids))


def test_archetypes_spread():
    arch = {p.archetype for p in load_corpus()}
    assert {"coding", "debugging", "security"} <= arch


def test_difficulty_classes_present():
    diffs = {p.difficulty for p in load_corpus()}
    assert {"underspecified", "hard", "easy"} <= diffs


def test_underspecified_prompts_are_short_and_dominant():
    corpus = load_corpus()
    us = [p for p in corpus if p.difficulty == "underspecified"]
    # underspecified prompts are the realistic vague one-liners, and they are the bulk of the corpus
    assert len(us) >= 8
    assert all(len(p.prompt) < 60 for p in us)
    # each underspecified prompt lists several real requirements (room for a raw answer to miss)
    assert all(len(p.rubric) >= 4 for p in us)
