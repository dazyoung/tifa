"""Disruption figures: one line's over the last 24 hours to 90 days, and the network's over any period.

Everything here is pure: month payloads (months.py), open episodes and the time come in, plain data goes out.

- window_stats() and line_report(): one line as the live record saw it, for /v1/lines/{id}.
- parse_period(), period_of(), gather(), cover_of(), line_period() and period_figures(): a period of 1 day to
  1 year ending now or on a London day, against the period before it, for /v1/stats.

A line is watched when a source covering it was: the FOI release ("f") covers the Underground, reporting Circle and
Hammersmith & City only as one shared line; the live record ("a") covers every line it polls, and no shared line.
Time a source did not watch is never counted as good service. Information statuses (such as no step-free access)
are listed but not counted as disruption, and two disruptions at once on a line count once. Days and hours are
London time; internal times are epoch seconds (floats in the period figures).
"""

from __future__ import annotations

import bisect
import collections
import datetime as dt
import math
import re
from collections.abc import Iterable, Mapping
from typing import Any, TypedDict

from .causes import CAUSE_LABELS
from .causes import cause as cause_of
from .months import SOURCE_FOI, SOURCE_LIVE
from .tfl import CATEGORY_IDS, SEVERITY_NAMES, rank
from .timeutil import DAY, LONDON, UTC, epoch, iso, london_midnight, month_bounds, month_keys
from .timeutil import midnights as _midnights

CATS = CATEGORY_IDS                       # delays, suspended, closure, other, info: the display order
COUNTED = tuple(c for c in CATS if c != "info")
STACK = tuple((c, pl) for c in COUNTED for pl in (False, True))   # chart layers, bottom up
SOURCES = (SOURCE_FOI, SOURCE_LIVE)

# ---------- one line, live: the last 24 hours to 90 days ----------

WINDOWS = (("24h", DAY), ("7d", 7 * DAY), ("30d", 30 * DAY), ("90d", 90 * DAY))
SPAN = WINDOWS[-1][1]    # the longest window
RECENT = 25              # latest episodes per line, with reasons


def minutes(seconds: float) -> int:
    return int(round(seconds / 60))


def union_seconds(spans: Iterable) -> int:
    """The time covered by spans that may overlap."""
    whole, reach = 0, None
    for a, b in sorted(spans):
        if reach is None or a > reach:
            whole, reach = whole + b - a, b
        elif b > reach:
            whole, reach = whole + b - reach, b
    return whole


def covered_seconds(covered: Iterable, start: float, end: float) -> float:
    """The part of [start, end] inside separate covered spans."""
    return sum(max(0, min(b, end) - max(a, start)) for a, b in covered)


def window_stats(episodes: Iterable, start: int, now_ts: int, covered_s: float) -> dict:
    """One line's figures over [start, now_ts]. episodes are (severity, status, category, planned, start, end or None).

    An episode counts if it overlaps the window; minutes are the time inside it, time with two episodes at once
    counted once. "info" is in by_category but not in the totals (episodes, minutes_disrupted, disrupted_pct,
    longest_min). disrupted_pct is of covered_s, the time watched in the window (None when nothing was); longest_min
    is the longest single episode's time inside the window."""
    cats, spans, count, longest = {}, [], 0, 0
    for _, status, cat, _, s, e in episodes:
        e = now_ts if e is None else e
        if e <= start and s < start:
            continue
        a = max(s, start)
        b = max(min(e, now_ts), a)
        c = cats.get(cat)
        if c is None:
            c = cats[cat] = {"episodes": 0, "spans": [], "longest": 0, "by_status": {}}
        c["episodes"] += 1
        c["spans"].append((a, b))
        c["longest"] = max(c["longest"], b - a)
        c["by_status"][status] = c["by_status"].get(status, 0) + 1
        if cat != "info":
            count += 1
            spans.append((a, b))
            longest = max(longest, b - a)
    disrupted = union_seconds(spans)
    return {
        "episodes": count,
        "minutes_disrupted": minutes(disrupted),
        "disrupted_pct": round(min(100.0, 100 * disrupted / covered_s), 1) if covered_s else None,
        "longest_min": minutes(longest),
        "by_category": {cid: {"episodes": cats[cid]["episodes"], "minutes": minutes(union_seconds(cats[cid]["spans"])),
                              "longest_min": minutes(cats[cid]["longest"]), "by_status": cats[cid]["by_status"]}
                        for cid in CATS if cid in cats},
    }


def _status(status, severity: int) -> str:
    return status if isinstance(status, str) and status else SEVERITY_NAMES.get(severity, f"Severity {severity}")


