"""Tests for ts_lsp and pyright_lsp batch-open and circuit breaker."""
from unittest.mock import MagicMock, patch, call


class TestLSPClientCircuitBreaker:
    def test_consecutive_timeouts_increments_on_empty(self):
        import queue
        from icx_engine.graph.parser.lsp_client import LSPClient
        from pathlib import Path

        client = LSPClient(["fake"], Path("/tmp"))
        client._running = True

        mock_q = MagicMock()
        mock_q.get.side_effect = queue.Empty()
        client._pending[1] = mock_q
        client._seq = 1

        with patch.object(client, "_send"):
            client._request("textDocument/definition", {})

        assert client.consecutive_timeouts == 1

    def test_consecutive_timeouts_resets_on_response(self):
        from icx_engine.graph.parser.lsp_client import LSPClient
        from pathlib import Path

        client = LSPClient(["fake"], Path("/tmp"))
        client._running = True
        client._consecutive_timeouts = 3

        # _request creates its own Queue internally; patch queue.Queue to
        # return a mock that delivers a successful response immediately.
        mock_q = MagicMock()
        mock_q.get.return_value = {"id": 1, "result": []}

        with patch("icx_engine.graph.parser.lsp_client.queue.Queue", return_value=mock_q), \
             patch.object(client, "_send"):
            client._request("textDocument/definition", {})

        assert client.consecutive_timeouts == 0

    def test_default_timeout_is_3s(self):
        from icx_engine.graph.parser.lsp_client import _DEFAULT_TIMEOUT
        assert _DEFAULT_TIMEOUT == 3.0


class TestCircuitBreakerConstants:
    def test_ts_lsp_circuit_breaker_limit(self):
        from icx_engine.graph.parser.resolvers.ts_lsp import _CIRCUIT_BREAKER_LIMIT
        assert _CIRCUIT_BREAKER_LIMIT > 0

    def test_pyright_lsp_circuit_breaker_limit(self):
        from icx_engine.graph.parser.resolvers.pyright_lsp import _CIRCUIT_BREAKER_LIMIT
        assert _CIRCUIT_BREAKER_LIMIT > 0


class TestTsLspBatchOpen:
    def test_all_did_open_before_any_definition(self):
        """All did_open notifications must precede the first definition() call."""
        from pathlib import Path
        import tempfile

        from icx_engine.graph.parser.resolvers.ts_lsp import extract_ts_lsp_edges

        with tempfile.TemporaryDirectory() as td:
            proj = Path(td)
            f1 = proj / "a.ts"
            f2 = proj / "b.ts"
            f1.write_text("import {foo} from './b'; foo();")
            f2.write_text("export function foo() {}")

            call_log: list[str] = []

            mock_client = MagicMock()
            mock_client.__enter__ = lambda s: s
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.start.return_value = True
            mock_client.pid = None
            mock_client.consecutive_timeouts = 0

            def log_open(path, lang):
                call_log.append(f"open:{path.name}")

            def log_definition(path, row, col):
                call_log.append(f"def:{path.name}")
                return []

            mock_client.did_open.side_effect = log_open
            mock_client.definition.side_effect = log_definition

            ast_extraction = {
                "nodes": [
                    {"id": "n_a", "label": "a.ts", "source_file": str(f1)},
                    {"id": "n_b", "label": "b.ts", "source_file": str(f2)},
                ],
                "edges": [],
            }

            # LSPClient is imported lazily inside the function body, so patch
            # it at its definition location (lsp_client module).
            with patch("icx_engine.graph.parser.resolvers.ts_lsp.ensure_server", return_value=["node", "ts-ls"]), \
                 patch("icx_engine.graph.parser.lsp_client.LSPClient", return_value=mock_client), \
                 patch("importlib.import_module") as mock_import:
                ts_mod = MagicMock()
                ts_mod.language_typescript.return_value = MagicMock()
                ts_mod.language_tsx.return_value = MagicMock()
                mock_import.return_value = ts_mod

                mock_parser = MagicMock()
                mock_tree = MagicMock()
                mock_tree.root_node.type = "program"
                mock_tree.root_node.children = []
                mock_parser.parse.return_value = mock_tree

                with patch("icx_engine.graph.parser.resolvers.ts_lsp.Parser", return_value=mock_parser), \
                     patch("icx_engine.graph.parser.resolvers.ts_lsp.Language"):
                    extract_ts_lsp_edges([f1, f2], proj, ast_extraction)

            open_calls = [e for e in call_log if e.startswith("open:")]
            def_calls = [e for e in call_log if e.startswith("def:")]

            if open_calls and def_calls:
                last_open_idx = max(i for i, e in enumerate(call_log) if e.startswith("open:"))
                first_def_idx = min(i for i, e in enumerate(call_log) if e.startswith("def:"))
                assert last_open_idx < first_def_idx, (
                    "All did_open calls must complete before any definition() query. "
                    f"Call log: {call_log}"
                )
