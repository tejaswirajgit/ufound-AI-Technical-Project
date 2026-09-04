"""Builds make/check_availability.blueprint.json, the Make.com scenario for check_availability.

Why a generator: the blueprint is ~20 modules of JSON with IML expressions inside JSON
strings inside JSON. Writing it by hand invites quoting mistakes; generating it keeps every
expression readable here and lets tests/test_artifacts.py check the output.

Run:  python make/build_blueprint.py
Then import the JSON in Make (Scenarios > Create > ... > Import Blueprint), pick the webhook
and Google connection, paste the calendar id and Maps key into module 2.

Flow (module ids are stable and referenced in README and the video):
  1 webhook -> 2 config/routing -> 3 Maps: validate caller address -> 4 address check -> 5 router
    route 1:  6 respond unknown_trade
    route 2:  7 respond address_not_found / error
    route 3:  8 time window -> 9 Calendar events -> 10 iterator
              -> 11 event times (filter: this technician, not cancelled) -> 12 Maps: drive time
              -> 13 event row -> 14 array aggregator -> 15 busy map
              -> 16 repeater (14 days) -> 17 day -> 18 day slots (filter: weekday, not far)
              -> 19 text aggregator -> 20 respond ok / no_availability
  Error handlers on 3, 9, 12 respond {"status":"error"} and ignore.
"""
from __future__ import annotations

import json
import pathlib

TZ = '"America/New_York"'
COMPANY = "4820 Burnet Road, Austin, TX 78756, USA"
MAPS = "https://maps.googleapis.com/maps/api/distancematrix/json"
JSON_HEADER = [{"key": "Content-Type", "value": "application/json"}]
OUT = pathlib.Path(__file__).with_name("check_availability.blueprint.json")


def iml(expr: str) -> str:
    return "{{" + expr + "}}"


def fmt(expr: str, pattern: str, tz: str = TZ) -> str:
    return f'formatDate({expr}; "{pattern}"; {tz})'


def designer(x: int, y: int, name: str | None = None) -> dict:
    d: dict = {"x": x, "y": y}
    if name:
        d["name"] = name
    return {"designer": d}


def cond(a: str, o: str, b: str) -> dict:
    return {"a": a, "o": o, "b": b}


def filt(name: str, *conds: dict) -> dict:
    return {"name": name, "conditions": [list(conds)]}


def setvars(id_: int, name: str, variables: list[tuple[str, str]], x: int, y: int, filter_: dict | None = None) -> dict:
    m = {
        "id": id_,
        "module": "util:SetVariables",
        "version": 1,
        "parameters": {},
        "mapper": {"variables": [{"name": n, "value": v} for n, v in variables], "scope": "roundtrip"},
        "metadata": designer(x, y, name),
    }
    if filter_:
        m["filter"] = filter_
    return m


def respond(id_: int, name: str, body: str, x: int, y: int, filter_: dict | None = None) -> dict:
    m = {
        "id": id_,
        "module": "gateway:WebhookRespond",
        "version": 1,
        "parameters": {},
        "mapper": {"status": "200", "body": body, "headers": JSON_HEADER},
        "metadata": designer(x, y, name),
    }
    if filter_:
        m["filter"] = filter_
    return m


ERROR_BODY = (
    '{"status":"error","trade":"{{2.trade}}","technician":"{{2.tech}}","timezone":"America/New_York",'
    '"slots":[],"slot_count":0,"message":"The scheduling system is temporarily unavailable. '
    'Apologise and tell the caller a team member will call back shortly to schedule."}'
)


def on_error(id_: int, x: int, y: int) -> list[dict]:
    """Error handler route: answer Retell with status error, then Ignore so the run ends cleanly."""
    return [
        respond(id_ * 10 + 1, "Respond: error", ERROR_BODY, x, y + 220),
        {"id": id_ * 10 + 2, "module": "builtin:Ignore", "version": 1, "mapper": None, "metadata": designer(x + 300, y + 220)},
    ]


