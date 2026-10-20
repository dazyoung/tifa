"""Views: read model + query -> response data. Shared by the REST API and the MCP server.

Each view returns the "data" part of a response (the HTTP layer adds the envelope, ETag and caching) and raises
ViewError for bad input (400), unknown ids (404), cursors that are too old (410) or a store that cannot be read
(503). Views never read the clock or the store themselves: time comes from ``rm.now()`` and data from the read
model's snapshot (and its cached days of poll times).

Times in responses are ISO 8601 in UTC ("+00:00"), except where a field says epoch: month payloads keep their
established format (epoch seconds), and /v1/changes takes and returns its cursor and poll times as epoch seconds.
Only the sources that may be published (TIFA_PUBLIC_SOURCES) are ever read.

Open episodes run to now while the recording runs. Once it has a gap (no poll for over GAP seconds) they run to the
last poll instead, where the gap rule will close them when polling resumes.
"""

from __future__ import annotations

import base64
import binascii
import datetime as dt
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import TYPE_CHECKING, Any

from tifa.api.ratelimit import client_egress
from tifa.api.readmodel import HistoryBusy, StoreUnavailable, day_start
from tifa.core import stats
from tifa.core.causes import CAUSE_LABELS, CAUSES, CAUSES_SIG
from tifa.core.causes import cause as cause_of
from tifa.core.episodes import GAP
from tifa.core.months import (
    SOURCE_FOI,
    SOURCE_LIVE,
    episode_key,
    merge_month,
    merge_spans,
    new_month,
)
from tifa.core.tfl import CATEGORIES, CATEGORY_IDS, SEVERITY_NAMES
from tifa.core.timeutil import DAY, iso, london_date, london_midnight, month_bounds, month_key, month_keys

if TYPE_CHECKING:
    from tifa.api.readmodel import ReadModel, Snapshot

API_VERSION = "1"
ATTRIBUTION = "Powered by TfL Open Data"
NOTICE = "Not affiliated with or endorsed by Transport for London."
LICENCE = (
    "Data: Transport for London open data, re-used under TfL's open data terms (based on the Open Government "
    "Licence v2.0). Code: MIT."
)
SOURCE_INFO = {
    SOURCE_LIVE: {
        "id": SOURCE_LIVE,
        "name": "Live recording",
        "description": "TfL's line status, polled every minute; episodes start and end at the polls that saw them.",
    },
    SOURCE_FOI: {
        "id": SOURCE_FOI,
        "name": "TfL FOI release",
        "description": "TfL's own records of line status messages, released under Freedom of Information.",
    },
}

STALE_AFTER = 300  # seconds since the last poll after which the data is stale
CHANGES_WINDOW = 35 * DAY  # how far back /v1/changes can catch up
DISRUPTIONS_MAX_SPAN = 366 * DAY
DISRUPTIONS_DEFAULT_SPAN = 30 * DAY
DISRUPTIONS_LIMIT = 100
DISRUPTIONS_MAX_LIMIT = 500
COVERAGE_MAX_SPAN = 90 * DAY
COVERAGE_DEFAULT_SPAN = 7 * DAY
POLLS_PER_DAY = 1440
MAX_PARAM = 200  # characters of a query value read
MAX_LIST = 20  # values in a comma-separated filter

BUSY_RETRY_AFTER = 60  # seconds, when too many days of history were read just now
EARLIEST_TS = int(dt.datetime(2000, 1, 1, tzinfo=dt.UTC).timestamp())  # times outside these are refused, in any form
LATEST_TS = int(dt.datetime(2101, 1, 1, tzinfo=dt.UTC).timestamp())

_EPOCH = re.compile(r"[0-9]{1,11}")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_PLUS_LOST = re.compile(r"^(.*[T ][0-9]{2}:[0-9]{2}(?::[0-9]{2}(?:\.[0-9]+)?)?) ([0-9]{2}(?::?[0-9]{2})?)$")
_TRUE = {"true", "1", "yes"}
_FALSE = {"false", "0", "no"}
UNKNOWN_CAUSE = "unknown"


