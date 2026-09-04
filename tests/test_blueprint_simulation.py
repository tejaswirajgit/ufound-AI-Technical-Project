"""Runs the Make blueprint locally and diffs its slots against the reference rules.

tests/test_artifacts.py proves the blueprint JSON is well formed. This file proves it computes
the right answer: make/simulate.py executes the real expressions from the real file against
mocked Google responses, and every case is compared with reference/availability.py.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from make.simulate import run
from reference.availability import TZ, Event, available_slots
from tests.test_stress import random_case

CALLER = "1600 Barton Springs Rd, Austin, TX 78704"
GEOCODED = "1600 Barton Springs Rd, Austin, TX 78704, USA"


def google_event(e: Event) -> dict:
    """The reference's Event as Google Calendar returns it."""
    out = {"status": "confirmed", "location": e.location,
           "attendees": [{"email": a} for a in e.attendees if a]}
    if e.all_day:
        d = e.start.astimezone(TZ).date()
        out["start"] = {"date": d.isoformat()}
        out["end"] = {"date": (d + timedelta(days=1)).isoformat()}
    else:
        out["start"] = {"dateTime": e.start.astimezone(TZ).isoformat()}
        out["end"] = {"dateTime": e.end.astimezone(TZ).isoformat()}
    return out


def responder(events, drive, *, geocode_status="OK", location_type="ROOFTOP", fail=()):
    """Mock Google. `drive` maps a job location to seconds, or None for unmeasurable."""
    def http(module_id, mapper):
        if module_id in fail:
            return None
        if module_id == 3:
            if geocode_status != "OK":
                return {"data": {"status": geocode_status, "results": []}}
            return {"data": {"status": "OK", "results": [{
                "formatted_address": GEOCODED,
                "geometry": {"location": {"lat": 30.2626, "lng": -97.7664}, "location_type": location_type}}]}}
        if module_id == 9:
            return {"body": {"items": [google_event(e) for e in events]}}
        if module_id == 12:
            destination = json.loads(mapper["data"])["destinations"][0]["waypoint"]["address"]
            seconds = drive.get(destination)
            if seconds is None:
                return {"data": [{"originIndex": 0, "destinationIndex": 0, "status": {},
                                  "condition": "ROUTE_NOT_FOUND"}]}
            return {"data": [{"originIndex": 0, "destinationIndex": 0, "status": {},
                              "condition": "ROUTE_EXISTS", "duration": f"{int(seconds)}s"}]}
        raise AssertionError(f"unexpected HTTP module {module_id}")
    return http


def scenario(now, trade, events, drive, *, address=CALLER, org_tz="UTC", **kw):
    request = {"name": "check_availability", "args": {"trade": trade, "address": address},
               "debug": "yes", "call": {"call_id": "sim"}}
    return run(request, now, responder(events, drive, **kw), org_tz=org_tz)


def keys(response):
    return [(s["date"], s["start"]) for s in response.get("slots", [])]


def expected(now, trade, events, drive):
    return [(s["date"], s["start"]) for s in available_slots(now, trade, events, drive)]


# --------------------------------------------------------------------------- the main check

@pytest.mark.parametrize("seed", range(300))
def test_blueprint_matches_reference(seed):
    now, trade, events, drive = random_case(seed)
    response = scenario(now, trade.capitalize(), events, drive)
    assert response["status"] in ("ok", "no_availability")
    assert keys(response) == expected(now, trade, events, drive)


@pytest.mark.parametrize("org_tz", ["UTC", "Asia/Kolkata", "America/Los_Angeles"])
def test_answer_does_not_depend_on_the_make_organisation_timezone(org_tz):
    now, trade, events, drive = random_case(3)
    assert keys(scenario(now, trade.capitalize(), events, drive, org_tz=org_tz)) == expected(now, trade, events, drive)


# --------------------------------------------------------------------------- specific rules

NOW = datetime(2026, 9, 8, 9, 30, tzinfo=TZ)  # Tuesday 09:30 Eastern
TECH1 = "tech1@ufound-ai.com"


def job(day, sh, eh, who=TECH1, loc="near", sm=0, em=0, **kw):
    return Event(datetime(2026, 9, day, sh, sm, tzinfo=TZ), datetime(2026, 9, day, eh, em, tzinfo=TZ),
                 [who], loc, **kw)


DRIVE = {"near": 600, "far": 1200, GEOCODED: 0, "4820 Burnet Road, Austin, TX 78756, USA": 0}


def day_of(response, date):
    return sorted(s["start"] for s in response["slots"] if s["date"] == date)


def test_empty_calendar_today_and_weekend():
    r = scenario(NOW, "Plumbing", [], DRIVE)
    assert r["status"] == "ok" and r["slot_count"] == len(r["slots"]) == 38
    assert day_of(r, "2026-09-08") == ["12:00", "14:00"]  # 2-hour lead time from 09:30
    assert day_of(r, "2026-09-12") == [] and day_of(r, "2026-09-13") == []  # weekend
    assert r["window"] == {"from": "2026-09-08", "to": "2026-09-21"}
    assert r["resolved_address"] == GEOCODED and r["technician"] == TECH1


def test_one_free_hour_keeps_the_slot_open():
    r = scenario(NOW, "Plumbing", [job(9, 9, 11)], DRIVE)
    assert day_of(r, "2026-09-09") == ["08:00", "10:00", "12:00", "14:00"]


def test_both_hours_busy_closes_the_slot():
    r = scenario(NOW, "Plumbing", [job(9, 8, 10)], DRIVE)
    assert day_of(r, "2026-09-09") == ["10:00", "12:00", "14:00"]


