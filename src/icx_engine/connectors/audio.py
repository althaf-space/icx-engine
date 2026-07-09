from __future__ import annotations
import asyncio
import logging
import os
import sys
import tempfile
import threading
from pathlib import Path
from typing import TYPE_CHECKING

_log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from icx_engine.models.config import ChannelConfig

WHISPER_MODEL = "base"
AUDIO_DIR = Path.home() / ".icx" / "audio"
MODEL_DIR = AUDIO_DIR / "model"
SENTINEL_PATH = AUDIO_DIR / ".whisper_initialized"
WHISPER_TIMEOUT = 300.0  # 5 minutes; long audio is CPU-bound in the thread executor

# Files required by faster-whisper from Systran/faster-whisper-{model} on HuggingFace.
# Downloaded flat into MODEL_DIR; WhisperModel receives the directory path directly.
_WHISPER_HF_REPO = f"Systran/faster-whisper-{WHISPER_MODEL}"
_WHISPER_FILES = (
    "config.json",
    "model.bin",
    "tokenizer.json",
    "vocabulary.txt",
)

_AUDIO_MIME: dict[str, str] = {
    ".mp3":  "audio/mpeg",
    ".wav":  "audio/wav",
    ".m4a":  "audio/m4a",
    ".ogg":  "audio/ogg",
    ".flac": "audio/flac",
    ".aac":  "audio/aac",
    ".opus": "audio/opus",
}

_CLEANUP_PROMPT = (
    "The following is a raw speech-to-text transcript. "
    "Clean it up: fix transcription errors, improve punctuation, remove filler words (um, uh, like). "
    "Return only the cleaned transcript, nothing else."
)


def audio_mime_type(filename: str) -> str:
    return _AUDIO_MIME.get(Path(filename).suffix.lower(), "audio/mpeg")


def _is_whisper_ready() -> bool:
    if not SENTINEL_PATH.exists():
        return False
    return SENTINEL_PATH.read_text(encoding="utf-8").strip() == WHISPER_MODEL


def _mark_whisper_ready() -> None:
    AUDIO_DIR.mkdir(parents=True, exist_ok=True, **({"mode": 0o700} if sys.platform != "win32" else {}))
    SENTINEL_PATH.write_text(WHISPER_MODEL, encoding="utf-8")
    if sys.platform != "win32":
        SENTINEL_PATH.chmod(0o600)


class WhisperManager:
    """Manages local faster-whisper model. Downloads base model (~145 MB) on first use."""

    def __init__(self) -> None:
        self._model = None
        self._load_lock = threading.Lock()

    def _load(self) -> None:
        """Load the Whisper model into memory. Raises if not downloaded - run `icx setup` first."""
        if self._model is not None:
            return
        with self._load_lock:
            if self._model is not None:
                return
            if not _is_whisper_ready():
                raise RuntimeError(
                    "Whisper model not found. Run `icx setup` to download it."
                )
            import warnings
            from faster_whisper import WhisperModel
            with warnings.catch_warnings():
                warnings.filterwarnings("ignore", message=".*HF_TOKEN.*")
                # Pass MODEL_DIR as a directory path so WhisperModel loads directly
                # from the flat file layout written by download() - no network access.
                self._model = WhisperModel(
                    str(MODEL_DIR), device="cpu", compute_type="int8"
                )

    def _verify_model_files(self) -> None:
        """Confirm downloaded files exist and are non-empty. Raises OSError on failure.

        Prevents a partial download (urlretrieve failing mid-transfer) from writing
        a sentinel that marks the model as ready with a corrupt file on disk.
        """
        for fname in _WHISPER_FILES:
            path = MODEL_DIR / fname
            if not path.exists() or path.stat().st_size == 0:
                raise OSError(f"Whisper model file missing or empty after download: {path}")

    def _download_model_files(self, progress) -> None:
        import socket
        import urllib.request

        MODEL_DIR.mkdir(parents=True, exist_ok=True, **({"mode": 0o700} if sys.platform != "win32" else {}))
        base_url = f"https://huggingface.co/{_WHISPER_HF_REPO}/resolve/main/"
        _old_timeout = socket.getdefaulttimeout()
        socket.setdefaulttimeout(60)
        try:
            for fname in _WHISPER_FILES:
                dest = MODEL_DIR / fname
                # Only skip if the file is present AND non-empty. A zero-byte or
                # missing file means a previous download was interrupted - re-fetch.
                if dest.exists() and dest.stat().st_size > 0:
                    continue
                # Remove any partial file before re-downloading so urlretrieve
                # writes a fresh copy rather than appending/overwriting partially.
                try:
                    dest.unlink(missing_ok=True)
                except OSError:
                    pass
                task = progress.add_task(f"[cyan]{fname}", total=None)

                def _reporthook(count: int, block: int, total: int, _task=task) -> None:
                    if total > 0:
                        progress.update(_task, total=total, completed=count * block)

                urllib.request.urlretrieve(base_url + fname, dest, reporthook=_reporthook)  # noqa: S310
                progress.update(task, completed=dest.stat().st_size, total=dest.stat().st_size)
        finally:
            socket.setdefaulttimeout(_old_timeout)

    def download(self) -> None:
        """Download and cache the Whisper model files. Only called from `icx setup`."""
        with self._load_lock:
            import warnings
            from rich.console import Console
            from rich.progress import (
                Progress, SpinnerColumn, TextColumn, BarColumn,
                DownloadColumn, TransferSpeedColumn, TimeRemainingColumn,
            )

            con = Console()
            if _is_whisper_ready():
                con.print("[green]OK[/green] Whisper model already downloaded.")
                return

            con.print(
                "\n[bold cyan]ICX Audio Engine[/bold cyan] - one-time setup\n"
                f"Downloading Whisper {WHISPER_MODEL} model (~145 MB)\n"
                "Cached at [dim]~/.icx/audio/model/[/dim] - one-time download.\n"
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
                    console=con,
                    transient=True,
                ) as progress:
                    self._download_model_files(progress)
            finally:
                for k, v in _saved.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v

            self._verify_model_files()

            with warnings.catch_warnings():
                warnings.filterwarnings("ignore", message=".*HF_TOKEN.*")
                from faster_whisper import WhisperModel
                self._model = WhisperModel(
                    str(MODEL_DIR), device="cpu", compute_type="int8"
                )

            _mark_whisper_ready()
            con.print("[bold green]OK[/bold green] Audio engine ready.\n")

    def _transcribe_sync(self, audio_path: str) -> str:
        self._load()
        segments, _ = self._model.transcribe(audio_path, beam_size=5)
        return " ".join(seg.text.strip() for seg in segments).strip()

    async def atranscribe(self, audio_path: str) -> str:
        """Run transcription in a thread executor to keep the event loop free."""
        loop = asyncio.get_running_loop()
        return await asyncio.wait_for(
            loop.run_in_executor(None, self._transcribe_sync, audio_path),
            timeout=WHISPER_TIMEOUT,
        )


