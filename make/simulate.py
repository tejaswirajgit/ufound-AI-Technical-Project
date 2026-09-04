"""Runs make/check_availability.blueprint.json locally, against mocked Google APIs.

The blueprint is the thing that produces the slots, and slots are 45% of the score. Static
checks (tests/test_artifacts.py) only prove the JSON is well formed. This module executes it:
a small interpreter for the IML expressions the scenario uses, plus a bundle-flow runner for
the module types it contains. tests/test_blueprint_simulation.py feeds it random calendars and
diffs the response against reference/availability.py, so a typo in an expression fails the
build instead of surfacing on a live call.

It is not a general Make emulator. It implements exactly the 18 functions and 8 module types
this scenario uses, and models Make's semantics where they matter: 1-based array indexing,
0-based substring, numeric-only `+`, timezone arguments on date functions, filters stopping a
bundle, aggregators collapsing a loop.

    from make.simulate import run
    response = run(request={"args": {"trade": "Plumbing", "address": "..."}}, now=..., events=[...])
"""
from __future__ import annotations

import json
import math
import pathlib
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

BLUEPRINT = pathlib.Path(__file__).with_name("check_availability.blueprint.json")
MONTHS = ["January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


class IMLError(Exception):
    """Raised when an expression cannot be evaluated: a bug in the blueprint, not in the data."""


# --------------------------------------------------------------------------- dates

_FORMAT_TOKEN = re.compile(r"YYYY|MMMM|MMM|MM|M|dddd|ddd|DD|D|HH|H|hh|h|mm|m|ss|s|X|E|A|Z")


def _fmt_token(dt: datetime, token: str) -> str:
    if token == "YYYY":
        return f"{dt.year:04d}"
    if token == "MMMM":
        return MONTHS[dt.month - 1]
    if token == "MMM":
        return MONTHS[dt.month - 1][:3]
    if token == "MM":
        return f"{dt.month:02d}"
    if token == "M":
        return str(dt.month)
    if token == "dddd":
        return DAYS[dt.weekday()]
    if token == "ddd":
        return DAYS[dt.weekday()][:3]
    if token == "DD":
        return f"{dt.day:02d}"
    if token == "D":
        return str(dt.day)
    if token == "HH":
        return f"{dt.hour:02d}"
    if token == "H":
        return str(dt.hour)
    if token == "hh":
        return f"{(dt.hour % 12) or 12:02d}"
    if token == "h":
        return str((dt.hour % 12) or 12)
    if token == "mm":
        return f"{dt.minute:02d}"
    if token == "m":
        return str(dt.minute)
    if token == "ss":
        return f"{dt.second:02d}"
    if token == "s":
        return str(dt.second)
    if token == "X":
        return str(int(dt.timestamp()))
    if token == "E":
        return str(dt.isoweekday())
    if token == "A":
        return "AM" if dt.hour < 12 else "PM"
    if token == "Z":
        off = dt.utcoffset() or timedelta(0)
        total = int(off.total_seconds())
        sign = "+" if total >= 0 else "-"
        return f"{sign}{abs(total) // 3600:02d}:{abs(total) % 3600 // 60:02d}"
    raise IMLError(f"unknown format token {token}")


def format_date(value, pattern: str, tz: str | None, org_tz: str) -> str:
    if not isinstance(value, datetime):
        return ""  # Make renders an unparseable date as an empty string rather than failing
    dt = value.astimezone(ZoneInfo(tz or org_tz))
    return _FORMAT_TOKEN.sub(lambda m: _fmt_token(dt, m.group(0)), pattern)


def parse_date(value, tz: str | None, org_tz: str):
    """Google sends RFC3339 with an offset, or a bare date for all-day events."""
    if isinstance(value, datetime):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=ZoneInfo(tz or org_tz))


def _shift(dt: datetime, tz: str | None, org_tz: str, **kw) -> datetime:
    """Calendar-style edit in the given timezone: keep the wall clock, re-resolve the offset."""
    zone = ZoneInfo(tz or org_tz)
    local = dt.astimezone(zone)
    if "days" in kw:
        local = local + timedelta(days=kw.pop("days"))
        local = local.replace(tzinfo=zone)
    if kw:
        local = local.replace(**kw, tzinfo=zone)
    return local


# --------------------------------------------------------------------------- values


