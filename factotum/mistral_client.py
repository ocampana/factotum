"""Access to the Mistral APIs: Voxtral STT, chat (with tools) and Voxtral TTS.

Every request has an explicit timeout and a limited number of retries with
backoff, only for transient errors (timeouts, network, 429, 5xx).
"""

from __future__ import annotations

import asyncio
import base64
import logging
import random
from typing import Any, AsyncIterator, Awaitable, Callable, TypeVar

from mistralai.client import Mistral, errors

from .audio import SAMPLE_RATE, TtsConverter, pcm16_to_wav
from .config import MistralConfig
from .languages import PHRASES, phrase

log = logging.getLogger(__name__)

T = TypeVar("T")


class MistralServiceError(Exception):
    """Definitive Mistral API error (after any retries)."""


def _is_transient(e: BaseException) -> bool:
    if isinstance(e, (asyncio.TimeoutError, errors.NoResponseError, OSError)):
        return True
    if isinstance(e, errors.MistralError):
        return e.status_code == 429 or e.status_code >= 500
    # Network errors of the underlying HTTP client
    return type(e).__module__.split(".")[0] in ("httpx", "httpx2", "httpcore")


class MistralServices:
    def __init__(self, cfg: MistralConfig):
        self.cfg = cfg
        self.client = Mistral(api_key=cfg.api_key)
        self.voices: dict[str, str] = dict(cfg.tts_voices)
        self._phrases: dict[tuple[str, str, str | None], bytes] = {}

    async def _with_retries(self, name: str, factory: Callable[[], Awaitable[T]], timeout: float) -> T:
        last: BaseException | None = None
        for attempt in range(self.cfg.retries + 1):
            try:
                return await asyncio.wait_for(factory(), timeout)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                last = e
                if not _is_transient(e) or attempt == self.cfg.retries:
                    break
                delay = 0.4 * (2 ** attempt) + random.uniform(0, 0.2)
                log.warning("%s: transient error (%s), retrying in %.1fs", name, e, delay)
                await asyncio.sleep(delay)
        raise MistralServiceError(f"{name}: {type(last).__name__}: {last}") from last

    # ------------------------------------------------------------------ TTS voices

    async def load_voices(self) -> None:
        """Assign a voice to each language unless configured explicitly.

        Custom (cloned) voices take precedence over presets."""
        try:
            response = await self._with_retries(
                "voice list",
                lambda: self.client.audio.voices.list_async(limit=100, timeout_ms=10000),
                15,
            )
        except MistralServiceError as e:
            log.warning("Cannot list TTS voices: %s", e)
            return
        voices = sorted(response.items, key=lambda v: 0 if str(v.type) != "preset" else 1)
        for voice in voices:
            log.info("Available TTS voice: %s (%s) languages=%s type=%s",
                     voice.name, voice.id, ",".join(voice.languages or []), voice.type)
            for language in voice.languages or []:
                self.voices.setdefault(language.split("-")[0].split("_")[0].lower(), voice.id)
        if not self.cfg.tts_voice and voices:
            self.cfg.tts_voice = voices[0].id
        log.info("Voices per language: %s (fallback: %s)", self.voices, self.cfg.tts_voice or "API default")

    def voice_for(self, language: str) -> str | None:
        return self.voices.get(language) or self.cfg.tts_voice or None

    # ------------------------------------------------------------------ STT

    async def transcribe(self, pcm: bytes, language: str) -> str:
        wav = pcm16_to_wav(pcm, SAMPLE_RATE)
        response = await self._with_retries(
            "transcription",
            lambda: self.client.audio.transcriptions.complete_async(
                model=self.cfg.stt_model,
                file={"file_name": "turn.wav", "content": wav, "content_type": "audio/wav"},
                language=language,
                timeout_ms=int(self.cfg.stt_timeout_s * 1000),
            ),
            self.cfg.stt_timeout_s + 1,
        )
        return (response.text or "").strip()

    # ------------------------------------------------------------------ LLM

    async def chat(self, messages: list[dict[str, Any]], tools: list[dict] | None = None):
        """Return the assistant message (text and/or tool calls)."""
        kwargs: dict[str, Any] = {
            "model": self.cfg.llm_model,
            "messages": messages,
            "temperature": 0.4,
            "max_tokens": 300,
            "timeout_ms": int(self.cfg.llm_timeout_s * 1000),
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        response = await self._with_retries(
            "chat",
            lambda: self.client.chat.complete_async(**kwargs),
            self.cfg.llm_timeout_s + 1,
        )
        return response.choices[0].message

    # ------------------------------------------------------------------ TTS

    async def synthesize(self, text: str, language: str) -> AsyncIterator[bytes]:
        """Stream 16-bit 16 kHz PCM, chunk by chunk.

        The timeout covers opening the stream and each chunk; retries only
        happen before any audio is produced (so no words are repeated)."""
        voice = self.voice_for(language)
        kwargs: dict[str, Any] = {
            "model": self.cfg.tts_model,
            "input": text,
            "response_format": "pcm",
            "stream": True,
            "timeout_ms": int(self.cfg.tts_timeout_s * 1000),
        }
        if voice:
            kwargs["voice_id"] = voice

        stream = await self._with_retries(
            "speech synthesis",
            lambda: self.client.audio.speech.complete_async(**kwargs),
            self.cfg.tts_timeout_s,
        )
        converter = TtsConverter()
        async with stream as events:
            iterator = events.__aiter__()
            while True:
                try:
                    event = await asyncio.wait_for(iterator.__anext__(), self.cfg.tts_timeout_s)
                except StopAsyncIteration:
                    break
                except asyncio.TimeoutError as e:
                    raise MistralServiceError("speech synthesis: stream stalled (timeout)") from e
                data = getattr(event.data, "audio_data", None)
                if data:
                    pcm = converter.convert(base64.b64decode(data))
                    if pcm:
                        yield pcm

    async def synthesize_all(self, text: str, language: str) -> bytes:
        return b"".join([pcm async for pcm in self.synthesize(text, language)])

    # ------------------------------------------------------------------ service phrases

    def cached_phrase(self, key: str, language: str) -> bytes | None:
        return self._phrases.get((key, language, self.voice_for(language)))

    async def prepare_phrases(self, language: str) -> None:
        """Pre-synthesize (once per language and voice) the phrases used when
        the APIs do not respond, so that the callee is never left in silence."""
        for key in PHRASES:
            if self.cached_phrase(key, language) is not None:
                continue
            try:
                pcm = await self.synthesize_all(phrase(key, language), language)
                self._phrases[(key, language, self.voice_for(language))] = pcm
            except MistralServiceError as e:
                log.warning("Service phrase %r (%s) not available: %s", key, language, e)
