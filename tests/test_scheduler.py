import asyncio
from datetime import datetime, timedelta, timezone

from factotum.config import CallsConfig
from factotum.scheduler import COMPLETED, EXPIRED, FAILED, PENDING, Call, Scheduler

NOW = datetime(2026, 10, 12, 10, 0, tzinfo=timezone.utc)


def _call(start_min: int, end_min: int, **kw) -> Call:
    return Call(user_id="u1", number="+390444123456", language="it", goal="test",
                window_start=NOW + timedelta(minutes=start_min), window_end=NOW + timedelta(minutes=end_min), **kw)


def _scheduler(**cfg) -> Scheduler:
    async def run(_):
        pass
    return Scheduler(CallsConfig(**cfg), run, ready=lambda: True)


def test_next_due_respects_windows():
    s = _scheduler()
    future = _call(30, 90)
    expired = _call(-90, -10)
    now1 = _call(-10, 120)
    now2 = _call(-10, 60)
    for c in (future, expired, now1, now2):
        s.calls[c.id] = c
    assert s.next_due(NOW) is now2  # the window that closes first
    assert expired.state == EXPIRED
    assert future.state == PENDING


def test_retry_when_busy():
    s = _scheduler(retry_after_s=600, max_attempts=3)
    c = _call(-10, 60, attempts=1)
    c.outcome = "busy"
    s.record_outcome(c, NOW)
    assert c.state == PENDING
    assert c.next_attempt == NOW + timedelta(minutes=10)
    s.calls[c.id] = c
    assert s.next_due(NOW + timedelta(minutes=5)) is None
    assert s.next_due(NOW + timedelta(minutes=11)) is c


def test_no_retry_beyond_window():
    s = _scheduler(retry_after_s=600)
    c = _call(-10, 5, attempts=1)
    c.outcome = "no answer"
    s.record_outcome(c, NOW)
    assert c.state == FAILED


def test_completed_when_answered():
    s = _scheduler()
    c = _call(-10, 60, attempts=1)
    c.answered = True
    c.outcome = "appointment_booked"
    s.record_outcome(c, NOW)
    assert c.state == COMPLETED


def test_add_rejects_inverted_window():
    s = _scheduler()
    try:
        s.add(_call(60, 30))
    except ValueError:
        return
    raise AssertionError("ValueError expected")


def test_one_call_at_a_time():
    placed = []

    async def run(c):
        placed.append(c.id)
        await asyncio.sleep(0.05)
        c.answered = True

    async def scenario():
        s = Scheduler(CallsConfig(), run, ready=lambda: True, interval_s=0.01)
        now = datetime.now(timezone.utc)
        for _ in range(3):
            s.add(Call(user_id="u", number="+39044412345", language="it", goal="x",
                       window_start=now - timedelta(minutes=1), window_end=now + timedelta(hours=1)))
        task = asyncio.create_task(s.run())
        max_concurrent = 0
        for _ in range(40):
            await asyncio.sleep(0.01)
            max_concurrent = max(max_concurrent, sum(c.state == "in_progress" for c in s.calls.values()))
        await s.stop()
        task.cancel()
        return s, max_concurrent

    s, max_concurrent = asyncio.run(scenario())
    assert max_concurrent == 1
    assert len(placed) == 3
    assert all(c.state == COMPLETED for c in s.calls.values())
