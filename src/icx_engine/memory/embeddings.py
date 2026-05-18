from __future__ import annotations
import os
import sys
from pathlib import Path
from rich.console import Console
from rich.progress import (
    Progress, SpinnerColumn, TextColumn, BarColumn,
    DownloadColumn, TransferSpeedColumn, TimeRemainingColumn,
)
from icx_engine.exceptions import MemoryError

EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"
_ONNX_REPO = "Xenova/bge-small-en-v1.5"
_ONNX_FILE = "onnx/model_quantized.onnx"
VECTOR_DIM = 384
MEMORY_DIR = Path.home() / ".icx" / "memory"
MODEL_DIR = MEMORY_DIR / "model"
SENTINEL_PATH = MEMORY_DIR / ".mem_initialized"


def _is_initialized() -> bool:
    if not SENTINEL_PATH.exists():
        return False
    return SENTINEL_PATH.read_text().strip() == EMBEDDING_MODEL


def _mark_initialized() -> None:
    MEMORY_DIR.mkdir(parents=True, exist_ok=True, **({"mode": 0o700} if sys.platform != "win32" else {}))
    SENTINEL_PATH.write_text(EMBEDDING_MODEL)
    if sys.platform != "win32":
        SENTINEL_PATH.chmod(0o600)


class EmbeddingsManager:
    """Runs local ONNX embeddings via onnxruntime + tokenizers.

    Lifecycle:
    1. ``ensure_ready()`` at startup - downloads model files once, writes sentinel.
    2. ``embed(text)`` on demand - loads model lazily on first call.
    """

    def __init__(self) -> None:
        self._session = None
        self._tokenizer = None

    def ensure_ready(self, console: Console | None = None) -> None:
        if _is_initialized():
            return
        self._download_and_init(console or Console(stderr=True))

    def _download_and_init(self, console: Console) -> None:
        console.print(
            "\n[bold cyan]ICX Memory Engine[/bold cyan] - one-time setup\n"
            f"Downloading embedding model [dim]({EMBEDDING_MODEL}, ~24 MB)[/dim]\n"
            "Cached at [dim]~/.icx/memory/model/[/dim] - every subsequent start is instant.\n"
        )
        _saved = {k: os.environ.get(k) for k in ("TQDM_DISABLE", "HF_HUB_DISABLE_PROGRESS_BARS")}
        try:
            os.environ["TQDM_DISABLE"] = "1"
            os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"
            with Progress(
                SpinnerColumn(),
                TextColumn("[cyan]{task.description:<28}"),
                BarColumn(bar_width=30),
                DownloadColumn(),
                TransferSpeedColumn(),
                TimeRemainingColumn(),
                console=console,
                transient=True,
            ) as progress:
                self._download_model_files(progress)
            self._verify_model_files()
        except Exception as exc:
            raise MemoryError(
                f"Failed to download embedding model '{EMBEDDING_MODEL}'. "
                "Check your network connection and retry. "
                "ICX analysis continues to work without memory."
            ) from exc
        finally:
            for k, v in _saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

        _mark_initialized()
        console.print("[bold green]✓[/bold green] Memory ready.\n")

    def _download_model_files(self, progress: Progress) -> None:
        import socket
        import urllib.request  # noqa: PLC0415

        MODEL_DIR.mkdir(parents=True, exist_ok=True, **({"mode": 0o700} if sys.platform != "win32" else {}))
        _old_timeout = socket.getdefaulttimeout()
        socket.setdefaulttimeout(60)
        try:
            files = [
                ("tokenizer.json",
                 f"https://huggingface.co/{EMBEDDING_MODEL}/resolve/main/tokenizer.json",
                 MODEL_DIR / "tokenizer.json"),
                (_ONNX_FILE,
                 f"https://huggingface.co/{_ONNX_REPO}/resolve/main/{_ONNX_FILE}",
                 MODEL_DIR / _ONNX_FILE),
            ]

            for label, url, dest in files:
                dest.parent.mkdir(parents=True, exist_ok=True, **({"mode": 0o700} if sys.platform != "win32" else {}))
                task = progress.add_task(f"[cyan]{label}", total=None)

                def _reporthook(count: int, block: int, total: int, _task=task) -> None:
                    if total > 0:
                        progress.update(_task, total=total, completed=count * block)

                urllib.request.urlretrieve(url, dest, reporthook=_reporthook)  # noqa: S310
                progress.update(task, completed=dest.stat().st_size, total=dest.stat().st_size)
        finally:
            socket.setdefaulttimeout(_old_timeout)

    def _verify_model_files(self) -> None:
        """Confirm downloaded files exist and are non-empty before writing sentinel.

        Prevents a bad download from permanently locking out re-download on next run.
        """
        tokenizer_path = MODEL_DIR / "tokenizer.json"
        onnx_path = MODEL_DIR / _ONNX_FILE
        for path in (tokenizer_path, onnx_path):
            if not path.exists() or path.stat().st_size == 0:
                raise OSError(f"Model file missing or empty after download: {path}")

    def _load_model(self) -> None:
        import onnxruntime as ort  # noqa: PLC0415
        from tokenizers import Tokenizer  # noqa: PLC0415

        self._tokenizer = Tokenizer.from_file(str(MODEL_DIR / "tokenizer.json"))
        self._tokenizer.enable_padding(pad_id=0, pad_token="[PAD]")
        self._tokenizer.enable_truncation(max_length=512)

        self._session = ort.InferenceSession(
            str(MODEL_DIR / _ONNX_FILE),
            providers=["CPUExecutionProvider"],
        )

    def embed(self, text: str) -> list[float]:
        """Embed a single string. Loads the model lazily on first call."""
        if not text or not text.strip():
            return [0.0] * VECTOR_DIM

        if self._session is None:
            self._load_model()

        import numpy as np  # noqa: PLC0415

        encoding = self._tokenizer.encode(text)
        input_ids = np.array([encoding.ids], dtype=np.int64)
        attention_mask = np.array([encoding.attention_mask], dtype=np.int64)
        token_type_ids = np.zeros_like(input_ids, dtype=np.int64)

        # Only pass token_type_ids if the model expects it (quantized models may not)
        feed: dict = {"input_ids": input_ids, "attention_mask": attention_mask}
        known_inputs = {inp.name for inp in self._session.get_inputs()}
        if "token_type_ids" in known_inputs:
            feed["token_type_ids"] = token_type_ids

        outputs = self._session.run(None, feed)

        # Mean pooling over token dimension, weighted by attention mask
        token_embeddings = outputs[0]  # (1, seq_len, 384)
        mask = attention_mask[..., np.newaxis].astype(np.float32)
        pooled = (token_embeddings * mask).sum(axis=1) / mask.sum(axis=1).clip(min=1e-9)

        # L2 normalize
        norm = np.linalg.norm(pooled, axis=1, keepdims=True)
        normalized = pooled / np.maximum(norm, 1e-12)

        return normalized[0].tolist()
