"""Build the period-stats test fixtures and their golden outputs.

    uv run python tools/make_stats_fixtures.py synth                   # synthetic months: tests/fixtures/stats/months/
    uv run python tools/make_stats_fixtures.py golden REFERENCE        # goldens from a reference implementation
    uv run python tools/make_stats_fixtures.py compare REFERENCE --months DIR   # Tifa against it, any month files

The synthetic months are made from a fixed seed, so they are the same on every run: about 14 months from the FOI
source ("f", the Underground only, Circle and Hammersmith & City as one shared line), a gap, then five months of
live recording ("a", every line) with gaps, open episodes, a month-spanning closure and both DST changes.

REFERENCE is a directory holding a reference implementation of these figures: one Python module that defines
window_stats() and report(), and one that defines period_figures(). The tool finds both by their definitions, loads
them by path and records what they return for the fixture months (golden), or compares them with tifa.core.stats on
any directory of month files plus an index.json (compare: nothing is written, differences are printed).
"""

from __future__ import annotations

import argparse
import calendar
import datetime as dt
import hashlib
import importlib.util
import json
import random
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "stats"
UTC = dt.UTC
LONDON = ZoneInfo("Europe/London")
DAY = 86400


def ts(text: str) -> int:
    return int(dt.datetime.fromisoformat(text).timestamp())


def iso(t: int) -> str:
    return dt.datetime.fromtimestamp(t, UTC).isoformat(timespec="seconds")


# ---------- synthetic months ----------

NOW = ts("2025-11-03T12:00:00+00:00")  # the fixtures' "now": a Monday, after the October clock change
FOI_FROM, FOI_TO = ts("2024-01-01T00:00:00+00:00"), ts("2025-02-13T00:00:00+00:00")
LIVE_FROM = ts("2025-06-01T08:00:00+00:00")
LIVE_GAPS = [  # the live recorder was off, or TfL didn't answer
    ("2025-06-15T01:00:00+00:00", "2025-06-15T06:30:00+00:00"),
    ("2025-07-04T22:00:00+00:00", "2025-07-05T09:00:00+00:00"),
    ("2025-08-10T12:00:00+00:00", "2025-08-14T08:00:00+00:00"),
    ("2025-09-30T23:30:00+00:00", "2025-10-01T00:45:00+00:00"),
    ("2025-10-25T20:00:00+00:00", "2025-10-25T22:00:00+00:00"),
    ("2025-11-02T02:00:00+00:00", "2025-11-02T03:00:00+00:00"),
]

LINES = [  # id, name, mode, shared_with
    ("bakerloo", "Bakerloo", "tube", None),
    ("central", "Central", "tube", None),
    ("circle", "Circle", "tube", None),
    ("circle-hammersmith-city", "Circle and Hammersmith & City", "tube", ["circle", "hammersmith-city"]),
    ("district", "District", "tube", None),
    ("hammersmith-city", "Hammersmith & City", "tube", None),
    ("jubilee", "Jubilee", "tube", None),
    ("victoria", "Victoria", "tube", None),
    ("elizabeth", "Elizabeth line", "elizabeth-line", None),
    ("mildmay", "Mildmay", "overground", None),
    ("dlr", "DLR", "dlr", None),
]
LINE_WEIGHTS = {
    "bakerloo": 3,
    "central": 5,
    "circle": 2,
    "circle-hammersmith-city": 3,
    "district": 5,
    "hammersmith-city": 2,
    "jubilee": 3,
    "victoria": 0.3,
    "elizabeth": 3,
    "mildmay": 2,
    "dlr": 2,
}
FOI_LINES = ["bakerloo", "central", "circle-hammersmith-city", "district", "jubilee", "victoria"]
LIVE_LINES = [lid for lid, _, _, shared in LINES if not shared]

