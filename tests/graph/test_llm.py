"""Tests for the LLM rate-limit circuit breaker and retry-layering fix in
graph/parser/llm.py.

Real-world case this fixes: 184/186 chunks independently hit a rate-limited key and
retried individually (30-50s each) before giving up, contributing only 16 of 252,019
final edges - estimate_build_eta predicted ~28s, the actual build took 58m31s. Root
cause was two-fold: (1) neither OpenAI() nor anthropic.Anthropic() disabled the SDK's
own default retry-with-backoff, so each failing chunk paid that hidden cost before ICX's
own code ever saw the exception; (2) the chunk loop had no circuit breaker, so every
failing chunk paid that same cost independently with no early abort.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from icx_engine.graph.parser.llm import (
    _looks_like_rate_limited, _RATE_LIMIT_CIRCUIT_BREAKER_THRESHOLD,
    extract_corpus_parallel,
)


# -- _looks_like_rate_limited ----------------------------------------------------------

def test_looks_like_rate_limited_matches_by_exception_class_name():
    class RateLimitError(Exception):
        pass
    # Message deliberately carries no rate-limit-shaped text - only the class name should
    # be enough, since real SDK 429 response bodies vary by provider and are not reliable.
    assert _looks_like_rate_limited(RateLimitError("service unavailable")) is True


@pytest.mark.parametrize("message", [
    "Rate limit reached for requests",
    "HTTP 429 Too Many Requests",
    "rate_limit_exceeded",
    "You have exceeded your current quota exceeded",
    "RESOURCE_EXHAUSTED: quota exceeded",
])
def test_looks_like_rate_limited_matches_by_message(message):
    assert _looks_like_rate_limited(Exception(message)) is True


def test_looks_like_rate_limited_false_for_unrelated_error():
    assert _looks_like_rate_limited(ValueError("malformed JSON in response")) is False


# -- extract_corpus_parallel circuit breaker --------------------------------------------

def _fake_files(n: int) -> list[Path]:
    return [Path(f"src/file_{i}.py") for i in range(n)]


class _FakeRateLimitError(Exception):
    pass


def test_circuit_breaker_trips_sequential_path():
    """max_concurrency=1 forces the sequential loop. All 10 chunks would fail with a
    rate-limit-shaped error; the breaker must stop after exactly the threshold count."""
    files = _fake_files(10)
    attempts = []

    def _always_rate_limited(chunk, **kwargs):
        attempts.append(chunk)
        raise _FakeRateLimitError("Rate limit reached for requests")

    with patch(
        "icx_engine.graph.parser.llm._extract_with_adaptive_retry",
        side_effect=_always_rate_limited,
    ):
        result = extract_corpus_parallel(
            files, backend="claude", api_key="k", token_budget=None, chunk_size=1,
            max_concurrency=1,
        )

    assert len(attempts) == _RATE_LIMIT_CIRCUIT_BREAKER_THRESHOLD
    assert result["failed_chunks"] == _RATE_LIMIT_CIRCUIT_BREAKER_THRESHOLD
    assert result["aborted_chunks"] == 10 - _RATE_LIMIT_CIRCUIT_BREAKER_THRESHOLD


def test_circuit_breaker_does_not_trip_on_non_rate_limit_failures():
    """A different failure class (e.g. a malformed-JSON bug) must not trip the rate-limit
    breaker - only genuinely rate-limit-shaped failures should abort early."""
    files = _fake_files(10)
    attempts = []

    def _always_other_error(chunk, **kwargs):
        attempts.append(chunk)
        raise ValueError("malformed JSON in response")

    with patch(
        "icx_engine.graph.parser.llm._extract_with_adaptive_retry",
        side_effect=_always_other_error,
    ):
        result = extract_corpus_parallel(
            files, backend="claude", api_key="k", token_budget=None, chunk_size=1,
            max_concurrency=1,
        )

    assert len(attempts) == 10
    assert result["failed_chunks"] == 10
    assert result["aborted_chunks"] == 0


def test_circuit_breaker_trips_parallel_path():
    """max_concurrency>1 uses the batched submission path - chunks are submitted in
    batches of `workers` size (not all eagerly upfront), so the breaker has a
    deterministic point to stop at: at most one extra full batch beyond the one that
    tripped it. With workers=4 and every chunk failing, the 5th failure lands in the
    2nd batch, so exactly 2 batches (8 chunks) run before the loop stops - never all 20."""
    files = _fake_files(20)
    attempts = []

    def _always_rate_limited(chunk, **kwargs):
        attempts.append(chunk)
        raise _FakeRateLimitError("429 too many requests")

    with patch(
        "icx_engine.graph.parser.llm._extract_with_adaptive_retry",
        side_effect=_always_rate_limited,
    ):
        result = extract_corpus_parallel(
            files, backend="claude", api_key="k", token_budget=None, chunk_size=1,
            max_concurrency=4,
        )

    assert len(attempts) == 8
    assert result["failed_chunks"] == 8
    assert result["aborted_chunks"] == 12
    assert result["failed_chunks"] + result["aborted_chunks"] == 20


def test_no_circuit_breaker_trip_when_failures_are_below_threshold():
    """Fewer than the threshold count of rate-limit failures must not abort the run -
    the remaining, non-failing chunks still get processed."""
    files = _fake_files(10)
    attempts = []

    def _fail_first_two_then_succeed(chunk, **kwargs):
        attempts.append(chunk)
        if len(attempts) <= 2:
            raise _FakeRateLimitError("rate limit")
        return {"nodes": [], "edges": [], "hyperedges": [], "input_tokens": 1, "output_tokens": 1, "model": "m", "finish_reason": "stop"}

    with patch(
        "icx_engine.graph.parser.llm._extract_with_adaptive_retry",
        side_effect=_fail_first_two_then_succeed,
    ):
        result = extract_corpus_parallel(
            files, backend="claude", api_key="k", token_budget=None, chunk_size=1,
            max_concurrency=1,
        )

    assert len(attempts) == 10
    assert result["failed_chunks"] == 2
    assert result["aborted_chunks"] == 0
