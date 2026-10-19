"""Make the golden fixtures for tests/core from the reference implementation that tifa.core was ported from.

    uv run python tools/make_core_fixtures.py REFERENCE_MODULE [--widget WIDGET_MODULE] [--out DIR]

REFERENCE_MODULE is the path to the Python module holding the original TfL recording logic (observe, advance,
causes, month files, London days); WIDGET_MODULE, optionally, the path to the module with the original London
midnight and UTC month helpers. Each is loaded from its file under a neutral name and run on fixed inputs: the real
TfL response in tests/fixtures/core/tfl_status_sample.json, and synthetic payloads covering every severity, planned
and unplanned works, validity now and later, several statuses on one line, reason prefixes, the GAP rule, month
ends and the clock changes of 29 March and 25 October 2026. The results are written as golden_*.json beside the
sample, and the tests in tests/core compare tifa.core with them. Nothing is fetched, and a rerun writes the same
bytes.
"""

import argparse
import contextlib
import datetime as dt
import hashlib
import importlib.util
import io
import json
import random
import sys
import tempfile
from pathlib import Path

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "core"
SAMPLE = "tfl_status_sample.json"
UTC = dt.UTC
MIN, HOUR, DAY = 60, 3600, 86400

SEVERITY_NAMES = {
    0: "Special Service",
    1: "Closed",
    2: "Suspended",
    3: "Part Suspended",
    4: "Planned Closure",
    5: "Part Closure",
    6: "Severe Delays",
    7: "Reduced Service",
    8: "Bus Service",
    9: "Minor Delays",
    10: "Good Service",
    11: "Part Closed",
    12: "Exit Only",
    13: "No Step Free Access",
    14: "Change of frequency",
    15: "Diverted",
    16: "Not Running",
    17: "Issues Reported",
    18: "No Issues",
    19: "Information",
    20: "Service Closed",
    42: "Something New",
}


def ts(text: str) -> int:
    return int(dt.datetime.fromisoformat(text).timestamp())


