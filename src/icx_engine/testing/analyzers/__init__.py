"""Analyzer-prompt suite: per-framework Element Census prompts that drive comprehensive,
zero-miss test authoring. `registry` selects the right prompt for a detected framework/language and
serves its text (bundled asset, user-overridable under ~/.icx/testing_analyzers/). `schema` validates
the returned census JSON and runs the reconciliation (nothing-missed) check.
"""
from icx_engine.testing.analyzers.registry import (
    AnalyzerSpec,
    select_analyzer,
    detect_framework,
    analyzers_dir,
    prompt_text,
    ensure_seeded,
    list_analyzers,
)

__all__ = [
    "AnalyzerSpec",
    "select_analyzer",
    "detect_framework",
    "analyzers_dir",
    "prompt_text",
    "ensure_seeded",
    "list_analyzers",
]