def maps_get(id_: int, name: str, destination: str, x: int, y: int) -> dict:
    return {
        "id": id_,
        "module": "http:ActionSendData",
        "version": 3,
        "parameters": {"handleErrors": False, "useNewZLibDeCompress": True},
        "mapper": {
            "url": MAPS,
            "serializeUrl": False,
            "method": "get",
            "headers": [],
            "qs": [
                {"name": "origins", "value": "{{2.address}}"},
                {"name": "destinations", "value": destination},
                {"name": "mode", "value": "driving"},
                {"name": "key", "value": "{{2.maps_key}}"},
            ],
            "bodyType": "raw",
            "parseResponse": True,
            "authUser": "",
            "authPass": "",
            "timeout": "20",
            "shareCookies": False,
            "ca": "",
            "rejectUnauthorized": True,
            "followRedirect": True,
            "useQuerystring": False,
            "gzip": True,
            "useMtls": False,
            "contentType": "application/json",
            "data": "",
            "followAllRedirects": False,
        },
        "metadata": designer(x, y, name),
        "onerror": on_error(id_, x, y),
    }


# ---- per-event expressions (module 10 = iterator bundle = one Google Calendar event) ----
P = 'parseDate(10.start.dateTime; "YYYY-MM-DDTHH:mm:ssZ")'
PE = 'parseDate(10.end.dateTime; "YYYY-MM-DDTHH:mm:ssZ")'
NO_DT = 'ifempty(10.start.dateTime; "") = ""'  # true for all-day events (start.date instead of start.dateTime)
EVENT_DATE = f'if({NO_DT}; 10.start.date; {fmt(P, "YYYY-MM-DD")})'
END_H = f'parseNumber({fmt(PE, "H")}) + if(parseNumber({fmt(PE, "m")}) > 0; 1; 0)'  # exclusive end hour, ceiling
END_H_EFF = f'if({NO_DT}; 24; if({fmt(PE, "YYYY-MM-DD")} = {fmt(P, "YYYY-MM-DD")}; {END_H}; 24))'  # ends another day -> rest of day busy

DRIVE = 'if(11.has_location = "yes"; ifempty(12.data.rows[1].elements[1].duration.value; -1); 0)'
FAR = (
    'if(11.has_location = "no"; "no"; if(12.data.status = "OK" and 12.data.rows[1].elements[1].status = "OK" '
    'and 12.data.rows[1].elements[1].duration.value <= 900; "no"; "yes"))'
)

# ---- per-day expressions (module 16 = repeater, i = 0..13) ----
DAY = "addDays(2.window_noon; 16.i)"  # noon anchor: +-1h DST drift never changes the calendar date
MIDNIGHT = f"setHour(setMinute(setSecond(now; 0); 0); 0; {TZ})"


def ts(hour: int) -> str:
    return iml(f'parseNumber(formatDate(setHour({DAY}; {hour}; {TZ}); "X"))')


def slot_flag(h: int) -> str:
    """Open unless both hours are busy; today also needs start >= now + 2h."""
    return iml(
        f'if(contains(15.busy; 17.k{h:02d}) and contains(15.busy; 17.k{h + 1:02d}); "no"; '
        f'if(17.date = 2.today and 17.ts{h:02d} < 8.earliest_ts; "no"; "yes"))'
    )


SPOKEN = {8: "8 AM to 10 AM", 10: "10 AM to 12 PM", 12: "12 PM to 2 PM", 14: "2 PM to 4 PM"}


def frag(h: int) -> str:
    return (
        ',{"date":"{{17.date}}","day":"{{17.day_name}}","start":"%02d:00","end":"%02d:00",'
        '"spoken":"{{17.spoken_day}}, %s"}' % (h, h + 2, SPOKEN[h])
    )


COUNT = 'length(19.text) - length(replace(19.text; "/\\{/g"; ""))'
OK_MSG = " open two-hour windows in the next 14 days. Offer the earliest two or three first, more only if the caller asks."
NONE_MSG = "No open windows in the next 14 days for this technician. Apologise and say a team member will call back to schedule."


