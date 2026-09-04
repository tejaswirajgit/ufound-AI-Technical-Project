"""Checks a Google Maps API key against the two APIs the scenario uses, before wiring up Make.

The key is read from the environment, never from an argument, so it stays out of shell history
and out of the process list. It is never printed.

    Git Bash / macOS / Linux:   export GOOGLE_MAPS_API_KEY=...
    Windows PowerShell:         $env:GOOGLE_MAPS_API_KEY="..."
    Windows cmd:                set GOOGLE_MAPS_API_KEY=...

    python reference/check_maps_key.py
    python reference/check_maps_key.py --address "1600 Barton Springs Rd, Austin, TX 78704"

Exit code 0 when both APIs answer and the 15-minute rule can be evaluated.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
ROUTES_URL = "https://routes.googleapis.com/distanceMatrix/v2:computeRouteMatrix"
COMPANY = "4820 Burnet Road, Austin, TX 78756, USA"
DEFAULT_ADDRESS = "1600 Barton Springs Rd, Austin, TX 78704"
MAX_DRIVE_SECONDS = 15 * 60

# What Google says when something is misconfigured, and what to actually do about it.
HINTS = {
    "REQUEST_DENIED": "The key is rejected. Either the Geocoding API is not enabled on this "
                      "project, or the key's API restrictions do not include it. "
                      "Console > APIs & Services > Credentials > your key > Restrict key.",
    "OVER_QUERY_LIMIT": "Quota or billing problem. Check that billing is enabled on the project.",
    "INVALID_REQUEST": "The request was malformed, which usually means an empty address.",
    "ZERO_RESULTS": "The key works. Google simply could not find that address.",
}


def request(url: str, *, data: bytes | None = None, headers: dict | None = None) -> tuple[int, object]:
    req = urllib.request.Request(url, data=data, headers=headers or {},
                                 method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(body)
        except json.JSONDecodeError:
            return e.code, {"raw": body[:400]}
    except urllib.error.URLError as e:
        return 0, {"raw": f"network error: {e.reason}"}


def google_message(payload: object) -> str:
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            return str(error.get("message", ""))
        return str(payload.get("error_message") or payload.get("raw") or "")
    return ""


def check_geocoding(key: str, address: str):
    """Returns (lat, lng, formatted_address, location_type) or None."""
    query = urllib.parse.urlencode({"address": address, "key": key})
    status, payload = request(f"{GEOCODE_URL}?{query}")
    if status != 200 or not isinstance(payload, dict):
        print(f"  FAIL  HTTP {status}. {google_message(payload)}")
        return None
    api_status = payload.get("status")
    if api_status != "OK":
        print(f"  FAIL  status {api_status}. {google_message(payload)}")
        if api_status in HINTS:
            print(f"        {HINTS[api_status]}")
        return None
    top = payload["results"][0]
    location = top["geometry"]["location"]
    kind = top["geometry"].get("location_type", "")
    print(f"  OK    {top['formatted_address']}")
    print(f"        match {kind}, {location['lat']:.5f}, {location['lng']:.5f}")
    if kind == "APPROXIMATE":
        print("        note: a city-level match. The scenario treats this as address_not_found "
              "and asks the caller to repeat the address.")
    return location["lat"], location["lng"], top["formatted_address"], kind


def check_routes(key: str, lat: float, lng: float, destination: str):
    """Returns driving seconds, or None."""
    body = json.dumps({
        "origins": [{"waypoint": {"location": {"latLng": {"latitude": lat, "longitude": lng}}}}],
        "destinations": [{"waypoint": {"address": destination}}],
        "travelMode": "DRIVE",
    }).encode()
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": key,
        "X-Goog-FieldMask": "originIndex,destinationIndex,status,condition,duration",
    }
    status, payload = request(ROUTES_URL, data=body, headers=headers)
    if status != 200:
        print(f"  FAIL  HTTP {status}. {google_message(payload)}")
        if status == 403:
            print("        Enable the Routes API on this project, and add it to the key's API "
                  "restrictions. It is a different API from Geocoding.")
        elif status == 400:
            print("        Check the X-Goog-FieldMask header; Routes requires one.")
        return None
    element = payload[0] if isinstance(payload, list) and payload else {}
    condition = element.get("condition")
    if condition != "ROUTE_EXISTS":
        print(f"  FAIL  no route: condition {condition}. {google_message(element.get('status'))}")
        print("        The scenario excludes a whole day when a job cannot be measured.")
        return None
    seconds = int(str(element.get("duration", "0s")).rstrip("s"))
    print(f"  OK    {seconds // 60} min {seconds % 60} s driving")
    return seconds


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--address", default=DEFAULT_ADDRESS, help="a caller address to test with")
    parser.add_argument("--job", default=COMPANY, help="a job address to measure the drive to")
    args = parser.parse_args()

    key = os.environ.get("GOOGLE_MAPS_API_KEY", "").strip()
    if not key:
        print("No key found. Set GOOGLE_MAPS_API_KEY in your environment first.", file=sys.stderr)
        print(__doc__, file=sys.stderr)
        return 2
    print(f"Key ending {key[-4:]}, {len(key)} characters.\n")

    print(f"1. Geocoding API, caller address: {args.address}")
    geocoded = check_geocoding(key, args.address)
    if not geocoded:
        return 1
    lat, lng, _, _ = geocoded

    print(f"\n2. Routes API, drive from the caller to: {args.job}")
    seconds = check_routes(key, lat, lng, args.job)
    if seconds is None:
        return 1

    verdict = "within" if seconds <= MAX_DRIVE_SECONDS else "over"
    print(f"\n3. 15-minute rule: {seconds // 60} min is {verdict} the limit, so a technician "
          f"with a job there would{'' if seconds <= MAX_DRIVE_SECONDS else ' not'} keep that day open.")
    print("\nBoth APIs work. Paste the key into Make module 2 (Config & routing), variable maps_key.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
