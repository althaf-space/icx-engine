from __future__ import annotations
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from icx_engine.connectors.audio import (
    WhisperManager,
    _is_whisper_ready,
    _mark_whisper_ready,
    transcribe_openai,
    transcribe_google,
    cleanup_transcript_llm,
    transcribe,
    WHISPER_MODEL,
    SENTINEL_PATH,
)
from icx_engine.models.config import ChannelConfig


# -- Sentinel helpers ----------------------------------------------------------

def test_is_whisper_ready_false_when_no_sentinel(tmp_path, monkeypatch):
    monkeypatch.setattr("icx_engine.connectors.audio.SENTINEL_PATH", tmp_path / "sentinel")
    assert _is_whisper_ready() is False


def test_is_whisper_ready_false_when_wrong_model(tmp_path, monkeypatch):
    sentinel = tmp_path / "sentinel"
    sentinel.write_text("wrong-model", encoding="utf-8")
    monkeypatch.setattr("icx_engine.connectors.audio.SENTINEL_PATH", sentinel)
    assert _is_whisper_ready() is False


def test_is_whisper_ready_true_when_sentinel_matches(tmp_path, monkeypatch):
    sentinel = tmp_path / "sentinel"
    sentinel.write_text(WHISPER_MODEL, encoding="utf-8")
    monkeypatch.setattr("icx_engine.connectors.audio.SENTINEL_PATH", sentinel)
    assert _is_whisper_ready() is True


def test_mark_whisper_ready_writes_sentinel(tmp_path, monkeypatch):
    monkeypatch.setattr("icx_engine.connectors.audio.AUDIO_DIR", tmp_path)
    monkeypatch.setattr("icx_engine.connectors.audio.SENTINEL_PATH", tmp_path / ".whisper_initialized")
    _mark_whisper_ready()
    assert (tmp_path / ".whisper_initialized").read_text(encoding="utf-8").strip() == WHISPER_MODEL


# -- WhisperManager ------------------------------------------------------------

async def test_whisper_manager_transcribe_runs_in_executor_and_returns_text(tmp_path):
    mgr = WhisperManager()

    fake_model = MagicMock()
    fake_seg = MagicMock()
    fake_seg.text = " Hello world. "
    fake_model.transcribe.return_value = ([fake_seg], MagicMock())
    mgr._model = fake_model

    result = await mgr.atranscribe(str(tmp_path / "audio.wav"))

    assert result == "Hello world."
    fake_model.transcribe.assert_called_once_with(str(tmp_path / "audio.wav"), beam_size=5)


async def test_whisper_manager_load_raises_with_setup_message_when_no_sentinel(tmp_path, monkeypatch):
    """_load() must raise RuntimeError containing 'icx setup' when model not downloaded.

    This is the signal that _is_setup_required_error() in attachments.py checks for,
    so if the message changes, the error surfacing in MCP will silently break.
    """
    monkeypatch.setattr("icx_engine.connectors.audio.SENTINEL_PATH",
                        tmp_path / ".whisper_initialized")
    mgr = WhisperManager()
    with pytest.raises(RuntimeError, match="icx setup"):
        mgr._load()


async def test_whisper_manager_loads_model_on_first_transcribe(tmp_path, monkeypatch):
    sentinel = tmp_path / ".whisper_initialized"
    sentinel.write_text(WHISPER_MODEL, encoding="utf-8")  # simulate `icx setup` already ran
    monkeypatch.setattr("icx_engine.connectors.audio.SENTINEL_PATH", sentinel)
    monkeypatch.setattr("icx_engine.connectors.audio.MODEL_DIR", tmp_path / "model")

    fake_model = MagicMock()
    fake_model.transcribe.return_value = ([], MagicMock())

    with patch("faster_whisper.WhisperModel", return_value=fake_model) as mock_wm:
        mgr = WhisperManager()
        await mgr.atranscribe(str(tmp_path / "audio.wav"))

    mock_wm.assert_called_once_with(
        str(tmp_path / "model"), device="cpu", compute_type="int8"
    )