class ViewError(Exception):
    """A request a view cannot answer: status (HTTP), code (machine-readable) and message (for people), and for a
    limit the seconds to wait (Retry-After)."""

    def __init__(self, status: int, code: str, message: str, *, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.retry_after = retry_after

    def __repr__(self) -> str:
        return f"ViewError({self.status}, {self.code!r}, {self.message!r})"


def bad(message: str, code: str = "bad_request") -> ViewError:
    return ViewError(400, code, message)


# ---- parameters ------------------------------------------------------------------------------------------------


def param(params: Mapping[str, Any], name: str) -> str | None:
    """One query value, stripped; None when absent or empty. A value given twice is refused. `params` is a plain
    mapping (values are strings, or lists of one) or a multidict with getlist(); numbers and booleans are read as
    their text."""
    getlist = getattr(params, "getlist", None)
    if callable(getlist):
        values = list(getlist(name))
    else:
        value = params.get(name)
        values = [] if value is None else list(value) if isinstance(value, list | tuple) else [value]
    if not values:
        return None
    if len(values) > 1:
        raise bad(f"The parameter {name} was given more than once.", "repeated_parameter")
    value = values[0]
    if value is None:
        return None
    if isinstance(value, bool):
        value = "true" if value else "false"
    elif isinstance(value, int | float):
        value = str(int(value)) if float(value).is_integer() else str(value)
    if not isinstance(value, str):
        raise bad(f"The parameter {name} must be text.", "bad_parameter")
    value = value.strip()
    if len(value) > MAX_PARAM:
        raise bad(f"The parameter {name} is too long.", "bad_parameter")
    return value or None


def _csv(value: str | None, name: str) -> list[str]:
    if value is None:
        return []
    items = list(dict.fromkeys(x.strip() for x in value.split(",") if x.strip()))
    if len(items) > MAX_LIST:
        raise bad(f"At most {MAX_LIST} values of {name} at once.", "bad_parameter")
    return items


def parse_time(value: str | None, name: str, *, end: bool = False) -> int | None:
    """An instant from a query: epoch seconds, ISO 8601 (no offset: UTC; "Z" allowed) or a London date, in the years
    2000 to 2100 whatever the form. A date means the start of that day in London, or for an end (to) the end of it,
    so a date range includes both days."""
    if value is None:
        return None
    if _EPOCH.fullmatch(value):
        t = int(value)
        if not EARLIEST_TS <= t < LATEST_TS:
            raise bad(f"{name} is outside the history.", "bad_time")
        return t
    if _DATE.fullmatch(value):
        try:
            day = dt.date.fromisoformat(value)
        except ValueError:
            raise bad(f"{name} is not a real date.", "bad_time") from None
        if day < stats.EARLIEST or day.year > 2100:
            raise bad(f"{name} is outside the history.", "bad_time")
        return london_midnight(day + dt.timedelta(days=1) if end else day)
    # "+00:00" sent unencoded in a query string arrives as " 00:00"
    text = _PLUS_LOST.sub(r"\1+\2", value).replace("Z", "+00:00").replace("z", "+00:00")
    try:
        t = dt.datetime.fromisoformat(text)
    except ValueError:
        raise bad(f"{name} must be epoch seconds, an ISO 8601 time or a date (YYYY-MM-DD).", "bad_time") from None
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.UTC)
    if not 2000 <= t.year <= 2100:
        raise bad(f"{name} is outside the history.", "bad_time")
    return int(t.timestamp())


def _range(params: Mapping[str, Any], now: int, default: int, longest: int, what: str) -> tuple[int, int]:
    to = parse_time(param(params, "to"), "to", end=True)
    frm = parse_time(param(params, "from"), "from")
    if to is None:
        to = now
    if frm is None:
        frm = to - default
    if frm >= to:
        raise bad("from must be before to.", "bad_range")
    if to - frm > longest:
        raise bad(f"{what} covers at most {longest // DAY} days per request.", "range_too_long")
    return frm, to


def _bool(value: str | None, name: str) -> bool | None:
    if value is None:
        return None
    v = value.lower()
    if v in _TRUE:
        return True
    if v in _FALSE:
        return False
    raise bad(f"{name} must be true or false.", "bad_parameter")


def _line_id(snap: Snapshot, line_id: Any) -> str:
    if not isinstance(line_id, str) or not stats.LINE_ID.fullmatch(line_id.strip().lower()):
        raise bad("That is not a line id (for example central or elizabeth).", "bad_line")
    lid = line_id.strip().lower()
    if lid not in snap.lines:
        raise ViewError(404, "unknown_line", f"No line {lid} is known. /v1/lines lists them.")
    return lid


