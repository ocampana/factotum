"""SIP/RTP stack based on pjsua2 (pjsip, compiled C).

pjsip must not be called from unregistered threads, so every library call
happens in a single dedicated "SIP thread", which runs commands sent by the
asyncio loop through a queue and forwards events (registration state, call
state) back to the loop with `call_soon_threadsafe`.

Audio goes through an `AudioMediaPort` attached to the conference bridge: the
pjsip media thread calls `onFrameReceived` / `onFrameRequested` every 20 ms
with 16-bit PCM at 16 kHz; codecs, jitter buffer and RTP stay in C.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import queue
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

from .audio import FRAME_BYTES, FRAME_MS, SAMPLE_RATE, PlaybackBuffer
from .config import SipConfig

try:
    import pjsua2 as pj
except ImportError:  # allows tests and development without pjsua2 installed
    pj = None

log = logging.getLogger(__name__)

# Final SIP status codes -> readable outcome
SIP_OUTCOMES = {
    404: "number not found",
    408: "no answer",
    480: "unavailable",
    486: "busy",
    487: "cancelled",
    600: "busy",
    603: "declined",
}


@dataclass
class CallEvent:
    kind: str  # "ringing" | "confirmed" | "media" | "disconnected"
    code: int = 0
    reason: str = ""


@dataclass
class SipCall:
    """asyncio side of a call: frame and event queues."""

    id: int
    uri: str
    events: asyncio.Queue = field(default_factory=asyncio.Queue)
    frames: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=500))
    playback: PlaybackBuffer = field(default_factory=PlaybackBuffer)
    disconnected: bool = False
    final_code: int = 0
    final_reason: str = ""


class SipError(Exception):
    pass


if pj is not None:

    class _LogWriter(pj.LogWriter):
        """Forward pjsip logs to Python logging."""

        def write(self, entry):
            level = logging.DEBUG if entry.level >= 4 else logging.INFO if entry.level >= 3 else logging.WARNING
            log.log(level, "pjsip: %s", entry.msg.rstrip())

    class _MediaPort(pj.AudioMediaPort):
        """Conference bridge audio port attached to one call."""

        def __init__(self, on_frame: Callable[[bytes], None], playback: PlaybackBuffer):
            super().__init__()
            self._on_frame = on_frame
            self._playback = playback

        def onFrameRequested(self, frame):
            data = self._playback.pop()
            if data:
                frame.type = pj.PJMEDIA_FRAME_TYPE_AUDIO
                frame.buf.assign_from_bytes(data)
                frame.size = len(data)

        def onFrameReceived(self, frame):
            size = frame.buf.size()
            if size != FRAME_BYTES:
                return
            data = bytearray(size)
            frame.buf.copy_to_bytearray(data)
            self._on_frame(bytes(data))

    class _Account(pj.Account):
        def __init__(self, engine: "SipEngine"):
            super().__init__()
            self._engine = engine

        def onRegState(self, prm):
            info = self.getInfo()
            self._engine._update_registration(info.regIsActive, info.regStatus, info.regStatusText)

    class _Call(pj.Call):
        def __init__(self, engine: "SipEngine", account, call: SipCall):
            super().__init__(account)
            self._engine = engine
            self.call = call
            self.port: _MediaPort | None = None

        def onCallState(self, prm):
            ci = self.getInfo()
            if ci.state == pj.PJSIP_INV_STATE_EARLY:
                self._engine._emit(self.call, CallEvent("ringing"))
            elif ci.state == pj.PJSIP_INV_STATE_CONFIRMED:
                self._engine._emit(self.call, CallEvent("confirmed"))
            elif ci.state == pj.PJSIP_INV_STATE_DISCONNECTED:
                self._engine._call_disconnected(self, ci.lastStatusCode, ci.lastReason)

        def onCallMediaState(self, prm):
            ci = self.getInfo()
            for i, mi in enumerate(ci.media):
                if mi.type != pj.PJMEDIA_TYPE_AUDIO or mi.status != pj.PJSUA_CALL_MEDIA_ACTIVE:
                    continue
                media = self.getAudioMedia(i)
                if self.port is None:
                    fmt = pj.MediaFormatAudio()
                    fmt.init(pj.PJMEDIA_FORMAT_PCM, SAMPLE_RATE, 1, FRAME_MS * 1000, 16)
                    self.port = _MediaPort(self._engine._frame_forwarder(self.call), self.call.playback)
                    self.port.createPort(f"factotum-{self.call.id}", fmt)
                media.startTransmit(self.port)
                self.port.startTransmit(media)
                self._engine._emit(self.call, CallEvent("media"))


class SipEngine:
    """Owns the pjsua2 endpoint, registration and calls in a dedicated thread."""

    def __init__(self, cfg: SipConfig, loop: asyncio.AbstractEventLoop):
        if pj is None:
            raise SipError("pjsua2 is not installed: use the Docker image or build pjproject")
        self.cfg = cfg
        self.loop = loop
        self.registered = False
        self.registration_status = "not started"
        self._commands: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._running = threading.Event()
        self._ready: asyncio.Future | None = None
        self._calls: dict[int, Any] = {}
        self._to_delete: list[Any] = []
        self._counter = itertools.count(1)
        self._ep = None
        self._account = None
        self._log_writer = None

    # ------------------------------------------------------------------ asyncio API

    async def start(self) -> None:
        self._ready = self.loop.create_future()
        self._running.set()
        self._thread = threading.Thread(target=self._run, name="sip", daemon=True)
        self._thread.start()
        await self._ready

    async def stop(self) -> None:
        if not self._thread:
            return
        self._running.clear()
        await asyncio.to_thread(self._thread.join, 10)

    async def dial(self, number: str) -> SipCall:
        """Start an outgoing call to `number` (already in dialable form)."""
        uri = f"sip:{number}@{self.cfg.sip_domain}"
        if self.cfg.transport != "udp":
            uri += f";transport={self.cfg.transport}"
        call = SipCall(id=next(self._counter), uri=uri)
        await self._submit(self._cmd_dial, call)
        return call

    async def hangup(self, call: SipCall) -> None:
        if not call.disconnected:
            await self._submit(self._cmd_hangup, call.id)

    # ------------------------------------------------------------------ bridge to asyncio

    async def _submit(self, function: Callable, *args):
        future = self.loop.create_future()
        self._commands.put((function, args, future))
        return await future

    def _resolve(self, future: asyncio.Future, result=None, error: BaseException | None = None):
        def apply():
            if future.done():
                return
            if error is not None:
                future.set_exception(error)
            else:
                future.set_result(result)
        self.loop.call_soon_threadsafe(apply)

    def _emit(self, call: SipCall, event: CallEvent) -> None:
        self.loop.call_soon_threadsafe(call.events.put_nowait, event)

    def _frame_forwarder(self, call: SipCall) -> Callable[[bytes], None]:
        def put(frame: bytes):
            try:
                call.frames.put_nowait(frame)
            except asyncio.QueueFull:
                pass  # the consumer is lagging: better lose audio than memory

        def forward(frame: bytes):
            self.loop.call_soon_threadsafe(put, frame)
        return forward

    def _update_registration(self, active: bool, code: int, text: str) -> None:
        self.registered = bool(active) and code // 100 == 2
        self.registration_status = f"{code} {text}"
        if self.registered:
            log.info("Registered on the SIP server (%s)", self.registration_status)
        else:
            log.warning("SIP registration not active: %s (retrying in %ss)",
                        self.registration_status, self.cfg.register_retry_s)

    # ------------------------------------------------------------------ SIP thread

    def _run(self) -> None:
        try:
            self._initialize()
        except Exception as e:  # noqa: BLE001
            log.exception("pjsua2 startup failed")
            self._resolve(self._ready, error=SipError(str(e)))
            return
        self._resolve(self._ready)

        while self._running.is_set():
            self._process_commands()
            try:
                self._ep.libHandleEvents(FRAME_MS)
            except Exception:  # noqa: BLE001
                log.exception("Error in the pjsip loop")
            # Call objects are deleted outside of their own callbacks
            self._to_delete.clear()

        self._shutdown()

    def _process_commands(self) -> None:
        while True:
            try:
                function, args, future = self._commands.get_nowait()
            except queue.Empty:
                return
            try:
                self._resolve(future, function(*args))
            except Exception as e:  # noqa: BLE001
                self._resolve(future, error=SipError(str(e)))

    def _initialize(self) -> None:
        cfg = self.cfg
        self._ep = pj.Endpoint()
        self._ep.libCreate()

        ep_cfg = pj.EpConfig()
        ep_cfg.uaConfig.threadCnt = 0
        ep_cfg.uaConfig.mainThreadOnly = False
        ep_cfg.uaConfig.userAgent = cfg.user_agent
        if cfg.stun:
            ep_cfg.uaConfig.stunServer.append(cfg.stun)
        self._log_writer = _LogWriter()
        ep_cfg.logConfig.writer = self._log_writer
        ep_cfg.logConfig.level = 4 if log.isEnabledFor(logging.DEBUG) else 3
        ep_cfg.logConfig.consoleLevel = 0
        ep_cfg.logConfig.decor &= ~(pj.PJ_LOG_HAS_CR | pj.PJ_LOG_HAS_NEWLINE)
        ep_cfg.medConfig.clockRate = SAMPLE_RATE
        ep_cfg.medConfig.sndClockRate = SAMPLE_RATE
        ep_cfg.medConfig.channelCount = 1
        ep_cfg.medConfig.audioFramePtime = FRAME_MS
        ep_cfg.medConfig.ecTailLen = 0
        ep_cfg.medConfig.noVad = True
        self._ep.libInit(ep_cfg)

        transport = {
            "udp": pj.PJSIP_TRANSPORT_UDP,
            "tcp": pj.PJSIP_TRANSPORT_TCP,
            "tls": pj.PJSIP_TRANSPORT_TLS,
        }[cfg.transport]
        t_cfg = pj.TransportConfig()
        t_cfg.port = cfg.local_port
        self._ep.transportCreate(transport, t_cfg)
        self._ep.libStart()
        # No sound card: the null device provides the conference bridge clock
        self._ep.audDevManager().setNullDev()
        self._configure_codecs()
        self._create_account()

    def _configure_codecs(self) -> None:
        preferred = [c.upper() for c in self.cfg.codecs]
        for codec in self._ep.codecEnum2():
            name = codec.codecId.split("/")[0].upper()
            priority = 255 - preferred.index(name) if name in preferred else 0
            self._ep.codecSetPriority(codec.codecId, priority)
        log.info("Enabled codecs in order: %s", ", ".join(preferred))

    def _create_account(self) -> None:
        cfg = self.cfg
        a_cfg = pj.AccountConfig()
        a_cfg.idUri = f"sip:{cfg.user}@{cfg.sip_domain}"
        registrar = f"sip:{cfg.server}:{cfg.port}"
        if cfg.transport != "udp":
            registrar += f";transport={cfg.transport}"
        a_cfg.regConfig.registrarUri = registrar
        a_cfg.regConfig.timeoutSec = cfg.register_timeout_s
        a_cfg.regConfig.retryIntervalSec = cfg.register_retry_s
        a_cfg.regConfig.firstRetryIntervalSec = min(10, cfg.register_retry_s)
        a_cfg.sipConfig.authCreds.append(pj.AuthCredInfo("digest", "*", cfg.user, 0, cfg.password))
        if cfg.outbound_proxy:
            a_cfg.sipConfig.proxies.append(f"sip:{cfg.outbound_proxy};lr")
        a_cfg.mediaConfig.transportConfig.port = cfg.rtp_port_start
        a_cfg.mediaConfig.transportConfig.portRange = cfg.rtp_port_range
        if cfg.stun:
            a_cfg.natConfig.sipStunUse = pj.PJSUA_STUN_USE_DEFAULT
            a_cfg.natConfig.mediaStunUse = pj.PJSUA_STUN_USE_DEFAULT
        self._account = _Account(self)
        self._account.create(a_cfg)
        self.registration_status = "in progress"
        log.info("Registering on %s as %s", registrar, a_cfg.idUri)

    def _cmd_dial(self, call: SipCall) -> None:
        if not self.registered:
            raise SipError(f"not registered on the SIP server ({self.registration_status})")
        pj_call = _Call(self, self._account, call)
        pj_call.makeCall(call.uri, pj.CallOpParam(True))
        self._calls[call.id] = pj_call
        log.info("Call %d to %s started", call.id, call.uri)

    def _cmd_hangup(self, call_id: int) -> None:
        pj_call = self._calls.get(call_id)
        if pj_call is None:
            return
        try:
            # CANCEL while still ringing, BYE once the call is established
            pj_call.hangup(pj.CallOpParam())
        except pj.Error as e:
            log.debug("hangup of call %d: %s", call_id, e.info())

    def _call_disconnected(self, pj_call, code: int, reason: str) -> None:
        call = pj_call.call
        call.disconnected = True
        call.final_code = code
        call.final_reason = reason
        self._calls.pop(call.id, None)
        # Delete the port and the Call object on the next loop iteration, outside the callback
        self._to_delete.append(pj_call)
        log.info("Call %d ended: %s %s", call.id, code, reason)
        self._emit(call, CallEvent("disconnected", code, reason))

    def _shutdown(self) -> None:
        log.info("Stopping the SIP stack")
        try:
            self._ep.hangupAllCalls()
            for _ in range(25):  # leave time to send BYE and un-REGISTER
                self._ep.libHandleEvents(FRAME_MS)
            if self._account is not None:
                self._account.shutdown()
                for _ in range(25):
                    self._ep.libHandleEvents(FRAME_MS)
        except Exception:  # noqa: BLE001
            log.exception("Error while stopping SIP")
        finally:
            self._calls.clear()
            self._to_delete.clear()
            self._account = None
            self._ep.libDestroy()
            self._ep = None