CAUSE_LABELS = [
    ("signal", "Signal failure"),
    ("train", "Faulty train"),
    ("track", "Track fault"),
    ("power", "Power failure"),
    ("equipment", "Faulty equipment"),
    ("customer", "Customer incident"),
    ("obstruction", "Obstruction on the track"),
    ("staff", "Staff shortage"),
    ("strike", "Strike"),
    ("security", "Police or security"),
    ("fire", "Fire alert"),
    ("weather", "Weather"),
    ("engineering", "Engineering works"),
    ("cancellations", "Train cancellations"),
    ("restrictions", "Operational restrictions"),
    ("network-rail", "Network Rail"),
]
PHRASE = {
    "signal": "an earlier signal failure",
    "train": "a faulty train",
    "track": "a track fault",
    "power": "a power failure",
    "equipment": "faulty equipment at a station",
    "customer": "a customer incident",
    "obstruction": "an obstruction on the track",
    "staff": "a shortage of staff",
    "strike": "strike action",
    "security": "a police investigation",
    "fire": "a fire alert",
    "weather": "flooding",
    "engineering": "planned engineering work",
    "cancellations": "train cancellations",
    "restrictions": "operational restrictions",
    "network-rail": "a Network Rail fault",
    None: "an incident",
}
UNPLANNED_CAUSES = [
    ("signal", 6),
    ("cancellations", 5),
    ("train", 4),
    ("customer", 3),
    ("fire", 1),
    ("track", 1),
    ("restrictions", 1),
    ("strike", 0.5),
    ("staff", 0.5),
    ("obstruction", 0.5),
    ("weather", 0.3),
    ("security", 0.3),
    ("power", 0.3),
    ("equipment", 0.2),
    ("network-rail", 0.1),
    (None, 1),
]
# kind: (severity, status, category, planned, shortest s, longest s, weight for FOI, weight for live)
KINDS = [
    (9, "Minor Delays", "delays", 0, 600, 7200, 8, 10),
    (6, "Severe Delays", "delays", 0, 900, 5400, 4, 2),
    (7, "Reduced Service", "delays", 0, 1800, 10800, 0.3, 0.5),
    (3, "Part Suspended", "suspended", 0, 1200, 10800, 3, 2),
    (2, "Suspended", "suspended", 0, 600, 3600, 0.6, 0.6),
    (5, "Part Closure", "closure", 1, 18 * 3600, 44 * 3600, 1.5, 1.5),
    (4, "Planned Closure", "closure", 1, 4 * 3600, 20 * 3600, 1, 0.5),
    (0, "Special Service", "other", 0, 3600, 36000, 1, 0),
    (13, "No Step Free Access", "info", 0, DAY, 5 * DAY, 0, 1.5),
    (19, "Information", "info", 0, 2 * 3600, 8 * 3600, 0, 0.5),
]


def month_start(y: int, m: int) -> int:
    return calendar.timegm((y, m, 1, 0, 0, 0))


def month_of(t: int) -> tuple:
    d = dt.datetime.fromtimestamp(t, UTC)
    return d.year, d.month


def next_month(y: int, m: int) -> tuple:
    return (y + 1, 1) if m == 12 else (y, m + 1)


def months_between(a: int, b: int) -> list:
    """(year, month) of every UTC month overlapping [a, b)."""
    out, (y, m) = [], month_of(a)
    while month_start(y, m) < b:
        out.append((y, m))
        y, m = next_month(y, m)
    return out


def complement(spans: list, a: int, b: int) -> list:
    out, t = [], a
    for x, y in sorted(spans):
        if x > t:
            out.append([t, min(x, b)])
        t = max(t, y)
    if t < b:
        out.append([t, b])
    return [s for s in out if s[1] > s[0]]


def watched_spans() -> dict:
    gaps = [(ts(a), ts(b)) for a, b in LIVE_GAPS]
    return {"f": [[FOI_FROM, FOI_TO]], "a": complement(gaps, LIVE_FROM, NOW)}


def row(line, start, end, kind, cause, source, estimated=0, reason=None):
    sev, status, cat, planned = kind[:4]
    if reason is None:
        reason = f"{status} due to {PHRASE[cause]}." if cat != "info" else f"{status}: lifts out of service."
    return [line, start, end, sev, cat, planned, source, cause, status, reason, estimated]


def kind_of(sev: int) -> tuple:
    return next(k for k in KINDS if k[0] == sev)