# ---- episodes ---------------------------------------------------------------------------------------------------


def _minutes(seconds: float) -> int:
    return stats.minutes(max(0, seconds))


def episode_out(row: Sequence, names: Mapping[str, Mapping], now: int) -> dict[str, Any]:
    """A month row (8 or 11 fields; end None while open) as an API episode with ISO times."""
    line, start, end, sev = row[0], row[1], row[2], row[3]
    status = row[8] if len(row) > 8 and isinstance(row[8], str) and row[8] else SEVERITY_NAMES.get(sev, "Disrupted")
    reason = row[9] if len(row) > 9 and isinstance(row[9], str) else None
    planned = bool(row[5])
    cause = row[7] if isinstance(row[7], str) else None
    if end is None and cause is None:
        cause = cause_of(reason, planned, status)
    info = names.get(line) or {}
    return {
        "line": line,
        "name": info.get("name", line),
        "mode": info.get("mode"),
        "start": iso(start),
        "end": None if end is None else iso(end),
        "end_estimated": bool(row[10]) if len(row) > 10 else False,
        "minutes": _minutes((now if end is None else end) - start),
        "severity": sev,
        "status": status,
        "category": stats.cat_of(row[4]),
        "planned": planned,
        "cause": cause,
        "reason": reason,
        "source": row[6],
    }


def _well_formed(row: Any) -> bool:
    return (
        isinstance(row, list | tuple)
        and len(row) >= 8
        and isinstance(row[0], str)
        and _isint(row[1])
        and (row[2] is None or (_isint(row[2]) and row[2] >= row[1]))
        and _isint(row[3])
    )


