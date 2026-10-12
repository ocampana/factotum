import math
import struct

from factotum.audio import (FRAME_BYTES, SAMPLES_PER_FRAME, PlaybackBuffer, TtsConverter, Vad,
                            float32_to_pcm16, pcm16_to_wav)


def _tone(amplitude: int, frames: int = 1) -> bytes:
    n = SAMPLES_PER_FRAME * frames
    return struct.pack(f"<{n}h", *(int(amplitude * math.sin(2 * math.pi * 440 * i / 16000)) for i in range(n)))


SILENCE = b"\x00" * FRAME_BYTES


def test_float32_to_pcm16_clipping():
    data = struct.pack("<4f", 0.0, 0.5, 1.5, -2.0)
    assert struct.unpack("<4h", float32_to_pcm16(data)) == (0, 16383, 32767, -32768)


def test_tts_converter_split_chunks():
    # 24 kHz -> 16 kHz: 2400 samples become ~1600; chunks not aligned to 4 bytes lose no data
    data = struct.pack("<2400f", *([0.1] * 2400))
    conv = TtsConverter()
    out = conv.convert(data[:1001]) + conv.convert(data[1001:])
    assert abs(len(out) // 2 - 1600) <= 2


def test_wav():
    wav = pcm16_to_wav(SILENCE)
    assert wav[:4] == b"RIFF" and wav[8:12] == b"WAVE"


def test_playback_buffer_frames_and_tail():
    b = PlaybackBuffer()
    b.push(b"\x01" * (FRAME_BYTES + 10))
    assert b.queued_ms == 20
    b.flush()
    assert b.queued_ms == 40
    assert len(b.pop()) == FRAME_BYTES
    last = b.pop()
    assert len(last) == FRAME_BYTES and last.endswith(b"\x00")
    assert b.pop() is None
    b.push(b"\x01" * FRAME_BYTES * 3)
    b.clear()
    assert not b.playing


def test_vad_full_turn():
    vad = Vad(end_of_turn_ms=200, min_speech_ms=100)
    for _ in range(20):
        assert vad.process(SILENCE) is None
    events = [vad.process(_tone(8000)) for _ in range(15)]
    assert events[0] == "start"
    events = [vad.process(SILENCE) for _ in range(10)]
    ends = [e for e in events if isinstance(e, tuple)]
    assert len(ends) == 1
    # pre-roll + speech + trailing silence
    assert len(ends[0][1]) >= 15 * FRAME_BYTES


def test_vad_discards_short_noises():
    vad = Vad(end_of_turn_ms=200, min_speech_ms=200)
    assert vad.process(_tone(8000)) == "start"
    events = [vad.process(SILENCE) for _ in range(10)]
    assert "discarded" in events


def test_vad_higher_threshold_while_agent_speaks():
    vad = Vad(min_threshold=300, barge_in_factor=2.0)
    assert vad.threshold(agent_speaking=True) == 2 * vad.threshold()
