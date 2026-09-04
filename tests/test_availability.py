from datetime import datetime, timezone

import pytest

from reference.availability import TZ, Event, available_slots, technician_for

TECH1, TECH2 = "tech1@ufound-ai.com", "tech2@ufound-ai.com"
NOW = datetime(2026, 9, 8, 9, 30, tzinfo=TZ)  # Tuesday 09:30 ET
DRIVE = {"near": 10 * 60, "far": 16 * 60, "edge": 15 * 60}


def ev(day, sh, eh, who=TECH1, loc="near", sm=0, em=0, **kw):
    return Event(
        start=datetime(2026, 9, day, sh, sm, tzinfo=TZ),
        end=datetime(2026, 9, day, eh, em, tzinfo=TZ),
        attendees=[who],
        location=loc,
        **kw,
    )


def day(slots, d):
    return sorted(s["start"] for s in slots if s["date"] == d)


def test_trade_routing():
    assert technician_for("Plumbing") == TECH1
    assert technician_for(" electrical ") == TECH2
    assert technician_for("HVAC") == "tech3@ufound-ai.com"
    assert technician_for("roofing") is None


def test_unknown_trade_raises():
    with pytest.raises(ValueError):
        available_slots(NOW, "roofing", [], {})


def test_empty_calendar_window_and_weekends():
    slots = available_slots(NOW, "plumbing", [], {})
    dates = sorted({s["date"] for s in slots})
    assert dates[0] == "2026-09-08" and dates[-1] == "2026-09-21"  # today + 13
    assert all(datetime.fromisoformat(d).weekday() < 5 for d in dates)
    assert "2026-09-12" not in dates and "2026-09-13" not in dates
    assert len(slots) == 2 + 9 * 4  # today keeps 12-14 and 14-16 only


def test_today_lead_time_boundary():
    assert day(available_slots(NOW, "plumbing", [], {}), "2026-09-08") == ["12:00", "14:00"]
    at_8 = NOW.replace(hour=8, minute=0)
    assert "10:00" in day(available_slots(at_8, "plumbing", [], {}), "2026-09-08")
    at_801 = NOW.replace(hour=8, minute=1)
    assert "10:00" not in day(available_slots(at_801, "plumbing", [], {}), "2026-09-08")


def test_both_hours_busy_closes_slot():
    slots = available_slots(NOW, "plumbing", [ev(9, 8, 10)], DRIVE)
    assert day(slots, "2026-09-09") == ["10:00", "12:00", "14:00"]


def test_one_free_hour_keeps_slot_open():
    slots = available_slots(NOW, "plumbing", [ev(9, 9, 11)], DRIVE)
    assert day(slots, "2026-09-09") == ["08:00", "10:00", "12:00", "14:00"]


def test_partial_overlap_marks_both_hours_busy():
    slots = available_slots(NOW, "plumbing", [ev(9, 8, 9, sm=30, em=30)], DRIVE)
    assert day(slots, "2026-09-09") == ["10:00", "12:00", "14:00"]


def test_back_to_back_jobs_close_the_day():
    jobs = [ev(9, 8, 10), ev(9, 10, 12), ev(9, 12, 14), ev(9, 14, 16)]
    assert day(available_slots(NOW, "plumbing", jobs, DRIVE), "2026-09-09") == []


def test_other_technician_is_ignored():
    slots = available_slots(NOW, "plumbing", [ev(9, 8, 10, who=TECH2)], DRIVE)
    assert "08:00" in day(slots, "2026-09-09")


def test_attendee_email_case_insensitive():
    slots = available_slots(NOW, "plumbing", [ev(9, 8, 10, who="Tech1@Ufound-AI.com")], DRIVE)
    assert "08:00" not in day(slots, "2026-09-09")


def test_far_job_excludes_whole_day():
    slots = available_slots(NOW, "plumbing", [ev(9, 8, 10, loc="far")], DRIVE)
    assert day(slots, "2026-09-09") == []
    assert day(slots, "2026-09-10") == ["08:00", "10:00", "12:00", "14:00"]


def test_exactly_15_minutes_is_not_far():
    slots = available_slots(NOW, "plumbing", [ev(9, 8, 10, loc="edge")], DRIVE)
    assert day(slots, "2026-09-09") == ["10:00", "12:00", "14:00"]


def test_job_without_location_only_blocks_its_hours():
    slots = available_slots(NOW, "plumbing", [ev(9, 8, 10, loc="")], {})
    assert day(slots, "2026-09-09") == ["10:00", "12:00", "14:00"]


def test_unmeasurable_drive_time_excludes_day():
    slots = available_slots(NOW, "plumbing", [ev(9, 8, 10, loc="x")], {"x": None})
    assert day(slots, "2026-09-09") == []


def test_event_given_in_utc_is_converted():
    e = Event(
        start=datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc),  # 08:00 EDT
        end=datetime(2026, 9, 9, 14, 0, tzinfo=timezone.utc),
        attendees=[TECH1],
        location="near",
    )
    assert day(available_slots(NOW, "plumbing", [e], DRIVE), "2026-09-09") == ["10:00", "12:00", "14:00"]


def test_all_day_event_blocks_day():
    e = ev(9, 0, 0, all_day=True)
    assert day(available_slots(NOW, "plumbing", [e], DRIVE), "2026-09-09") == []


def test_slot_shape_and_spoken_text():
    s = available_slots(NOW, "plumbing", [], {})[0]
    assert s == {
        "date": "2026-09-08",
        "day": "Tuesday",
        "start": "12:00",
        "end": "14:00",
        "spoken": "Tuesday, September 8, 12 PM to 2 PM",
    }


def test_window_from_a_friday_skips_weekend_and_ends_thursday():
    fri = datetime(2026, 9, 11, 7, 0, tzinfo=TZ)
    dates = sorted({s["date"] for s in available_slots(fri, "hvac", [], {})})
    assert dates[0] == "2026-09-11" and dates[-1] == "2026-09-24"
    assert len(dates) == 10
