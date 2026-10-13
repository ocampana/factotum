"""Turn merging: a turn interrupted by new speech is merged with the next one."""

import asyncio
from types import SimpleNamespace

from factotum.config import CallsConfig, VadConfig
from factotum.conversation import Conversation, _Turn
from factotum.scheduler import Call


class FakeServices:
    def __init__(self, stt_delay=0.0, llm_delay=0.0):
        self.cfg = SimpleNamespace(llm_model="llm", stt_model="stt", tts_model="tts")
        self.stt_delay = stt_delay
        self.llm_delay = llm_delay
        self.transcribed = []
        self.chats = []

    async def transcribe(self, pcm, language, costs=None):
        self.transcribed.append(pcm)
        await asyncio.sleep(self.stt_delay)
        return pcm.decode()

    async def chat(self, messages, tools=None, costs=None):
        self.chats.append([m["content"] for m in messages if m["role"] == "user"])
        await asyncio.sleep(self.llm_delay)
        return SimpleNamespace(tool_calls=None, content="Sure.")


class FakeTranscript:
    def __init__(self):
        self.lines = []

    def callee(self, text):
        self.lines.append(("callee", text))

    def system(self, text):
        self.lines.append(("system", text))


def _conversation(services):
    call = Call(user_id="u", number="+390444123456", language="it", goal="test",
                window_start=None, window_end=None)
    user = SimpleNamespace(name="Anna", timezone="Europe/Rome", email="", phone="", id="u", calendars=[])
    conv = Conversation(call, user, sip=None, services=services, calendar=None, transcript=FakeTranscript(),
                        calls_cfg=CallsConfig(), vad_cfg=VadConfig(), system_prompt="{goal}")
    spoken = []

    async def speak(text, wait=True):
        spoken.append(text)
    conv._speak = speak
    return conv, spoken


async def _interrupt_after(conv, delay):
    await asyncio.sleep(delay)
    assert conv._interruptible
    conv._turn_task.cancel()  # what _listen does when the callee resumes speaking


def test_interrupted_during_stt_merges_audio():
    async def scenario():
        services = FakeServices(stt_delay=0.05)
        conv, spoken = _conversation(services)
        await asyncio.gather(conv._process_turn(_Turn(b"yes thursday is fine", 0)),
                             _interrupt_after(conv, 0.01))
        await conv._process_turn(_Turn(b" but after ten", 0))
        return conv, services, spoken

    conv, services, spoken = asyncio.run(scenario())
    # The second STT request contains the audio of both turns
    assert services.transcribed[-1] == b"yes thursday is fine but after ten"
    user_messages = [m["content"] for m in conv.messages if m["role"] == "user"]
    assert user_messages == ["yes thursday is fine but after ten"]
    assert conv.transcript.lines.count(("callee", "yes thursday is fine but after ten")) == 1
    assert spoken == ["Sure."]


def test_interrupted_during_llm_merges_text():
    async def scenario():
        services = FakeServices(llm_delay=0.05)
        conv, spoken = _conversation(services)
        await asyncio.gather(conv._process_turn(_Turn(b"yes thursday is fine", 0)),
                             _interrupt_after(conv, 0.01))
        await conv._process_turn(_Turn(b"but after ten", 0))
        return conv, services, spoken

    conv, services, spoken = asyncio.run(scenario())
    # Only the new audio is transcribed again; the texts are joined
    assert services.transcribed == [b"yes thursday is fine", b"but after ten"]
    assert services.chats[-1] == ["yes thursday is fine but after ten"]
    assert [m["content"] for m in conv.messages if m["role"] == "user"] == ["yes thursday is fine but after ten"]
    assert [l for l in conv.transcript.lines if l[0] == "callee"] == [("callee", "yes thursday is fine but after ten")]
    assert spoken == ["Sure."]


def test_noise_resumes_interrupted_turn():
    async def scenario():
        services = FakeServices(llm_delay=0.05)
        conv, spoken = _conversation(services)
        await asyncio.gather(conv._process_turn(_Turn(b"yes thursday is fine", 0)),
                             _interrupt_after(conv, 0.01))
        # The interruption was noise: _listen queues an empty turn to resume
        await conv._process_turn(_Turn(b"", 0))
        return conv, services, spoken

    conv, services, spoken = asyncio.run(scenario())
    assert services.transcribed == [b"yes thursday is fine"]
    assert [m["content"] for m in conv.messages if m["role"] == "user"] == ["yes thursday is fine"]
    assert spoken == ["Sure."]


def test_not_interruptible_once_committed():
    async def scenario():
        services = FakeServices()
        conv, spoken = _conversation(services)
        await conv._process_turn(_Turn(b"hello", 0))
        return conv

    conv = asyncio.run(scenario())
    assert not conv._interruptible and not conv._has_carry()
