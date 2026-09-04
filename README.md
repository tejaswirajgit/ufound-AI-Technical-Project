# Ufound Mechanical: inbound booking voice agent

Job classification and `check_availability` for the ufound AI technical project.
Stack: Retell AI (voice agent) + Make.com (webhook scenario) + Google Calendar (`ufound Dispatch`) + Google Maps (Geocoding API and Routes API).

The agent asks what is wrong, works out the trade (Plumbing, Electrical or HVAC) without ever
reading the trade list aloud, collects and confirms the service address, calls
`check_availability`, reads the open 2-hour windows, and stops. If the caller picks one it says a
team member will confirm shortly. Booking, confirmation and everything after are out of scope.

## Deliverables

| File | What |
|---|---|
| `retell/agent-export.json` | Retell agent export (webhook URL stripped) |
| `retell/prompt.md` | Agent prompt: identity, classification guide with one follow-up per ambiguous case, address collection, tool-result handling by status |
| `retell/tool_check_availability.json` | The single custom tool |
| `make/check_availability.blueprint.json` | Make.com scenario export (Maps key stripped) |
| `make/build_blueprint.py` | Generates the blueprint; every IML expression is readable here |
| `make/simulate.py` | Runs the blueprint locally against mocked Google responses, so its logic is tested before it reaches Make |
| `reference/availability.py` | Python reference of the slot rules. The scenario is checked against it, not eyeballed |
| `reference/check_webhook.py` | Live checker: posts Retell-shaped requests, validates every slot, recomputes the slots from the scenario's debug payload and diffs |
| `reference/check_maps_key.py` | Proves a Google Maps key works on both APIs before it goes anywhere near Make |
| `reference/localenv.py` | Reads `.env` so the scripts work the same in PowerShell, cmd and bash |
| `reference/strip_secrets.py` | Blanks the Maps key and webhook URL in raw exports before commit |
| `tests/` | 951 pytest cases: rules, randomized oracle stress test, blueprint simulation, artifact checks |
| Video walkthrough | Sent with the submission email |

## Rules implemented

Sources: the brief (sections 2, 4, 5, 6) and Eyal's clarifications of 3 September 2026.

| # | Rule | Source |
|---|---|---|
| 1 | Company timezone is America/New_York. "Central" in the brief was a typo | Eyal, and the `ufound Dispatch` calendar is itself set to Eastern Time |
| 2 | Working days and hours: Monday to Friday, 08:00 to 16:00 | Eyal, brief s6 |
| 3 | Slots are fixed: 8-10, 10-12, 12-14, 14-16 | Brief s4 |
| 4 | A slot is open when at least one of its two hours is free. It is closed only when both hours overlap a job | Eyal, brief s4 |
| 5 | Window: today plus the next 13 days (14 calendar days including today) | Brief s6 |
| 6 | Today only: no slot starting less than 2 hours from now | Brief s6 |
| 7 | A job belongs to a technician only through the attendee email on the `ufound Dispatch` event (case-insensitive) | Brief s2 |
| 8 | Plumbing = tech1, Electrical = tech2, HVAC = tech3 (`@ufound-ai.com`). No fallback technician; an unknown trade returns an error status | Brief s2, s4 |
| 9 | 15-minute rule: if the technician already has a job that day more than 15 minutes' drive from the caller's address, the whole day is excluded. Measured with Google Maps | Brief s6, Eyal |
| 10 | The agent never reads the list of trades and never opens with "plumbing, electrical or HVAC?" | Brief s4 |
| 11 | Ambiguous problems get one targeted follow-up question. The agent never guesses | Brief s4 |
| 12 | Only the address is collected: street number, street name, unit, city, ZIP. It is read back and confirmed before the tool call | Brief s5, s3 |

## Assumptions where the brief is silent

| Topic | Decision | Why |
|---|---|---|
| When is an hour busy | Any overlap, even partial: a job from 8:30 to 9:30 makes both the 8 and 9 hours busy | Conservative: never offers a window the technician is half-booked in beyond rule 4 |
| Event shown as "Free" or with the invite not accepted | Still a job | The sample event on the calendar is exactly that |
| Cancelled events | Ignored | Not a job |
| All-day event with the technician as attendee | Blocks the whole working day | Safest reading (vacation, training) |
| Job without a location | Cannot be measured, so it does not exclude the day; its hours are still busy | Nothing to measure |
| Maps cannot measure a job (error, NOT_FOUND) | Day excluded | Never offer a possibly invalid slot |
| Caller address cannot be located | `status: address_not_found`; the agent asks the caller to repeat it (twice at most) | Error handling |
| Maps or Calendar API down | `status: error`; the agent apologises and promises a callback | The call must not break |
| Distance direction | Caller address to each job location, driving, no traffic model | Deterministic and testable |

## How check_availability works