def load(path: str, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def plain(value):
    """JSON-ready: tuples as lists, sets sorted, dates as ISO strings."""
    if isinstance(value, dict):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return sorted(plain(v) for v in value)
    if isinstance(value, dt.date):
        return value.isoformat()
    return value


# ---- synthetic TfL payloads ----------------------------------------------------------------------------------


def status(sev, desc=None, reason=None, category=None, now=None):
    """A lineStatuses entry. now=None gives no validity periods (as Good Service has); True/False one period."""
    s = {
        "statusSeverity": sev,
        "statusSeverityDescription": desc if desc is not None else SEVERITY_NAMES.get(sev, f"Code {sev}"),
        "validityPeriods": []
        if now is None
        else [{"fromDate": "2026-10-19T07:00:00Z", "toDate": "2026-10-20T00:29:00Z", "isNow": now}],
    }
    if reason is not None:
        s["reason"] = reason
    if category is not None:
        s["disruption"] = {"category": category, "categoryDescription": category}
    return s


GOOD = {"statusSeverity": 10, "statusSeverityDescription": "Good Service", "validityPeriods": []}


def line(line_id, name, mode, *statuses):
    return {"id": line_id, "name": name, "modeName": mode, "lineStatuses": list(statuses) or [dict(GOOD)]}


def minor(reason="Central Line: Minor delays due to a signal failure.", now=True):
    return status(9, "Minor Delays", reason, "RealTime", now)


def part_closure(reason="District Line: Saturday, no service between Earl's Court and Richmond.", now=True):
    return status(5, "Part Closure", reason, "PlannedWork", now)


def api(central=(), district=(), bakerloo=()):
    return [
        line("bakerloo", "Bakerloo", "tube", *bakerloo),
        line("central", "Central", "tube", *central),
        line("district", "District", "tube", *district),
    ]


# ---- what TfL's messages put disruptions down to: a phrase for each alternative of each rule ------------------

PHRASES = [
    "a signal failure at Baker Street",
    "an earlier signalling problem",
    "signal failures",
    "a faulty train",
    "a faulty engineering train",
    "a faulty engineer's train",
    "faulty engineers' trains",
    "a faulty freight train",
    "a faulty empty train",
    "a defective train",
    "a defective freight train",
    "a train fault",
    "a train failure",
    "a failed train",
    "a broken down train",
    "a broken-down train",
    "a disabled train",
    "a derailment",
    "a derailed train",
    "a track fault",
    "a track defect",
    "a track failure",
    "a track inspection",
    "track repair",
    "track repairs",
    "faulty track",
    "a fault with the track",
    "a broken rail",
    "broken rails",
    "a rail defect",
    "a points failure",
    "a point fault",
    "a points problem",
    "faulty points",
    "damaged track",
    "a power failure",
    "power supply problems",
    "a power cut",
    "a power outage",
    "a power problem",
    "a power fault",
    "loss of power",
    "a loss power event",
    "an electrical fault",
    "an electrical failure",
    "an electrical problem",
    "damage to the overhead wire",
    "overhead line problems",
    "a communications failure",
    "communication equipment problems",
    "a communications fault",
    "a communication problem",
    "a communications system fault",
    "faulty communications",
    "a faulty platform",
    "a faulty tunnel fan",
    "a faulty station",
    "faulty equipment",
    "faulty infrastructure",
    "faulty platform doors",
    "platform edge door faults",
    "platform equipment",
    "a ventilation problem",
    "an equipment fault",
    "an equipment failure",
    "a structural issue",
    "a lineside cable fault",
    "an escalator fault",
    "escalators out of order",
    "lifts out of service",
    "a lift fault",
    "airlifts",
    "a customer incident",
    "customer action",
    "passenger action",
    "public action",
    "a person ill on a train",
    "a passenger who is ill",
    "a customer who has been unwell",
    "customers unwell",
    "a person taken ill",
    "taken ill",
    "a person on the track",
    "people on the track",
    "passengers on the track",
    "a trespasser",
    "trespass",
    "a casualty on the track",
    "a causality",
    "an injured customer",
    "an injured person",
    "an injured passenger",
    "a medical emergency",
    "a person under a train",
    "help a customer",
    "assist a passenger",
    "help a person",
    "an obstruction on the track",
    "obstructions",
    "a tree on the track",
    "fallen trees",
    "debris on the line",
    "an animal on the track",
    "animals on the line",
    "an object on the track",
    "objects from the track",
    "items on the track",
    "an item blowing onto the track",
    "a fallen branch",
    "branches on the line",
    "a branch on the track",
    "a bridge strike at Putney",
    "a vehicle hitting a bridge",
    "a shortage of staff",
    "the unavailability of staff",
    "a lack of drivers",
    "the availability of train operators",
    "the absence of operators",
    "a shortage of available controllers",
    "the unavailability of control room staff",
    "a lack of available station staff",
    "staff shortage",
    "staff availability",
    "staff unavailability",
    "a strike",
    "strikes by the union",
    "industrial action",
    "police activity",
    "a security alert",
    "a security incident",
    "a security issue",
    "a suspicious item",
    "a suspicious package",
    "a suspicious object",
    "emergency services attending",
    "an unattended item",
    "an unattended bag",
    "vandalism",
    "vandals",
    "an assault",
    "a gas leak",
    "a fire",
    "smoke in the tunnel",
    "an earlier fire alert",
    "fireworks",
    "a smoked out station",
    "flooding",
    "floods",
    "the weather",
    "the heat",
    "high temperatures",
    "a high temperature",
    "snow",
    "ice",
    "icy conditions",
    "frost",
    "leaf fall",
    "leaves on the line",
    "high winds",
    "strong winds",
    "a storm",
    "storms",
    "lightning",
    "difficult track conditions",
    "poor rail conditions",
    "poor adhesion",
    "a slippery rail",
    "slippery rails",
    "Heathrow traffic",
    "Snowdon",
    "a storming crowd",
    "iced drinks",
    "a late finish of engineering work",
    "improvement works",
    "upgrade works",
    "planned works",
    "maintenance",
    "a planned closure",
    "train cancellations",
    "a trains cancellation",
    "train cancelations",
    "cancelled trains",
    "a canceled train",
    "a shortage of trains",
    "fewer trains",
    "operational restrictions",
    "an operational restriction",
    "operating restrictions",
    "a reduced service",
    "a special service",
    "speed restrictions",
    "a speed restriction",
    "a Network Rail issue",
    "a National Rail fault",
    "something else entirely",
    "an earlier incident",
]
TEMPLATES = [
    "Minor delays due to {}.",
    "Severe delays while we deal with {}.",
    "No service between Ealing Broadway and White City due to {}. GOOD SERVICE on the rest of the line",
    "MINOR DELAYS FOLLOWING {}.",
    "Part suspended: {}",
]
PLANNED_TEXTS = [
    "No service between Kennington and Moorgate due to planned engineering work. MINOR DELAYS between Acton Town "
    "and Uxbridge due to train cancellations.",
    "No service between Kennington and Moorgate due to planned engineering work. Severe delays elsewhere due to a "
    "signal failure.",
    "No service due to planned engineering work.",
    "Planned closure. Minor delays due to a person ill on a train.",
    "Planned works this weekend between A and B. Severe delays due to a points failure",
    "No service due to planned closures. No service due to strike action.",
    "Minor delays due to train cancellations after a signal failure.",
    "Minor delays due to a bridge strike at Putney.",
    "Service will resume at 06:00 on Monday.",
    "No service due to strike action.",
    "A 15 minute service is operating between Baker Street and Watford.",
    "Service resumes later this morning.",
    "Good service on the rest of the line.",
    "",
    " ",
    "...",
]
STATUSES = [
    "Special Service",
    "Reduced Service",
    "Severe Delays",
    "Minor Delays",
    "Part Closure",
    "Information",
    "Good Service",
    "Suspended",
    "Planned Closure",
]


def cause_texts() -> list:
    texts = [TEMPLATES[i % len(TEMPLATES)].format(p) for i, p in enumerate(PHRASES)]
    return texts + PLANNED_TEXTS


# ---- tfl ----------------------------------------------------------------------------------------------------


def sample_lines(sample: list) -> list:
    return [(x["id"], x["name"], x["modeName"]) for x in sample]


def payload_cases(sample: list) -> dict:
    lines = sample_lines(sample)
    sevs = list(range(21)) + [42]
    every = {lid: [] for lid, _, _ in lines}
    variants = [("RealTime", True), ("PlannedWork", True), ("PlannedWork", False), ("Information", None), (None, True)]
    for i, sev in enumerate(sevs):
        for j, (cat, now) in enumerate(variants):
            lid, name, _ = lines[(i * len(variants) + j) % len(lines)]
            phrase = PHRASES[(i * 7 + j * 3) % len(PHRASES)]
            prefix = [f"{name} Line: ", f"{name.upper()}: ", f"{name} line:", "", f"{name}:  "][j]
            every[lid].append(status(sev, None, f"{prefix}{SEVERITY_NAMES[sev]} due to {phrase}.", cat, now))
    every_severity = [line(lid, name, mode, *every[lid]) for lid, name, mode in lines]

    long_reason = "Central Line:   " + "word " * 200
    mixed = [
        line(
            "district",
            "District",
            "tube",
            part_closure("District Line: No service between Earl's Court and Richmond."),
            part_closure("District Line: No service between Tower Hill and Upminster."),
            part_closure("District Line: no service between Tower Hill and Upminster."),
            status(5, "Part Closure", "District Line: Fault at Barking.", "RealTime", True),
            minor("District Line: Minor delays."),
        ),
        line("central", "Central", "tube", minor(long_reason), part_closure(now=False)),
        line("dlr", "DLR", "dlr", status(6, "Severe Delays", "DOCKLANDS LIGHT RAILWAY: Severe delays.", "RealTime")),
        line(
            "waterloo-city",
            "Waterloo & City",
            "tube",
            status(9, "Minor Delays", "Waterloo & City line: minor delays.", "RealTime", True),
            status(9, "Minor Delays", "Waterloo & City Line: Minor delays.", "RealTime", True),
        ),
        line(
            "elizabeth",
            "Elizabeth line",
            "elizabeth-line",
            status(7, "  Reduced   Service ", "Elizabeth line: Reduced service due to a trespasser.", "RealTime", True),
            status(19, "Information", "Note: lifts out of service at Bond Street.", "Information", None),
        ),
        line(
            "windrush",
            "Windrush",
            "overground",
            status(3, "Part Suspended", "London Overground: No service 12:30 to 14:00.", "RealTime", True),
            status(3, "", "Windrush: Part suspended.", "RealTime", True),
        ),
        line(
            "hammersmith-city",
            "Hammersmith & City",
            "tube",
            status(2, "Suspended", "Hammersmith & City Line : suspended due to a fire.", "RealTime", True),
            status(16, "Not Running", None, "PlannedWork", True),
        ),
        line(
            "victoria",
            "Victoria",
            "tube",
            status(9, "Minor Delays", "   ", "RealTime", True),
            status(
                6,
                "Severe Delays",
                "A really long prefix that runs well past forty chars: Severe delays.",
                "RealTime",
                True,
            ),
        ),
    ]
    junk = [
        "junk",
        None,
        42,
        {"name": "No id", "lineStatuses": [minor()]},
        {"id": "", "name": "Empty id", "lineStatuses": [minor()]},
        {"id": "noname", "modeName": "tube", "lineStatuses": [minor("Noname Line: Minor delays.")]},
        {"id": "badname", "name": 7, "lineStatuses": [minor()]},
        line(
            "victoria",
            "Victoria",
            "tube",
            {"statusSeverity": "9"},
            {"statusSeverity": True},
            "junk",
            {"statusSeverity": 9.0},
            {"statusSeverity": 6, "validityPeriods": [{"isNow": "true"}]},
            {"statusSeverity": 6, "validityPeriods": "now"},
            {"statusSeverity": 6, "validityPeriods": ["x"]},
            {"statusSeverity": 7, "disruption": "PlannedWork", "reason": "Victoria Line: Reduced."},
            {
                "statusSeverity": 3,
                "disruption": {"category": "PlannedWork"},
                "validityPeriods": [{"isNow": False}, {"isNow": True}],
            },
        ),
        {"id": "jubilee", "name": "Jubilee", "modeName": 5, "lineStatuses": "none"},
        {"id": "piccadilly", "name": "Piccadilly", "lineStatuses": None},
    ]
    reference_fixture = [
        line("bakerloo", "Bakerloo", "tube"),
        line("central", "Central", "tube", minor("Central Line: Minor delays due to a signal failure.  ")),
        line(
            "district",
            "District",
            "tube",
            part_closure("District Line: Saturday, no service between A and B.", now=False),
        ),
        line("dlr", "DLR", "dlr", status(20, "Service Closed")),
    ]
    filtering = [
        line("central", "Central", "tube", minor(), part_closure(now=False)),
        line("bakerloo", "Bakerloo", "tube", dict(GOOD)),
        line("waterloo-city", "Waterloo & City", "tube", status(20, "Service Closed")),
        line("elizabeth", "Elizabeth line", "elizabeth-line", status(18, "No Issues")),
        line("dlr", "DLR", "dlr", status(6, "Severe Delays", category="RealTime")),
        line("victoria", "Victoria", "tube", {"statusSeverity": "9"}, {"statusSeverity": True}, "junk"),
    ]
    planned = [
        line(
            "central",
            "Central",
            "tube",
            status(5, category="PlannedWork"),
            status(3, category="RealTime"),
            status(19, category="Information"),
            status(2),
        )
    ]
    no_statuses = [
        {"id": "central", "name": "Central", "modeName": "tube", "lineStatuses": []},
        {"id": "district", "name": "District", "modeName": "tube"},
        line("bakerloo", "Bakerloo", "tube", status(10, "Good Service"), status(18, "No Issues")),
    ]
    all_good = [line(lid, name, mode) for lid, name, mode in lines]
    sorted_mix = [
        line("dlr", "DLR", "dlr", status(9, reason="DLR: delays", category="RealTime", now=True)),
        line("windrush", "Windrush", "overground", status(9, reason="Delays", category="RealTime")),
        line("elizabeth", "Elizabeth line", "elizabeth-line", part_closure("Closed.")),
        line("bakerloo", "bakerloo", "tube", part_closure("Closed.")),
        line("central", "Central", "tube", status(6, category="RealTime"), status(42)),
        line("district", "District", "tube", status(42, category="RealTime")),
        line("circle", "Circle", "tube", status(19, "Information", "Circle line: info.", "Information")),
        line("mystery", "Mystery", "funicular", status(9, category="RealTime")),
        line("northern", "Northern", None, status(10), status(9, category="RealTime")),
    ]
    return {
        "sample": sample,
        "every_severity": every_severity,
        "mixed": mixed,
        "junk": junk,
        "reference_fixture": reference_fixture,
        "filtering": filtering,
        "planned": planned,
        "no_statuses": no_statuses,
        "all_good": all_good,
        "sorted_mix": sorted_mix,
        "no_id_only": [{"name": "No id", "lineStatuses": [minor()]}],
        "empty": [],
    }


def call(fn, *args):
    try:
        return {"ok": plain(fn(*args))}
    except Exception as err:  # the type is the golden: tifa raises the same kind
        return {"error": type(err).__name__}


def golden_tfl(ref, sample: list) -> dict:
    cases = payload_cases(sample)
    out = {"payloads": {}, "clean_reason": [], "rank": [], "category": [], "in_effect": []}
    for name, raw in cases.items():
        obs, meta = ref.observe(raw)
        out["payloads"][name] = {
            "raw": raw,
            "observe": {
                "obs": [[k[0], k[1], k[2], v["status"], v["reason"]] for k, v in obs.items()],
                "meta": plain(meta),
            },
            "current": call(ref.current, raw),
            "summarise": [call(ref.summarise, x) for x in raw if isinstance(x, dict)],
        }
    names = [
        "Central",
        "District",
        "DLR",
        "Waterloo & City",
        "Elizabeth line",
        "Windrush",
        "Hammersmith & City",
        "Bakerloo",
    ]
    texts = [
        "Central Line: Minor delays.",
        "central line:minor delays",
        "DOCKLANDS LIGHT RAILWAY: Severe delays.",
        "Waterloo & City line: minor delays.",
        "Elizabeth line: Reduced service.",
        "Elizabeth Line : Reduced.",
        "London Overground: No service.",
        "Windrush: Part suspended.",
        "Windrush line - part suspended",
        "Hammersmith & City Line:   Suspended.",
        "Note: lifts out of service.",
        "12:30 trains: cancelled.",
        "Line 2: closed.",
        "A really long prefix that runs well past forty chars: Severe delays.",
        "minor delays due to a signal failure.",
        "  multiple   spaces\n and\tnewlines  ",
        "",
        ":",
        "Central:",
        "Central: ",
        "The Bakerloo railway: closed.",
        "Bakerloo Railway:closed",
        "bakerloo:x",
        "Ünïcode line: ok",
        "ß line: Strasse",
        "Severe delays: power failure.",
        "Part suspended: trespasser.",
    ]
    out["clean_reason"] = [[t, n, ref.clean_reason(t, n)] for t in texts for n in names[:3]]
    out["clean_reason"] += [[t, n, ref.clean_reason(t, n)] for t, n in zip(texts, names * 4, strict=False)]
    out["clean_reason"].append([12345, "Central", ref.clean_reason(12345, "Central")])
    sevs = list(range(-1, 26)) + [42, None, "9", True, False, 9.0]
    out["rank"] = [[s, ref.rank(s)] for s in sevs]
    out["category"] = [[s, ref.category(s)] for s in sevs]
    effects = [
        status(9, now=True),
        status(9),
        {"statusSeverity": 9},
        status(5, now=False),
        {"validityPeriods": [{"isNow": False}, {"isNow": True}]},
        {"validityPeriods": [{"isNow": "true"}]},
        {"validityPeriods": None},
        {"validityPeriods": "x"},
        {"validityPeriods": ["x", {"isNow": 1}]},
        {"validityPeriods": {}},
        {"validityPeriods": [{}]},
    ]
    out["in_effect"] = [[s, ref.in_effect(s)] for s in effects]
    return out


def golden_causes(ref, sample: list) -> dict:
    texts = cause_texts()
    for raw in payload_cases(sample).values():  # the reasons in the payloads, as recorded
        obs, _ = ref.observe(raw)
        texts += [o["reason"] for o in obs.values() if o["reason"]]
    texts = list(dict.fromkeys(texts))
    cases = []
    for i, text in enumerate(texts):
        cases.append([text, False, None, ref.cause(text, False, None)])
        cases.append([text, True, None, ref.cause(text, True, None)])
        st = STATUSES[i % len(STATUSES)]
        cases.append([text, False, st, ref.cause(text, False, st)])
    for st in STATUSES + [None, 7]:
        for reason in (None, "", "Service resumes later.", 12):
            for planned in (False, True):
                cases.append([reason, planned, st, ref.cause(reason, planned, st)])
    return {
        "sig": ref.CAUSES_SIG,
        "rules": [list(c) for c in ref.CAUSES],
        "planned_works": ref.PLANNED_WORKS.pattern,
        "cases": cases,
    }


# ---- episodes ------------------------------------------------------------------------------------------------


class Payloads:
    """Payloads kept once each, referred to by number."""

    def __init__(self):
        self.items, self.index = [], {}

    def add(self, raw) -> int:
        text = json.dumps(raw, sort_keys=True)
        if text not in self.index:
            self.index[text] = len(self.items)
            self.items.append(raw)
        return self.index[text]


SEVS = [9, 9, 9, 6, 6, 7, 5, 5, 3, 2, 4, 1, 11, 16, 0, 12, 14, 15, 17, 8, 13, 19, 19, 20, 10, 18, 42]
STREAM_LINES = [
    ("central", "Central", "tube"),
    ("district", "District", "tube"),
    ("elizabeth", "Elizabeth line", "elizabeth-line"),
    ("windrush", "Windrush", "overground"),
    ("dlr", "DLR", "dlr"),
]


def random_stream(rng: random.Random, start: int, n: int, step: int, gap_chance: float, forced=None) -> list:
    """[(at, raw)]: n polls from start, step apart, with now and then a pause (some within GAP, some over), lines
    changing their statuses at random. forced(at) -> {line: [status]} adds statuses at given times."""
    current = {lid: [] for lid, _, _ in STREAM_LINES}
    polls, t = [], start
    for _ in range(n):
        for lid, name, _ in STREAM_LINES:
            r, statuses = rng.random(), current[lid]
            if r < 0.025:
                sev = rng.choice(SEVS)
                cat = rng.choice(["RealTime", "RealTime", "PlannedWork", "Information", None])
                now = rng.choice([True, True, True, True, False, None])
                text = rng.choice(cause_texts())
                reason = rng.choice([f"{name} Line: {text}", text, None, f"{name.upper()}: {text}"])
                statuses.append(status(sev, None, reason, cat, now))
            elif r < 0.05 and statuses:
                statuses.pop(rng.randrange(len(statuses)))
            elif r < 0.065 and statuses:
                s = statuses[rng.randrange(len(statuses))]
                s["reason"] = rng.choice([f"{name} Line: {rng.choice(cause_texts())}", None])
                if s["reason"] is None:
                    del s["reason"]
            elif r < 0.07 and statuses:
                s = statuses[rng.randrange(len(statuses))]
                if s["validityPeriods"]:
                    s["validityPeriods"][0]["isNow"] = not s["validityPeriods"][0]["isNow"]
        extra = forced(t) if forced else {}
        raw = [
            line(lid, name, mode, *[json.loads(json.dumps(s)) for s in current[lid]], *extra.get(lid, ()))
            for lid, name, mode in STREAM_LINES
        ]
        polls.append((t, raw))
        g = rng.random()
        t += rng.choice([600, 899, 900, 901, 1200, 3 * HOUR]) if g < gap_chance else step
    return polls


def episode_scenarios() -> dict:
    t0 = ts("2026-10-19T08:00:00+00:00")
    severe = status(6, "Severe Delays", "Severe.", "RealTime", True)
    out = {
        "open_continue_close": [
            (t0, api(central=[minor("Central Line: First reason.")])),
            (t0 + 120, api(central=[minor("Central Line: Second reason.")])),
            (t0 + 240, api(central=[minor(reason=None)])),
            (t0 + 360, api()),
        ],
        "severity_change": [(t0, api(central=[minor()])), (t0 + 120, api(central=[severe])), (t0 + 240, api())],
        "two_statuses": [
            (t0, api(district=[part_closure()])),
            (t0 + 120, api(district=[part_closure(), minor("District Line: Minor delays.")])),
            (t0 + 240, api(district=[part_closure()])),
            (t0 + 360, api()),
        ],
        "planned_and_unplanned": [
            (t0, api(district=[part_closure(), status(5, "Part Closure", "Fault.", "RealTime", True)])),
            (t0 + 60, api(district=[status(5, "Part Closure", "Fault.", "RealTime", True)])),
            (t0 + 120, api(district=[part_closure(), status(5, "Part Closure", "Fault.", "RealTime", True)])),
            (t0 + 180, api()),
        ],
        "gap_over_15_minutes": [
            (t0, api(central=[minor()], district=[part_closure()])),
            (t0 + 120, api(central=[minor()], district=[part_closure()])),
            (t0 + 120 + 16 * MIN, api(district=[part_closure()])),
            (t0 + 180 + 16 * MIN, api(district=[part_closure()])),
            (t0 + 240 + 16 * MIN, api()),
        ],
        "gap_exactly_900": [
            (t0, api(central=[minor()])),
            (t0 + 900, api(central=[minor()])),
            (t0 + 1800, api()),
            (t0 + 2701, api(central=[minor()])),
            (t0 + 2761, api(central=[minor()])),
            (t0 + 3662, api(central=[minor()])),
        ],
        "future_status": [
            (t0, api(district=[part_closure(now=False)])),
            (t0 + 60, api(district=[part_closure()])),
            (t0 + 120, api(district=[part_closure(now=False)])),
            (t0 + 180, api()),
        ],
        "line_vanishes": [
            (t0, api(bakerloo=[minor("Bakerloo Line: Delays.")])),
            (t0 + 120, api(bakerloo=[minor("Bakerloo Line: Delays.")])[1:]),
            (t0 + 240, api(bakerloo=[minor("Bakerloo Line: Delays.")])),
        ],
        "same_second_and_clock_back": [
            (t0, api(central=[minor()])),
            (t0, api()),
            (t0 - 60, api()),
            (t0 + 60, api(central=[minor()])),
            (t0 + 120, api()),
        ],
        "renamed_line": [
            (t0, api(central=[minor()])),
            (t0 + 60, [line("central", "Central (renamed)", "tube", minor())]),
            (t0 + 120, [line("central", "Central", None, minor("Central Line: New reason."))]),
        ],
    }
    mb = ts("2026-10-01T00:00:00+00:00")
    out["month_boundary"] = [
        (mb - 180, api(central=[minor()], district=[part_closure()])),
        (mb - 120, api(central=[minor()], district=[part_closure()])),
        (mb - 60, api(central=[minor()], district=[part_closure()], bakerloo=[severe])),
        (mb, api(district=[part_closure()], bakerloo=[severe])),
        (mb + 60, api(district=[part_closure()], central=[minor()])),
        (mb + 120, api(central=[minor()])),
        (mb + 180, api()),
    ]
    for name, at in (("dst_autumn", "2026-10-24T23:30:00+00:00"), ("dst_spring", "2026-03-28T23:30:00+00:00")):
        start = ts(at)
        out[name] = [
            (
                start + i * 300,
                api(central=[minor()] if i % 9 < 6 else [], district=[part_closure()] if 3 <= i < 30 else []),
            )
            for i in range(37)
        ]
    for seed, (at, n, step) in enumerate(
        (
            ("2026-10-24T22:00:00+00:00", 300, 60),
            ("2026-03-28T22:00:00+00:00", 200, 120),
            ("2026-09-30T21:00:00+00:00", 240, 60),
        )
    ):
        out[f"random_{seed}"] = random_stream(random.Random(1000 + seed), ts(at), n, step, 0.02)
    return out


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]