def _start(value) -> int | None:
    """An open episode's start: epoch seconds, or ISO 8601 (no offset: UTC); None if it is neither."""
    if _isint(value):
        return value
    if isinstance(value, str):
        try:
            return epoch(value)
        except ValueError:
            return None
    return None


def _closed_row(r, lid: str, source: str) -> tuple | None:
    """A month row of this line and source with an end, as (start, end, severity, status, category, planned,
    reason, cause, end_estimated); None for anything else (open, another line or source, or not well formed)."""
    if (not isinstance(r, list) or len(r) < 8 or r[0] != lid or r[6] != source or not _isint(r[1])
            or not _isint(r[2]) or r[2] < r[1] or not _isint(r[3])):
        return None
    reason = r[9] if len(r) > 9 and isinstance(r[9], str) else None
    return (r[1], r[2], r[3], _status(r[8] if len(r) > 8 else None, r[3]), cat_of(r[4]), bool(r[5]), reason,
            r[7] if isinstance(r[7], str) else None, bool(r[10]) if len(r) > 10 else False)


def _open_episode(e, lid: str) -> dict | None:
    """One of this line's open episodes (OpenEpisode), checked, with its start in epoch seconds and its cause."""
    if not isinstance(e, Mapping) or e.get("line") != lid or not _isint(e.get("severity")):
        return None
    start = _start(e.get("start"))
    if start is None:
        return None
    sev, planned = e["severity"], bool(e.get("planned"))
    status = _status(e.get("status"), sev)
    reason = e.get("reason") if isinstance(e.get("reason"), str) else None
    return {"severity": sev, "status": status, "category": cat_of(e.get("category")), "planned": planned,
            "start": start, "reason": reason, "cause": cause_of(reason, planned, status)}


def _spans(value) -> list:
    """Watched spans [[s, e], ...], checked."""
    return [(s[0], s[1]) for s in _list(value)
            if isinstance(s, list) and len(s) >= 2 and _isint(s[0]) and _isint(s[1]) and s[1] >= s[0]]


def line_report(line: Mapping, months: Mapping[str, Mapping], open_eps: Iterable[Mapping], now_ts: int,
                source: str = SOURCE_LIVE) -> dict:
    """One line's entry: what is open now (worst first), window_stats over the last 24 hours, 7, 30 and 90 days
    with the share of each window that `source` watched, the RECENT latest episodes with their reasons and causes
    (newest first) and how many unplanned disruptions started in each London hour over the last 90 days.

    line is {"id", "name", "mode"}; months is {"YYYY-MM": month payload} and must hold at least the months
    overlapping the last 90 days (older ones are read only as far back as "recent" needs). open_eps are the open
    episodes (OpenEpisode; any line's: this line's are taken), so open rows in the months (end None) are ignored, as
    are other sources' rows. Keys that are not months, and rows or open episodes that are not well formed, are
    skipped. An open episode's cause is computed from its reason (causes.cause()), as it will be when it closes."""
    lid = line["id"]
    keys = []
    for k in months:
        try:
            keys.append((month_bounds(k), k))
        except ValueError:
            continue
    keys.sort(reverse=True)
    closed, covered = {}, []
    for (_, m1), k in keys:   # newest first, until the windows are covered and the latest episodes are all in
        if m1 <= now_ts - SPAN and sum(1 for r in closed.values() if r[0] >= m1) >= RECENT:
            break
        payload = _dict(months[k])
        covered += _spans(_dict(payload.get("watched")).get(source))
        for raw in _list(payload.get("episodes")):
            r = _closed_row(raw, lid, source)
            if r is None:
                continue
            have = closed.get((r[0], r[2], r[5]))
            if have is None or r[1] > have[1]:
                closed[(r[0], r[2], r[5])] = r
    covered = merged(covered, 0)
    # in the order they closed, as a recorder appends them
    past = sorted(closed.values(), key=lambda r: (r[1], r[0], r[2]))
    now_eps = sorted((e for e in (_open_episode(x, lid) for x in open_eps) if e is not None),
                     key=lambda e: (rank(e["severity"]), e["severity"], e["planned"]))
    eps = [(sev, status, cat, planned, s, e) for s, e, sev, status, cat, planned, *_ in past]
    eps += [(e["severity"], e["status"], e["category"], e["planned"], e["start"], None) for e in now_eps]

    stats, coverage = {}, {}
    for name, length in WINDOWS:
        cov = covered_seconds(covered, now_ts - length, now_ts)
        stats[name] = window_stats(eps, now_ts - length, now_ts, cov)
        coverage[name] = round(100 * cov / length, 1)

    recent = [(s, e, est, sev, status, cat, planned, reason, code)
              for s, e, sev, status, cat, planned, reason, code, est in past]
    recent += [(e["start"], None, False, e["severity"], e["status"], e["category"], e["planned"], e["reason"],
                e["cause"]) for e in now_eps]
    recent.sort(key=lambda r: (r[0], now_ts if r[1] is None else r[1]), reverse=True)

    hours = [0] * 24
    for _, _, cat, planned, s, _ in eps:
        if s >= now_ts - SPAN and not planned and cat != "info":
            hours[dt.datetime.fromtimestamp(s, LONDON).hour] += 1

    return {
        "id": lid, "name": line.get("name") or lid, "mode": line.get("mode"),
        "now": [{"severity": e["severity"], "status": e["status"], "category": e["category"], "planned": e["planned"],
                 "start": iso(e["start"]), "minutes": minutes(now_ts - e["start"]), "reason": e["reason"],
                 "cause": e["cause"]} for e in now_eps],
        "stats": stats,
        "coverage_pct": coverage,
        "recent": [{"start": iso(s), "end": None if e is None else iso(e), "end_estimated": est,
                    "minutes": minutes((now_ts if e is None else e) - s), "severity": sev, "status": status,
                    "category": cat, "planned": planned, "reason": reason, "cause": code}
                   for s, e, est, sev, status, cat, planned, reason, code in recent[:RECENT]],
        "starts_by_hour": hours,
    }