```
Caller ── phone ──> Retell agent (single prompt, one custom tool)
                        │  POST {name, args: {trade, address}, call}
                        ▼
              Make.com scenario "check_availability"
                        │
     ┌──────────────────┼──────────────────────┐
     ▼                  ▼                      ▼
Google Maps        Google Calendar        Slot engine (native Make modules:
validate address,  events in the 14-day   repeater over 14 days, fixed-width
drive time per job window, filtered by    busy-hour keys, filters, aggregators)
                   attendee email
                        │
                        ▼
      JSON: status, slots[] with spoken text, message for the agent
```

Module by module (ids as in the blueprint):

| ID | Module | Purpose |
|---|---|---|
| 1 | Custom webhook | Receives `{name, args: {trade, address}, call}` from Retell (the checker adds `debug: "yes"`) |
| 2 | Config & routing | `tech` by `switch(lower(trim(trade)))`, no fallback. `today`, `now_ts`, `window_noon` in Eastern time. Calendar id and Maps key live here |
| 3 | Maps: geocode caller address | Geocoding API. `ZERO_RESULTS` means the address could not be located; the formatted address and coordinates are kept for the drive-time calls. Error handler answers `status: error` |
| 4 | Address check | `address_ok`, `resolved_address`, `fail_status` |
| 5 | Router | Route 1 unknown trade, route 2 bad address or Maps error, route 3 main flow |
| 6 | Respond unknown_trade | Filter `tech = ""` |
| 7 | Respond address_not_found / error | Filter `address_ok = no` |
| 8 | Time window | `time_min` and `time_max` (midnight Eastern today and +14 days, sent as UTC), `window_to`, `earliest_ts = now + 7200` |
| 9 | Calendar: events in window | Google Calendar **Search events**, one bundle per event. Recurring jobs expanded, ordered by start, limit 250. Error handler |
| 11 | Event times (Eastern) | Filter: attendees contain the technician email, status not cancelled. Computes `date`, `end_date`, `start_h`, the exclusive end hour rounded up, and keys `k08`..`k15` and `j08`..`j15` = `YYYY-MM-DD HH\|` for the first and last day |
| 12 | Maps: drive time caller -> job | Routes API `computeRouteMatrix`: origin = caller coordinates, destination = job location (company address when a job has none, so the bundle keeps flowing). Traffic-unaware, so results are deterministic. Error handler |
| 13 | Event row | `hour_keys` for every hour the job touches, `drive_seconds`, `far` (over 900 s or unmeasurable), `debug_json` |
| 14 | All events -> array | Array aggregator, sourced from module 9 |
| 15 | Busy map | `busy` = all hour keys joined; `far_dates` = dates of far jobs, including a second day when the job runs past midnight |
| 16 | Each of 14 days | Repeater, i = 0..13 |
| 17 | Day (Eastern) | `date` from a noon anchor (DST-safe), ISO `weekday`, `spoken_day`, slot start timestamps, keys `k08`..`k15` |
| 18 | Day slots | Filter: weekday <= 5 and date not in `far_dates`. A slot is closed only when both of its hour keys are in `busy`; today also needs `ts >= earliest_ts`. Emits one JSON fragment per open slot |
| 19 | Open slots -> JSON | Text aggregator |
| 20 | Respond ok / no_availability | Status, slots, count, message, window, resolved address, debug block |

Why native modules only: the free plan has no Code module, and the logic stays visible in the
scenario for the walkthrough. Why fixed-width hour keys: `contains()` on `2026-09-09 08|` is an
exact lookup, so "both hours busy" is two substring checks. Why Search events rather than a raw
Calendar API call: Make's standard Google connection is not scoped for arbitrary Calendar API
requests and returns `403 insufficient authentication scopes`; widening it means registering
your own OAuth client. The native module works with the standard connection and reads better.

## Response

```json
{
  "status": "ok | no_availability | address_not_found | unknown_trade | error",
  "trade": "Plumbing",
  "technician": "tech1@ufound-ai.com",
  "timezone": "America/New_York",
  "today": "2026-09-04",
  "today_spoken": "Friday, September 4",
  "resolved_address": "1600 Barton Springs Rd, Austin, TX 78704, USA",
  "window": {"from": "2026-09-04", "to": "2026-09-17"},
  "slot_count": 6,
  "slots": [
    {"date": "2026-09-08", "day": "Tuesday", "start": "12:00", "end": "14:00",
     "spoken": "Tuesday, September 8, 12 PM to 2 PM"}
  ],
  "message": "6 open two-hour windows in the next 14 days. Offer the earliest two or three first, more only if the caller asks.",
  "agent_instructions": "Only offer windows from the slots list, using each slot's spoken text. Never invent a time. Ignore the debug field.",
  "debug": {"event_count": 3, "busy": "2026-09-08 08|2026-09-08 09|", "far_dates": "2026-09-10", "now_ts": 1757000000, "earliest_ts": 1757007200, "events": []}
}
```

`events` is filled only when the request carries `"debug": "yes"`; the checker uses it to
recompute the slots independently.

## Setup

Google Cloud: a project with billing enabled, the Geocoding API and the Routes API enabled, and
an API key restricted to those two. Verify it before wiring anything up:

```
cp env.example .env       # then put the real key in .env
python reference/check_maps_key.py
```

It geocodes a caller address, measures the drive to the company, and names the exact console
setting to change when either call is rejected. Secrets live in `.env`, which is gitignored;
`env.example` is the committed template. A real environment variable overrides the file. The
key is never taken as an argument and never printed. The legacy Distance Matrix API cannot be enabled on projects
created after March 2025, which is why the scenario uses `computeRouteMatrix`; both APIs have a
free monthly tier that covers this project many times over.

Make: Scenarios > Create > Import Blueprint > `make/check_availability.blueprint.json`. Then
open module 1 and create the webhook (copy its URL), open module 9 and pick the Google account
that has the `ufound Dispatch` calendar, open module 2 and paste the calendar id (Google
Calendar > calendar settings > Integrate calendar) and the Maps key. Save, turn the scenario on.
A call costs roughly 40 operations plus 5 per calendar event in the window.

Retell: create a single-prompt agent, paste `retell/prompt.md` (system prompt and begin
message), add the custom tool from `retell/tool_check_availability.json` with the webhook URL,
keep "speak during execution" on.

## Testing

```
python -m pytest -q                                   # 951 passed
python reference/check_webhook.py --matrix            # live: 3 trades x near/mid/far/bad address
python reference/check_webhook.py --stress 20 --concurrency 5
```

The scenario's logic is verified before it ever reaches Make. `make/simulate.py` is a small
interpreter for the expressions and module types this blueprint uses. It executes the real
exported file against mocked Google responses, so a typo in an expression fails the build
instead of surfacing on a live call.

- `tests/test_availability.py` (18): one case per rule, including the exact 15-minute edge, the
  2-hour lead-time boundary to the minute, partial overlaps, all-day events, UTC input, other
  technicians, mixed-case emails, a window starting on a Friday.
- `tests/test_stress.py` (608): 300 random calendars compared with an independent oracle written
  with different primitives, 300 invariant checks (no weekend, only the four fixed windows,
  inside the window, lead time respected, sorted, unique, spoken text well-formed), including
  both daylight-saving switch days, plus a 5,000-event timing check.
- `tests/test_blueprint_simulation.py` (324): the blueprint itself, executed. 300 random
  calendars produce exactly the reference's slots, the answer is independent of the Make
  organisation's timezone, and each rule and error path is pinned: one free hour keeps a slot,
  both hours busy closes it, a far or unmeasurable job clears the day, other technicians and
  cancelled events are ignored, an overnight job blocks the right hours on both days, a fully
  booked technician returns `no_availability`, and a failure of any of the three API modules
  returns `error` rather than breaking the call.
- `tests/test_artifacts.py`: the blueprint regenerates identically, module ids are unique, every
  IML reference resolves, every response body is valid JSON with `status` and `slots`, the
  routing, 7200-second and 900-second rules are present, every date function carries a
  timezone, no API key or webhook URL is committed.
- `reference/check_webhook.py`: live requests. Validates each slot, then rebuilds the events the
  scenario used from its debug block and diffs against the reference. Zero diff required.
  `--stress` fires concurrent requests and reports p50, p95 and max latency, status counts and
  any invariant violation.

## Blind spots and production notes

- Drive time is measured from the caller to each existing job, not between consecutive jobs,
  and without traffic (Routes API `TRAFFIC_UNAWARE`). A job at 8 AM and the caller at 4 PM are treated the same.
- A multi-day all-day event (a week of vacation) blocks only its first day. A timed job that
  runs past midnight is handled correctly on both of its days.
- Make's Search events module defaults to a limit of 10 and truncates silently. It is set to 250
  here, but a technician with more than 250 jobs in the window would still be cut off.
- `make/simulate.py` models Make's semantics from its documentation, not from Make itself. It
  catches logic and expression errors; it cannot catch a module id or connection that Make
  rejects at import. That is what the live webhook run is for.
- More than 250 events in the 14-day window would need pagination.
- The calendar is not re-checked between this call and the human confirmation; booking is out of scope.
- Retell retries a failed tool call; the scenario is read-only, so retries are harmless but cost operations.
- Make answers the webhook with "Accepted" if the scenario runs longer than about 40 seconds.
  The prompt treats any response without a status as an error and promises a callback.
- Free-plan operation budgets limit live testing to roughly ten calls a month; the Python
  reference and the stress test carry the logic verification instead.
- No service-area check: a caller far outside Austin gets every job-day excluded rather than a
  polite refusal.

## Repository layout

```
make/build_blueprint.py                     blueprint generator
make/simulate.py                            local blueprint interpreter (tests)
make/check_availability.blueprint.json      Make scenario (import this)
retell/prompt.md                            agent prompt and begin message
retell/tool_check_availability.json         custom tool definition
retell/agent-export.json                    Retell export
reference/availability.py                   rules, source of truth
reference/check_webhook.py                  live checker, cross-check, stress
reference/strip_secrets.py                  export hygiene
tests/                                      pytest suite
```