def test_whisper_manager_load_is_thread_safe(tmp_path, monkeypatch):
    """Concurrent _load() calls must construct WhisperModel only once.

    Without the threading.Lock, multiple threads can race past the
    `self._model is None` check and all construct a model, leaking memory
    and risking corruption of the first-time download.
    """
    import threading
    sentinel = tmp_path / ".whisper_initialized"
    sentinel.write_text(WHISPER_MODEL, encoding="utf-8")  # simulate `icx setup` already ran
    monkeypatch.setattr("icx_engine.connectors.audio.SENTINEL_PATH", sentinel)
    monkeypatch.setattr("icx_engine.connectors.audio.MODEL_DIR", tmp_path / "model")

    call_count = {"n": 0}
    construct_event = threading.Event()

    def _slow_construct(*args, **kwargs):
        call_count["n"] += 1
        # Hold the lock long enough for other threads to queue behind it.
        construct_event.wait(timeout=2.0)
        m = MagicMock()
        m.transcribe.return_value = ([], MagicMock())
        return m

    with patch("faster_whisper.WhisperModel", side_effect=_slow_construct):
        mgr = WhisperManager()
        threads = [threading.Thread(target=mgr._load) for _ in range(5)]
        for t in threads:
            t.start()
        construct_event.set()
        for t in threads:
            t.join(timeout=5.0)

    assert call_count["n"] == 1
    assert mgr._model is not None


async def test_whisper_manager_model_not_reloaded_on_second_call(tmp_path, monkeypatch):
    sentinel = tmp_path / ".whisper_initialized"
    sentinel.write_text(WHISPER_MODEL, encoding="utf-8")  # simulate `icx setup` already ran
    monkeypatch.setattr("icx_engine.connectors.audio.SENTINEL_PATH", sentinel)
    monkeypatch.setattr("icx_engine.connectors.audio.MODEL_DIR", tmp_path / "model")

    fake_model = MagicMock()
    fake_model.transcribe.return_value = ([], MagicMock())

    with patch("faster_whisper.WhisperModel", return_value=fake_model) as mock_wm:
        mgr = WhisperManager()
        await mgr.atranscribe(str(tmp_path / "a.wav"))
        await mgr.atranscribe(str(tmp_path / "b.wav"))

    assert mock_wm.call_count == 1


async def test_whisper_manager_atranscribe_raises_on_timeout(monkeypatch):
    """atranscribe must raise asyncio.TimeoutError when transcription takes too long."""
    import asyncio

    mgr = WhisperManager()
    mgr._model = MagicMock()

    # Patch WHISPER_TIMEOUT to a tiny value
    monkeypatch.setattr("icx_engine.connectors.audio.WHISPER_TIMEOUT", 0.01)

    # Make _transcribe_sync block longer than the timeout
    def _slow_transcribe(path):
        import time
        time.sleep(0.5)
        return "result"

    mgr._transcribe_sync = _slow_transcribe

    with pytest.raises(asyncio.TimeoutError):
        await mgr.atranscribe("/fake/audio.wav")


# -- LLM transcription functions -----------------------------------------------

async def test_transcribe_openai_calls_whisper_1_api():
    config = ChannelConfig(provider="openai", model="gpt-4o", api_key="sk-test")
    audio_bytes = b"fake audio bytes"

    mock_response = "Transcribed text from OpenAI."

    with patch("openai.AsyncOpenAI") as mock_cls:
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.audio.transcriptions.create = AsyncMock(return_value=mock_response)
        result = await transcribe_openai(config, audio_bytes, "meeting.mp3")

    assert result == "Transcribed text from OpenAI."
    _, kwargs = mock_client.audio.transcriptions.create.call_args
    assert kwargs["model"] == "whisper-1"
    assert kwargs["response_format"] == "text"
    assert kwargs.get("timeout") == 120.0


async def test_transcribe_openai_strips_whitespace():
    config = ChannelConfig(provider="openai", model="gpt-4o", api_key="sk-test")

    with patch("openai.AsyncOpenAI") as mock_cls:
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.audio.transcriptions.create = AsyncMock(return_value="  spaced  ")
        result = await transcribe_openai(config, b"bytes", "a.wav")

    assert result == "spaced"