def to_number(value) -> float:
    """Strict: only a value that is entirely a number counts as one.

    This matters for comparisons. "2026-01-16" must not read as 2026, or two different dates
    would compare equal, which is how Make behaves and how the slot logic depends on it.
    """
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, datetime):
        return value.timestamp()
    text = str(value or "").strip()
    return float(text) if re.fullmatch(r"[+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?", text) else math.nan


def extract_number(value) -> float:
    """Lenient, for parseNumber(): Make pulls the number out of surrounding characters."""
    if isinstance(value, (bool, int, float, datetime)):
        return to_number(value)
    match = re.search(r"[+-]?\d+(?:\.\d+)?", str(value or ""))
    return float(match.group(0)) if match else math.nan


def to_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    return str(value)


def is_empty(value) -> bool:
    return value is None or value == "" or value == [] or (isinstance(value, float) and math.isnan(value))


# --------------------------------------------------------------------------- lexer / parser

_TOKEN = re.compile(
    r"""\s+
      | "(?:[^"\\]|\\.)*"
      | \d+(?:\.[A-Za-z_][A-Za-z0-9_]*|\[\d+\])+          # module path: 2.tech, 12.data[1].duration
      | \d+(?:\.\d+)?                                      # number
      | [A-Za-z_][A-Za-z0-9_]*
      | <=|>=|!=|=|<|>|\+|-|\(|\)|;""",
    re.X,
)
_COMPARISONS = {"=", "!=", "<", "<=", ">", ">="}


def tokenize(source: str) -> list[str]:
    out, pos = [], 0
    while pos < len(source):
        m = _TOKEN.match(source, pos)
        if not m:
            raise IMLError(f"cannot tokenize {source[pos:pos + 30]!r} in {source!r}")
        pos = m.end()
        if m.group(0).strip():
            out.append(m.group(0))
    return out


class Parser:
    """Recursive descent: or < and < comparison < additive < primary."""

    def __init__(self, tokens: list[str]):
        self.tokens, self.pos = tokens, 0

    def peek(self):
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def take(self, expected=None):
        token = self.peek()
        if expected and token != expected:
            raise IMLError(f"expected {expected!r}, found {token!r}")
        self.pos += 1
        return token

    def parse(self):
        node = self.parse_or()
        if self.peek() is not None:
            raise IMLError(f"trailing tokens from {self.peek()!r}")
        return node

    def parse_or(self):
        node = self.parse_and()
        while self.peek() == "or":
            self.take()
            node = ("or", node, self.parse_and())
        return node

    def parse_and(self):
        node = self.parse_cmp()
        while self.peek() == "and":
            self.take()
            node = ("and", node, self.parse_cmp())
        return node

    def parse_cmp(self):
        node = self.parse_add()
        while self.peek() in _COMPARISONS:
            node = ("cmp", self.take(), node, self.parse_add())
        return node

    def parse_add(self):
        node = self.parse_primary()
        while self.peek() in ("+", "-"):
            node = ("arith", self.take(), node, self.parse_primary())
        return node

    def parse_primary(self):
        token = self.take()
        if token is None:
            raise IMLError("unexpected end of expression")
        if token == "(":
            node = self.parse_or()
            self.take(")")
            return node
        if token.startswith('"'):
            # An IML literal is not JSON: only the quote and the backslash are escaped, so a
            # regex like "/[\x22]+/g" keeps its backslashes for the regex engine.
            return ("const", re.sub(r'\\(["\\])', r"\1", token[1:-1]))
        if re.fullmatch(r"\d+(\.\d+)?", token):
            return ("const", float(token))
        if re.match(r"^\d", token):
            return ("path", token)
        if self.peek() == "(":  # function call
            self.take("(")
            args = []
            if self.peek() != ")":
                args.append(self.parse_or())
                while self.peek() == ";":
                    self.take()
                    args.append(self.parse_or())
            self.take(")")
            return ("call", token, args)
        return ("name", token)


# --------------------------------------------------------------------------- evaluator

_LAZY = {"if", "ifempty"}  # arguments are only evaluated on the branch that is taken