def test_far_job_excludes_the_whole_day_only():
    r = scenario(NOW, "Plumbing", [job(9, 8, 10, loc="far")], DRIVE)
    assert day_of(r, "2026-09-09") == []
    assert day_of(r, "2026-09-10") == ["08:00", "10:00", "12:00", "14:00"]


def test_unmeasurable_job_excludes_the_day():
    r = scenario(NOW, "Plumbing", [job(9, 8, 10, loc="mystery")], DRIVE)
    assert day_of(r, "2026-09-09") == []


def test_other_technicians_are_ignored():
    r = scenario(NOW, "Plumbing", [job(9, 8, 10, who="tech2@ufound-ai.com", loc="far")], DRIVE)
    assert day_of(r, "2026-09-09") == ["08:00", "10:00", "12:00", "14:00"]


def test_cancelled_event_is_ignored():
    http = responder([job(9, 8, 10)], DRIVE)

    def cancelled(module_id, mapper):
        result = http(module_id, mapper)
        if module_id == 9:
            for item in result["body"]["items"]:
                item["status"] = "cancelled"
        return result

    request = {"args": {"trade": "Plumbing", "address": CALLER}}
    assert day_of(run(request, NOW, cancelled), "2026-09-09") == ["08:00", "10:00", "12:00", "14:00"]


def test_all_day_event_blocks_the_day():
    r = scenario(NOW, "Plumbing", [job(9, 0, 0, all_day=True)], DRIVE)
    assert day_of(r, "2026-09-09") == []


def test_job_running_past_midnight_blocks_both_days():
    events = [Event(datetime(2026, 9, 9, 14, tzinfo=TZ), datetime(2026, 9, 10, 9, tzinfo=TZ), [TECH1], "near")]
    r = scenario(NOW, "Plumbing", events, DRIVE)
    assert day_of(r, "2026-09-09") == ["08:00", "10:00", "12:00"]  # 14-16 busy
    assert day_of(r, "2026-09-10") == ["08:00", "10:00", "12:00", "14:00"]  # only hour 8 busy, 9 free
    assert keys(r) == expected(NOW, "plumbing", events, DRIVE)


def test_far_job_running_past_midnight_excludes_both_days():
    events = [Event(datetime(2026, 9, 9, 14, tzinfo=TZ), datetime(2026, 9, 10, 9, tzinfo=TZ), [TECH1], "far")]
    r = scenario(NOW, "Plumbing", events, DRIVE)
    assert day_of(r, "2026-09-09") == [] and day_of(r, "2026-09-10") == []
    assert day_of(r, "2026-09-11") == ["08:00", "10:00", "12:00", "14:00"]
    assert keys(r) == expected(NOW, "plumbing", events, DRIVE)


def test_partial_hour_marks_both_hours_busy():
    r = scenario(NOW, "Plumbing", [job(9, 8, 9, sm=30, em=30)], DRIVE)
    assert day_of(r, "2026-09-09") == ["10:00", "12:00", "14:00"]


def test_fully_booked_technician_returns_no_availability():
    events = [Event(datetime(2026, 9, d, 8, tzinfo=TZ), datetime(2026, 9, d, 16, tzinfo=TZ), [TECH1], "near")
              for d in range(8, 22)]
    r = scenario(NOW, "Plumbing", events, DRIVE)
    assert r["status"] == "no_availability" and r["slots"] == [] and r["slot_count"] == 0
    assert "team member will call back" in r["message"]


# --------------------------------------------------------------------------- error paths

def test_unknown_trade():
    r = scenario(NOW, "Roofing", [], DRIVE)
    assert r["status"] == "unknown_trade" and r["slots"] == []


def test_address_not_found():
    r = scenario(NOW, "Plumbing", [], DRIVE, geocode_status="ZERO_RESULTS")
    assert r["status"] == "address_not_found" and r["slots"] == []
    assert "repeat" in r["message"]


def test_city_level_match_counts_as_not_found():
    r = scenario(NOW, "Plumbing", [], DRIVE, location_type="APPROXIMATE")
    assert r["status"] == "address_not_found"


def test_maps_quota_error_is_an_error_not_a_bad_address():
    r = scenario(NOW, "Plumbing", [], DRIVE, geocode_status="OVER_QUERY_LIMIT")
    assert r["status"] == "error"


@pytest.mark.parametrize("module_id", [3, 9, 12])
def test_api_failure_returns_error_rather_than_breaking_the_call(module_id):
    r = scenario(NOW, "Plumbing", [job(9, 8, 10)], DRIVE, fail=(module_id,))
    assert r["status"] == "error" and r["slots"] == []
    assert "team member will call back" in r["message"]


# --------------------------------------------------------------------------- response shape

def test_response_is_speakable_and_self_consistent():
    r = scenario(NOW, "Plumbing", [job(9, 8, 10)], DRIVE)
    assert r["timezone"] == "America/New_York" and r["trade"] == "Plumbing"
    assert r["slot_count"] == len(r["slots"])
    assert r["message"].startswith(str(r["slot_count"]))
    first = r["slots"][0]
    assert first == {"date": "2026-09-08", "day": "Tuesday", "start": "12:00", "end": "14:00",
                     "spoken": "Tuesday, September 8, 12 PM to 2 PM"}
    assert r["debug"]["event_count"] == 1


def test_debug_events_only_when_requested():
    request = {"args": {"trade": "Plumbing", "address": CALLER}}
    r = run(request, NOW, responder([job(9, 8, 10)], DRIVE))
    assert r["debug"]["events"] == []
