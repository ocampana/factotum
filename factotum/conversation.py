"""Conversation loop of a phone call.

    incoming audio -> VAD -> Voxtral STT -> mistral-small (+ tools) -> Voxtral TTS -> outgoing audio

Structure:
- a "listen" task reads frames from the call, feeds the VAD and queues the
  callee's completed turns; it also handles barge-in (if the callee talks
  over the agent, playback stops);
- the main task processes one turn at a time and watches for silence,
  maximum duration and remote hangup.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .audio import FRAME_MS, Vad
from .calendars import (Appointment, CalendarBackend, CalendarError, CallContext, build_event,
                        parse_datetime)
from .config import CallsConfig, VadConfig
from .languages import LANGUAGES, dial_string, phrase
from .mistral_client import MistralServiceError, MistralServices
from .scheduler import Call
from .sip import SIP_OUTCOMES, SipCall, SipEngine
from .transcripts import Transcript
from .users import User

log = logging.getLogger(__name__)

MAX_API_ERRORS = 3
MAX_TOOL_STEPS = 4
FIRST_GREETING_WAIT_S = 2.5

FINAL_OUTCOMES = ["appointment_booked", "goal_achieved", "not_interested",
                  "call_back", "wrong_number", "other"]


@dataclass
class _Turn:
    pcm: bytes
    received: float


def _clean_for_speech(text: str) -> str:
    """Strip formatting that TTS would read badly."""
    text = re.sub(r"[*_#`>\[\]]", "", text)
    return re.sub(r"\s+", " ", text).strip()


class Conversation:
    def __init__(self, call: Call, user: User, sip: SipEngine, services: MistralServices,
                 calendar: CalendarBackend | None, transcript: Transcript,
                 calls_cfg: CallsConfig, vad_cfg: VadConfig, system_prompt: str):
        self.call = call
        self.user = user
        self.sip = sip
        self.services = services
        self.calendar = calendar
        self.transcript = transcript
        self.cfg = calls_cfg
        self.vad = Vad(**vars(vad_cfg))
        self.language = call.language
        self.tz = user.timezone
        self.messages: list[dict[str, Any]] = [{"role": "system", "content": self._prompt(system_prompt)}]
        self.sip_call: SipCall | None = None
        self.turns: asyncio.Queue[_Turn] = asyncio.Queue()
        self.api_errors = 0
        self.closing = False
        self.last_heard = time.monotonic()
        self.prompted = False
        self._speak_task: asyncio.Task | None = None

    # ------------------------------------------------------------------ prompt and tools

    def _prompt(self, template: str) -> str:
        now = datetime.now(ZoneInfo(self.tz))
        values = {
            "user_name": self.user.name,
            "user_email": self.user.email or "not available",
            "user_phone": self.user.phone or "not available",
            "goal": self.call.goal,
            "language": LANGUAGES.get(self.language, "English"),
            "summary_language": self.cfg.summary_language,
            "now": now.strftime("%A %Y-%m-%d %H:%M"),
            "timezone": self.tz,
            "number": self.call.number,
            "calendar_available": "yes" if (self.calendar and self.call.calendars) else "no",
        }
        try:
            return template.format(**values)
        except (KeyError, IndexError, ValueError):
            log.warning("System prompt has invalid placeholders, using a minimal one")
            return (f"You are the phone assistant of {values['user_name']}. Speak only {values['language']}. "
                    f"Goal: {values['goal']}. Now: {values['now']} ({values['timezone']}).")

    def _tools(self) -> list[dict]:
        tools = [{
            "type": "function",
            "function": {
                "name": "end_call",
                "description": "Ends the call after saying goodbye. Use it when the conversation is over.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "outcome": {"type": "string", "enum": FINAL_OUTCOMES},
                        "summary": {"type": "string",
                                    "description": f"Summary of the call, in {self.cfg.summary_language}"},
                        "contact": {"type": "string", "description": "Name of the person who answered, if known"},
                        "goodbye": {"type": "string", "description": "Final goodbye sentence, in the call language"},
                    },
                    "required": ["outcome", "summary", "goodbye"],
                },
            },
        }]
        if self.calendar and self.call.calendars:
            tools += [{
                "type": "function",
                "function": {
                    "name": "check_availability",
                    "description": f"Checks whether {self.user.name} is free in a time range. ISO 8601 dates.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "start": {"type": "string", "description": "e.g. 2026-10-15T10:00:00"},
                            "end": {"type": "string", "description": "e.g. 2026-10-15T11:00:00"},
                        },
                        "required": ["start", "end"],
                    },
                },
            }, {
                "type": "function",
                "function": {
                    "name": "book_appointment",
                    "description": ("Saves to the calendar an appointment the callee confirmed verbally. "
                                    "Before using it, ask the callee's name and read back date, time and place."),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string",
                                      "description": f"Subject of the appointment, in {self.cfg.summary_language}"},
                            "start": {"type": "string", "description": "ISO 8601"},
                            "end": {"type": "string", "description": "ISO 8601"},
                            "contact_name": {"type": "string"},
                            "contact_role": {"type": "string", "description": "Role or company"},
                            "location": {"type": "string"},
                            "notes": {"type": "string",
                                      "description": "Useful details: costs, what to bring, references"},
                        },
                        "required": ["title", "start", "end", "contact_name"],
                    },
                },
            }]
        return tools

    # ------------------------------------------------------------------ execution

    async def run(self) -> None:
        """Run the whole call; update outcome and summary of the `Call`."""
        c = self.call
        # Service phrases are prepared while the phone is ringing
        phrases_task = asyncio.create_task(self.services.prepare_phrases(self.language))
        listen_task: asyncio.Task | None = None
        try:
            number = dial_string(c.number, self.sip.cfg.plus_replacement)
            self.transcript.system(f"Calling {c.number} (dialed {number}), language {self.language}")
            try:
                self.sip_call = await self.sip.dial(number)
            except Exception as e:  # noqa: BLE001
                c.outcome, c.error = "sip_error", str(e)
                self.transcript.system(f"Cannot start the call: {e}")
                return

            if not await self._wait_for_answer():
                return
            c.answered = True
            self.transcript.system("The callee answered")
            listen_task = asyncio.create_task(self._listen())
            await self._converse()
        except asyncio.CancelledError:
            self.transcript.system("Call interrupted")
            raise
        finally:
            phrases_task.cancel()
            if listen_task:
                listen_task.cancel()
            if self._speak_task:
                self._speak_task.cancel()
            if self.sip_call and not self.sip_call.disconnected:
                try:
                    await asyncio.shield(self.sip.hangup(self.sip_call))
                except Exception:  # noqa: BLE001
                    log.exception("Error while hanging up")
            self.transcript.system(f"Call ended. Outcome: {c.outcome or 'n/a'}")

    async def _wait_for_answer(self) -> bool:
        """Wait for the CONFIRMED state; handle unanswered and rejected calls."""
        c = self.call
        deadline = time.monotonic() + self.cfg.invite_timeout_s
        media = confirmed = False
        while not (media and confirmed):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                c.outcome = "no answer"
                self.transcript.system("No answer within the maximum ringing time")
                await self.sip.hangup(self.sip_call)
                return False
            try:
                event = await asyncio.wait_for(self.sip_call.events.get(), remaining)
            except asyncio.TimeoutError:
                continue
            if event.kind == "confirmed":
                confirmed = True
            elif event.kind == "media":
                media = True
            elif event.kind == "disconnected":
                c.outcome = SIP_OUTCOMES.get(event.code, f"ended ({event.code} {event.reason})")
                self.transcript.system(f"Call failed: {event.code} {event.reason}")
                return False
        return True

    async def _listen(self) -> None:
        """Read frames, run the VAD and handle barge-in."""
        playback = self.sip_call.playback
        while True:
            frame = await self.sip_call.frames.get()
            agent_speaking = playback.playing
            event = self.vad.process(frame, agent_speaking=agent_speaking)
            if event == "start":
                self.last_heard = time.monotonic()
                if agent_speaking and not self.closing:
                    log.debug("Barge-in: stopping playback")
                    playback.clear()
                    if self._speak_task:
                        self._speak_task.cancel()
            elif isinstance(event, tuple):
                self.last_heard = time.monotonic()
                await self.turns.put(_Turn(event[1], time.monotonic()))

    async def _converse(self) -> None:
        c = self.call
        started = time.monotonic()
        # Whoever answers usually says "Hello?": wait for it briefly,
        # otherwise the agent opens the conversation.
        try:
            turn = await asyncio.wait_for(self.turns.get(), FIRST_GREETING_WAIT_S)
            await self._handle_turn(turn)
        except asyncio.TimeoutError:
            await self._respond("(The callee answered but has not spoken yet. Introduce yourself.)")

        while not self.closing:
            if time.monotonic() - started > self.cfg.max_duration_s:
                c.outcome = c.outcome or "maximum duration reached"
                await self._say_phrase("time_up")
                break
            if self._remote_hung_up():
                c.outcome = c.outcome or "hung up by the callee"
                return
            try:
                turn = await asyncio.wait_for(self.turns.get(), 0.25)
            except asyncio.TimeoutError:
                await self._check_silence()
                continue
            await self._handle_turn(turn)

        await self._wait_playback_finished()

    def _remote_hung_up(self) -> bool:
        if self.sip_call.disconnected:
            return True
        while not self.sip_call.events.empty():
            if self.sip_call.events.get_nowait().kind == "disconnected":
                return True
        return False

    async def _check_silence(self) -> None:
        if self.sip_call.playback.playing or self.vad.speaking or not self.turns.empty():
            self.last_heard = time.monotonic()
            return
        if self._speak_task and not self._speak_task.done():
            return
        if time.monotonic() - self.last_heard < self.cfg.max_silence_s:
            return
        if not self.prompted:
            self.prompted = True
            self.transcript.system("Long silence, prompting the callee")
            await self._say_phrase("prompt")
            self.last_heard = time.monotonic()
        else:
            self.call.outcome = self.call.outcome or "silence"
            self.transcript.system("No answer after prompting, closing")
            self.closing = True

    async def _handle_turn(self, turn: _Turn) -> None:
        # Merge turns that arrived while the previous one was being processed
        pcm = turn.pcm
        while not self.turns.empty():
            pcm += self.turns.get_nowait().pcm
        try:
            text = await self.services.transcribe(pcm, self.language)
            self.api_errors = 0
        except MistralServiceError as e:
            await self._api_error("STT", e)
            return
        if not text:
            return
        self.prompted = False
        log.info("Callee: %s", text)
        self.transcript.callee(text)
        await self._respond(text)

    async def _respond(self, user_text: str) -> None:
        """Ask the LLM for an answer (running tools) and speak it."""
        self.messages.append({"role": "user", "content": user_text})
        for _ in range(MAX_TOOL_STEPS):
            try:
                message = await self.services.chat(self.messages, self._tools())
                self.api_errors = 0
            except MistralServiceError as e:
                await self._api_error("LLM", e)
                return

            tool_calls = message.tool_calls or []
            content = message.content if isinstance(message.content, str) else ""
            if not tool_calls:
                self.messages.append({"role": "assistant", "content": content})
                await self._speak(content)
                return

            self.messages.append({
                "role": "assistant",
                "content": content,
                "tool_calls": [{
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments
                                 if isinstance(tc.function.arguments, str) else json.dumps(tc.function.arguments)},
                } for tc in tool_calls],
            })
            if content.strip():
                # E.g. "One moment, let me check": said while the tool runs
                await self._speak(content, wait=False)
            for tc in tool_calls:
                result = await self._run_tool(tc.function.name, tc.function.arguments)
                self.messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "name": tc.function.name,
                    "content": json.dumps(result, ensure_ascii=False),
                })
            if self.closing:
                return
        log.warning("Too many consecutive tool steps, waiting for the next turn")

    async def _run_tool(self, name: str, raw_arguments: Any) -> dict:
        try:
            arguments = json.loads(raw_arguments) if isinstance(raw_arguments, str) else dict(raw_arguments or {})
        except json.JSONDecodeError:
            return {"error": "invalid arguments"}
        try:
            if name == "end_call":
                result = await self._end_call(arguments)
            elif name == "check_availability":
                result = await self._check_availability(arguments)
            elif name == "book_appointment":
                result = await self._book_appointment(arguments)
            else:
                result = {"error": f"unknown tool: {name}"}
        except (ValueError, KeyError) as e:
            result = {"error": f"invalid data: {e}"}
        self.transcript.tool(name, arguments, result)
        return result

    async def _end_call(self, arguments: dict) -> dict:
        c = self.call
        c.outcome = str(arguments.get("outcome", "other"))
        c.summary = str(arguments.get("summary", ""))
        if arguments.get("contact"):
            c.contact = str(arguments["contact"])
        goodbye = str(arguments.get("goodbye", "")).strip()
        if goodbye:
            self.messages.append({"role": "assistant", "content": goodbye})
            await self._speak(goodbye)
        self.closing = True
        return {"ok": True}

    async def _check_availability(self, arguments: dict) -> dict:
        start = parse_datetime(arguments["start"], self.tz)
        end = parse_datetime(arguments["end"], self.tz)
        if end <= start:
            end = start + timedelta(hours=1)
        calendars = [cal.id for cal in self.user.calendars] or self.call.calendars
        try:
            busy = await self.calendar.busy(calendars, start, end)
        except CalendarError as e:
            log.warning("Availability check failed: %s", e)
            return {"error": "calendar unreachable, do not promise certain availability"}
        return {"free": not busy, "busy": [{"start": a, "end": b} for a, b in busy]}

    async def _book_appointment(self, arguments: dict) -> dict:
        c = self.call
        appt = Appointment(
            title=str(arguments["title"]),
            start=parse_datetime(arguments["start"], self.tz),
            end=parse_datetime(arguments["end"], self.tz),
            contact_name=str(arguments.get("contact_name", "")).strip() or "not given",
            contact_role=str(arguments.get("contact_role", "")),
            location=str(arguments.get("location", "")),
            notes=str(arguments.get("notes", "")),
        )
        if appt.end <= appt.start:
            appt.end = appt.start + timedelta(hours=1)
        c.contact = c.contact or appt.contact_name
        ctx = CallContext(
            call_id=c.id, number=c.number, goal=c.goal,
            call_time=datetime.now(ZoneInfo(self.tz)), user_name=self.user.name,
            user_id=self.user.id, transcript_excerpt=self.transcript.excerpt(),
            transcript_file=self.transcript.txt_path.name,
        )
        event = build_event(appt, ctx, self.tz)
        created, failed = [], []
        for cal_id in c.calendars:
            try:
                created.append(await self.calendar.create_event(cal_id, event))
            except CalendarError as e:
                log.warning("Event creation on %s failed: %s", cal_id, e)
                failed.append({"calendar": cal_id, "event": event, "error": str(e)})
        c.events += created
        c.failed_events += failed
        if created:
            return {"ok": True, "saved_to": len(created)}
        return {"ok": False, "error": "could not save it now: say that the confirmation will follow later"}

    # ------------------------------------------------------------------ voice

    async def _speak(self, text: str, wait: bool = True) -> None:
        text = _clean_for_speech(text)
        if not text:
            return
        log.info("Agent: %s", text)
        self.transcript.agent(text)
        if self._speak_task and not self._speak_task.done():
            await asyncio.wait({self._speak_task})
        self._speak_task = asyncio.create_task(self._synthesize_and_play(text))
        if wait:
            await asyncio.wait({self._speak_task})

    async def _synthesize_and_play(self, text: str) -> None:
        playback = self.sip_call.playback
        try:
            async for pcm in self.services.synthesize(text, self.language):
                playback.push(pcm)
            playback.flush()
            self.api_errors = 0
        except asyncio.CancelledError:
            raise
        except MistralServiceError as e:
            # We are inside the playback task: no waiting, cached audio only
            await self._api_error("TTS", e, wait=False)

    async def _say_phrase(self, key: str, wait: bool = True) -> None:
        """Speak a service phrase, using the pre-synthesized audio if available."""
        text = phrase(key, self.language)
        self.transcript.agent(text)
        pcm = self.services.cached_phrase(key, self.language)
        if pcm is None and wait:
            try:
                pcm = await self.services.synthesize_all(text, self.language)
            except MistralServiceError as e:
                log.warning("Service phrase %r cannot be spoken: %s", key, e)
        if pcm:
            self.sip_call.playback.push(pcm)
            self.sip_call.playback.flush()
            if wait:
                await self._wait_playback_drained()

    async def _wait_playback_finished(self, max_s: float = 20.0) -> None:
        """Wait for the ongoing synthesis to finish and then for playback."""
        if self._speak_task and not self._speak_task.done():
            try:
                await asyncio.wait_for(asyncio.shield(self._speak_task), max_s)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
        await self._wait_playback_drained(max_s)

    async def _wait_playback_drained(self, max_s: float = 20.0) -> None:
        deadline = time.monotonic() + max_s
        while self.sip_call.playback.playing and not self.sip_call.disconnected:
            if time.monotonic() > deadline:
                break
            await asyncio.sleep(FRAME_MS / 1000 * 5)
        await asyncio.sleep(0.3)  # tail of the remote jitter buffer

    async def _api_error(self, service: str, error: Exception, wait: bool = True) -> None:
        self.api_errors += 1
        log.warning("%s error (%d/%d): %s", service, self.api_errors, MAX_API_ERRORS, error)
        self.transcript.system(f"{service} error: {error}")
        if self.api_errors >= MAX_API_ERRORS:
            self.call.outcome = "api_error"
            self.call.error = f"{service}: {error}"
            await self._say_phrase("technical_problem", wait=wait)
            self.closing = True
        else:
            await self._say_phrase("repeat", wait=wait)
