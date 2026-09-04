"""Static checks on the deliverable files: Retell tool + prompt, Make blueprint, no secrets."""
from __future__ import annotations

import importlib.util
import json
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
BLUEPRINT = ROOT / "make" / "check_availability.blueprint.json"
BUILDER = ROOT / "make" / "build_blueprint.py"
TOOL = ROOT / "retell" / "tool_check_availability.json"
PROMPT = ROOT / "retell" / "prompt.md"
SECRET_PATTERNS = [re.compile(r"AIza[0-9A-Za-z_\-]{35,}"), re.compile(r"hook\.[a-z0-9.\-]+\.make\.com/")]
IML_REF = re.compile(r"\{\{(\d+)\.")
IML_BLOCK = re.compile(r"\{\{.*?\}\}")


def walk(flow):
    for m in flow:
        yield m
        for route in m.get("routes", []):
            yield from walk(route["flow"])
        yield from walk(m.get("onerror", []))


def strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from strings(v)


@pytest.fixture(scope="module")
def bp():
    return json.loads(BLUEPRINT.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def mods(bp):
    return list(walk(bp["flow"]))


def test_no_secrets_in_deliverables():
    files = list((ROOT / "make").glob("*.json")) + list((ROOT / "retell").glob("*.json")) + [PROMPT]
    for path in files:
        text = path.read_text(encoding="utf-8")
        for rx in SECRET_PATTERNS:
            assert not rx.search(text), f"secret pattern {rx.pattern} in {path.name}"


def test_tool_definition():
    tool = json.loads(TOOL.read_text(encoding="utf-8"))
    assert tool["type"] == "custom" and tool["name"] == "check_availability"
    props = tool["parameters"]["properties"]
    assert props["trade"]["enum"] == ["Plumbing", "Electrical", "HVAC"]
    assert sorted(tool["parameters"]["required"]) == ["address", "trade"]
    assert tool["speak_during_execution"] is True and tool["timeout_ms"] >= 30000


def test_prompt_covers_the_scored_rules():
    text = PROMPT.read_text(encoding="utf-8").lower()
    for phrase in ["check_availability", "read the full address back", "never invent", "do not book",
                   "do not guess", "never say those three names", "no_availability", "address_not_found"]:
        assert phrase in text, phrase


def test_blueprint_regenerates_identically(bp):
    spec = importlib.util.spec_from_file_location("build_blueprint", BUILDER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.build() == bp


def test_module_ids_unique_and_references_resolve(mods):
    ids = [m["id"] for m in mods]
    assert len(ids) == len(set(ids))
    known = set(ids)
    for m in mods:
        for s in list(strings(m.get("mapper"))) + list(strings(m.get("filter"))):
            assert "{{" not in IML_BLOCK.sub("", s), f"unclosed IML in module {m['id']}: {s[:80]}"
            for ref in IML_REF.findall(s):
                assert int(ref) in known, f"module {m['id']} references missing module {ref}"


def test_every_response_body_is_json_with_status_and_slots(mods):
    responders = [m for m in mods if m["module"] == "gateway:WebhookRespond"]
    assert len(responders) == 6  # unknown_trade, address, ok/no_availability, 3 error handlers
    for m in responders:
        body = json.loads(IML_BLOCK.sub("0", m["mapper"]["body"]))
        assert "status" in body and isinstance(body["slots"], list), m["metadata"]["designer"]["name"]
        assert m["mapper"]["headers"] == [{"key": "Content-Type", "value": "application/json"}]


def test_rules_are_encoded(bp, mods):
    text = "\n".join(strings(bp))  # raw mapper strings, not JSON-escaped
    for needle in ['"plumbing"; "tech1@ufound-ai.com"', '"electrical"; "tech2@ufound-ai.com"', '"hvac"; "tech3@ufound-ai.com"',
                   "2.now_ts + 7200", "<= 900", 'condition = "ROUTE_EXISTS"', "singleEvents",
                   "routes.googleapis.com/distanceMatrix/v2:computeRouteMatrix", "maps.googleapis.com/maps/api/geocode/json"]:
        assert needle in text, needle
    by_id = {m["id"]: m for m in mods}
    assert by_id[16]["mapper"] == {"start": "0", "repeats": "14", "step": "1"}
    assert by_id[18]["filter"]["conditions"][0] == [{"a": "{{17.weekday}}", "o": "number:less", "b": "6"},
                                                     {"a": "{{17.far_day}}", "o": "text:equal", "b": "no"}]
    assert json.loads(IML_BLOCK.sub("0", by_id[12]["mapper"]["data"]))["travelMode"] == "DRIVE"
    assert by_id[12]["parameters"]["handleErrors"] is True and by_id[3]["parameters"]["handleErrors"] is True
    assert by_id[11]["filter"]["conditions"][0][1] == {"a": "{{10.status}}", "o": "text:notequal", "b": "cancelled"}
    for m in mods:
        if m["module"] == "http:ActionSendData" or m["module"].startswith("google-calendar"):
            assert m["onerror"][-1]["module"] == "builtin:Ignore", f"module {m['id']} has no error handler"
    # every formatDate/setHour carries an explicit timezone (parseDate of RFC3339 carries its own offset)
    assert text.count('"America/New_York"') + text.count('"UTC"') >= text.count("formatDate(") + text.count("setHour(")


def test_slot_fragments_match_reference_shape(mods):
    day_slots = next(m for m in mods if m["id"] == 18)
    frags = {v["name"]: v["value"] for v in day_slots["mapper"]["variables"] if v["name"].startswith("frag")}
    assert set(frags) == {"frag08", "frag10", "frag12", "frag14"}
    sample = json.loads(IML_BLOCK.sub("X", frags["frag12"])[1:])
    assert sample == {"date": "X", "day": "X", "start": "12:00", "end": "14:00", "spoken": "X, 12 PM to 2 PM"}