# ---------- spans of time ----------

def merged(spans: Iterable, pad: float) -> list:
    """Sorted spans, those within pad of each other joined."""
    out = []
    for a, b in sorted(spans):
        if out and a <= out[-1][1] + pad:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def clip(spans: Iterable, a: float, b: float) -> list:
    return [(max(x, a), min(y, b)) for x, y in spans if y > a and x < b]


def total(spans: Iterable) -> float:
    return sum(b - a for a, b in spans)


def intersect(xs: list, ys: list) -> list:
    """Where two sorted lists of separate spans overlap."""
    out, i, j = [], 0, 0
    while i < len(xs) and j < len(ys):
        a, b = max(xs[i][0], ys[j][0]), min(xs[i][1], ys[j][1])
        if b > a:
            out.append((a, b))
        if xs[i][1] < ys[j][1]:
            i += 1
        else:
            j += 1
    return out


def complement(spans: Iterable, a: float, b: float) -> list:
    """The parts of [a, b] outside sorted, separate spans."""
    out, t = [], a
    for x, y in spans:
        if y <= t:
            continue
        if x >= b:
            break
        if x > t:
            out.append((t, x))
        t = max(t, y)
    if t < b:
        out.append((t, b))
    return out


def binned(spans: list, edges: list) -> list:
    """Sorted, separate spans cut at the bin edges: one list of spans per bin."""
    out = [[] for _ in range(len(edges) - 1)]
    for a, b in spans:
        i = max(bisect.bisect_right(edges, a) - 1, 0)
        while i < len(out) and edges[i] < b:
            lo, hi = max(a, edges[i]), min(b, edges[i + 1])
            if hi > lo:
                out[i].append((lo, hi))
            i += 1
    return out


# ---------- London days and their names ----------

def lt(ts: float) -> dt.datetime:
    return dt.datetime.fromtimestamp(ts, UTC).astimezone(LONDON)


def midnight(d: dt.date) -> float:
    """The London midnight that starts day d."""
    return float(london_midnight(d))


def date_text(ts: float) -> str:
    d = lt(ts)
    return f"{d.day} {d:%b %Y}"


def day_name(d) -> str:
    return f"{d:%a} {d.day} {d:%b}"


def date_range(d0: dt.date, d1: dt.date) -> str:
    if d0 == d1:
        return f"{d0:%a} {d0.day} {d0:%b %Y}"
    if (d0.year, d0.month) == (d1.year, d1.month):
        return f"{d0.day}–{d1.day} {d1:%b %Y}"
    if d0.year == d1.year:
        return f"{d0.day} {d0:%b} – {d1.day} {d1:%b %Y}"
    return f"{d0.day} {d0:%b %Y} – {d1.day} {d1:%b %Y}"


# ---------- periods over the month payloads ----------

SPAN_DAYS = {"1d": 1, "7d": 7, "30d": 30, "90d": 90, "365d": 365}
SPAN_WORDS = {"1d": "the last 24 hours", "7d": "the last 7 days", "30d": "the last 30 days", "90d": "the last 90 days",
              "365d": "the last year"}
