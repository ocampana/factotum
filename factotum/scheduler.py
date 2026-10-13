"""Call list (in RAM) and scheduling inside the time windows.

One call at a time: keeps RAM and CPU low and is enough for the intended use.
A call that fails for temporary reasons (busy, no answer, dropped) is retried
as long as it stays inside its time window.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

from .config import CallsConfig

log = logging.getLogger(__name__)

# Call states
PENDING = "pending"
IN_PROGRESS = "in_progress"
COMPLETED = "completed"
FAILED = "failed"
EXPIRED = "expired"
CANCELLED = "cancelled"

# Outcomes worth retrying
RETRYABLE_OUTCOMES = {"busy", "no answer", "unavailable", "dropped", "sip_error", "api_error"}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Call:
    user_id: str
    number: str          # E.164
    language: str
    goal: str
    window_start: datetime  # time window (timezone-aware)
    window_end: datetime
    calendars: list[str] = field(default_factory=list)
    report: bool = False
    auto_language: bool = True
    id: str = field(default_factory=lambda: secrets.token_hex(4))
    state: str = PENDING
    attempts: int = 0
    answered: bool = False  # the callee answered (current attempt)
    next_attempt: datetime | None = None
    outcome: str = ""
    summary: str = ""
    contact: str = ""
    events: list[dict] = field(default_factory=list)
    failed_events: list[dict] = field(default_factory=list)
    transcript: str = ""
    costs: dict = field(default_factory=dict)  # cost estimate of the last attempt
    error: str = ""
    created: datetime = field(default_factory=utcnow)
    started: datetime | None = None
    ended: datetime | None = None

    def public(self) -> dict:
        data = asdict(self)
        for key, value in data.items():
            if isinstance(value, datetime):
                data[key] = value.isoformat()
        data.pop("failed_events")
        data["events_to_retry"] = len(self.failed_events)
        return data


CallRunner = Callable[[Call], Awaitable[None]]


class Scheduler:
    def __init__(self, cfg: CallsConfig, run_call: CallRunner,
                 ready: Callable[[], bool], interval_s: float = 2.0):
        self.cfg = cfg
        self._run_call = run_call
        self._ready = ready
        self._interval = interval_s
        self.calls: dict[str, Call] = {}
        self.current: Call | None = None
        self._current_task: asyncio.Task | None = None
        self._running = False
        self._wakeup = asyncio.Event()

    # ------------------------------------------------------------------ list management

    def add(self, call: Call) -> Call:
        if call.window_end <= call.window_start:
            raise ValueError("the end of the window must be after its start")
        if call.window_end <= utcnow():
            raise ValueError("the time window is already over")
        self.calls[call.id] = call
        self._wakeup.set()
        log.info("Call %s to %s added (%s - %s, language %s)", call.id, call.number,
                 call.window_start, call.window_end, call.language)
        return call

    def list(self, user_id: str | None = None) -> list[Call]:
        calls = [c for c in self.calls.values() if user_id is None or c.user_id == user_id]
        return sorted(calls, key=lambda c: c.window_start)

    def get(self, call_id: str, user_id: str | None = None) -> Call | None:
        call = self.calls.get(call_id)
        if call and (user_id is None or call.user_id == user_id):
            return call
        return None

    def cancel(self, call: Call) -> None:
        """Cancel a pending call or interrupt the one in progress."""
        if call.state == PENDING:
            call.state = CANCELLED
        elif call.state == IN_PROGRESS and self._current_task and self.current is call:
            call.outcome = "interrupted by the user"
            self._current_task.cancel()

    def remove(self, call: Call) -> None:
        if call.state != IN_PROGRESS:
            self.calls.pop(call.id, None)

    # ------------------------------------------------------------------ scheduling

    def next_due(self, now: datetime | None = None) -> Call | None:
        """Pick the next call to place and mark expired ones."""
        now = now or utcnow()
        candidates = []
        for c in self.calls.values():
            if c.state != PENDING:
                continue
            if now >= c.window_end:
                c.state = EXPIRED
                c.outcome = c.outcome or "time window over"
                continue
            if now < c.window_start or (c.next_attempt and now < c.next_attempt):
                continue
            candidates.append(c)
        # Calls whose window closes first go first
        return min(candidates, key=lambda c: (c.window_end, c.created)) if candidates else None

    def record_outcome(self, call: Call, now: datetime | None = None) -> None:
        """Decide the final state, or schedule a new attempt, after a call."""
        now = now or utcnow()
        call.ended = now
        if call.state == CANCELLED:
            return
        if call.outcome in RETRYABLE_OUTCOMES and call.attempts < self.cfg.max_attempts:
            next_time = now + timedelta(seconds=self.cfg.retry_after_s)
            if next_time < call.window_end:
                call.state = PENDING
                call.next_attempt = next_time
                log.info("Call %s: %s, next attempt at %s", call.id, call.outcome, next_time)
                return
        call.state = COMPLETED if call.answered and not call.error else FAILED

    async def run(self) -> None:
        self._running = True
        while self._running:
            try:
                await asyncio.wait_for(self._wakeup.wait(), self._interval)
            except asyncio.TimeoutError:
                pass
            self._wakeup.clear()
            if self.current is not None or not self._ready():
                continue
            call = self.next_due()
            if call is None:
                continue
            self.current = call
            call.state = IN_PROGRESS
            call.attempts += 1
            call.started = utcnow()
            call.outcome = ""
            call.error = ""
            call.answered = False
            self._current_task = asyncio.create_task(self._run_one(call))

    async def _run_one(self, call: Call) -> None:
        try:
            await self._run_call(call)
        except asyncio.CancelledError:
            call.state = CANCELLED
            call.outcome = call.outcome or "interrupted"
        except Exception as e:  # noqa: BLE001
            log.exception("Unhandled error in call %s", call.id)
            call.error = str(e)
            call.outcome = call.outcome or "error"
        finally:
            self.record_outcome(call)
            self.current = None
            self._current_task = None
            self._wakeup.set()

    async def stop(self) -> None:
        self._running = False
        self._wakeup.set()
        if self._current_task:
            self._current_task.cancel()
            try:
                await self._current_task
            except asyncio.CancelledError:
                pass