def run_reference_polls(ref, polls: list, tmp: Path, brief: bool = False) -> list:
    """Each poll through the reference as it ran in production: its open episodes saved and loaded again between
    polls, and nothing recorded for a poll not after the last one. brief: the open episodes as a digest, except
    after the last poll."""
    path, state, steps = tmp / "open.json", {"last_poll": None, "open": {}}, []
    for i, (at, raw) in enumerate(polls):
        if state["last_poll"] is not None and at <= state["last_poll"]:
            steps.append(None)
            continue
        obs, meta = ref.observe(raw)
        closed = ref.advance(state, obs, meta, at)
        ref.save_open(path, state)
        saved = json.loads(path.read_text(encoding="utf-8"))
        state = ref.load_open(path)
        if brief and i < len(polls) - 1:
            steps.append({"closed": closed, "open_sha": digest(saved["open"])})
        else:
            steps.append({"closed": closed, "open": saved["open"]})
    path.unlink(missing_ok=True)
    return steps


def golden_episodes(ref) -> dict:
    store, scenarios = Payloads(), {}
    with tempfile.TemporaryDirectory() as tmp:
        for name, polls in episode_scenarios().items():
            steps = run_reference_polls(ref, polls, Path(tmp), brief=name.startswith("random"))
            scenarios[name] = {"polls": [[at, store.add(raw)] for at, raw in polls], "steps": steps}
    return {"gap": ref.GAP, "reason_max": ref.REASON_MAX, "payloads": store.items, "scenarios": scenarios}