LINE_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,59}")
CAUSE_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
MONTH_ID = re.compile(r"[0-9]{4}-(?:0[1-9]|1[0-2])")   # ASCII digits: \d takes any script
DATE_ID = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
EARLIEST = dt.date(2000, 1, 1)
MAX_PARAM = 32              # characters of a query value read
WATCHED_ENOUGH = 0.8        # under this share of a period (or the one before) watched, no changes are given
TOP_CAUSES = 6
CORE = ((7, 9.5), (16.5, 19))   # TfL's weekday core times, London hours


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _list(value) -> list:
    return value if isinstance(value, list) else []


def _isint(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _time(value) -> float | None:
    """ISO 8601 (no offset: UTC) or epoch seconds -> epoch seconds."""
    if _isint(value) or (isinstance(value, float) and math.isfinite(value)):
        return float(value)
    if not value or not isinstance(value, str):
        return None
    try:
        t = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (t if t.tzinfo else t.replace(tzinfo=UTC)).timestamp()


def cat_of(value) -> str:
    return value if value in CATS else "other"


def parse_index(raw) -> dict | None:
    """The months index (/v1/months: months, first, lines, causes), checked: {"months": set, "first": epoch or
    None, "lines": [{"id", "name", "mode", "shared_with"}], "causes": {code: label}}. None if it isn't one.

    Each index line is {"id", "name", "mode", "shared_with"}: shared_with lists the member lines of a line the FOI
    release reports as one (Circle and Hammersmith & City), and is [] for every other line. The coverage depends on
    it (cover_of()): without it the FOI release would count as watching the member lines, and the live record as
    watching the shared line. causes is [{"code", "label"}] (a {code: label} mapping is read too; none given: the
    rules' labels)."""
    if not isinstance(raw, dict):
        return None
    months = {m for m in _list(raw.get("months")) if isinstance(m, str) and MONTH_ID.fullmatch(m)}
    lines, seen = [], set()
    for line in _list(raw.get("lines")):
        if not isinstance(line, dict) or not isinstance(line.get("id"), str) or not LINE_ID.fullmatch(line["id"]):
            continue
        if line["id"] in seen:
            continue
        seen.add(line["id"])
        shared = [s for s in _list(line.get("shared_with")) if isinstance(s, str) and LINE_ID.fullmatch(s)]
        lines.append({"id": line["id"], "name": str(line.get("name") or line["id"])[:60],
                      "mode": line.get("mode") if isinstance(line.get("mode"), str) else None, "shared_with": shared})
    causes = {}
    given = raw.get("causes", CAUSE_LABELS)
    # [{"code", "label"}] as the index serves them; a {code: label} mapping is read too
    listed = [{"code": k, "label": v} for k, v in given.items()] if isinstance(given, dict) else _list(given)
    for c in listed:
        if isinstance(c, dict) and isinstance(c.get("code"), str) and CAUSE_ID.fullmatch(c["code"]):
            causes.setdefault(c["code"], str(c.get("label") or c["code"])[:40])
    first = _time(raw.get("first"))
    if first is None and months:
        y, m = map(int, min(months).split("-"))
        first = dt.datetime(y, m, 1, tzinfo=UTC).timestamp()
    return {"months": months, "first": first, "lines": lines, "causes": causes}


def parse_month(raw) -> dict | None:
    """A month payload's episodes as (line, start, end or None, severity, category, planned, source, cause) and its
    watched spans by source, checked; None if it isn't one. Only the first 8 fields of a row are read."""
    if not isinstance(raw, dict):
        return None
    eps = []
    for x in _list(raw.get("episodes")):
        if not isinstance(x, list) or len(x) < 7:
            continue
        lid, s, e, sev, cat, planned, src = x[:7]
        cause = x[7] if len(x) > 7 else None
        if (not isinstance(lid, str) or not LINE_ID.fullmatch(lid) or not _isint(s) or src not in SOURCES
                or (e is not None and (not _isint(e) or e < s))):
            continue
        cat = cat_of(cat)
        eps.append((lid, s, e, sev if _isint(sev) else None, cat, bool(planned) and cat != "info", src,
                    cause if isinstance(cause, str) and CAUSE_ID.fullmatch(cause) else None))
    watched = {}
    for src, spans in _dict(raw.get("watched")).items():
        if src in SOURCES:
            watched[src] = [(a, b) for a, b in (s for s in _list(spans) if isinstance(s, list) and len(s) == 2)
                            if _isint(a) and _isint(b) and b > a]
    return {"eps": eps, "watched": watched}


def gather(months: Mapping[str, Any], index: Mapping | None, a: float, b: float, now_ts: float,
           sources: Iterable[str] = SOURCES) -> dict:
    """The episodes and watched spans of the months overlapping [a, b]: {"eps": {line: [episode]}, "watched":
    {source: spans}}. An episode spanning months is in each; the copy with an end wins. An open one (end None) runs
    to the last time its source watched (at most now).

    months is {"YYYY-MM": month payload, or parse_month() of one}; give it the open episodes as rows with end None
    (months.merge_month), as /v1/months serves them. index is parse_index() of the months index: only the months it
    lists are read (None: all those given). Only the sources given are read: the others' episodes are left out and
    their watched spans are empty, so nothing in period_figures() comes from them (pass the sources that may be
    published)."""
    keep = frozenset(sources)
    known = index["months"] if index is not None else None
    seen, watched = {}, {SOURCE_FOI: [], SOURCE_LIVE: []}
    for key in month_keys(a, b):
        if known is not None and key not in known:
            continue
        raw = months.get(key)
        data = raw if isinstance(raw, dict) and "eps" in raw else parse_month(raw)
        if data is None:
            continue
        for src, spans in data["watched"].items():
            if src in keep:
                watched[src].extend(spans)
        for ep in data["eps"]:
            if ep[6] not in keep:
                continue
            k = (ep[0], ep[1], ep[3], ep[4], ep[5], ep[6])
            old = seen.get(k)
            if old is None or (ep[2] is not None and (old[2] is None or ep[2] > old[2])):
                seen[k] = ep
    watched = {src: [tuple(s) for s in merged(spans, 0)] for src, spans in watched.items()}
    last = {src: spans[-1][1] if spans else None for src, spans in watched.items()}
    eps = {}
    for lid, s, e, sev, cat, planned, src, cause in seen.values():
        ongoing = e is None
        if ongoing:
            e = max(min(now_ts, last.get(src) or s), s)
        eps.setdefault(lid, []).append({"start": float(s), "end": float(e), "ongoing": ongoing, "estimated": False,
                                        "severity": sev, "status": SEVERITY_NAMES.get(sev, "Disrupted"), "cat": cat,
                                        "planned": planned, "src": src, "cause": cause})
    for v in eps.values():
        v.sort(key=lambda e: e["start"])
    return {"eps": eps, "watched": watched}


def members_of(idx: Mapping) -> dict:
    """Lines reported together in the FOI release: member id -> the shared line."""
    return {m: line for line in idx["lines"] if line["shared_with"] for m in line["shared_with"]}


def cover_of(line: Mapping, members: Mapping, watched: Mapping) -> list:
    """When a line was watched. The FOI release covers the Underground, Circle and Hammersmith & City only as their
    shared line; the live record covers every line it polls, and no shared line."""
    if line["shared_with"]:
        srcs = [SOURCE_FOI] if line["mode"] == "tube" else []
    else:
        srcs = [SOURCE_LIVE] + ([SOURCE_FOI] if line["mode"] == "tube" and line["id"] not in members else [])
    return [tuple(s) for s in merged([s for src in srcs for s in watched.get(src, ())], 0)]


def short_name(name: str) -> str:
    return re.sub(r"\s+line$", "", name, flags=re.I)


def parse_period(query: Mapping, today: dt.date) -> tuple:
    """((span, end), None) from a query's span and end (other parameters are left alone), end "now" or a London
    date, today meaning now; or (None, why). query is a dict whose values are strings or lists of one (as parse_qs
    gives), or a multidict with getlist() (such as a web framework's query parameters), so that a parameter given
    twice is refused rather than one of its values taken."""
    if not isinstance(query, Mapping):
        return None, "That period could not be read."
    getlist = getattr(query, "getlist", None)
    got = {}
    for name in ("span", "end"):
        v = getlist(name) if callable(getlist) else query.get(name)
        if isinstance(v, (list, tuple)):
            if len(v) != 1:
                return None, "A period needs a span and an end, once each."
            v = v[0]
        if not isinstance(v, str):
            return None, "A period needs a span and an end, once each."
        if len(v) > MAX_PARAM:
            return None, "That period could not be read."
        got[name] = v
    span, end = got["span"], got["end"]
    if span not in SPAN_DAYS:
        return None, "The span must be 1d, 7d, 30d, 90d or 365d."
    if end == "now":
        return (span, "now"), None
    if not DATE_ID.fullmatch(end):
        return None, "The end must be now or a date."
    try:
        d = dt.date.fromisoformat(end)
    except ValueError:
        return None, "The end must be now or a date."
    if d < EARLIEST or d > today:
        return None, "That date is outside the history."
    return (span, "now" if d == today else d), None


class Period(TypedDict):
    span: str
    end: str | dt.date    # "now" or the London day it ends on
    n: int                # days
    t0: float             # the period is [t0, t1]
    t1: float
    p0: float             # the period before it is [p0, t0]
    label: str
    before: str           # the period before, in words
    prev: str             # the end a step back ("YYYY-MM-DD"), "" at the start of the history
    next: str             # a step on: "now", a date, or "" when it ends now
    today: dt.date
    mids: list            # [(epoch, date)] of the London midnights inside it
    days: list            # t0, those midnights and t1: the London days in it
    first: float | None   # the start of the history


def period_of(span: str, end, now_ts: float, first: float | None) -> Period:
    """The period: [t0, t1] (now and the span before it, or whole London days ending on a date), the period before
    it for comparison [p0, t0], labels, the steps back and on, and the London days in it."""
    n = SPAN_DAYS[span]
    one = dt.timedelta(days=1)
    today = lt(now_ts).date()
    if end == "now":
        t1 = float(now_ts)
        t0, p0 = t1 - n * DAY, t1 - 2 * n * DAY
        d0 = lt(t0)
        label = f"{SPAN_WORDS[span]}, from " + (f"{day_name(d0)} {d0:%H:%M}" if n <= 30 else date_text(t0))
        before = "the 24 hours before" if n == 1 else f"the {n} days before"
        back, nxt = d0.date(), ""
    else:
        t1, t0, p0 = midnight(end + one), midnight(end - (n - 1) * one), midnight(end - (2 * n - 1) * one)
        label = date_range(end - (n - 1) * one, end)
        before = date_range(end - (2 * n - 1) * one, end - n * one)
        back, fwd = end - n * one, end + n * one
        nxt = "now" if fwd >= today else fwd.isoformat()
    prev = back.isoformat() if (first is None or t0 > first) and back >= EARLIEST else ""
    mids = [(float(t), d) for t, d in _midnights(t0, t1)]
    return {"span": span, "end": end, "n": n, "t0": t0, "t1": t1, "p0": p0, "label": label, "before": before,
            "prev": prev, "next": nxt, "today": today, "mids": mids, "days": [t0] + [t for t, _ in mids] + [t1],
            "first": first}


def core_spans(p: Mapping) -> list:
    """TfL's weekday core times (07:00-09:30, 16:30-19:00 London) in the period, sorted and separate. Each of
    p["days"] but the last starts a different London day (t0 its own, and each midnight its own date), so no day is
    counted twice. The clocks change on Sundays."""
    out = []
    for i in range(len(p["days"]) - 1):
        day = lt(p["days"][i]).date()
        if day.weekday() >= 5:
            continue
        m = midnight(day)
        for h0, h1 in CORE:
            out.append((m + h0 * 3600, m + h1 * 3600))
    return clip(out, p["t0"], p["t1"])


def line_period(eps: list, cover: list, a: float, b: float, extra: Mapping | None = None) -> dict:
    """One line's figures over [a, b]: the time watched ("w") and disrupted ("dis": information not counted, two at
    once counted once), unplanned and planned counts and good service (%). With extra (the period's "days", chart
    "bins" or None, and "core" times) also its categories, durations, longest unplanned episode, share of
    disrupted time in core times, unplanned time per day, causes and chart bins."""
    cov = clip(cover, a, b)
    shown = [e for e in eps if e["cat"] != "info" and e["end"] > a and e["start"] < b]

    def cut(es):
        return intersect([tuple(s) for s in merged([(max(e["start"], a), min(e["end"], b)) for e in es], 0)], cov)

    w = total(cov)
    spans = cut(shown)
    dis = total(spans)
    unplanned = [e for e in shown if not e["planned"]]
    f = {"w": w, "dis": dis, "n_unpl": len(unplanned), "n_pl": len(shown) - len(unplanned),
         "good": 100 - min(dis / w, 1.0) * 100 if w > 0 else None}
    if extra is None:
        return f
    f["by_cat"] = collections.Counter(e["cat"] for e in shown)
    durs = sorted(e["end"] - e["start"] for e in shown)
    f["avg"] = sum(durs) / len(durs) if durs else None
    f["median"] = (durs[len(durs) // 2] if len(durs) % 2 else (durs[len(durs) // 2 - 1] + durs[len(durs) // 2]) / 2
                   ) if durs else None
    f["longest"] = max(unplanned, key=lambda e: e["end"] - e["start"]) if unplanned else None
    f["core"] = total(intersect(spans, extra["core"])) / dis if dis > 0 else None
    f["per_day"] = [total(x) for x in binned(cut(unplanned), extra["days"])]
    f["days"] = sum(1 for s in f["per_day"] if s > 0)
    f["causes"] = collections.Counter(e["cause"] for e in shown)
    f["cause_s"] = collections.Counter()
    for e in shown:
        f["cause_s"][e["cause"]] += min(e["end"], b) - max(e["start"], a)
    f["chart"] = None
    if extra.get("bins"):
        union = [total(x) for x in binned(spans, extra["bins"])]
        layers = {}
        for e in shown:
            layers.setdefault((e["cat"], e["planned"]), []).append(e)
        lay = {k: [total(x) for x in binned(cut(v), extra["bins"])] for k, v in layers.items()}
        chart = []
        for i, u in enumerate(union):   # split each bin's disrupted time in proportion: categories can overlap
            whole = sum(v[i] for v in lay.values())
            chart.append({k: u * v[i] / whole for k, v in lay.items() if v[i] > 0} if whole > 0 else {})
        f["chart"] = chart
    return f


def part_key(cat: str, planned: bool) -> str:
    """A chart layer's name in the figures: the category, with "-planned" for planned works."""
    return f"{cat}-planned" if planned else cat


def episode_fig(e: Mapping) -> dict:
    return {"status": e["status"], "severity": e["severity"], "category": e["cat"], "cause": e["cause"],
            "source": e["src"], "start": e["start"], "end": e["end"], "duration_s": e["end"] - e["start"],
            "ongoing": e["ongoing"]}


def cause_name(code, labels: Mapping) -> str:
    return "Unknown" if code is None else labels.get(code, code.replace("-", " ").capitalize())


def period_figures(idx: Mapping, data: Mapping, p: Mapping, now_ts: float) -> dict:
    """The period's figures as plain data: the share of [t0, t1] each source watched ("before" is any source over
    [p0, t0]) and the time no source did; over the lines watched in it, good service, unplanned and planned
    disruptions and line-time disrupted, with the period before's when both were watched enough to compare
    (WATCHED_ENOUGH); the most disrupted line, the longest unplanned disruption, the busiest day (unplanned
    line-time, periods of a week or more), the causes (the TOP_CAUSES commonest, Unknown always among them, and the
    rest together), the chart's bins (per day up to a month, per week over it) and one row per line. Shares are 0
    to 1, good service a percentage, times epoch seconds and durations seconds.

    idx is parse_index() of the months index, data gather() over [p0, t1] (with only the sources that may be
    published: every source in data is counted) and p period_of()."""
    a, b, p0 = p["t0"], p["t1"], p["p0"]
    members = members_of(idx)
    wall = b - a
    seen = {s: clip(data["watched"].get(s, []), a, b) for s in SOURCES}
    any_cov = [tuple(s) for s in merged(seen[SOURCE_FOI] + seen[SOURCE_LIVE], 0)]
    share = total(any_cov) / wall
    before = [tuple(s) for s in merged(clip(data["watched"].get(SOURCE_FOI, []) + data["watched"].get(SOURCE_LIVE, []),
                                            p0, a), 0)]
    share_before = total(before) / (a - p0)
    fig = {"span": p["span"], "end": p["end"] if p["end"] == "now" else p["end"].isoformat(), "t0": a, "t1": b,
           "p0": p0, "watched": {SOURCE_FOI: total(seen[SOURCE_FOI]) / wall,
                                 SOURCE_LIVE: total(seen[SOURCE_LIVE]) / wall, "any": share, "before": share_before},
           "unwatched": [list(s) for s in complement(any_cov, a, b)], "comparable": False, "lines_watched": 0,
           "good_pct": None, "unplanned": 0, "planned": 0, "disrupted_s": 0, "before": None, "most_disrupted": None,
           "longest_unplanned": None, "busiest_day": None, "causes": [], "other_causes": None, "bins": None,
           "lines": []}
    if share <= 0:
        return fig
    bins = None
    if p["n"] >= 7:
        bins = p["days"] if p["n"] <= 30 else [a] + [t for t, d in p["mids"] if d.weekday() == 0] + [b]
    extra = {"days": p["days"], "bins": bins, "core": core_spans(p)}
    rows = []
    for line in idx["lines"]:
        cover = cover_of(line, members, data["watched"])
        if total(clip(cover, a, b)) <= 0:
            continue
        eps = data["eps"].get(line["id"], [])
        rows.append((line, line_period(eps, cover, a, b, extra), line_period(eps, cover, p0, a)))
    if not rows:
        return fig
    comparable = share >= WATCHED_ENOUGH and share_before >= WATCHED_ENOUGH

    # the network
    def tot(key, which=1):
        return sum(r[which][key] for r in rows)

    w, dis, w_b, dis_b = tot("w"), tot("dis"), tot("w", 2), tot("dis", 2)
    fig.update(comparable=comparable, lines_watched=len(rows), good_pct=100 - min(dis / w, 1) * 100 if w else None,
               unplanned=tot("n_unpl"), planned=tot("n_pl"), disrupted_s=dis)
    if comparable:
        fig["before"] = {"good_pct": 100 - min(dis_b / w_b, 1) * 100 if w_b else None, "unplanned": tot("n_unpl", 2),
                         "planned": tot("n_pl", 2), "disrupted_s": dis_b}
    names = {line["id"]: short_name(line["name"]) for line in idx["lines"]}
    worst = max(rows, key=lambda r: (r[1]["dis"] / r[1]["w"], r[1]["dis"]))
    if worst[1]["dis"] > 0:
        lid = worst[0]["id"]
        fig["most_disrupted"] = {"line": lid, "name": names[lid], "share": worst[1]["dis"] / worst[1]["w"],
                                 "disrupted_s": worst[1]["dis"]}
    longs = [(r[1]["longest"], r[0]) for r in rows if r[1]["longest"]]
    if longs:
        e, line = max(longs, key=lambda x: x[0]["end"] - x[0]["start"])
        fig["longest_unplanned"] = {"line": line["id"], "name": names[line["id"]], **episode_fig(e)}
    if p["n"] >= 7:
        per_day = [sum(r[1]["per_day"][i] for r in rows) for i in range(len(p["days"]) - 1)]
        if per_day and max(per_day) > 0:
            i = per_day.index(max(per_day))
            day = lt((p["days"][i] + p["days"][i + 1]) / 2).date()
            fig["busiest_day"] = {"date": day.isoformat(), "t0": p["days"][i], "t1": p["days"][i + 1],
                                  "unplanned_s": per_day[i], "lines": sum(1 for r in rows if r[1]["per_day"][i] > 0)}

    # causes, network-wide: by number of disruptions, then time
    labels = idx["causes"]
    n, s = collections.Counter(), collections.Counter()
    for _, f, _ in rows:
        n.update(f["causes"])
        s.update(f["cause_s"])
    if n:
        ranked = sorted(n.items(), key=lambda kv: (-kv[1], -s[kv[0]]))
        shown, rest = ranked[:TOP_CAUSES], ranked[TOP_CAUSES:]
        if rest and None in dict(rest):   # Unknown is always shown, honestly
            shown = shown[:-1] + [(None, n[None])]
            rest = [kv for kv in ranked if kv not in shown]
        fig["causes"] = [{"code": code, "label": cause_name(code, labels), "count": count, "seconds": s[code]}
                         for code, count in shown]
        if rest:
            fig["other_causes"] = {"causes": len(rest), "count": sum(c for _, c in rest),
                                   "seconds": sum(s[k] for k, _ in rest)}

    # the chart: line-time disrupted per bin, all lines added together, split by category and planned
    if bins:
        sums = [collections.Counter() for _ in range(len(bins) - 1)]
        for _, f, _ in rows:
            for i, part in enumerate(f["chart"] or ()):
                sums[i].update(part)
        fig["bins"] = {"per": "day" if p["n"] <= 30 else "week", "items": [
            {"t0": bins[i], "t1": bins[i + 1], "disrupted_s": sum(sm.values()),
             "lines": sum(1 for _, f, _ in rows if f["chart"] and f["chart"][i]),
             "parts": {part_key(*k): v for k, v in sm.items()}} for i, sm in enumerate(sums)]}

    # one row per line
    for line, f, fb in rows:
        ranked = sorted(f["causes"].items(), key=lambda kv: (-kv[1], -f["cause_s"][kv[0]]))
        top = None
        if ranked:
            code = ranked[0][0]
            top = {"code": code, "label": "Unknown" if code is None else labels.get(code, code)}
        fig["lines"].append({
            "id": line["id"], "name": names[line["id"]], "mode": line["mode"], "shared": bool(line["shared_with"]),
            "watched_s": f["w"], "disrupted_s": f["dis"], "good_pct": f["good"],
            "good_pct_before": fb["good"] if comparable else None, "unplanned": f["n_unpl"], "planned": f["n_pl"],
            "by_category": {c: f["by_cat"].get(c, 0) for c in COUNTED}, "avg_s": f["avg"], "median_s": f["median"],
            "longest_unplanned": episode_fig(f["longest"]) if f["longest"] else None, "core_share": f["core"],
            "days": f["days"], "days_in_period": len(f["per_day"]), "top_cause": top})
    return fig
