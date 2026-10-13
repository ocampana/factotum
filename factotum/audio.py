"""Audio processing: formats, resampling, VAD and playback buffer.

All internal audio is 16-bit linear PCM, mono, 16 kHz, in 20 ms frames.
The pjsip conference bridge converts from/to the call codec (PCMA/PCMU at
8 kHz or G.722 at 16 kHz), so G.711 is never handled here.
"""

from __future__ import annotations

import io
import sys
import threading
import warnings
import wave
from array import array
from collections import deque

# Standard library up to Python 3.12, audioop-lts package from 3.13 on
with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)
    import audioop

SAMPLE_RATE = 16000
FRAME_MS = 20
SAMPLE_WIDTH = 2
SAMPLES_PER_FRAME = SAMPLE_RATE * FRAME_MS // 1000
FRAME_BYTES = SAMPLES_PER_FRAME * SAMPLE_WIDTH

TTS_SAMPLE_RATE = 24000  # Voxtral TTS "pcm" format: float32 LE, 24 kHz, mono


def float32_to_pcm16(data: bytes) -> bytes:
    """Convert little-endian float32 samples in [-1, 1] to 16-bit PCM."""
    samples = array("f")
    samples.frombytes(data)
    if sys.byteorder != "little":
        samples.byteswap()
    out = array("h", (
        32767 if s >= 1.0 else -32768 if s <= -1.0 else int(s * 32767.0)
        for s in samples
    ))
    if sys.byteorder != "little":
        out.byteswap()
    return out.tobytes()


def pcm16_to_wav(pcm: bytes, sample_rate: int = SAMPLE_RATE) -> bytes:
    """Wrap mono 16-bit PCM into an in-memory WAV file (for STT)."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(SAMPLE_WIDTH)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return buffer.getvalue()


def duration_ms(pcm: bytes, sample_rate: int = SAMPLE_RATE) -> int:
    return len(pcm) * 1000 // (sample_rate * SAMPLE_WIDTH)


class TtsConverter:
    """Convert the TTS stream (float32 24 kHz, chunks of arbitrary length) to
    16-bit PCM at 16 kHz, keeping the resampler state across chunks."""

    def __init__(self, input_rate: int = TTS_SAMPLE_RATE, output_rate: int = SAMPLE_RATE):
        self._from = input_rate
        self._to = output_rate
        self._state = None
        self._leftover = b""

    def convert(self, chunk: bytes) -> bytes:
        data = self._leftover + chunk
        usable = len(data) - len(data) % 4  # one float32 sample = 4 bytes
        self._leftover = data[usable:]
        if not usable:
            return b""
        pcm = float32_to_pcm16(data[:usable])
        if self._from == self._to:
            return pcm
        pcm, self._state = audioop.ratecv(pcm, SAMPLE_WIDTH, 1, self._from, self._to, self._state)
        return pcm


class PlaybackBuffer:
    """Queue of frames to send into the call.

    Filled by the asyncio loop and drained by the pjsip media thread
    (`onFrameRequested`), hence the lock: operations are very short.
    """

    def __init__(self) -> None:
        self._frames: deque[bytes] = deque()
        self._leftover = b""
        self._lock = threading.Lock()

    def push(self, pcm: bytes) -> None:
        with self._lock:
            data = self._leftover + pcm
            complete = len(data) - len(data) % FRAME_BYTES
            for i in range(0, complete, FRAME_BYTES):
                self._frames.append(data[i:i + FRAME_BYTES])
            self._leftover = data[complete:]

    def flush(self) -> None:
        """Pad the last partial frame with silence so that it gets played."""
        with self._lock:
            if self._leftover:
                self._frames.append(self._leftover.ljust(FRAME_BYTES, b"\x00"))
                self._leftover = b""

    def pop(self) -> bytes | None:
        with self._lock:
            return self._frames.popleft() if self._frames else None

    def clear(self) -> None:
        with self._lock:
            self._frames.clear()
            self._leftover = b""

    @property
    def playing(self) -> bool:
        return bool(self._frames)

    @property
    def queued_ms(self) -> int:
        return len(self._frames) * FRAME_MS


class Vad:
    """Energy-based voice activity detector with an adaptive threshold.

    It estimates the background noise while nobody speaks and treats as voice
    the frames whose RMS exceeds `noise * factor`. Returned events:
    - "start": the callee started speaking
    - ("end", pcm): turn finished, with the segment audio (including a short
      pre-roll so that the onset of the first word is not cut)
    - "discarded": noise too short to be a turn
    """

    PRE_ROLL_MS = 300

    def __init__(self, end_of_turn_ms: int = 900, min_speech_ms: int = 250,
                 max_segment_ms: int = 30000, min_threshold: int = 300,
                 noise_factor: float = 3.0, barge_in_factor: float = 2.0):
        self.end_of_turn_frames = max(1, end_of_turn_ms // FRAME_MS)
        self.min_speech_frames = max(1, min_speech_ms // FRAME_MS)
        self.max_segment_frames = max_segment_ms // FRAME_MS
        self.min_threshold = min_threshold
        self.noise_factor = noise_factor
        self.barge_in_factor = barge_in_factor
        self.noise = float(min_threshold) / noise_factor
        self._pre_roll: deque[bytes] = deque(maxlen=self.PRE_ROLL_MS // FRAME_MS)
        self._segment: list[bytes] = []
        self._speaking = False
        self._voice_frames = 0
        self._silence_frames = 0

    @property
    def speaking(self) -> bool:
        return self._speaking

    def threshold(self, agent_speaking: bool = False) -> float:
        base = max(self.min_threshold, self.noise * self.noise_factor)
        return base * self.barge_in_factor if agent_speaking else base

    def process(self, frame: bytes, agent_speaking: bool = False):
        rms = audioop.rms(frame, SAMPLE_WIDTH)
        voice = rms > self.threshold(agent_speaking)

        if not self._speaking:
            if not voice and not agent_speaking:
                # Update the noise estimate only during real silence
                self.noise = 0.95 * self.noise + 0.05 * rms
            self._pre_roll.append(frame)
            if voice:
                self._speaking = True
                self._segment = list(self._pre_roll)
                self._pre_roll.clear()
                self._voice_frames = 1
                self._silence_frames = 0
                return "start"
            return None

        self._segment.append(frame)
        if voice:
            self._voice_frames += 1
            self._silence_frames = 0
        else:
            self._silence_frames += 1

        if self._silence_frames >= self.end_of_turn_frames or len(self._segment) >= self.max_segment_frames:
            valid = self._voice_frames >= self.min_speech_frames
            segment = b"".join(self._segment)
            self.reset()
            return ("end", segment) if valid else "discarded"
        return None

    def reset(self) -> None:
        self._speaking = False
        self._segment = []
        self._voice_frames = 0
        self._silence_frames = 0
        self._pre_roll.clear()