# ---- months --------------------------------------------------------------------------------------------------


def month_stream() -> list:
    """Polls over two month ends and the October clock change, with pauses and forced long episodes."""
    rng = random.Random(7)
    sep30 = ts("2026-09-30T00:00:00+00:00")
    oct1 = ts("2026-10-01T00:00:00+00:00")

    def forced(at):
        extra = {}
        if sep30 + 21 * HOUR <= at < oct1 + 2 * HOUR:  # a planned closure over the month end
            extra["district"] = [part_closure("District Line: Closed for works between A and B.")]
        if sep30 + 23 * HOUR <= at < oct1:  # ends exactly as October begins
            extra["central"] = [minor("Central Line: Minor delays due to a shortage of train operators.")]
        if oct1 <= at < oct1 + 30 * MIN:  # starts exactly as October begins
            extra["windrush"] = [
                status(2, "Suspended", "Windrush line: Suspended due to a fire alert.", "RealTime", True)
            ]
        if at >= sep30 + 20 * HOUR:  # information that stays open to the end
            extra.setdefault("elizabeth", []).append(
                status(19, "Information", "Elizabeth line: Lifts out of service at Bond Street.", "Information")
            )
        return extra

    polls = random_stream(rng, sep30 + 20 * HOUR, 241, 120, 0.0, forced)
    polls += random_stream(rng, ts("2026-10-15T12:00:00+00:00"), 1, 60, 0.0, forced)  # one poll alone
    polls += random_stream(rng, ts("2026-10-24T23:00:00+00:00"), 121, 120, 0.03, forced)
    polls += random_stream(rng, ts("2026-10-31T22:00:00+00:00"), 241, 60, 0.02, forced)
    polls.sort(key=lambda p: p[0])
    out, last = [], None
    for at, raw in polls:  # keep times increasing (a random pause can run into the next segment)
        if last is None or at > last:
            out.append((at, raw))
            last = at
    return out


