"""Live checker for the Make.com check_availability webhook.

Posts Retell-shaped requests, validates every slot against the rules, and (with the
scenario's debug payload) recomputes the slots with reference/availability.py so the
Make output is verified, not eyeballed. Exit code 1 if anything is off.

    set MAKE_WEBHOOK_URL=https://hook.eu1.make.com/xxxx      (or pass --url)
    python reference/check_webhook.py --trade Plumbing --address "1600 Barton Springs Rd, Austin, TX 78704"
    python reference/check_webhook.py --matrix                 # 3 trades x near/mid/far/bad addresses
    python reference/check_webhook.py --stress 20 --concurrency 5   # load test, costs Make operations

Every request costs Make operations (roughly 40 + 5 per calendar event). The Free plan
has about 1,000 a month, so keep --matrix and --stress for when the scenario is stable.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import random
import statistics
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, time as dtime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from reference.availability import SLOTS, TZ, Event, available_slots, technician_for  # noqa: E402

STATUSES = {"ok", "no_availability", "address_not_found", "unknown_trade", "error"}
ADDRESSES = {
    "near": "4700 Burnet Rd, Austin, TX 78756",
    "mid": "1600 Barton Springs Rd, Austin, TX 78704",
    "far": "500 E Whitestone Blvd, Cedar Park, TX 78613",
    "bad": "99999 Nowhere Lane, Zzyzx, ZZ 00000",
}
TRADES = ["Plumbing", "Electrical", "HVAC"]


def post(url: str, trade: str, address: str, debug: bool = True, timeout: float = 60) -> tuple[float, int, str]:
    body = {"name": "check_availability", "args": {"trade": trade, "address": address}, "call": {"call_id": "local-check"}}
    if debug:
        body["debug"] = "yes"
    req = urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": "application/json"})
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return time.perf_counter() - t0, r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return time.perf_counter() - t0, e.code, e.read().decode("utf-8", "replace")


def validate(resp: dict) -> list[str]:
    """Rule checks that need no calendar knowledge: weekday, hours, window, ordering, shape."""
    problems = []
    status = resp.get("status")
    if status not in STATUSES:
        problems.append(f"bad status {status!r}")
    slots = resp.get("slots") or []
    if status == "ok" and not slots:
        problems.append("status ok but no slots")
    if status != "ok" and slots:
        problems.append(f"status {status} but {len(slots)} slots")
    today = date.fromisoformat(resp["today"]) if resp.get("today") else None
    for s in slots:
        try:
            d = date.fromisoformat(s["date"])
        except (KeyError, ValueError):
            problems.append(f"bad date in {s}")
            continue
        if d.weekday() >= 5:
            problems.append(f"weekend slot {s['date']} {s['start']}")
        if (int(s["start"][:2]), int(s["end"][:2])) not in SLOTS or s["start"][2:] != ":00" or s["end"][2:] != ":00":
            problems.append(f"invalid hours {s['date']} {s['start']}-{s['end']}")
        if today and not (today <= d <= today + timedelta(days=13)):
            problems.append(f"outside 14-day window {s['date']}")
        if s.get("day") != d.strftime("%A"):
            problems.append(f"day name mismatch {s}")
        if not s.get("spoken", "").startswith(d.strftime("%A, %B ") + str(d.day) + ", "):
            problems.append(f"spoken text mismatch {s}")
    keys = [(s["date"], s["start"]) for s in slots if "date" in s and "start" in s]
    if keys != sorted(keys):
        problems.append("slots not sorted")
    if len(keys) != len(set(keys)):
        problems.append("duplicate slots")
    if "slot_count" in resp and resp["slot_count"] != len(slots):
        problems.append(f"slot_count {resp['slot_count']} but {len(slots)} slots")
    return problems


def cross_check(resp: dict, trade: str) -> list[str]:
    """Rebuild the events Make used from its debug payload and diff against the reference rules."""
    dbg = resp.get("debug") or {}
    if "events" not in dbg or "now_ts" not in dbg:
        return ["no debug payload to cross-check (send debug: yes)"]
    now = datetime.fromtimestamp(int(dbg["now_ts"]), TZ)
    tech = technician_for(trade)
    events, drive = [], {}
    for i, e in enumerate(dbg["events"]):
        d = date.fromisoformat(e["date"])
        loc = f"job{i}" if e.get("has_location") == "yes" else ""
        if loc:
            secs = int(e.get("drive_seconds", -1))
            drive[loc] = None if secs < 0 else secs
        if e.get("all_day") == "yes":
            events.append(Event(datetime.combine(d, dtime(0), TZ), datetime.combine(d + timedelta(days=1), dtime(0), TZ),
                                [tech], loc, all_day=True))
            continue
        start = datetime.combine(d, dtime(int(e["start_h"])), TZ)
        end_h = int(e["end_h"])
        end = datetime.combine(d + timedelta(days=1), dtime(0), TZ) if end_h >= 24 else datetime.combine(d, dtime(end_h), TZ)
        events.append(Event(start, end, [tech], loc))
    expected = [(s["date"], s["start"]) for s in available_slots(now, trade, events, drive)]
    got = [(s["date"], s["start"]) for s in resp.get("slots") or []]
    if expected == got:
        return []
    return [f"reference expects {len(expected)} slots, Make returned {len(got)}",
            f"  only in reference: {sorted(set(expected) - set(got))}",
            f"  only in Make:      {sorted(set(got) - set(expected))}"]


def check_one(url: str, trade: str, address: str, timeout: float, verbose: bool = True) -> tuple[list[str], float, str]:
    elapsed, code, raw = post(url, trade, address, timeout=timeout)
    try:
        resp = json.loads(raw)
    except json.JSONDecodeError:
        return [f"HTTP {code}, non-JSON body: {raw[:200]!r}"], elapsed, "invalid"
    problems = validate(resp)
    if resp.get("status") in {"ok", "no_availability"}:
        problems += cross_check(resp, trade)
    if verbose:
        print(f"\n== {trade} @ {address}  ({elapsed:.1f}s, HTTP {code}) -> {resp.get('status')} "
              f"{len(resp.get('slots') or [])} slots")
        for s in resp.get("slots") or []:
            print(f"   {s.get('spoken')}")
        for p in problems:
            print(f"   PROBLEM: {p}")
        if not problems:
            print("   all checks passed")
    return problems, elapsed, str(resp.get("status"))


def stress(url: str, n: int, concurrency: int, timeout: float) -> int:
    rng = random.Random(1)
    cases = [(rng.choice(TRADES), ADDRESSES[rng.choice(["near", "mid", "far"])]) for _ in range(n)]
    print(f"stress: {n} requests, concurrency {concurrency} (this spends Make operations)")
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
        for problems, elapsed, status in pool.map(lambda c: check_one(url, c[0], c[1], timeout, verbose=False), cases):
            results.append((problems, elapsed, status))
    lat = sorted(r[1] for r in results)
    by_status: dict[str, int] = {}
    for _, _, s in results:
        by_status[s] = by_status.get(s, 0) + 1
    failures = [r for r in results if r[0]]
    p95 = lat[max(0, int(len(lat) * 0.95) - 1)]
    print(f"latency  p50 {statistics.median(lat):.1f}s  p95 {p95:.1f}s  max {lat[-1]:.1f}s")
    print(f"statuses {by_status}")
    print(f"failures {len(failures)}/{n}")
    for problems, _, status in failures[:10]:
        print(f"  {status}: {problems[0]}")
    return 1 if failures else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=os.environ.get("MAKE_WEBHOOK_URL"), help="Make webhook URL (or env MAKE_WEBHOOK_URL)")
    ap.add_argument("--trade", default="Plumbing")
    ap.add_argument("--address", default=ADDRESSES["mid"])
    ap.add_argument("--matrix", action="store_true", help="run 3 trades x near/mid/far/bad addresses")
    ap.add_argument("--stress", type=int, metavar="N", help="fire N requests")
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument("--timeout", type=float, default=60)
    args = ap.parse_args()
    if not args.url:
        ap.error("no webhook URL: pass --url or set MAKE_WEBHOOK_URL")
    if args.stress:
        return stress(args.url, args.stress, args.concurrency, args.timeout)
    cases = [(t, a) for t in TRADES for a in ADDRESSES.values()] if args.matrix else [(args.trade, args.address)]
    failed = 0
    for trade, address in cases:
        problems, _, _ = check_one(args.url, trade, address, args.timeout)
        failed += bool(problems)
    print(f"\n{len(cases) - failed}/{len(cases)} requests passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