def _isint(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _rows(snap: Snapshot, months: Iterable[str], *, with_open: bool = True) -> list[list]:
    """The public rows of the given months, each episode once (the copy with the latest end; a closed copy beats
    an open one), with the open episodes added (end None)."""
    best: dict[tuple, list] = {}

    def add(row: list) -> None:
        key = episode_key(row)
        have = best.get(key)
        if have is None or (row[2] is not None and (have[2] is None or row[2] > have[2])):
            best[key] = row

    for m in months:
        payload = snap.payload(m)
        for row in payload["episodes"] if payload else ():
            if _well_formed(row):
                add(list(row))
    if with_open:
        for row in snap.open_rows():
            if _well_formed(row):
                add(list(row))
    return list(best.values())


def open_until(snap: Snapshot, now: int) -> int:
    """The time open episodes run to: now, or once the recording has a gap (no poll for over GAP seconds) the last
    poll, where the gap rule will close them (end_estimated) when polling resumes."""
    last = snap.last_poll
    return min(now, last) if last is not None and now - last > GAP else now


def _open_out(snap: Snapshot, now: int) -> list[dict[str, Any]]:
    names = snap.lines
    rows = sorted(snap.open_rows(), key=lambda r: (r[1], r[0], r[3], r[5]))
    until = open_until(snap, now)
    out = []
    for row in rows:
        ep = episode_out(row, names, until)
        ep.pop("end_estimated")
        out.append(ep)
    return out


# ---- views ------------------------------------------------------------------------------------------------------


def status_view(rm: ReadModel) -> dict[str, Any]:
    """Every line's status now (tfl.current() at the last poll), the open episodes, and whether this is stale
    (no poll for over 5 minutes, or the store could not be read)."""
    snap, now = rm.snapshot(), rm.now()
    last = snap.last_poll
    status = snap.status
    return {
        **status,
        "open": _open_out(snap, now),
        "last_poll": iso(last) if last is not None else None,
        "stale": is_stale(snap, now),
    }


def is_stale(snap: Snapshot, now: int) -> bool:
    last = snap.last_poll
    return last is None or now - last > STALE_AFTER or snap.store_error is not None


def changes_view(rm: ReadModel, since: int) -> dict[str, Any]:
    """What changed after `since` (epoch seconds; use the cursor of the previous answer): the status now, the
    open episodes, the episodes that closed at or after `since`, and the poll times after it.

    An episode closed at a gap ends at the last poll before the gap, which can be the cursor's own poll, so closed
    episodes ending exactly at `since` are included: merge them on (line, severity, planned, start) and repeats are
    harmless. The polls are those in (since, cursor], every one of them: the cursor is the snapshot's last poll and
    the poll times read for it hold every poll up to it. `since` older than 35 days is refused (410): fetch the
    months instead."""
    if not _isint(since):
        try:
            since = int(str(since).strip())
        except ValueError:
            raise bad("since must be epoch seconds.", "bad_since") from None
    snap, now = rm.snapshot(), rm.now()
    if since < now - CHANGES_WINDOW:
        raise ViewError(
            410,
            "cursor_expired",
            "since is more than 35 days ago. Reload /v1/months and the month payloads, then sync from /v1/status.",
        )
    if since > now + DAY:
        raise bad("since is in the future.", "bad_since")
    if snap.partial:  # the state is newer than some months held: closed episodes could be missing
        raise _unavailable()
    names = snap.lines
    last = snap.last_poll
    upto = max(now, last or 0) + 1
    closed = [r for r in _rows(snap, month_keys(since, upto), with_open=False) if r[2] is not None and r[2] >= since]
    closed.sort(key=lambda r: (r[2], r[1], r[0], r[3], r[5]))
    polls: list[int] = []
    if last is not None and last > since:
        for day in rm.poll_days(since + 1, last + 1):  # the days of (since, last]
            doc = _polls(rm, day, need=last, strict=True, budget=False)  # at most 36 days: no budget needed
            if doc is not None:
                polls.extend(t for t in doc.at if since < t <= last)
    until = open_until(snap, now)
    return {
        "status": snap.status,
        "open": _open_out(snap, now),
        "closed": [episode_out(r, names, until) for r in closed],
        "polls": sorted(set(polls)),
        "last_poll": iso(last) if last is not None else None,
        "cursor": max(last, since) if last is not None else since,
        "stale": is_stale(snap, now),
    }


def _unavailable() -> ViewError:
    return ViewError(503, "store_unavailable", "The history store cannot be read just now; try again soon.")


def _polls(rm: ReadModel, day: str, need: int | None = None, *, strict: bool = False, budget: bool = True):
    try:
        return rm.polls(day, need, strict=strict, budget=budget)
    except HistoryBusy:
        raise ViewError(
            503,
            "over_capacity",
            "Too many days of history were read just now. Try a shorter window, or again in a minute.",
            retry_after=BUSY_RETRY_AFTER,
        ) from None
    except StoreUnavailable:
        raise _unavailable() from None


def lines_view(rm: ReadModel) -> dict[str, Any]:
    """Every known line: id, name, mode, the member lines of a shared line, and when it was first and last seen
    by the live recording."""
    snap = rm.snapshot()
    return {
        "lines": [
            {
                "id": x["id"],
                "name": x["name"],
                "mode": x["mode"],
                "shared_with": list(x["shared_with"]),
                "first_seen": iso(x["first_seen"]) if x["first_seen"] is not None else None,
                "last_seen": iso(x["last_seen"]) if x["last_seen"] is not None else None,
            }
            for x in snap.lines.values()
        ]
    }


def line_view(rm: ReadModel, line_id: str) -> dict[str, Any]:
    """One line's report (stats.line_report()): open episodes, figures over 24 hours to 90 days, its latest
    episodes with reasons and causes, and when unplanned disruptions start (London hours). Live recording only.
    While the recording has a gap the report is as of the last poll (see open_until), so the open episodes do not
    count time nobody watched."""
    snap, now = rm.snapshot(), rm.now()
    lid = _line_id(snap, line_id)
    info = snap.lines[lid]
    source = SOURCE_LIVE if SOURCE_LIVE in snap.public_sources else snap.public_sources[0]
    months = {m: e.payload for m, e in snap.months.items()}
    report = stats.line_report(
        {"id": lid, "name": info["name"], "mode": info["mode"]},
        months,
        snap.open,
        open_until(snap, now),
        source=source,
    )
    report["shared_with"] = list(info["shared_with"])
    report["source"] = source
    return report


def months_index_view(rm: ReadModel) -> dict[str, Any]:
    """The months index: the months with published data, the start of the history, the lines (with shared_with),
    the categories and the causes."""
    snap = rm.snapshot()
    return snap.index(rm.now())


def _month_name(month: Any) -> str:
    if not isinstance(month, str) or not stats.MONTH_ID.fullmatch(month.strip()):
        raise bad("A month is YYYY-MM, for example 2025-11.", "bad_month")
    return month.strip()


def month_view(rm: ReadModel, month: str) -> dict[str, Any]:
    """One UTC month's payload in the established format, with the open episodes added (end null)."""
    snap, now = rm.snapshot(), rm.now()
    name = _month_name(month)
    if name > month_key(now):
        raise ViewError(404, "unknown_month", f"{name} has not happened yet.")
    stored = snap.payload(name)
    a, b = month_bounds(name)
    opens = [r for r in snap.open_rows() if r[1] < b]
    if (stored is None or not snap.months[name].content) and not opens and name not in snap.month_names(now):
        raise ViewError(404, "unknown_month", f"Nothing is published for {name}. /v1/months lists the months.")
    base = stored if stored is not None else _empty_month(name, snap.public_sources)
    payload = merge_month(base, opens, (), SOURCE_LIVE) if opens else dict(base)
    payload["watched"] = {s: v for s, v in payload["watched"].items() if s in snap.public_sources}
    for s in snap.public_sources:
        payload["watched"].setdefault(s, [])
    return {"month": name, "episodes": payload["episodes"], "watched": payload["watched"]}


def _empty_month(name: str, sources: Sequence[str]) -> dict[str, Any]:
    m = new_month(name)
    m["watched"] = {s: [] for s in sources}
    return m


def month_is_past(month: str, now: int) -> bool:
    return month < month_key(now)


def disruptions_view(rm: ReadModel, params: Mapping[str, Any]) -> dict[str, Any]:
    """Episodes overlapping [from, to), oldest first, filtered and paged.

    Parameters (all optional): line, category, cause (a cause code or "unknown"), source (comma-separated lists),
    planned (true or false), from and to (epoch seconds, ISO 8601 or a London date; at most 366 days apart;
    default: the 30 days to now), limit (1-500, default 100) and cursor (next_cursor of the previous page)."""
    snap, now = rm.snapshot(), rm.now()
    frm, to = _range(params, now, DISRUPTIONS_DEFAULT_SPAN, DISRUPTIONS_MAX_SPAN, "A disruptions query")
    lines = [_line_id(snap, x) for x in _csv(param(params, "line"), "line")]
    cats = _csv(param(params, "category"), "category")
    if any(c not in CATEGORY_IDS for c in cats):
        raise bad(f"category must be one of {', '.join(CATEGORY_IDS)}.", "bad_category")
    causes = _csv(param(params, "cause"), "cause")
    if any(c not in CAUSE_LABELS and c != UNKNOWN_CAUSE for c in causes):
        raise bad("cause must be a cause code from /v1/meta, or unknown.", "bad_cause")
    sources = _csv(param(params, "source"), "source")
    for s in sources:
        if s not in snap.public_sources:
            raise bad(
                f"source must be one of {', '.join(snap.public_sources)} (the sources published here).",
                "bad_source",
            )
    planned = _bool(param(params, "planned"), "planned")
    limit = _limit(param(params, "limit"))
    after = _cursor(param(params, "cursor"))

    want_lines, want_cats, want_srcs = set(lines), set(cats), set(sources)
    want_causes = {None if c == UNKNOWN_CAUSE else c for c in causes}
    rows = []
    for r in _rows(snap, month_keys(frm, to)):
        if not (r[1] < to and (r[2] is None or r[2] > frm or (r[2] == r[1] == frm))):
            continue
        if want_lines and r[0] not in want_lines:
            continue
        if want_cats and stats.cat_of(r[4]) not in want_cats:
            continue
        if want_srcs and r[6] not in want_srcs:
            continue
        if planned is not None and bool(r[5]) != planned:
            continue
        if want_causes:
            code = r[7] if isinstance(r[7], str) else None
            if r[2] is None and code is None:
                code = cause_of(r[9] if len(r) > 9 else None, bool(r[5]), r[8] if len(r) > 8 else None)
            if code not in want_causes:
                continue
        rows.append(r)
    rows.sort(key=_order)
    total = len(rows)
    if after is not None:
        rows = [r for r in rows if _order(r) > after]
    page = rows[:limit]
    more = len(rows) > limit
    names = snap.lines
    until = open_until(snap, now)
    return {
        "from": iso(frm),
        "to": iso(to),
        "episodes": [episode_out(r, names, until) for r in page],
        "count": len(page),
        "total": total,
        "limit": limit,
        "next_cursor": _encode_cursor(_order(page[-1])) if more and page else None,
    }


def _order(row: Sequence) -> tuple:
    return (row[1], row[0], row[3], int(bool(row[5])), str(row[6]))


def _limit(value: str | None) -> int:
    if value is None:
        return DISRUPTIONS_LIMIT
    if not re.fullmatch(r"[0-9]{1,4}", value) or not 1 <= int(value) <= DISRUPTIONS_MAX_LIMIT:
        raise bad(f"limit must be a whole number from 1 to {DISRUPTIONS_MAX_LIMIT}.", "bad_limit")
    return int(value)


def _encode_cursor(key: tuple) -> str:
    raw = json.dumps(list(key), separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _cursor(value: str | None) -> tuple | None:
    if value is None:
        return None
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        key = json.loads(raw)
    except (binascii.Error, ValueError, UnicodeDecodeError):
        raise bad("That cursor could not be read; start again without it.", "bad_cursor") from None
    if (
        not isinstance(key, list)
        or len(key) != 5
        or not _isint(key[0])
        or not isinstance(key[1], str)
        or not _isint(key[2])
        or key[3] not in (0, 1)
        or not isinstance(key[4], str)
    ):
        raise bad("That cursor could not be read; start again without it.", "bad_cursor")
    return (key[0], key[1], key[2], key[3], key[4])


def stats_view(rm: ReadModel, params: Mapping[str, Any]) -> dict[str, Any]:
    """Figures for a period (stats.period_figures()): span 1d, 7d, 30d, 90d or 365d ending now or on a London date
    (default 7d ending now), against the period before it; with line, for one line only. "period" adds the period's
    labels and the steps back and on."""
    snap, now = rm.snapshot(), rm.now()
    query = {"span": param(params, "span") or "7d", "end": param(params, "end") or "now"}
    parsed, why = stats.parse_period(query, london_date(now))
    if parsed is None:
        raise bad(why or "That period could not be read.", "bad_period")
    span, end = parsed
    line_param = param(params, "line")
    idx = snap.idx(now)
    p = stats.period_of(span, end, now, idx["first"])
    months = {}
    for m in month_keys(p["p0"], p["t1"]):
        parsed_month = snap.parsed(m)
        if parsed_month is not None:
            months[m] = parsed_month
    data = stats.gather(months, idx, p["p0"], p["t1"], now, sources=snap.public_sources)
    line = None
    if line_param is not None:
        line = _line_id(snap, line_param)
        entry = next(x for x in idx["lines"] if x["id"] == line)
        members = stats.members_of(idx)
        srcs = _sources_of(entry, members)
        data = {
            "eps": {line: data["eps"].get(line, [])},
            "watched": {s: (spans if s in srcs else []) for s, spans in data["watched"].items()},
        }
        idx = {**idx, "lines": [entry]}
    fig = stats.period_figures(idx, data, p, now)
    fig["line"] = line
    fig["period"] = {
        "label": p["label"],
        "before": p["before"],
        "prev": p["prev"],
        "next": p["next"],
        "today": p["today"].isoformat(),
        "first": iso(p["first"]) if p["first"] is not None else None,
    }
    return fig


def _sources_of(line: Mapping, members: Mapping) -> list[str]:
    """The sources that watch a line, as stats.cover_of() decides."""
    if line["shared_with"]:
        return [SOURCE_FOI] if line["mode"] == "tube" else []
    return [SOURCE_LIVE] + ([SOURCE_FOI] if line["mode"] == "tube" and line["id"] not in members else [])


def stats_is_past(params: Mapping[str, Any], now: int) -> bool:
    """Whether a stats query names a past date (its figures no longer change)."""
    try:
        end = param(params, "end")
    except ViewError:
        return False
    return end is not None and end != "now" and end < london_date(now).isoformat()


def coverage_view(rm: ReadModel, params: Mapping[str, Any]) -> dict[str, Any]:
    """When each published source watched in [from, to) (default the 7 days to now, at most 90 days), the share
    watched, the gaps no source watched, and per UTC day the polls recorded and the slots that failed.

    Only the days from the live recording's start to now are read from the store; days outside it hold nothing
    and are answered without a read."""
    snap, now = rm.snapshot(), rm.now()
    frm, to = _range(params, now, COVERAGE_DEFAULT_SPAN, COVERAGE_MAX_SPAN, "A coverage query")
    wall = to - frm
    spans_by_src: dict[str, list] = {s: [] for s in snap.public_sources}
    for m in month_keys(frm, to):
        payload = snap.payload(m)
        for src, spans in (payload["watched"] if payload else {}).items():
            if src in spans_by_src:
                spans_by_src[src].extend(list(s) for s in spans if _isint(s[0]) and _isint(s[1]) and s[1] > s[0])
    sources = {}
    union: list = []
    for src, spans in spans_by_src.items():
        clipped = [[max(a, frm), min(b, to)] for a, b in merge_spans(spans) if min(b, to) > max(a, frm)]
        union += clipped
        secs = sum(b - a for a, b in clipped)
        sources[src] = {
            "watched": [[iso(a), iso(b)] for a, b in clipped],
            "watched_s": secs,
            "coverage_pct": round(100 * secs / wall, 2),
        }
    union = merge_spans(union)
    covered = sum(b - a for a, b in union)
    gaps = stats.complement([tuple(s) for s in union], frm, to)
    days = []
    lo, hi = snap.polls_from, min(to, now + 1)  # nothing can be recorded before the recording or after now
    for day in rm.poll_days(frm, to):
        start = day_start(day)
        doc = None
        if lo is not None and start + DAY > lo and start < hi:
            doc = _polls(rm, day, need=snap.last_poll)
        at = [t for t in (doc.at if doc else ()) if frm <= t < to]
        failed = [f for f in (doc.failed if doc else ()) if isinstance(f, Mapping) and _isint(f.get("slot"))]
        failed = [f for f in failed if frm <= f["slot"] * 60 < to]
        days.append(
            {
                "date": day,
                "polls": len(at),
                "expected": POLLS_PER_DAY,
                "failed": [
                    {"slot": f["slot"], "at": iso(f["slot"] * 60), "code": str(f.get("code") or "unknown")}
                    for f in sorted(failed, key=lambda f: f["slot"])
                ],
            }
        )
    return {
        "from": iso(frm),
        "to": iso(to),
        "sources": sources,
        "coverage_pct": round(100 * covered / wall, 2),
        "gaps": [{"start": iso(a), "end": iso(b), "minutes": _minutes(b - a)} for a, b in gaps],
        "days": days,
    }


def causes_view(rm: ReadModel) -> dict[str, Any]:
    """The causes an episode can be put down to, in the order the rules try them. A null cause means unknown."""
    return {"causes": [{"code": code, "label": label} for code, label, _ in CAUSES], "signature": CAUSES_SIG}


def meta_view(rm: ReadModel) -> dict[str, Any]:
    """About this API: the sources published, categories, causes, licence and attribution, and the fair-use
    limits."""
    lim = rm.limits
    return {
        "name": "Tifa",
        "description": "London TfL line-status disruptions, recorded every minute.",
        "api_version": API_VERSION,
        "sources": [{**SOURCE_INFO[s], "published": True} for s in rm.public_sources if s in SOURCE_INFO],
        "categories": [dict(c) for c in CATEGORIES],
        "causes": causes_view(rm)["causes"],
        "licence": LICENCE,
        "attribution": ATTRIBUTION,
        "notice": NOTICE,
        "fair_use": {
            "per_ip": {
                "bucket": lim.ip_bucket,
                "refill_per_s": lim.ip_refill,
                "costs": {"cached": lim.cost_cached, "uncached": lim.cost_uncached, "mcp_tool_call": lim.cost_mcp_call},
                "egress_bytes_per_hour": client_egress(lim),
            },
            "global": {
                "requests_per_s": lim.global_rps,
                "burst": lim.global_burst,
                "uncached_per_s": lim.uncached_per_s,
                "egress_bytes_per_hour": lim.egress_per_hour,
            },
            "over_limit": "429 (per client) or 503 (whole service) with Retry-After",
        },
        "links": {"docs": "/docs", "openapi": "/openapi.json", "mcp": "/mcp"},
    }