async def transcribe_openai(config: "ChannelConfig", audio_bytes: bytes, fname: str) -> str:
    """Transcribe using OpenAI Whisper API (whisper-1 = large-v2, highest accuracy)."""
    import io
    from openai import AsyncOpenAI
    kwargs: dict = {"api_key": config.api_key}
    if config.base_url:
        kwargs["base_url"] = config.base_url
    client = AsyncOpenAI(**kwargs)
    audio_file = io.BytesIO(audio_bytes)
    audio_file.name = fname
    response = await client.audio.transcriptions.create(
        model="whisper-1",
        file=audio_file,
        response_format="text",
        timeout=120.0,
    )
    return response.strip() if isinstance(response, str) else str(response).strip()


async def transcribe_google(config: "ChannelConfig", audio_bytes: bytes, fname: str) -> str:
    """Transcribe using Gemini native audio (multimodal, high accuracy)."""
    from google import genai
    from google.genai import types
    client = genai.Client(api_key=config.api_key)
    mime = audio_mime_type(fname)
    contents = [
        types.Part.from_bytes(data=audio_bytes, mime_type=mime),
        types.Part.from_text(
            text="Transcribe this audio accurately. Return only the transcript text, nothing else."
        ),
    ]
    response = await asyncio.wait_for(
        client.aio.models.generate_content(model=config.model, contents=contents),
        timeout=120.0,
    )
    return (response.text or "").strip()


async def cleanup_transcript_llm(config: "ChannelConfig", transcript: str) -> str:
    """Run a local Whisper transcript through a text LLM to clean errors and formatting.

    Used for providers without native audio (Anthropic, xAI, Ollama, NIM).
    Falls back to returning the original transcript on any error.
    """
    if not transcript:
        return transcript
    prompt = f"{_CLEANUP_PROMPT}\n\n{transcript}"
    try:
        if config.provider == "anthropic":
            from anthropic import AsyncAnthropic
            client = AsyncAnthropic(api_key=config.api_key)
            resp = await client.messages.create(
                model=config.model,
                max_tokens=2048,
                timeout=90.0,
                messages=[{"role": "user", "content": prompt}],
            )
            return resp.content[0].text.strip() if resp.content else transcript
        from openai import AsyncOpenAI
        from icx_engine.connectors.attachments import _DEFAULT_BASE_URLS
        kwargs: dict = {"api_key": config.api_key or "ollama"}
        base_url = config.base_url or _DEFAULT_BASE_URLS.get(config.provider)
        if base_url:
            kwargs["base_url"] = base_url
        client = AsyncOpenAI(**kwargs)
        resp = await client.chat.completions.create(
            model=config.model,
            max_tokens=2048,
            timeout=90.0,
            messages=[{"role": "user", "content": prompt}],
        )
        return (resp.choices[0].message.content or transcript).strip()
    except Exception as exc:
        _log.warning("cleanup_transcript_llm failed: %s", exc)
        return transcript


async def _local_transcribe(audio_bytes: bytes, fname: str, whisper: "WhisperManager") -> str:
    suffix = Path(fname).suffix.lower() or ".mp3"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
        f.write(audio_bytes)
        tmp_path = f.name
    try:
        return await whisper.atranscribe(tmp_path)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


async def transcribe(
    config: "ChannelConfig | None",
    audio_bytes: bytes,
    fname: str,
    whisper: "WhisperManager",
) -> str:
    """
    Main dispatch for audio transcription.

    openai  -> Whisper API (large-v2, highest accuracy), local fallback on error
    google  -> Gemini native audio, local fallback on error
    others  -> local Whisper base -> text LLM cleanup
    no LLM  -> local Whisper base only
    """
    if config is None:
        return await _local_transcribe(audio_bytes, fname, whisper)

    if config.provider == "openai":
        try:
            return await transcribe_openai(config, audio_bytes, fname)
        except Exception as exc:
            _log.warning("OpenAI transcription failed (%s); falling back to local Whisper.", exc)
            return await _local_transcribe(audio_bytes, fname, whisper)

    if config.provider == "google":
        try:
            return await transcribe_google(config, audio_bytes, fname)
        except Exception as exc:
            _log.warning("Google transcription failed (%s); falling back to local Whisper.", exc)
            return await _local_transcribe(audio_bytes, fname, whisper)

    local = await _local_transcribe(audio_bytes, fname, whisper)
    return await cleanup_transcript_llm(config, local)