def random_episodes(rng: random.Random, source: str, spans: list, lines: list, per_month: tuple) -> list:
    kinds = [k for k in KINDS if (k[6] if source == "f" else k[7]) > 0]
    weights = [k[6] if source == "f" else k[7] for k in kinds]
    out = []
    a, b = spans[0][0], spans[-1][1]
    for y, m in months_between(a, b):
        lo, hi = max(month_start(y, m), a), min(month_start(*next_month(y, m)), b)
        inside = [[max(x, lo), min(z, hi)] for x, z in spans if z > lo and x < hi]
        if not inside:
            continue
        for _ in range(rng.randint(*per_month)):
            span = rng.choices(inside, weights=[z - x for x, z in inside])[0]
            start = rng.randrange(span[0], span[1])
            kind = rng.choices(kinds, weights=weights)[0]
            end = start + rng.randrange(kind[4], kind[5])
            line = rng.choices(lines, weights=[LINE_WEIGHTS[x] for x in lines])[0]
            if kind[2] == "info":
                cause = rng.choice(["equipment", None])
            elif kind[3]:
                cause = "engineering" if rng.random() < 0.9 else "strike"
            else:
                cause = rng.choices([c for c, _ in UNPLANNED_CAUSES], weights=[w for _, w in UNPLANNED_CAUSES])[0]
            # one running past its watched run ends with it (estimated, when live), or is still open now
            run_end = next(z for x, z in spans if x <= start < z)
            estimated = 0
            if end > run_end:
                end, estimated = (None, 0) if run_end == NOW else (run_end, int(source == "a"))
            out.append(row(line, start, end, kind, cause, source, estimated))
    return out


def special_episodes() -> list:
    """Hand-placed cases the random ones may miss."""
    t = ts
    eps = [
        # open now: delays for nine hours, and step-free access out since before the month began
        row("central", NOW - 9 * 3600, None, kind_of(9), "signal", "a"),
        row("dlr", t("2025-10-28T09:00:00+00:00"), None, kind_of(13), "equipment", "a"),
        # a planned closure running over a month end
        row("district", t("2025-08-30T23:00:00+00:00"), t("2025-09-01T03:00:00+00:00"), kind_of(5), "engineering", "a"),
        # zero length, and one ended at the start of a gap (estimated)
        row("victoria", t("2025-10-29T08:00:00+00:00"), t("2025-10-29T08:00:00+00:00"), kind_of(9), "signal", "a"),
        row("mildmay", t("2025-08-10T11:00:00+00:00"), t("2025-08-10T12:00:00+00:00"), kind_of(9), "train", "a", 1),
        # a planned closure with delays inside it
        row("central", t("2025-10-11T06:00:00+00:00"), t("2025-10-12T20:00:00+00:00"), kind_of(5), "engineering", "a"),
        row("central", t("2025-10-11T09:00:00+00:00"), t("2025-10-11T10:00:00+00:00"), kind_of(9), "customer", "a"),
        # TfL's records over the spring clock change and a month end
        row("bakerloo", t("2024-03-30T22:00:00+00:00"), t("2024-03-31T20:00:00+00:00"), kind_of(4), "engineering", "f"),
        row(
            "circle-hammersmith-city",
            t("2024-03-31T20:00:00+00:00"),
            t("2024-04-01T02:00:00+00:00"),
            kind_of(0),
            "restrictions",
            "f",
        ),
        # the autumn change in live recording, 01:00-02:00 London happening twice
        row("elizabeth", t("2025-10-26T00:30:00+00:00"), t("2025-10-26T00:50:00+00:00"), kind_of(9), "signal", "a"),
        row("elizabeth", t("2025-10-26T01:10:00+00:00"), t("2025-10-26T01:40:00+00:00"), kind_of(6), "power", "a"),
    ]
    # the last week has more causes than are listed by name, Unknown among the fewest
    week = [
        ("bakerloo", "signal", 9),
        ("district", "signal", 9),
        ("jubilee", "signal", 6),
        ("circle", "train", 9),
        ("hammersmith-city", "train", 3),
        ("elizabeth", "customer", 9),
        ("mildmay", "customer", 6),
        ("dlr", "track", 9),
        ("district", "power", 3),
        ("central", "fire", 2),
        ("jubilee", "weather", 9),
        ("bakerloo", "staff", 9),
        ("victoria", None, 9),
    ]
    for i, (line, cause, sev) in enumerate(week):
        s = t("2025-10-27T07:00:00+00:00") + i * 43000
        eps.append(row(line, s, s + 1800 + 300 * i, kind_of(sev), cause, "a"))
    return eps


