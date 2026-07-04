from __future__ import annotations
from unittest.mock import MagicMock, patch
import numpy as np
import pytest

from icx_engine.exceptions import MemoryError


def test_sentinel_absent_triggers_setup(tmp_path, monkeypatch):
    from icx_engine.memory import embeddings as emb_mod
    monkeypatch.setattr(emb_mod, "MEMORY_DIR", tmp_path)
    monkeypatch.setattr(emb_mod, "MODEL_DIR", tmp_path / "model")
    monkeypatch.setattr(emb_mod, "SENTINEL_PATH", tmp_path / ".mem_initialized")

    with patch.object(emb_mod.EmbeddingsManager, "_download_model_files") as mock_dl, \
         patch.object(emb_mod.EmbeddingsManager, "_verify_model_files"):
        mgr = emb_mod.EmbeddingsManager()
        mgr.ensure_ready()
        mock_dl.assert_called_once()

    assert (tmp_path / ".mem_initialized").exists()
    assert (tmp_path / ".mem_initialized").read_text(encoding="utf-8").strip() == emb_mod.EMBEDDING_MODEL


def test_sentinel_present_skips_setup(tmp_path, monkeypatch):
    from icx_engine.memory import embeddings as emb_mod
    monkeypatch.setattr(emb_mod, "MEMORY_DIR", tmp_path)
    monkeypatch.setattr(emb_mod, "SENTINEL_PATH", tmp_path / ".mem_initialized")
    (tmp_path / ".mem_initialized").write_text(emb_mod.EMBEDDING_MODEL, encoding="utf-8")

    with patch.object(emb_mod.EmbeddingsManager, "_download_model_files") as mock_dl:
        mgr = emb_mod.EmbeddingsManager()
        mgr.ensure_ready()
        mock_dl.assert_not_called()


def test_sentinel_model_mismatch_retriggers_setup(tmp_path, monkeypatch):
    from icx_engine.memory import embeddings as emb_mod
    monkeypatch.setattr(emb_mod, "MEMORY_DIR", tmp_path)
    monkeypatch.setattr(emb_mod, "MODEL_DIR", tmp_path / "model")
    sentinel = tmp_path / ".mem_initialized"
    sentinel.write_text("some-old-model-name", encoding="utf-8")
    monkeypatch.setattr(emb_mod, "SENTINEL_PATH", sentinel)

    with patch.object(emb_mod.EmbeddingsManager, "_download_model_files"), \
         patch.object(emb_mod.EmbeddingsManager, "_verify_model_files"):
        mgr = emb_mod.EmbeddingsManager()
        mgr.ensure_ready()

    assert sentinel.read_text(encoding="utf-8").strip() == emb_mod.EMBEDDING_MODEL


def test_download_failure_raises_memory_error(tmp_path, monkeypatch):
    from icx_engine.memory import embeddings as emb_mod
    monkeypatch.setattr(emb_mod, "MEMORY_DIR", tmp_path)
    monkeypatch.setattr(emb_mod, "MODEL_DIR", tmp_path / "model")
    monkeypatch.setattr(emb_mod, "SENTINEL_PATH", tmp_path / ".mem_initialized")

    with patch.object(
        emb_mod.EmbeddingsManager,
        "_download_model_files",
        side_effect=OSError("network error"),
    ):
        mgr = emb_mod.EmbeddingsManager()
        with pytest.raises(MemoryError, match="Failed to download"):
            mgr.ensure_ready()


def test_sentinel_not_written_when_verification_fails(tmp_path, monkeypatch):
    from icx_engine.memory import embeddings as emb_mod
    monkeypatch.setattr(emb_mod, "MEMORY_DIR", tmp_path)
    monkeypatch.setattr(emb_mod, "MODEL_DIR", tmp_path / "model")
    monkeypatch.setattr(emb_mod, "SENTINEL_PATH", tmp_path / ".mem_initialized")

    with patch.object(emb_mod.EmbeddingsManager, "_download_model_files"), \
         patch.object(
             emb_mod.EmbeddingsManager,
             "_verify_model_files",
             side_effect=OSError("corrupt file"),
         ):
        mgr = emb_mod.EmbeddingsManager()
        with pytest.raises(MemoryError, match="Failed to download"):
            mgr.ensure_ready()

    assert not (tmp_path / ".mem_initialized").exists()