async def test_transcribe_openai_reuses_cached_client_across_calls():
    # Perf fix: passing the same _client_cache dict across two calls with the
    # same config must construct the SDK client only once - proves the cache
    # is real, not just present in the signature.
    config = ChannelConfig(provider="openai", model="gpt-4o", api_key="sk-test")
    cache: dict = {}

    with patch("openai.AsyncOpenAI") as mock_cls:
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.audio.transcriptions.create = AsyncMock(return_value="one")
        await transcribe_openai(config, b"a", "a.mp3", _client_cache=cache)
        mock_client.audio.transcriptions.create = AsyncMock(return_value="two")
        await transcribe_openai(config, b"b", "b.mp3", _client_cache=cache)

    assert mock_cls.call_count == 1


async def test_transcribe_openai_no_cache_still_builds_fresh_client_each_call():
    # Backward-compatibility guarantee: _client_cache=None (the default, and
    # every pre-existing call site) must preserve the original per-call
    # construction behavior exactly.
    config = ChannelConfig(provider="openai", model="gpt-4o", api_key="sk-test")

    with patch("openai.AsyncOpenAI") as mock_cls:
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.audio.transcriptions.create = AsyncMock(return_value="one")
        await transcribe_openai(config, b"a", "a.mp3")
        await transcribe_openai(config, b"b", "b.mp3")

    assert mock_cls.call_count == 2


async def test_transcribe_google_calls_gemini_with_audio_bytes():
    config = ChannelConfig(provider="google", model="gemini-1.5-flash", api_key="goog-test")
    audio_bytes = b"fake audio"

    mock_response = MagicMock()
    mock_response.text = "Google transcript."

    mock_client = MagicMock()
    mock_client.aio.models.generate_content = AsyncMock(return_value=mock_response)

    with patch("google.genai.Client", return_value=mock_client):
        result = await transcribe_google(config, audio_bytes, "clip.m4a")

    assert result == "Google transcript."
    call_args = mock_client.aio.models.generate_content.call_args
    assert call_args.kwargs["model"] == "gemini-1.5-flash"


async def test_cleanup_transcript_anthropic():
    config = ChannelConfig(provider="anthropic", model="claude-3-haiku-20240307", api_key="sk-ant-test")

    mock_response = MagicMock()
    mock_response.content = [MagicMock()]
    mock_response.content[0].text = "Cleaned transcript."

    with patch("anthropic.AsyncAnthropic") as mock_cls:
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.messages.create = AsyncMock(return_value=mock_response)
        result = await cleanup_transcript_llm(config, "um like raw transcript uh")

    assert result == "Cleaned transcript."


async def test_cleanup_transcript_openai():
    config = ChannelConfig(provider="openai", model="gpt-4o-mini", api_key="sk-test")

    mock_response = MagicMock()
    mock_response.choices = [MagicMock()]
    mock_response.choices[0].message.content = "Cleaned by GPT."

    with patch("openai.AsyncOpenAI") as mock_cls:
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.chat.completions.create = AsyncMock(return_value=mock_response)
        result = await cleanup_transcript_llm(config, "raw text")

    assert result == "Cleaned by GPT."


async def test_cleanup_transcript_returns_original_on_error():
    config = ChannelConfig(provider="anthropic", model="claude-3-haiku-20240307", api_key="sk-ant-test")

    with patch("anthropic.AsyncAnthropic") as mock_cls:
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.messages.create = AsyncMock(side_effect=Exception("network error"))
        result = await cleanup_transcript_llm(config, "original transcript")

    assert result == "original transcript"