def build_months() -> tuple:
    """({"YYYY-MM": payload}, index) for the synthetic history."""
    rng = random.Random(20250101)
    watched = watched_spans()
    eps = (
        random_episodes(rng, "f", watched["f"], FOI_LINES, (12, 18))
        + random_episodes(rng, "a", watched["a"], LIVE_LINES, (34, 44))
        + special_episodes()
    )
    keys = sorted({k for spans in watched.values() for x, y in spans for k in months_between(x, y)})
    # a month end crossed while a copy was still open: the copy with an end is the one that counts
    s = ts("2025-10-31T22:00:00+00:00")
    eps.append(row("jubilee", s, ts("2025-11-01T01:00:00+00:00"), kind_of(6), "signal", "a"))
    stale = row("jubilee", s, None, kind_of(6), "signal", "a")
    months = {}
    for y, m in keys:
        lo, hi = month_start(y, m), month_start(*next_month(y, m))
        name = f"{y:04d}-{m:02d}"
        rows = [e for e in eps if e[1] < hi and (e[2] is None or e[2] > lo or (e[2] == lo and e[1] >= lo))]
        if name == "2025-10":
            rows = [e for e in rows if e[0] != "jubilee" or e[1] != s] + [stale]
        rows.sort(key=lambda e: (e[1], e[0], e[6], e[3], e[5], e[2] is None, e[2] or 0))
        months[name] = {
            "month": name,
            "episodes": [list(e) for e in rows],
            "watched": {
                src: [[max(a, lo), min(b, hi)] for a, b in spans if b > lo and a < hi]
                for src, spans in (("a", watched["a"]), ("f", watched["f"]))
            },
        }
    index = {
        "updated": iso(NOW),
        "months": sorted(months),
        "first": iso(FOI_FROM),
        "lines": [{"id": i, "name": n, "mode": md, "shared_with": sh} for i, n, md, sh in LINES],
        "categories": ["delays", "suspended", "closure", "other", "info"],
        "causes": [{"code": c, "label": label} for c, label in CAUSE_LABELS],
    }
    return months, index


def write_months(out: Path, months: dict, index: dict, fields: int = 11) -> None:
    out.mkdir(parents=True, exist_ok=True)
    for name, payload in months.items():
        doc = dict(payload, episodes=[e[:fields] for e in payload["episodes"]])
        (out / f"{name}.json").write_text(json.dumps(doc, separators=(",", ":")) + "\n", encoding="utf-8")
    (out / "index.json").write_text(json.dumps(index, separators=(",", ":")) + "\n", encoding="utf-8")


def read_months(folder: Path) -> tuple:
    index = json.loads((folder / "index.json").read_text(encoding="utf-8"))
    months = {}
    for name in index["months"]:
        path = folder / f"{name}.json"
        if path.exists():
            months[name] = json.loads(path.read_text(encoding="utf-8"))
    return months, index


# ---------- inputs shared by the goldens and the tests ----------