def test_tqdm_env_restored_after_download(tmp_path, monkeypatch):
    from icx_engine.memory import embeddings as emb_mod
    monkeypatch.setattr(emb_mod, "MEMORY_DIR", tmp_path)
    monkeypatch.setattr(emb_mod, "MODEL_DIR", tmp_path / "model")
    monkeypatch.setattr(emb_mod, "SENTINEL_PATH", tmp_path / ".mem_initialized")

    import os
    os.environ["TQDM_DISABLE"] = "0"
    try:
        with patch.object(emb_mod.EmbeddingsManager, "_download_model_files"), \
             patch.object(emb_mod.EmbeddingsManager, "_verify_model_files"):
            mgr = emb_mod.EmbeddingsManager()
            mgr.ensure_ready()
        assert os.environ.get("TQDM_DISABLE") == "0"
    finally:
        os.environ.pop("TQDM_DISABLE", None)


def test_vector_dim_constant_is_768():
    from icx_engine.memory.embeddings import VECTOR_DIM
    assert VECTOR_DIM == 768


# -- Model integrity verification (finding S1a) --------------------------------

def _seed_model_dir(model_dir, tok_bytes: bytes, onnx_bytes: bytes):
    (model_dir / "onnx").mkdir(parents=True)
    (model_dir / "tokenizer.json").write_bytes(tok_bytes)
    (model_dir / "onnx" / "model_quantized.onnx").write_bytes(onnx_bytes)


def test_verify_model_files_checksum_match(tmp_path, monkeypatch):
    import hashlib
    from icx_engine.memory import embeddings as emb_mod
    model_dir = tmp_path / "model"
    _seed_model_dir(model_dir, b"tokenizer-bytes", b"onnx-bytes")
    monkeypatch.setattr(emb_mod, "MODEL_DIR", model_dir)
    monkeypatch.setattr(emb_mod, "_MODEL_CHECKSUMS", {
        "tokenizer.json": hashlib.sha256(b"tokenizer-bytes").hexdigest(),
        "onnx/model_quantized.onnx": hashlib.sha256(b"onnx-bytes").hexdigest(),
    })
    emb_mod.EmbeddingsManager()._verify_model_files()  # no raise


def test_verify_model_files_checksum_mismatch_raises(tmp_path, monkeypatch):
    from icx_engine.memory import embeddings as emb_mod
    model_dir = tmp_path / "model"
    _seed_model_dir(model_dir, b"good", b"tampered")
    monkeypatch.setattr(emb_mod, "MODEL_DIR", model_dir)
    monkeypatch.setattr(emb_mod, "_MODEL_CHECKSUMS", {
        "onnx/model_quantized.onnx": "0" * 64,
    })
    with pytest.raises(OSError, match="checksum mismatch"):
        emb_mod.EmbeddingsManager()._verify_model_files()


def test_verify_model_files_missing_file_raises(tmp_path, monkeypatch):
    from icx_engine.memory import embeddings as emb_mod
    (tmp_path / "model" / "onnx").mkdir(parents=True)
    (tmp_path / "model" / "tokenizer.json").write_bytes(b"x")
    # onnx file absent
    monkeypatch.setattr(emb_mod, "MODEL_DIR", tmp_path / "model")
    monkeypatch.setattr(emb_mod, "_MODEL_CHECKSUMS", {})
    with pytest.raises(OSError, match="missing or empty"):
        emb_mod.EmbeddingsManager()._verify_model_files()


def test_embed_lazy_load_is_thread_safe(monkeypatch):
    """Concurrent embed() calls load the model exactly once (finding R2)."""
    import threading
    import time
    from unittest.mock import MagicMock
    import numpy as np
    from icx_engine.memory import embeddings as emb_mod

    mgr = emb_mod.EmbeddingsManager()
    load_calls = []

    def fake_load():
        load_calls.append(1)
        time.sleep(0.02)  # widen the race window
        sess = MagicMock()
        sess.get_inputs.return_value = []
        sess.run.return_value = [np.zeros((1, 3, emb_mod.VECTOR_DIM), dtype=np.float32)]
        mgr._session = sess
        tok = MagicMock()
        enc = MagicMock()
        enc.ids = [1, 2, 3]
        enc.attention_mask = [1, 1, 1]
        tok.encode.return_value = enc
        mgr._tokenizer = tok

    monkeypatch.setattr(mgr, "_load_model", fake_load)
    errors = []

    def worker():
        try:
            mgr.embed("hello world")
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert len(load_calls) == 1


