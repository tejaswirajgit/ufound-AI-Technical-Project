"""Randomized stress test: the reference rules against an independent oracle plus invariants.

Hundreds of random calendars (partial hours, all-day events, other technicians, mixed-case
emails, near/far/unmeasurable/missing locations, random 'now' across a year including both
DST switch days). Any invalid slot here would be an invalid slot on a real call.
"""
from __future__ import annotations

import random
import re
import time as clock
from datetime import date, datetime, time, timedelta

import pytest

from reference.availability import SLOTS, TECH_BY_TRADE, TZ, Event, available_slots

TECHS = list(TECH_BY_TRADE.values())
ATTENDEE_POOL = TECHS + ["Tech1@Ufound-AI.com", "TECH3@ufound-ai.com", "dispatcher@ufound-ai.com", ""]
DRIVE_CHOICES = [None, 0, 300, 899, 900, 901, 1800, 7200]
SPOKEN_RX = re.compile(r"^[A-Z][a-z]+day, [A-Z][a-z]+ \d{1,2}, (8 AM to 10 AM|10 AM to 12 PM|12 PM to 2 PM|2 PM to 4 PM)$")
DST_DAYS = [datetime(2026, 11, 1, 1, 30, tzinfo=TZ), datetime(2026, 10, 30, 9, 0, tzinfo=TZ),
            datetime(2027, 3, 14, 3, 30, tzinfo=TZ), datetime(2027, 3, 12, 15, 59, tzinfo=TZ)]


def random_case(seed: int):
    rng = random.Random(seed)
    if seed % 10 == 0:
        now = DST_DAYS[seed // 10 % len(DST_DAYS)]
    else:
        now = datetime(2026, 1, 1, tzinfo=TZ) + timedelta(days=rng.randrange(400), hours=rng.randrange(24),
                                                          minutes=rng.choice([0, 1, 15, 30, 59]))
    trade = rng.choice(list(TECH_BY_TRADE))
    events, drive = [], {}
    for i in range(rng.randrange(0, 40)):
        d = now.date() + timedelta(days=rng.randrange(-1, 16))
        loc = rng.choice(["", "", f"job-{i}"])
        if loc:
            drive[loc] = rng.choice(DRIVE_CHOICES)
        who = [rng.choice(ATTENDEE_POOL)] + ([rng.choice(ATTENDEE_POOL)] if rng.random() < 0.2 else [])
        if rng.random() < 0.08:
            events.append(Event(datetime.combine(d, time(0), TZ), datetime.combine(d + timedelta(days=1), time(0), TZ),
                                who, loc, all_day=True))
            continue
        start = datetime.combine(d, time(rng.randrange(0, 23), rng.choice([0, 0, 15, 30, 45])), TZ)
        minutes = rng.choice([15, 30, 60, 60, 90, 120, 120, 180, 240, 480, 900])
        events.append(Event(start, start + timedelta(minutes=minutes), who, loc))
    return now, trade, events, drive


def oracle(now: datetime, trade: str, events: list[Event], drive: dict) -> list[tuple[str, str]]:
    """Same rules, written differently: interval intersection per hour, date ranges for exclusion."""
    tech = TECH_BY_TRADE[trade]
    mine = [e for e in events if tech in [a.strip().lower() for a in e.attendees]]
    now_et = now.astimezone(TZ)
    today = now_et.date()

    def event_dates(e: Event) -> set[date]:
        if e.all_day:
            return {e.start.astimezone(TZ).date()}
        first, last = e.start.astimezone(TZ).date(), (e.end.astimezone(TZ) - timedelta(microseconds=1)).date()
        return {first + timedelta(days=k) for k in range((last - first).days + 1)}

    def busy(d: date, h: int) -> bool:
        lo, hi = datetime.combine(d, time(h), TZ), datetime.combine(d, time(h + 1), TZ)
        return any((e.all_day and d in event_dates(e)) or (not e.all_day and e.start < hi and e.end > lo) for e in mine)

    def far_day(d: date) -> bool:
        return any(e.location and (drive.get(e.location) is None or drive[e.location] > 900) and d in event_dates(e)
                   for e in mine)

    out = []
    for i in range(14):
        d = today + timedelta(days=i)
        if d.weekday() >= 5 or far_day(d):
            continue
        for sh, eh in SLOTS:
            if d == today and datetime.combine(d, time(sh), TZ) < now_et + timedelta(hours=2):
                continue
            if busy(d, sh) and busy(d, sh + 1):
                continue
            out.append((d.isoformat(), f"{sh:02d}:00"))
    return out


@pytest.mark.parametrize("seed", range(300))
def test_matches_independent_oracle(seed):
    now, trade, events, drive = random_case(seed)
    got = [(s["date"], s["start"]) for s in available_slots(now, trade, events, drive)]
    assert got == oracle(now, trade, events, drive)


@pytest.mark.parametrize("seed", range(300))
def test_invariants(seed):
    now, trade, events, drive = random_case(seed)
    slots = available_slots(now, trade, events, drive)
    now_et = now.astimezone(TZ)
    today = now_et.date()
    keys = [(s["date"], s["start"]) for s in slots]
    assert keys == sorted(keys) and len(keys) == len(set(keys))
    for s in slots:
        d = date.fromisoformat(s["date"])
        assert today <= d <= today + timedelta(days=13), "outside the 14-day window"
        assert d.weekday() < 5, "weekend"
        assert (int(s["start"][:2]), int(s["end"][:2])) in SLOTS, "not a fixed 2-hour slot"
        assert s["day"] == d.strftime("%A")
        assert SPOKEN_RX.match(s["spoken"]), s["spoken"]
        if d == today:
            assert datetime.combine(d, time(int(s["start"][:2])), TZ) >= now_et + timedelta(hours=2), "inside lead time"


def test_large_calendar_is_fast():
    rng = random.Random(7)
    now = datetime(2026, 9, 8, 9, 30, tzinfo=TZ)
    events = []
    for i in range(5000):
        start = datetime.combine(now.date() + timedelta(days=rng.randrange(14)), time(rng.randrange(6, 20)), TZ)
        events.append(Event(start, start + timedelta(hours=1), [rng.choice(TECHS)], f"job-{i}"))
    drive = {f"job-{i}": rng.choice([600, 1200]) for i in range(5000)}
    t0 = clock.perf_counter()
    available_slots(now, "plumbing", events, drive)
    assert clock.perf_counter() - t0 < 1.0
