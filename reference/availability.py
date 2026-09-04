"""Reference implementation of the check_availability rules.

This is the source of truth for what the Make.com scenario must return.
tests/test_availability.py pins every rule; reference/check_webhook.py replays
the live webhook's inputs through this code and diffs the slot lists.

Rules (PDF section 6 + Eyal's email of 2026-09-03):
  * Company works in America/New_York (Eastern). Mon-Fri 08:00-16:00.
  * Window: today plus the next 13 days (14 calendar days, today included).
  * Slots: 08-10, 10-12, 12-14, 14-16. A slot is open when at least one of its
    two hours has no event for the technician ("when at least one of two are free
    it is open").
  * Today only: a slot must start at least 2 hours from now.
  * A technician is identified by their email in the event's attendee list.
  * If the technician has a job that day whose driving time from the caller's
    address is over 15 minutes, the whole day is excluded.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/New_York")
TECH_BY_TRADE = {
    "plumbing": "tech1@ufound-ai.com",
    "electrical": "tech2@ufound-ai.com",
    "hvac": "tech3@ufound-ai.com",
}
SLOTS = [(8, 10), (10, 12), (12, 14), (14, 16)]
WORK_START, WORK_END = 8, 16
WINDOW_DAYS = 14
LEAD_TIME = timedelta(hours=2)
MAX_DRIVE_SECONDS = 15 * 60


@dataclass
class Event:
    start: datetime  # timezone-aware
    end: datetime
    attendees: list[str]
    location: str = ""
    all_day: bool = False  # Google "date" events: treated as the whole working day


def technician_for(trade: str) -> str | None:
    return TECH_BY_TRADE.get(trade.strip().lower())


def hours_touched(event: Event) -> list[tuple[date, int]]:
    """Every (Eastern date, hour) the event overlaps, even partially."""
    if event.all_day:
        d = event.start.astimezone(TZ).date()
        return [(d, h) for h in range(WORK_START, WORK_END)]
    cur = event.start.astimezone(TZ).replace(minute=0, second=0, microsecond=0)
    end = event.end.astimezone(TZ)
    out = []
    while cur < end:
        out.append((cur.date(), cur.hour))
        cur += timedelta(hours=1)
    return out


def excluded_dates(events: list[Event], drive_seconds: dict[str, int | None]) -> set[date]:
    """Dates ruled out by the 15-minute rule.

    drive_seconds maps an event location to driving seconds from the caller.
    A missing or None value means Maps could not measure it: exclude the day
    rather than risk offering a slot the technician cannot reach.
    An event with no location cannot be measured at all, so it never excludes.
    """
    out: set[date] = set()
    for e in events:
        if not e.location:
            continue
        secs = drive_seconds.get(e.location)
        if secs is None or secs > MAX_DRIVE_SECONDS:
            out.update(d for d, _ in hours_touched(e))
    return out


def spoken_hour(h: int) -> str:
    return f"{h if h <= 12 else h - 12} {'AM' if h < 12 else 'PM'}"


def slot_dict(d: date, start_h: int, end_h: int) -> dict:
    return {
        "date": d.isoformat(),
        "day": f"{d:%A}",
        "start": f"{start_h:02d}:00",
        "end": f"{end_h:02d}:00",
        "spoken": f"{d:%A}, {d:%B} {d.day}, {spoken_hour(start_h)} to {spoken_hour(end_h)}",
    }


def available_slots(
    now: datetime,
    trade: str,
    events: list[Event],
    drive_seconds: dict[str, int | None],
) -> list[dict]:
    tech = technician_for(trade)
    if tech is None:
        raise ValueError(f"unknown trade: {trade!r}")
    mine = [e for e in events if tech in {a.strip().lower() for a in e.attendees}]
    busy = {key for e in mine for key in hours_touched(e)}
    excluded = excluded_dates(mine, drive_seconds)

    now_et = now.astimezone(TZ)
    earliest_start = now_et + LEAD_TIME
    today = now_et.date()

    slots = []
    for i in range(WINDOW_DAYS):
        d = today + timedelta(days=i)
        if d.weekday() >= 5 or d in excluded:
            continue
        for start_h, end_h in SLOTS:
            if d == today and datetime.combine(d, time(start_h), TZ) < earliest_start:
                continue
            if (d, start_h) in busy and (d, start_h + 1) in busy:
                continue
            slots.append(slot_dict(d, start_h, end_h))
    return slots
