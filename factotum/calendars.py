"""Calendar backend interface and event construction.

Today there is a Google backend; the interface allows adding a Home Assistant
backend (`calendar.*` entities) later without touching the conversation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from zoneinfo import ZoneInfo


class CalendarError(Exception):
    pass


@dataclass
class Appointment:
    """Data collected by the agent during the call (tool arguments)."""

    title: str
    start: datetime
    end: datetime
    contact_name: str
    contact_role: str = ""
    location: str = ""
    notes: str = ""


@dataclass
class CallContext:
    """Call details to include in the event."""

    call_id: str
    number: str
    goal: str
    call_time: datetime
    user_name: str
    user_id: str
    transcript_excerpt: str = ""
    transcript_file: str = ""


class CalendarBackend(Protocol):
    name: str

    async def busy(self, calendars: list[str], start: datetime, end: datetime) -> list[tuple[str, str]]:
        """Busy intervals (ISO 8601) in the requested range, across all calendars."""

    async def create_event(self, calendar: str, event: dict) -> dict:
        """Create the event and return at least `id` and `link`."""

    async def check_access(self, calendar: str) -> tuple[bool, str]:
        """Check that the calendar is reachable and writable."""


def parse_datetime(value: str, tz: str) -> datetime:
    """Parse an ISO 8601 date from the LLM; without an offset the user's time zone is used."""
    parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(tz))
    return parsed


def event_title(appt: Appointment) -> str:
    contact = appt.contact_name.strip()
    if appt.contact_role.strip():
        contact += f" ({appt.contact_role.strip()})"
    return f"{appt.title.strip()} – {contact}" if contact else appt.title.strip()


def event_description(appt: Appointment, ctx: CallContext) -> str:
    lines = [
        "Appointment booked by factotum during a phone call.",
        "",
        f"Spoke with: {appt.contact_name}",
    ]
    if appt.contact_role:
        lines.append(f"Role / company: {appt.contact_role}")
    lines += [
        f"Number called: {ctx.number}",
        f"Call time: {ctx.call_time:%Y-%m-%d %H:%M}",
        f"On behalf of: {ctx.user_name}",
        f"Call goal: {ctx.goal}",
    ]
    if appt.location:
        lines.append(f"Location: {appt.location}")
    if appt.notes:
        lines += ["", "Notes:", appt.notes]
    if ctx.transcript_excerpt:
        lines += ["", "Last turns of the call:", ctx.transcript_excerpt]
    lines += ["", f"Call ID: {ctx.call_id}"]
    if ctx.transcript_file:
        lines.append(f"Full transcript: {ctx.transcript_file}")
    return "\n".join(lines)


def build_event(appt: Appointment, ctx: CallContext, tz: str) -> dict:
    """Event in Google Calendar API v3 format (also used as the neutral format)."""
    event = {
        "summary": event_title(appt),
        "description": event_description(appt, ctx),
        "start": {"dateTime": appt.start.isoformat(), "timeZone": tz},
        "end": {"dateTime": appt.end.isoformat(), "timeZone": tz},
        "reminders": {"useDefault": True},
        "extendedProperties": {
            "private": {
                "factotum_call_id": ctx.call_id,
                "factotum_number": ctx.number,
                "factotum_user": ctx.user_id,
                "factotum_contact": appt.contact_name,
            }
        },
    }
    if appt.location:
        event["location"] = appt.location
    return event