async def test_cleanup_transcript_xai_uses_xai_base_url():
    """xAI provider must send cleanup request to api.x.ai/v1, not api.openai.com."""
    config = ChannelConfig(provider="xai", model="grok-3", api_key="xai-testkey")

    mock_response = MagicMock()
    mock_response.choices = [MagicMock()]
    mock_response.choices[0].message.content = "Cleaned by Grok."

    with patch("openai.AsyncOpenAI") as mock_cls:
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.chat.completions.create = AsyncMock(return_value=mock_response)
        result = await cleanup_transcript_llm(config, "raw xai text")

    _, kwargs = mock_cls.call_args
    assert kwargs.get("base_url") == "https://api.x.ai/v1", (
        f"Expected xAI base_url but got: {kwargs}"
    )
    assert result == "Cleaned by Grok."


# -- transcribe() dispatch -----------------------------------------------------

async def test_transcribe_dispatch_openai_uses_api_not_local():
    config = ChannelConfig(provider="openai", model="gpt-4o", api_key="sk-test")
    whisper = WhisperManager()
    whisper._model = MagicMock()

    with patch("icx_engine.connectors.audio.transcribe_openai", new=AsyncMock(return_value="api result")) as mock_api:
        result = await transcribe(config, b"audio", "note.mp3", whisper)

    assert result == "api result"
    mock_api.assert_called_once()
    whisper._model.transcribe.assert_not_called()


async def test_transcribe_dispatch_google_uses_gemini_not_local():
    config = ChannelConfig(provider="google", model="gemini-1.5-flash", api_key="goog-test")
    whisper = WhisperManager()
    whisper._model = MagicMock()

    with patch("icx_engine.connectors.audio.transcribe_google", new=AsyncMock(return_value="gemini result")) as mock_api:
        result = await transcribe(config, b"audio", "note.m4a", whisper)

    assert result == "gemini result"
    mock_api.assert_called_once()
    whisper._model.transcribe.assert_not_called()


async def test_transcribe_dispatch_anthropic_uses_local_then_cleanup():
    config = ChannelConfig(provider="anthropic", model="claude-3-haiku-20240307", api_key="sk-ant-test")
    whisper = WhisperManager()

    with patch("icx_engine.connectors.audio._local_transcribe", new=AsyncMock(return_value="raw local")) as mock_local, \
         patch("icx_engine.connectors.audio.cleanup_transcript_llm", new=AsyncMock(return_value="cleaned")) as mock_cleanup:
        result = await transcribe(config, b"audio", "note.ogg", whisper)

    assert result == "cleaned"
    mock_local.assert_called_once()
    mock_cleanup.assert_called_once_with(config, "raw local", _client_cache=None)


async def test_transcribe_dispatch_no_llm_uses_local_only():
    whisper = WhisperManager()

    with patch("icx_engine.connectors.audio._local_transcribe", new=AsyncMock(return_value="local result")) as mock_local, \
         patch("icx_engine.connectors.audio.cleanup_transcript_llm") as mock_cleanup:
        result = await transcribe(None, b"audio", "note.wav", whisper)

    assert result == "local result"
    mock_cleanup.assert_not_called()


async def test_transcribe_dispatch_openai_fallback_to_local_on_error():
    config = ChannelConfig(provider="openai", model="gpt-4o", api_key="sk-test")
    whisper = WhisperManager()

    with patch("icx_engine.connectors.audio.transcribe_openai", new=AsyncMock(side_effect=Exception("api error"))), \
         patch("icx_engine.connectors.audio._local_transcribe", new=AsyncMock(return_value="fallback")) as mock_local:
        result = await transcribe(config, b"audio", "note.mp3", whisper)

    assert result == "fallback"
    mock_local.assert_called_once()


async def test_transcribe_dispatch_google_fallback_to_local_on_error():
    config = ChannelConfig(provider="google", model="gemini-1.5-flash", api_key="goog-test")
    whisper = WhisperManager()

    with patch("icx_engine.connectors.audio.transcribe_google", new=AsyncMock(side_effect=Exception("api error"))), \
         patch("icx_engine.connectors.audio._local_transcribe", new=AsyncMock(return_value="fallback")) as mock_local:
        result = await transcribe(config, b"audio", "note.m4a", whisper)

    assert result == "fallback"
    mock_local.assert_called_once()