def golden_month_stream(ref) -> dict:
    """The stream through the reference's whole recording step, and the month files, closed and open episodes
    it left."""
    store, polls = Payloads(), month_stream()
    with tempfile.TemporaryDirectory() as tmp:
        state_dir = Path(tmp) / "state"
        state_dir.mkdir()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            for at, raw in polls:
                if ref.update_history(state_dir, raw, at) is None:
                    raise SystemExit(f"reference recorded nothing at {at}")
        if err.getvalue():
            raise SystemExit(f"reference complained: {err.getvalue()[:500]}")
        tdir = state_dir / ref.TIMELINE_DIR
        months = {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in sorted(tdir.glob("????-??.json"))}
        data_dir = Path(tmp) / "tfl"
        closed = [json.loads(x) for x in (data_dir / "episodes.jsonl").read_text(encoding="utf-8").splitlines()]
        saved = json.loads((data_dir / "open.json").read_text(encoding="utf-8"))
    refs = [[at, store.add(raw)] for at, raw in polls]
    return {
        "payloads": store.items,
        "polls": refs,
        "months": months,
        "closed": closed,
        "open": saved["open"],
        "last_poll": saved["last_poll"],
    }


def golden_months(ref) -> dict:
    rng = random.Random(42)
    base = ts("2026-01-01T00:00:00+00:00")
    bounds = [ref.month_start(ref.month_index(base) + i) for i in range(14)]
    points = sorted(set(bounds + [b + d for b in bounds for d in (-61, -1, 1, 3600)]))
    eps = []
    for i in range(400):
        s = rng.choice(points) if i % 3 else rng.randrange(bounds[0] - DAY, bounds[-1])
        kind = rng.random()
        if kind < 0.1:
            e = None
        elif kind < 0.2:
            e = s
        elif kind < 0.5:
            e = rng.choice([p for p in points if p >= s] or [s])
        else:
            e = s + rng.choice([60, 3600, DAY, 20 * DAY, 45 * DAY, 100 * DAY])
        eps.append([f"l{i}", s, e, 9, "delays", 0, "a", None])
    first = ref.month_index(base)
    ranges = [(first, first), (first, first + 3), (first + 2, first + 11), (first - 1, first + 13)]
    split = []
    for lo, hi in ranges:
        got = ref.split_months(eps, lo, hi)
        split.append({"first": lo, "last": hi, "months": {ref.month_name(i): [e[0] for e in got[i]] for i in got}})

    spans = [[rng.randrange(0, 1000), 0] for _ in range(30)]
    for s in spans:
        s[1] = s[0] + rng.choice([0, 1, 10, 100, 400])
    clips = [
        {"spans": spans, "a": a, "b": b, "out": ref.clip_spans(spans, a, b)}
        for a, b in ((0, 1000), (100, 200), (500, 500), (450, 460), (-10, 2000), (300, 100))
    ]
    runs = []
    for seed in range(5):
        r, t, times = random.Random(seed), 1_790_000_000, []
        for _ in range(300):
            times.append(t)
            t += r.choice([60, 60, 60, 120, 899, 900, 901, 1800, 0])
        if seed == 4:
            times = times[:50] + [times[10]] + times[50:]  # one out of order: ignored
        runs.append({"times": times, "out": ref.runs_of(times)})
    merges = []
    for seed in range(5):
        r = random.Random(seed)
        spans = [[x, x + r.choice([0, 5, 50])] for x in (r.randrange(0, 500) for _ in range(40))]
        merges.append({"spans": spans, "out": ref.merge_spans(spans)})

    t0 = ts("2026-10-19T08:00:00+00:00")
    episode_args = [
        ["central", t0, t0 + 600, 9, "delays", False, "a", "Minor delays due to a signal failure.", "Minor Delays"],
        ["central", t0, None, 9, "delays", False, "a", "Minor delays due to a signal failure.", "Minor Delays"],
        ["district", t0, t0 + 600, 5, "closure", True, "a", "Saturday, no service between A and B.", "Part Closure"],
        ["district", t0, t0 + 600, 5, "closure", 1, "f", None, "Part Closure"],
        ["dlr", t0, t0, 0, "other", 0, "a", None, "Special Service"],
        ["dlr", t0, t0, 0, "other", False, "a", 12, 34],
        ["victoria", t0, t0 + 60, 6, "delays", False, "a", "Severe delays: power failure.", None],
        ["victoria", t0, t0 + 60, 19, "info", False, "a", "Lift out of service.", "Information"],
    ]
    month_eps = [[args, ref.month_episode(*args)] for args in episode_args]
    return {
        "split": {"episodes": eps, "ranges": split},
        "clip": clips,
        "runs": runs,
        "merge_spans": merges,
        "month_episode": month_eps,
        "stream": golden_month_stream(ref),
    }