class Evaluator:
    def __init__(self, ctx: "Context"):
        self.ctx = ctx

    def eval(self, node):
        kind = node[0]
        if kind == "const":
            return node[1]
        if kind == "path":
            return self.ctx.resolve(node[1])
        if kind == "name":
            if node[1] == "now":
                return self.ctx.now
            if node[1] in ("emptystring", "empty"):
                return ""
            if node[1] == "space":
                return " "
            raise IMLError(f"unknown name {node[1]!r}")
        if kind == "and":
            return bool(self.truthy(self.eval(node[1])) and self.truthy(self.eval(node[2])))
        if kind == "or":
            return bool(self.truthy(self.eval(node[1])) or self.truthy(self.eval(node[2])))
        if kind == "cmp":
            return self.compare(node[1], self.eval(node[2]), self.eval(node[3]))
        if kind == "arith":
            left, right = to_number(self.eval(node[2])), to_number(self.eval(node[3]))
            return left + right if node[1] == "+" else left - right
        if kind == "call":
            return self.call(node[1], node[2])
        raise IMLError(f"unknown node {kind}")

    @staticmethod
    def truthy(value) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value not in ("", "false", "0")
        if isinstance(value, float):
            return not math.isnan(value) and value != 0
        return bool(value)

    @staticmethod
    def compare(op: str, left, right) -> bool:
        ln, rn = to_number(left), to_number(right)
        if not (math.isnan(ln) or math.isnan(rn)):
            a, b = ln, rn
        elif op in ("=", "!="):
            a, b = to_text(left), to_text(right)
        else:
            raise IMLError(f"cannot order-compare {left!r} and {right!r}")
        return {"=": a == b, "!=": a != b, "<": a < b, "<=": a <= b, ">": a > b, ">=": a >= b}[op]

    def call(self, name: str, arg_nodes: list):
        if name in _LAZY:
            return getattr(self, f"fn_{name}")(arg_nodes)
        args = [self.eval(a) for a in arg_nodes]
        fn = getattr(self, f"fn_{name}", None)
        if fn is None:
            raise IMLError(f"unsupported IML function {name}()")
        return fn(*args)

    # -- lazy ---------------------------------------------------------------
    def fn_if(self, nodes):
        if len(nodes) != 3:
            raise IMLError("if() takes 3 arguments")
        return self.eval(nodes[1] if self.truthy(self.eval(nodes[0])) else nodes[2])

    def fn_ifempty(self, nodes):
        value = self.eval(nodes[0])
        return self.eval(nodes[1]) if is_empty(value) else value

    # -- text ---------------------------------------------------------------
    def fn_lower(self, value):
        return to_text(value).lower()

    def fn_upper(self, value):
        return to_text(value).upper()

    def fn_trim(self, value):
        return to_text(value).strip()

    def fn_length(self, value):
        return float(len(value)) if isinstance(value, (list, dict)) else float(len(to_text(value)))

    def fn_contains(self, haystack, needle):
        if isinstance(haystack, list):
            return any(to_text(x) == to_text(needle) for x in haystack)
        return to_text(needle) in to_text(haystack)

    def fn_substring(self, value, start, end=None):
        text = to_text(value)
        i = int(to_number(start))
        j = len(text) if end is None else int(to_number(end))
        return text[i:j]

    def fn_replace(self, value, search, replacement):
        text, repl = to_text(value), to_text(replacement)
        pattern = to_text(search)
        m = re.fullmatch(r"/(.*)/([gimsu]*)", pattern, re.S)
        if m:
            body, flags = m.group(1), m.group(2)
            re_flags = (re.I if "i" in flags else 0) | (re.S if "s" in flags else 0) | (re.M if "m" in flags else 0)
            return re.sub(body, repl.replace("\\", "\\\\"), text, count=0 if "g" in flags else 1, flags=re_flags)
        return text.replace(pattern, repl)

    def fn_switch(self, value, *rest):
        key = to_text(value)
        pairs, default = list(rest), ""
        if len(pairs) % 2 == 1:
            default = pairs.pop()
        for i in range(0, len(pairs), 2):
            if to_text(pairs[i]) == key:
                return pairs[i + 1]
        return default

    # -- arrays -------------------------------------------------------------
    def fn_join(self, array, separator=""):
        if not isinstance(array, list):
            array = [] if is_empty(array) else [array]
        return to_text(separator).join(to_text(x) for x in array)

    def fn_map(self, array, key, filter_key=None, filter_value=None):
        if not isinstance(array, list):
            return []
        rows = array
        if filter_key is not None:
            rows = [r for r in rows if isinstance(r, dict) and to_text(r.get(to_text(filter_key))) == to_text(filter_value)]
        out = []
        for row in rows:
            value = row.get(to_text(key)) if isinstance(row, dict) else None
            if not is_empty(value):
                out.append(value)
        return out

    # -- numbers / dates ----------------------------------------------------
    def fn_parseNumber(self, value, *_):
        return extract_number(value)

    def fn_formatDate(self, value, pattern, tz=None):
        return format_date(value, to_text(pattern), to_text(tz) or None, self.ctx.org_tz)

    def fn_parseDate(self, value, _pattern=None, tz=None):
        return parse_date(value, to_text(tz) or None, self.ctx.org_tz)

    def fn_addDays(self, value, days):
        return _shift(value, None, self.ctx.org_tz, days=int(to_number(days)))

    def fn_setHour(self, value, hour, tz=None):
        return _shift(value, to_text(tz) or None, self.ctx.org_tz, hour=int(to_number(hour)))

    def fn_setMinute(self, value, minute, tz=None):
        return _shift(value, to_text(tz) or None, self.ctx.org_tz, minute=int(to_number(minute)))

    def fn_setSecond(self, value, second, tz=None):
        return _shift(value, to_text(tz) or None, self.ctx.org_tz, second=int(to_number(second)), microsecond=0)


