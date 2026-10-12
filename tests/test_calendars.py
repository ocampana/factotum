from datetime import datetime
from zoneinfo import ZoneInfo

from factotum.calendars import Appointment, CallContext, build_event, parse_datetime

TZ = "Europe/Rome"


def test_event_contains_all_details():
    appt = Appointment(
        title="Boiler inspection",
        start=parse_datetime("2026-10-15T10:00:00", TZ),
        end=parse_datetime("2026-10-15T11:00:00", TZ),
        contact_name="Mario Rossi",
        contact_role="Rossi Plumbing",
        location="Via Roma 1, Vicenza",
        notes="Call-out fee 50 euro",
    )
    ctx = CallContext(
        call_id="abc123", number="+390444123456", goal="Book the inspection",
        call_time=datetime(2026, 10, 12, 9, 30, tzinfo=ZoneInfo(TZ)),
        user_name="Ottavio", user_id="u1",
        transcript_excerpt="Agent: Is Thursday at ten OK?\nCallee: Perfect.",
        transcript_file="20261012-093000-abc123.txt",
    )
    event = build_event(appt, ctx, TZ)
    assert event["summary"] == "Boiler inspection – Mario Rossi (Rossi Plumbing)"
    assert event["location"] == "Via Roma 1, Vicenza"
    assert event["start"]["dateTime"] == "2026-10-15T10:00:00+02:00"
    assert event["start"]["timeZone"] == TZ
    description = event["description"]
    for expected in ("Mario Rossi", "Rossi Plumbing", "+390444123456", "Book the inspection",
                     "Call-out fee 50 euro", "Perfect.", "abc123", "Ottavio"):
        assert expected in description
    private = event["extendedProperties"]["private"]
    assert private["factotum_call_id"] == "abc123"
    assert private["factotum_contact"] == "Mario Rossi"


def test_parse_datetime_with_and_without_offset():
    assert parse_datetime("2026-01-15T10:00:00", TZ).utcoffset().total_seconds() == 3600
    assert parse_datetime("2026-01-15T10:00:00Z", TZ).utcoffset().total_seconds() == 0