def build() -> dict:
    webhook = {
        "id": 1,
        "module": "gateway:CustomWebHook",
        "version": 1,
        "parameters": {"maxResults": 1},
        "mapper": {},
        "metadata": {
            **designer(0, 0, "Retell: check_availability"),
            "interface": [
                {"name": "name", "type": "text"},
                {"name": "args", "type": "collection", "spec": [{"name": "trade", "type": "text"}, {"name": "address", "type": "text"}]},
                {"name": "debug", "type": "text"},
                {"name": "call", "type": "collection", "spec": []},
            ],
        },
    }

    config = setvars(2, "Config & routing", [
        ("trade", "{{1.args.trade}}"),
        ("tech", iml('switch(lower(trim(1.args.trade)); "plumbing"; "tech1@ufound-ai.com"; '
                     '"electrical"; "tech2@ufound-ai.com"; "hvac"; "tech3@ufound-ai.com"; "")')),
        ("address", "{{trim(1.args.address)}}"),
        ("company", COMPANY),
        ("calendar_id", "PASTE_UFOUND_DISPATCH_CALENDAR_ID"),
        ("maps_key", "PASTE_GOOGLE_MAPS_API_KEY"),
        ("timezone", "America/New_York"),
        ("today", iml(fmt("now", "YYYY-MM-DD"))),
        ("today_spoken", iml(fmt("now", "dddd, MMMM D"))),
        ("now_ts", iml('parseNumber(formatDate(now; "X"))')),
        ("window_noon", iml(f"setHour(setMinute(setSecond(now; 0); 0); 12; {TZ})")),
        ("debug", iml('if(1.debug = "yes"; "yes"; "no")')),
    ], 300, 0)

    validate = maps_get(3, "Maps: validate caller address", "{{2.company}}", 600, 0)

    address_check = setvars(4, "Address check", [
        ("maps_status", "{{3.data.status}}"),
        ("element_status", "{{3.data.rows[1].elements[1].status}}"),
        ("address_ok", iml('if(3.data.status = "OK" and 3.data.rows[1].elements[1].status = "OK"; "yes"; "no")')),
        ("resolved_address", "{{3.data.origin_addresses[1]}}"),
        ("fail_status", iml('if(3.data.status = "OK" or 3.data.status = "INVALID_REQUEST" or 3.data.status = "ZERO_RESULTS"; '
                            '"address_not_found"; "error")')),
    ], 900, 0)

    unknown = respond(6, "Respond: unknown_trade", (
        '{"status":"unknown_trade","trade":"{{2.trade}}","timezone":"America/New_York","slots":[],"slot_count":0,'
        '"message":"That job type does not match one of our technicians. Ask one more short question about the '
        'problem, then call check_availability again with Plumbing, Electrical or HVAC."}'
    ), 1500, -400, filt("Unknown trade", cond("{{2.tech}}", "text:equal", "")))

    address_fail = respond(7, "Respond: address_not_found / error", (
        '{"status":"{{4.fail_status}}","trade":"{{2.trade}}","technician":"{{2.tech}}","timezone":"America/New_York",'
        '"slots":[],"slot_count":0,"maps_status":"{{4.maps_status}}","message":"'
        + iml('if(4.fail_status = "address_not_found"; "The address could not be located. Ask the caller to repeat the '
              'street number, street name, city and ZIP code, then call check_availability again."; '
              '"The scheduling system is temporarily unavailable. Apologise and tell the caller a team member will '
              'call back shortly to schedule.")')
        + '"}'
    ), 1500, -200, filt("Address not found or Maps error",
                        cond("{{2.tech}}", "text:notequal", ""), cond("{{4.address_ok}}", "text:equal", "no")))

    window = setvars(8, "Time window", [
        ("time_min", iml(fmt(MIDNIGHT, "YYYY-MM-DDTHH:mm:ssZ", '"UTC"'))),
        ("time_max", iml(fmt(f"addDays({MIDNIGHT}; 14)", "YYYY-MM-DDTHH:mm:ssZ", '"UTC"'))),
        ("window_to", iml(fmt("addDays(2.window_noon; 13)", "YYYY-MM-DD"))),
        ("earliest_ts", "{{2.now_ts + 7200}}"),
    ], 1500, 0, filt("Known trade and valid address",
                     cond("{{2.tech}}", "text:notequal", ""), cond("{{4.address_ok}}", "text:equal", "yes")))

    calendar = {
        "id": 9,
        "module": "google-calendar:makeApiCall",
        "version": 5,
        "parameters": {},
        "mapper": {
            "url": "/calendar/v3/calendars/{{2.calendar_id}}/events",
            "method": "GET",
            "headers": [],
            "qs": [
                {"key": "timeMin", "value": "{{8.time_min}}"},
                {"key": "timeMax", "value": "{{8.time_max}}"},
                {"key": "singleEvents", "value": "true"},
                {"key": "orderBy", "value": "startTime"},
                {"key": "maxResults", "value": "250"},
            ],
            "body": "",
        },
        "metadata": designer(1800, 0, "Calendar: events in window"),
        "onerror": on_error(9, 1800, 0),
    }

    iterator = {
        "id": 10,
        "module": "builtin:BasicFeeder",
        "version": 1,
        "parameters": {},
        "mapper": {"array": "{{9.body.items}}"},
        "metadata": designer(2100, 0, "Each event"),
    }

    event_times = setvars(11, "Event times (Eastern)", [
        ("all_day", iml(f'if({NO_DT}; "yes"; "no")')),
        ("date", iml(EVENT_DATE)),
        ("start_h", iml(f'if({NO_DT}; 0; parseNumber({fmt(P, "H")}))')),
        ("end_h_eff", iml(END_H_EFF)),
        ("location", iml('ifempty(10.location; "")')),
        ("has_location", iml('if(ifempty(10.location; "") = ""; "no"; "yes")')),
        *[(f"k{h:02d}", iml(EVENT_DATE) + f" {h:02d}|") for h in range(8, 16)],
    ], 2400, 0, filt("This technician's events, not cancelled",
                     cond('{{lower(join(map(10.attendees; "email"); ","))}}', "text:contain", "{{2.tech}}"),
                     cond("{{10.status}}", "text:notequal", "cancelled")))

    drive = maps_get(12, "Maps: drive time caller -> job", "{{ifempty(11.location; 2.company)}}", 2700, 0)

    event_row = setvars(13, "Event row", [
        ("date", "{{11.date}}"),
        ("hour_keys", "".join(iml(f'if(11.start_h <= {h} and 11.end_h_eff > {h}; 11.k{h:02d}; "")') for h in range(8, 16))),
        ("drive_seconds", iml(DRIVE)),
        ("far", iml(FAR)),
        ("debug_json", '{"date":"{{11.date}}","start_h":{{11.start_h}},"end_h":{{11.end_h_eff}},"all_day":"{{11.all_day}}",'
                       '"has_location":"{{11.has_location}}","drive_seconds":' + iml(DRIVE) + ',"far":"' + iml(FAR) + '"}'),
    ], 3000, 0)

    aggregator = {
        "id": 14,
        "module": "builtin:BasicAggregator",
        "version": 1,
        "parameters": {"feeder": 10},
        "mapper": {
            "date": "{{13.date}}",
            "hour_keys": "{{13.hour_keys}}",
            "far": "{{13.far}}",
            "drive_seconds": "{{13.drive_seconds}}",
            "debug_json": "{{13.debug_json}}",
        },
        "metadata": designer(3300, 0, "All events -> array"),
    }

    busy_map = setvars(15, "Busy map", [
        ("busy", iml('join(map(14.array; "hour_keys"); "")')),
        ("far_dates", iml('join(map(14.array; "date"; "far"; "yes"); "|")')),
        ("event_count", iml("length(14.array)")),
        ("debug_events", iml('if(2.debug = "yes"; join(map(14.array; "debug_json"); ","); "")')),
    ], 3600, 0)

    repeater = {
        "id": 16,
        "module": "builtin:BasicRepeater",
        "version": 1,
        "parameters": {},
        "mapper": {"start": "0", "repeats": "14", "step": "1"},
        "metadata": designer(3900, 0, "Each of 14 days"),
    }

    day = setvars(17, "Day (Eastern)", [
        ("date", iml(fmt(DAY, "YYYY-MM-DD"))),
        ("weekday", iml(f'parseNumber({fmt(DAY, "E")})')),  # ISO weekday 1 = Monday ... 7 = Sunday
        ("day_name", iml(fmt(DAY, "dddd"))),
        ("spoken_day", iml(fmt(DAY, "dddd, MMMM D"))),
        ("ts08", ts(8)), ("ts10", ts(10)), ("ts12", ts(12)), ("ts14", ts(14)),
        *[(f"k{h:02d}", iml(fmt(DAY, "YYYY-MM-DD")) + f" {h:02d}|") for h in range(8, 16)],
    ], 4200, 0)

    day_slots = setvars(18, "Day slots", [
        *[(f"s{h:02d}", slot_flag(h)) for h in (8, 10, 12, 14)],
        *[(f"frag{h:02d}", frag(h)) for h in (8, 10, 12, 14)],
    ], 4500, 0, filt("Working day, not excluded by the 15-minute rule",
                     cond("{{17.weekday}}", "number:lessorequal", "5"),
                     cond("{{15.far_dates}}", "text:notcontain", "{{17.date}}")))

    text_agg = {
        "id": 19,
        "module": "util:TextAggregator",
        "version": 1,
        "parameters": {"rowSeparator": "", "feeder": 16},
        "mapper": {"value": "".join(iml(f'if(18.s{h:02d} = "yes"; 18.frag{h:02d}; "")') for h in (8, 10, 12, 14))},
        "metadata": designer(4800, 0, "Open slots -> JSON"),
    }

    final = respond(20, "Respond: ok / no_availability", (
        '{"status":"' + iml('if(length(19.text) > 0; "ok"; "no_availability")') + '",'
        '"trade":"{{2.trade}}","technician":"{{2.tech}}","timezone":"America/New_York",'
        '"today":"{{2.today}}","today_spoken":"{{2.today_spoken}}","resolved_address":"{{4.resolved_address}}",'
        '"window":{"from":"{{2.today}}","to":"{{8.window_to}}"},'
        '"slot_count":' + iml(COUNT) + ','
        '"slots":[' + iml("substring(19.text; 1; length(19.text))") + '],'
        '"message":"' + iml(f'if(length(19.text) > 0; {COUNT}; "")')
        + iml(f'if(length(19.text) > 0; "{OK_MSG}"; "{NONE_MSG}")') + '",'
        '"agent_instructions":"Only offer windows from the slots list, using each slot\'s spoken text. '
        'Never invent a time. Ignore the debug field.",'
        '"debug":{"event_count":{{15.event_count}},"busy":"{{15.busy}}","far_dates":"{{15.far_dates}}",'
        '"now_ts":{{2.now_ts}},"earliest_ts":{{8.earliest_ts}},"events":[{{15.debug_events}}]}}'
    ), 5100, 0)

    router = {
        "id": 5,
        "module": "builtin:BasicRouter",
        "version": 1,
        "mapper": None,
        "metadata": designer(1200, 0),
        "routes": [
            {"flow": [unknown]},
            {"flow": [address_fail]},
            {"flow": [window, calendar, iterator, event_times, drive, event_row, aggregator, busy_map,
                      repeater, day, day_slots, text_agg, final]},
        ],
    }

    return {
        "name": "check_availability",
        "flow": [webhook, config, validate, address_check, router],
        "metadata": {
            "instant": True,
            "version": 1,
            "scenario": {
                "roundtrips": 1,
                "maxErrors": 3,
                "autoCommit": True,
                "autoCommitTriggerLast": True,
                "sequential": False,
                "slots": None,
                "confidential": False,
                "dataloss": False,
                "dlq": False,
                "freshVariables": False,
            },
            "designer": {"orphans": []},
            "zone": "eu1.make.com",
            "notes": [],
        },
    }


if __name__ == "__main__":
    OUT.write_text(json.dumps(build(), indent=2) + "\n", encoding="utf-8")
    print(f"wrote {OUT}")