# --------------------------------------------------------------------------- context

_BLOCK = re.compile(r"\{\{(.*?)\}\}", re.S)


class Context:
    def __init__(self, now: datetime, org_tz: str = "UTC"):
        self.now, self.org_tz = now, org_tz
        self.outputs: dict[int, dict] = {}
        self.responses: list[str] = []
        self.evaluator = Evaluator(self)
        self._cache: dict[str, object] = {}

    def resolve(self, path: str):
        head, *rest = re.findall(r"[A-Za-z0-9_]+", path.split("[")[0])[:1] or [path],
        parts = re.findall(r"\.([A-Za-z_][A-Za-z0-9_]*)|\[(\d+)\]", path)
        module_id = int(re.match(r"\d+", path).group(0))
        value = self.outputs.get(module_id)
        for name, index in parts:
            if value is None:
                return None
            if name:
                value = value.get(name) if isinstance(value, dict) else None
            else:  # IML arrays are 1-based
                i = int(index) - 1
                value = value[i] if isinstance(value, list) and 0 <= i < len(value) else None
        return value

    def evaluate(self, template):
        """A mapper value is literal text with {{...}} blocks spliced in."""
        if not isinstance(template, str):
            return template
        blocks = list(_BLOCK.finditer(template))
        if not blocks:
            return template
        if len(blocks) == 1 and blocks[0].group(0) == template:
            return self._eval_source(blocks[0].group(1))
        return _BLOCK.sub(lambda m: to_text(self._eval_source(m.group(1))), template)

    def _eval_source(self, source: str):
        node = self._cache.get(source)
        if node is None:
            node = self._cache[source] = Parser(tokenize(source)).parse()
        return self.evaluator.eval(node)


# --------------------------------------------------------------------------- module runner

_OPERATORS = {
    "text:equal": lambda a, b: to_text(a) == to_text(b),
    "text:notequal": lambda a, b: to_text(a) != to_text(b),
    "text:contain": lambda a, b: to_text(b) in to_text(a),
    "text:notcontain": lambda a, b: to_text(b) not in to_text(a),
    "number:equal": lambda a, b: to_number(a) == to_number(b),
    "number:less": lambda a, b: to_number(a) < to_number(b),
    "number:lessorequal": lambda a, b: to_number(a) <= to_number(b),
    "number:greater": lambda a, b: to_number(a) > to_number(b),
    "exist": lambda a, b: not is_empty(a),
    "notexist": lambda a, b: is_empty(a),
}


def passes(module: dict, ctx: Context) -> bool:
    spec = module.get("filter")
    if not spec:
        return True
    for group in spec["conditions"]:
        if all(_OPERATORS[c["o"]](ctx.evaluate(c["a"]), ctx.evaluate(c.get("b", ""))) for c in group):
            return True
    return False


class StopBundle(Exception):
    """A filter rejected this bundle; the rest of the branch does not run for it."""


