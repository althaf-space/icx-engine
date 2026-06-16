from unittest.mock import patch

from datasketch import MinHashLSH

from icx_engine.graph.parser.dedup import _UF, _llm_tiebreak, _make_minhash, _norm, deduplicate_entities
from icx_engine.graph.parser.build import _norm_label, _normalize_id


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


class TestNormNoneGuard:
    def test_norm_none_returns_empty(self):
        assert _norm(None) == ""

    def test_norm_empty_string_returns_empty(self):
        assert _norm("") == ""

    def test_norm_valid_string_unchanged_behavior(self):
        assert _norm("UserService") == "userservice"

    def test_norm_label_none_returns_empty(self):
        assert _norm_label(None) == ""

    def test_norm_label_empty_returns_empty(self):
        assert _norm_label("") == ""

    def test_norm_label_valid_string_unchanged_behavior(self):
        assert _norm_label("OrderProcessor") == "orderprocessor"

    def test_normalize_id_none_returns_empty(self):
        assert _normalize_id(None) == ""

    def test_normalize_id_empty_returns_empty(self):
        assert _normalize_id("") == ""

    def test_normalize_id_valid_string_unchanged_behavior(self):
        assert _normalize_id("MyClass") == "myclass"


class TestDeduplicateEntitiesNoneLabel:
    def test_node_with_null_label_does_not_crash(self):
        nodes = [
            {"id": "n1", "label": None, "source_file": "a.py"},
            {"id": "n2", "label": "UserService", "source_file": "b.py"},
        ]
        edges = [{"source": "n1", "target": "n2", "relation": "calls"}]
        result_nodes, result_edges = deduplicate_entities(nodes, edges, communities={})
        assert any(n["id"] == "n2" for n in result_nodes)

    def test_node_with_null_id_and_null_label_skipped(self):
        nodes = [
            {"id": "n1", "label": None, "source_file": "a.py"},
            {"id": "n2", "label": None, "source_file": "b.py"},
            {"id": "n3", "label": "RealService", "source_file": "c.py"},
        ]
        result_nodes, _ = deduplicate_entities(nodes, [], communities={})
        assert any(n["id"] == "n3" for n in result_nodes)

    def test_valid_nodes_still_deduplicated_correctly(self):
        nodes = [
            {"id": "n1", "label": "AuthService", "source_file": "auth.py"},
            {"id": "n2", "label": "AuthService", "source_file": "auth.py"},
            {"id": "n3", "label": None, "source_file": "auth.py"},
        ]
        result_nodes, _ = deduplicate_entities(nodes, [], communities={})
        ids = {n["id"] for n in result_nodes}
        assert len(ids & {"n1", "n2"}) == 1


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