def test_embed_returns_768_dim_list(tmp_path, monkeypatch):
    from icx_engine.memory import embeddings as emb_mod
    monkeypatch.setattr(emb_mod, "MEMORY_DIR", tmp_path)
    monkeypatch.setattr(emb_mod, "SENTINEL_PATH", tmp_path / ".mem_initialized")
    (tmp_path / ".mem_initialized").write_text(emb_mod.EMBEDDING_MODEL, encoding="utf-8")

    mock_encoding = MagicMock()
    mock_encoding.ids = [101, 2023, 2003, 1037, 3231, 102]
    mock_encoding.attention_mask = [1, 1, 1, 1, 1, 1]

    mock_tokenizer = MagicMock()
    mock_tokenizer.encode.return_value = mock_encoding

    fake_hidden = np.random.rand(1, 6, 768).astype(np.float32)
    mock_session = MagicMock()
    mock_session.run.return_value = [fake_hidden]
    mock_session.get_inputs.return_value = []  # no token_type_ids

    mgr = emb_mod.EmbeddingsManager()
    mgr._tokenizer = mock_tokenizer
    mgr._session = mock_session

    result = mgr.embed("auth token expired")

    assert isinstance(result, list)
    assert len(result) == 768
    assert isinstance(result[0], float)


def test_embed_passes_token_type_ids_when_model_expects_it(tmp_path, monkeypatch):
    from icx_engine.memory import embeddings as emb_mod
    monkeypatch.setattr(emb_mod, "MEMORY_DIR", tmp_path)
    monkeypatch.setattr(emb_mod, "SENTINEL_PATH", tmp_path / ".mem_initialized")
    (tmp_path / ".mem_initialized").write_text(emb_mod.EMBEDDING_MODEL, encoding="utf-8")

    mock_encoding = MagicMock()
    mock_encoding.ids = [101, 1000, 102]
    mock_encoding.attention_mask = [1, 1, 1]

    mock_tokenizer = MagicMock()
    mock_tokenizer.encode.return_value = mock_encoding

    fake_hidden = np.random.rand(1, 3, 768).astype(np.float32)
    mock_session = MagicMock()
    mock_session.run.return_value = [fake_hidden]

    inp_ids = MagicMock()
    inp_ids.name = "input_ids"
    inp_att = MagicMock()
    inp_att.name = "attention_mask"
    inp_tti = MagicMock()
    inp_tti.name = "token_type_ids"
    mock_session.get_inputs.return_value = [inp_ids, inp_att, inp_tti]

    mgr = emb_mod.EmbeddingsManager()
    mgr._tokenizer = mock_tokenizer
    mgr._session = mock_session

    mgr.embed("test query")

    call_kwargs = mock_session.run.call_args[0][1]
    assert "token_type_ids" in call_kwargs


def test_embed_empty_string_returns_zero_vector(tmp_path, monkeypatch):
    from icx_engine.memory import embeddings as emb_mod
    monkeypatch.setattr(emb_mod, "MEMORY_DIR", tmp_path)
    monkeypatch.setattr(emb_mod, "SENTINEL_PATH", tmp_path / ".mem_initialized")
    (tmp_path / ".mem_initialized").write_text(emb_mod.EMBEDDING_MODEL, encoding="utf-8")

    mgr = emb_mod.EmbeddingsManager()
    result = mgr.embed("")

    assert isinstance(result, list)
    assert len(result) == 768
    assert all(v == 0.0 for v in result)


def test_embed_whitespace_only_returns_zero_vector(tmp_path, monkeypatch):
    from icx_engine.memory import embeddings as emb_mod
    monkeypatch.setattr(emb_mod, "MEMORY_DIR", tmp_path)
    monkeypatch.setattr(emb_mod, "SENTINEL_PATH", tmp_path / ".mem_initialized")
    (tmp_path / ".mem_initialized").write_text(emb_mod.EMBEDDING_MODEL, encoding="utf-8")

    mgr = emb_mod.EmbeddingsManager()
    result = mgr.embed("   ")

    assert len(result) == 768
    assert all(v == 0.0 for v in result)