SPANS = ("1d", "7d", "30d", "90d", "365d")
ENDS = (
    "now",
    "2025-10-26",
    "2025-02-12",
    "2024-03-31",
    "2025-04-15",
    "2025-06-01",
    "2024-01-03",
    "2025-09-30",
    "2024-10-27",
    "2025-11-02",
)
REPORT_NOWS = (NOW, ts("2025-10-26T12:00:00+00:00"))
# late-evening nows (a period then starts in its first London day's last hour: Wed 23:30 GMT, Tue 23:30 BST and
# Mon 23:59 GMT, whose 24 hours start on a Sunday) and the night the clocks go forward
LATE_NOWS = (ts("2025-10-29T23:30:00+00:00"), ts("2025-10-21T22:30:00+00:00"), ts("2025-10-27T23:59:00+00:00"))
CORE_NOWS = (NOW, ts("2026-03-29T00:30:00+00:00")) + LATE_NOWS
LATE_SPANS = ("1d", "7d", "30d")
# period figures kept in full; the others as a digest of their JSON
FULL_FIGURES = {(span, "now") for span in SPANS} | {
    ("7d", "2025-10-26"),
    ("30d", "2025-02-12"),
    ("1d", "2024-03-31"),
    ("365d", "2025-02-12"),
    ("90d", "2025-09-30"),
    ("7d", "2025-06-01"),
}
LINE_PERIODS = {
    ("7d", "now"): None,
    ("30d", "2025-10-26"): None,
    ("1d", "2024-03-31"): None,
    ("365d", "2025-02-12"): ("bakerloo", "circle-hammersmith-city"),
}  # None: every line
GATHERS = (("7d", "now"), ("30d", "2025-02-12"))
# span=…&end=… queries; a dict of them (parse_qs) must give the same answer
QUERIES = (
    "span=7d&end=now",
    "end=2024-03-31&span=365d",
    "span=1d&end=2000-01-01",
    "span=30d&end=2025-11-03",
    "span=1d&end=2025-11-02",
    "",
    "span=7d",
    "end=now",
    "span=7d&end=now&end=now",
    "span=2d&end=now",
    "span=7D&end=now",
    "span=&end=now",
    "span=7d&end=",
    "span=7d&end=yesterday",
    "span=7d&end=2025-1-3",
    "span=7d&end=2025-02-30",
    "span=7d&end=1999-12-31",
    "span=7d&end=2025-11-04",
    "span=7d&end=" + "9" * 60,
    "span=365d&end=2020-02-29",
    "span=90d&end=2024-02-29T00:00",
)
WINDOW_CASES = [  # (episodes, start, now, covered seconds): episodes over a window edge, inside each other, open
    (
        [
            (9, "Minor Delays", "delays", False, NOW - 3 * DAY, NOW - 3 * DAY + 1800),
            (6, "Severe Delays", "delays", False, NOW - 7 * DAY - 3600, NOW - 7 * DAY + 1200),
            (9, "Minor Delays", "delays", False, NOW - 9 * DAY, NOW - 8 * DAY),
            (5, "Part Closure", "closure", True, NOW - 2 * DAY, NOW - 2 * DAY + 7200),
            (9, "Minor Delays", "delays", False, NOW - 2 * DAY + 3600, NOW - 2 * DAY + 5400),
            (13, "No Step Free Access", "info", False, NOW - 5 * DAY, None),
            (2, "Suspended", "suspended", False, NOW - 1200, None),
            (9, "Minor Delays", "delays", False, NOW - 7 * DAY - 1800, NOW - 7 * DAY),
        ],
        NOW - 7 * DAY,
        NOW,
        7 * DAY,
    ),
    ([(9, "Minor Delays", "delays", False, NOW - 60, NOW - 60)], NOW - DAY, NOW, DAY),
    ([(9, "Minor Delays", "delays", False, NOW - 3600, NOW - 1800)], NOW - DAY, NOW, 0),
    ([(9, "Minor Delays", "delays", False, NOW - 3600, NOW - 1800)], NOW - DAY, NOW, 600),
    ([], NOW - DAY, NOW, DAY),
]


def end_of(end: str):
    return end if end == "now" else dt.date.fromisoformat(end)


def snapshot(months: dict, now: int, names: dict | None = None) -> tuple:
    """The months as a live recorder would have them at `now`, and its open episodes: rows starting later are
    dropped, live rows still running at `now` become open episodes (OpenEpisode), watched spans end at `now`.
    Of copies of one episode in several months the one with an end counts."""
    best = {}
    for payload in months.values():
        for r in payload["episodes"]:
            key = (r[0], r[3], r[5], r[1], r[6])
            have = best.get(key)
            if have is None or (r[2] is not None and (have[2] is None or r[2] > have[2])):
                best[key] = r
    names = names or {lid: (name, mode) for lid, name, mode, _ in LINES}
    out, open_eps = {}, {}
    for name, payload in months.items():
        rows = []
        for r in payload["episodes"]:
            r = best[(r[0], r[3], r[5], r[1], r[6])]
            if r[1] >= now:
                continue
            if r[2] is None or r[2] > now:
                if r[6] == "a":
                    open_eps[(r[0], r[3], r[5], r[1])] = {
                        "line": r[0],
                        "name": names[r[0]][0],
                        "mode": names[r[0]][1],
                        "severity": r[3],
                        "status": r[8],
                        "category": r[4],
                        "planned": bool(r[5]),
                        "start": r[1],
                        "reason": r[9],
                    }
                    continue
                r = r[:2] + [None] + r[3:]
            rows.append(list(r))
        watched = {src: [[a, min(b, now)] for a, b in spans if a < now] for src, spans in payload["watched"].items()}
        out[name] = dict(payload, episodes=rows, watched=watched)
    return out, sorted(open_eps.values(), key=lambda e: (e["start"], e["line"], e["severity"]))


