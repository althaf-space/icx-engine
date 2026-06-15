from unittest.mock import patch

from datasketch import MinHashLSH

from icx_engine.graph.parser.dedup import _UF, _llm_tiebreak, _make_minhash, _norm


def _candidate(node_id: str, label: str) -> dict:
    return {"id": node_id, "label": label}


def _build_lsh(candidates: list[dict]) -> tuple[MinHashLSH, dict]:
    lsh = MinHashLSH(threshold=0.5, num_perm=128)
    minhashes = {}
    for c in candidates:
        m = _make_minhash(_norm(c["label"]))
        minhashes[c["id"]] = m
        lsh.insert(c["id"], m)
    return lsh, minhashes


def _patch_llm_backend(monkeypatch):
    monkeypatch.setattr("icx_engine.graph.parser.llm.BACKENDS", {"test": object()})
    monkeypatch.setattr("icx_engine.graph.parser.llm.get_backend_api_key", lambda backend: "fake-key")
    monkeypatch.setattr("icx_engine.graph.parser.llm.format_backend_env_keys", lambda backend: "FAKE_KEY")


class TestLlmTiebreakLSHScoping:
    def test_non_neighboring_candidates_skip_llm_call(self, monkeypatch):
        """Candidates with no LSH overlap produce zero ambiguous pairs - no LLM call at all.

        Demonstrates the O(n^2) cross-product is gone: previously every one of these
        3 candidates would be compared against every other regardless of LSH overlap.
        """
        candidates = [
            _candidate("n1", "UserAuthenticationService"),
            _candidate("n2", "PaymentGatewayConnector"),
            _candidate("n3", "InventoryStockTracker"),
        ]
        lsh, minhashes = _build_lsh(candidates)
        uf = _UF()

        _patch_llm_backend(monkeypatch)

        with patch("icx_engine.graph.parser.llm._call_llm") as mock_call:
            _llm_tiebreak(candidates, uf, {}, minhashes, lsh, backend="test")

        mock_call.assert_not_called()

    def test_lsh_neighbor_pair_in_ambiguous_band_resolved_via_llm(self, monkeypatch):
        """Two candidates that ARE LSH neighbors with a JW score in [75, 92) go to the LLM,
        and a 'yes' response merges them via union-find."""
        candidates = [
            _candidate("n1", "OrderValidationService"),
            _candidate("n2", "OrderNotificationService"),
        ]
        lsh, minhashes = _build_lsh(candidates)
        uf = _UF()

        _patch_llm_backend(monkeypatch)

        with patch("icx_engine.graph.parser.llm._call_llm", return_value="1. yes") as mock_call:
            _llm_tiebreak(candidates, uf, {}, minhashes, lsh, backend="test")

        mock_call.assert_called_once()
        assert uf.find("n1") == uf.find("n2")