def run_flow(flow: list[dict], ctx: Context, http) -> None:
    index = 0
    while index < len(flow):
        module = flow[index]
        if not passes(module, ctx):
            raise StopBundle
        # Any module an aggregator names as its feeder emits several bundles: the built-in
        # iterator and repeater, and app modules like Google Calendar's Search events.
        sink = next((j for j in range(index + 1, len(flow))
                     if flow[j].get("parameters", {}).get("feeder") == module["id"]), None)
        if sink is not None:
            body, aggregator = flow[index + 1:sink], flow[sink]
            try:
                bundles = _bundles(module, ctx, http)
            except _ModuleError:
                run_flow(module.get("onerror", []), ctx, http)
                return
            rows = []
            for bundle in bundles:
                ctx.outputs[module["id"]] = bundle
                try:
                    run_flow(body, ctx, http)
                except StopBundle:
                    continue
                rows.append({k: ctx.evaluate(v) for k, v in (aggregator.get("mapper") or {}).items()})
            if aggregator["module"] == "util:TextAggregator":
                separator = to_text(aggregator["parameters"].get("rowSeparator", ""))
                ctx.outputs[aggregator["id"]] = {"text": separator.join(to_text(r.get("value")) for r in rows)}
            else:
                ctx.outputs[aggregator["id"]] = {"array": rows}
            index = sink + 1
            continue

        try:
            _run_module(module, ctx, http)
        except _ModuleError:
            run_flow(module.get("onerror", []), ctx, http)
            return
        index += 1


def _bundles(module: dict, ctx: Context, http) -> list[dict]:
    kind = module["module"]
    if kind == "builtin:BasicFeeder":
        array = ctx.evaluate(module["mapper"]["array"])
        return array if isinstance(array, list) else []
    if kind == "builtin:BasicRepeater":
        start = int(to_number(ctx.evaluate(module["mapper"]["start"])))
        repeats = int(to_number(ctx.evaluate(module["mapper"]["repeats"])))
        step = int(to_number(ctx.evaluate(module["mapper"].get("step", "1")))) or 1
        return [{"i": float(start + n * step)} for n in range(repeats)]
    mapper = {k: ctx.evaluate(v) if isinstance(v, str) else v for k, v in (module.get("mapper") or {}).items()}
    result = http(module["id"], mapper)
    if result is None:
        raise _ModuleError(f"module {module['id']} failed")
    return result if isinstance(result, list) else [result]


class _ModuleError(Exception):
    pass


def _run_module(module: dict, ctx: Context, http) -> None:
    kind, mid = module["module"], module["id"]
    if kind == "util:SetVariables":
        ctx.outputs[mid] = {v["name"]: ctx.evaluate(v["value"]) for v in module["mapper"]["variables"]}
    elif kind == "http:ActionSendData" or kind.startswith("google-calendar:"):
        mapper = {k: ctx.evaluate(v) if isinstance(v, str) else v for k, v in module["mapper"].items()}
        mapper["qs"] = {i.get("name") or i.get("key"): ctx.evaluate(i["value"]) for i in module["mapper"].get("qs", [])}
        mapper["headers"] = {i.get("name") or i.get("key"): ctx.evaluate(i["value"]) for i in module["mapper"].get("headers", [])}
        result = http(mid, mapper)
        if result is None:
            raise _ModuleError(f"module {mid} failed")
        ctx.outputs[mid] = result
    elif kind == "gateway:WebhookRespond":
        ctx.responses.append(ctx.evaluate(module["mapper"]["body"]))
    elif kind == "builtin:BasicRouter":
        for route in module["routes"]:
            try:
                run_flow(route["flow"], ctx, http)
            except StopBundle:
                continue
    elif kind in ("builtin:Ignore", "gateway:CustomWebHook"):
        pass
    else:
        raise IMLError(f"unsupported module type {kind}")


def run(request: dict, now: datetime, http, org_tz: str = "UTC",
        blueprint: dict | None = None) -> dict:
    """Execute the scenario. `http(module_id, mapper) -> parsed response or None for a failure`."""
    bp = blueprint or json.loads(BLUEPRINT.read_text(encoding="utf-8"))
    ctx = Context(now, org_tz)
    ctx.outputs[1] = request
    try:
        run_flow(bp["flow"], ctx, http)
    except StopBundle:
        pass
    if not ctx.responses:
        raise IMLError("scenario produced no webhook response")
    return json.loads(ctx.responses[0])