def plain(x):
    """JSON-ready: tuples as lists, dates as ISO, None and tuple keys as strings."""
    if isinstance(x, dict):
        return {
            ("null" if k is None else "|".join(map(str, k)) if isinstance(k, tuple) else k): plain(v)
            for k, v in x.items()
        }
    if isinstance(x, (list, tuple)):
        return [plain(v) for v in x]
    if isinstance(x, dt.date):
        return x.isoformat()
    return x


PERIOD_KEYS = (
    "span",
    "end",
    "n",
    "t0",
    "t1",
    "p0",
    "label",
    "before",
    "prev",
    "next",
    "today",
    "mids",
    "days",
    "first",
)


def digest(x) -> str:
    """The sha256 of x's JSON: the same only for the same values of the same types."""
    return hashlib.sha256(json.dumps(plain(x), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def period_entry(p: dict) -> dict:
    """period_of()'s fields for a golden: the London days in full up to a week, else their number and digest."""
    out = plain({k: p[k] for k in PERIOD_KEYS})
    if p["n"] > 7:
        out["mids"] = {"n": len(p["mids"]), "sha256": digest(p["mids"])}
        out["days"] = {"n": len(p["days"]), "sha256": digest(p["days"])}
    return out


def extra_of(mod, p: dict) -> dict:
    """line_period()'s extra for a period, as period_figures() makes it (mod: either implementation)."""
    bins = None
    if p["n"] >= 7:
        bins = p["days"] if p["n"] <= 30 else [p["t0"]] + [t for t, d in p["mids"] if d.weekday() == 0] + [p["t1"]]
    return {"days": p["days"], "bins": bins, "core": mod.core_spans(p)}


def figures_entry(fig: dict, full: bool):
    return plain(fig) if full else {"sha256": digest(fig)}


def core_entry(spans: list) -> dict | list:
    """core_spans()'s output for a golden: in full up to a week's worth (10 spans), else its number and digest."""
    return plain(spans) if len(spans) <= 10 else {"n": len(spans), "sha256": digest(spans)}


def core_keys() -> list:
    """(span, end, now) of the core-time goldens: every span ending now at each of CORE_NOWS, and at NOW ending on
    each date of ENDS."""
    return [(span, "now", now) for now in CORE_NOWS for span in SPANS] + [
        (span, end, NOW) for end in ENDS if end != "now" for span in SPANS
    ]


# ---------- the reference implementation ----------


class MemFiles:
    """Month payloads in memory, served as the reference reads its month files."""

    def __init__(self, months: dict, index: dict):
        self.docs = {f"{k}.json": v for k, v in months.items()} | {"index.json": index}

    def get(self, name: str, parse) -> tuple:
        doc = self.docs.get(name)
        return (None, None) if doc is None else (parse(json.loads(json.dumps(doc))), name)


def find_module(root: Path, *defs: str) -> Path:
    for path in sorted(root.rglob("*.py")):
        if any(part.startswith(".") or part in ("tests", "node_modules") for part in path.relative_to(root).parts):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if all(f"\ndef {d}(" in text for d in defs):
            return path
    raise SystemExit(f"no module under {root} defines {', '.join(defs)}")


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def reference(root: Path) -> tuple:
    """(report module, period module) of the reference implementation under root."""
    return (
        load(find_module(root, "window_stats", "report", "union_seconds", "add_episodes"), "reference_report"),
        load(find_module(root, "period_figures", "gather", "period_of"), "reference_period"),
    )


def reference_report(rep, months: dict, index: dict, now: int) -> dict:
    """The reference's per-line entries at `now` (without the strips), built as its recorder would hold them."""
    names = {line["id"]: [line["name"], line["mode"]] for line in index["lines"] if not line["shared_with"]}
    snap, open_eps = snapshot(months, now, names)
    closed = {}
    for payload in snap.values():
        for r in payload["episodes"]:
            if r[6] == "a" and r[2] is not None:
                closed[(r[0], r[3], r[5], r[1])] = r
    records = [
        {
            "line": r[0],
            "name": names[r[0]][0],
            "mode": names[r[0]][1],
            "severity": r[3],
            "status": r[8],
            "category": r[4],
            "planned": bool(r[5]),
            "start": iso(r[1]),
            "end": iso(r[2]),
            "end_estimated": bool(r[10]),
            "reason": r[9],
        }
        for r in sorted(closed.values(), key=lambda r: (r[2], r[1], r[0], r[3]))
    ]
    cache = rep.new_cache()
    rep.add_episodes(cache, records)
    covered = [list(s) for p in snap.values() for s in p["watched"].get("a", [])]
    runs = []
    for a, b in sorted(covered):
        if runs and a <= runs[-1][1]:
            runs[-1][1] = max(runs[-1][1], b)
        else:
            runs.append([a, b])
    cache.update(covered=runs, since=runs[0][0] if runs else None)
    state = {"last_poll": now, "open": {(e["line"], e["severity"], e["planned"]): dict(e) for e in open_eps}}
    data = rep.report(cache, state, names, now)
    return {
        line["id"]: dict({k: v for k, v in line.items() if k != "timeline"}, coverage_pct=data["coverage_pct"])
        for line in data["lines"]
    }


def reference_goldens(rep, per, months: dict, index: dict) -> dict:
    files = MemFiles(months, index)
    idx, _ = files.get("index.json", per._parse_index)
    today = dt.datetime.fromtimestamp(NOW, LONDON).date()
    out = {
        "now": NOW,
        "parse_period": [],
        "period_of": {},
        "gather": {},
        "cover_of": {},
        "line_period": {},
        "period_figures": {},
        "window_stats": [],
        "line_report": {},
        "core_spans": {},
        "period_figures_late": {},
    }
    for q in QUERIES:
        parsed, why = per.parse_period(q, today)
        out["parse_period"].append([q, today.isoformat(), plain(parsed), why])
    for now in (NOW, ts("2026-03-29T00:30:00+00:00")):  # and a now in the night the clocks go forward
        for span in SPANS:
            for end in ENDS:
                p = per.period_of(span, end_of(end), now, idx["first"])
                out["period_of"][f"{span}|{end}|{now}"] = period_entry(p)
    members = per.members_of(idx)
    for span in SPANS:
        for end in ENDS:
            p = per.period_of(span, end_of(end), NOW, idx["first"])
            data = per.gather(files, idx, p["p0"], p["t1"], NOW)
            fig = per.period_figures(idx, data, p, NOW)
            out["period_figures"][f"{span}|{end}"] = figures_entry(fig, (span, end) in FULL_FIGURES)
            if (span, end) in GATHERS:
                out["gather"][f"{span}|{end}"] = plain(data)
            if (span, end) == ("7d", "now"):
                out["cover_of"] = {
                    line["id"]: plain(per.cover_of(line, members, data["watched"])) for line in idx["lines"]
                }
            if (span, end) in LINE_PERIODS:
                extra = extra_of(per, p)
                out["line_period"][f"{span}|{end}"] = {"core": plain(extra["core"]), "lines": {}}
                for line in idx["lines"]:
                    if LINE_PERIODS[(span, end)] and line["id"] not in LINE_PERIODS[(span, end)]:
                        continue
                    cover = per.cover_of(line, members, data["watched"])
                    eps = data["eps"].get(line["id"], [])
                    out["line_period"][f"{span}|{end}"]["lines"][line["id"]] = plain(
                        {
                            "before": per.line_period(eps, cover, p["p0"], p["t0"]),
                            "period": per.line_period(eps, cover, p["t0"], p["t1"], extra),
                        }
                    )
    for span, end, now in core_keys():
        p = per.period_of(span, end_of(end), now, idx["first"])
        out["core_spans"][f"{span}|{end}|{now}"] = core_entry(per.core_spans(p))
    for now in LATE_NOWS:  # the period figures there, over the fixture months as they are (clipped at now)
        for span in LATE_SPANS:
            p = per.period_of(span, "now", now, idx["first"])
            fig = per.period_figures(idx, per.gather(files, idx, p["p0"], p["t1"], now), p, now)
            out["period_figures_late"][f"{span}|now|{now}"] = figures_entry(fig, False)
    for eps, start, now, cov in WINDOW_CASES:
        out["window_stats"].append([plain(eps), start, now, cov, rep.window_stats(eps, start, now, cov)])
    for i, now in enumerate(REPORT_NOWS):  # the first in full, the others as digests
        entries = reference_report(rep, months, index, now)
        out["line_report"][str(now)] = entries if i == 0 else {k: {"sha256": digest(v)} for k, v in entries.items()}
    return out


# ---------- the comparison on any month files ----------


def compare(rep, per, folder: Path, now: int, ends: list) -> int:
    """Tifa's period figures against the reference's for every span and end; and line_report against the
    reference's entries. Prints a summary and returns the number of differences."""
    from tifa.core import stats
    from tifa.core.tfl import SEVERITY_NAMES

    months, index = read_months(folder)
    for payload in months.values():  # rows in the 8-field format get a status, no reason and no estimate
        tail = [[SEVERITY_NAMES.get(r[3], f"Severity {r[3]}"), None, 0] for r in payload["episodes"]]
        payload["episodes"] = [list(r) + t[len(r) - 8 :] for r, t in zip(payload["episodes"], tail, strict=True)]
    files = MemFiles(months, index)
    ref_idx, _ = files.get("index.json", per._parse_index)
    idx = stats.parse_index(index)
    bad = n = 0
    for span in SPANS:
        for end in ends:
            p_ref = per.period_of(span, end_of(end), now, ref_idx["first"])
            p = stats.period_of(span, end_of(end), now, idx["first"])
            want = per.period_figures(ref_idx, per.gather(files, ref_idx, p_ref["p0"], p_ref["t1"], now), p_ref, now)
            got = stats.period_figures(idx, stats.gather(months, idx, p["p0"], p["t1"], now), p, now)
            n += 1
            if digest(got) != digest(want):
                bad += 1
                print(f"period_figures differ: span={span} end={end}")
    print(f"period_figures: {n - bad}/{n} identical")
    want = reference_report(rep, months, index, now)
    snap, open_eps = snapshot(months, now, {line["id"]: (line["name"], line["mode"]) for line in index["lines"]})
    same = 0
    for line in index["lines"]:
        if line["shared_with"]:
            continue
        got = stats.line_report(line, snap, open_eps, now)
        for r in got["now"] + got["recent"]:
            r.pop("cause")  # Tifa adds it; the reference has none
        if digest(got) == digest(want[line["id"]]):
            same += 1
        else:
            bad += 1
            print(f"line_report differs: {line['id']}")
    print(f"line_report: {same}/{len(want)} identical")
    return bad


# ---------- command line ----------


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("synth", help="write the synthetic month payloads")
    s.add_argument("--out", type=Path, default=FIXTURES / "months")
    s.add_argument("--fields", type=int, default=11, help="episode fields to keep (8 gives the older month format)")
    g = sub.add_parser("golden", help="write the reference's outputs for the synthetic months")
    g.add_argument("reference", type=Path)
    g.add_argument("--out", type=Path, default=FIXTURES / "golden.json")
    c = sub.add_parser("compare", help="compare Tifa with the reference on a folder of month files")
    c.add_argument("reference", type=Path)
    c.add_argument("--months", type=Path, required=True, help="a folder of YYYY-MM.json and index.json")
    c.add_argument("--now", default=None, help="ISO time (default: the index's updated time)")
    args = ap.parse_args(argv)
    if args.cmd == "synth":
        months, index = build_months()
        write_months(args.out, months, index, args.fields)
        print(f"{len(months)} months, {sum(len(m['episodes']) for m in months.values())} episode rows -> {args.out}")
        return 0
    rep, per = reference(args.reference.resolve())
    if args.cmd == "golden":
        months, index = build_months()
        out = reference_goldens(rep, per, months, index)
        args.out.write_text(json.dumps(out, separators=(",", ":")) + "\n", encoding="utf-8")
        print(f"goldens -> {args.out} ({args.out.stat().st_size // 1024} kB)")
        return 0
    _, index = read_months(args.months)
    now = ts(args.now) if args.now else ts(index["updated"])
    now -= now % 60
    first = dt.date.fromisoformat(index["months"][0] + "-01")
    today = dt.datetime.fromtimestamp(now, LONDON).date()
    ends, d = ["now"], first
    while d <= today:  # the 1st and 15th of every month, and the last days of the history
        ends.append(d.isoformat())
        d = d.replace(day=15) if d.day == 1 else (d.replace(day=1) + dt.timedelta(days=32)).replace(day=1)
    ends += [(today - dt.timedelta(days=i)).isoformat() for i in range(1, 4)]
    return 1 if compare(rep, per, args.months, now, ends) else 0


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT / "src"))
    sys.exit(main())