# ---- time ----------------------------------------------------------------------------------------------------


def golden_time(ref, widget) -> dict:
    out = {}
    stamps = [
        0,
        1,
        59,
        86399,
        951782400,
        1774745999,
        1774746000,
        1774749600,
        1792890000,
        1792893600,
        ts("2026-10-19T08:00:00+00:00"),
        ts("2040-02-29T12:34:56+00:00"),
    ]
    out["iso"] = [[t, ref.iso(t)] for t in stamps]
    texts = [
        "2026-10-19T08:00:00+00:00",
        "2026-10-19T09:00:00+01:00",
        "2026-10-19T08:00:00Z",
        "2026-10-19T08:00:00.999+00:00",
        "2026-03-29T01:30:00+00:00",
        "2026-10-25T01:59:59-05:00",
        "2000-01-01T00:00:00+00:00",
    ]
    out["epoch"] = [[x, ref.epoch(x)] for x in texts]

    zone = ref.LONDON
    samples = []
    for at in (
        "2026-03-28T22:00:00+00:00",
        "2026-10-24T22:00:00+00:00",
        "2025-03-29T22:00:00+00:00",
        "2025-10-25T22:00:00+00:00",
        "2026-12-31T22:00:00+00:00",
        "2027-03-27T22:00:00+00:00",
    ):
        samples += [ts(at) + i * 15 * MIN for i in range(4 * 8)]
    out["london"] = [
        [
            t,
            dt.datetime.fromtimestamp(t, zone).isoformat(),
            dt.datetime.fromtimestamp(t, zone).hour,
            dt.datetime.fromtimestamp(t, zone).weekday(),
            dt.datetime.fromtimestamp(t, zone).year,
        ]
        for t in samples
    ]
    out["london_year"] = [[t, ref.london_year(t)] for t in samples[::7]]
    out["year_start"] = [[y, ref.year_start(y)] for y in range(2000, 2041)]
    ranges = [
        ("2026-10-24T23:00:00+00:00", "2026-10-25T23:59:00+00:00"),
        ("2026-10-24T23:00:00+00:00", "2026-10-24T23:00:00+00:00"),
        ("2026-03-28T12:00:00+00:00", "2026-03-30T12:00:00+00:00"),
        ("2026-03-28T23:59:59+00:00", "2026-03-29T00:00:00+00:00"),
        ("2026-09-30T22:00:00+00:00", "2026-10-02T01:00:00+00:00"),
        ("2026-12-31T23:30:00+00:00", "2027-01-01T00:30:00+00:00"),
        ("2026-10-25T00:00:00+00:00", "2026-10-26T00:00:00+00:00"),
        ("2026-10-24T22:59:59+00:00", "2026-10-31T00:00:01+00:00"),
        ("2026-10-25T23:00:00+00:00", "2026-10-25T23:00:00+00:00"),
    ]
    out["london_days"] = [[ts(a), ts(b), plain(ref.london_days(ts(a), ts(b)))] for a, b in ranges]

    months = []
    for t in samples[::5] + stamps + [ts("2026-01-31T23:59:59+00:00"), ts("2026-02-01T00:00:00+00:00")]:
        i = ref.month_index(t)
        months.append([t, i, ref.month_name(i), ref.month_start(i), ref.month_start(i + 1)])
    out["months"] = months

    if widget is not None:
        days = (
            [dt.date(2026, 3, d) for d in range(27, 32)]
            + [dt.date(2026, 10, d) for d in range(23, 28)]
            + [dt.date(2026, 1, 1), dt.date(2024, 2, 29), dt.date(2030, 3, 31), dt.date(2031, 10, 26)]
        )
        out["midnight"] = [[d.isoformat(), widget.midnight(d)] for d in days]
        out["midnights"] = [[ts(a), ts(b), plain(widget.midnights(ts(a), ts(b)))] for a, b in ranges]
        keys = [
            (ts("2026-09-15T00:00:00+00:00"), ts("2026-11-01T00:00:00+00:00")),
            (ts("2026-10-01T00:00:00+00:00"), ts("2026-10-01T00:00:00+00:00")),
            (ts("2026-10-01T00:00:00+00:00"), ts("2026-10-01T00:00:01+00:00")),
            (ts("2025-12-31T23:59:59+00:00"), ts("2027-01-01T00:00:00+00:00")),
        ]
        out["month_keys"] = [[a, b, widget.month_keys(a, b)] for a, b in keys]
        # the UK-rule fallback, used when the time zone database is missing
        zone_saved, widget.LONDON = widget.LONDON, None
        try:
            fallback = []
            for year in (2000, 2007, 2019, 2026, 2027, 2035, 2040):
                for month, day in ((3, 24), (10, 24)):
                    start = ts(f"{year}-{month:02d}-{day:02d}T00:00:00+00:00")
                    fallback += [[t, widget.lt(t).isoformat()] for t in range(start, start + 8 * DAY, 1800)]
            out["fallback_london"] = fallback
            out["fallback_midnight"] = [[d.isoformat(), widget.midnight(d)] for d in days]
        finally:
            widget.LONDON = zone_saved
    return out


def write(out: Path, name: str, data) -> None:
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    (out / name).write_text(text + "\n", encoding="utf-8")
    print(f"{name}: {len(text) // 1024} KiB")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("reference", help="path to the reference module (a .py file)")
    parser.add_argument("--widget", help="path to the reference module with the London midnight helpers")
    parser.add_argument("--out", default=str(FIXTURES), help="where to write the golden files")
    args = parser.parse_args()
    ref = load(args.reference, "reference_core")
    widget = load(args.widget, "reference_widget") if args.widget else None
    out = Path(args.out)
    sample = json.loads((FIXTURES / SAMPLE).read_text(encoding="utf-8"))
    write(out, "golden_tfl.json", golden_tfl(ref, sample))
    write(out, "golden_causes.json", golden_causes(ref, sample))
    write(out, "golden_episodes.json", golden_episodes(ref))
    write(out, "golden_months.json", golden_months(ref))
    write(out, "golden_time.json", golden_time(ref, widget))


if __name__ == "__main__":
    main()
