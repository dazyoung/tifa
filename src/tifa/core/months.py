"""Month payloads: every episode of a UTC month and the time each source watched in it.

    {"month": "YYYY-MM",
     "episodes": [[line, start, end, severity, category, planned01, source, cause, status, reason,
                   end_estimated01], ...],
     "watched": {"a": [[s, e], ...], "f": [[s, e], ...]}}

An episode is in every month it overlaps (one running over a month end is in both; one ending exactly at a month's
start is not in that month), and episodes are sorted by start. The source is "a" for the live recording and "f" for
TfL's FOI release. "watched" holds the spans of the month each source covered; live coverage is the runs of polls
no more than GAP apart. Stored months hold closed episodes; open ones (end None) are added when served. Clients
that read only the first 8 fields get the established month format.

A poll at ts records into the months like this:

    run = extend_run(prev_run, ts)
    piece = run_segment(prev_run, run)                # only the time this poll added
    for name in split_months(*piece):                 # skip when piece is None
        months[name] = merge_month(months.get(name) or new_month(name), (), [piece])
    for ep in closed:                                 # the episodes this poll closed
        for name in split_months(ep["start"], ep["end"]):
            months[name] = merge_month(months.get(name) or new_month(name), [ep])
"""

from collections.abc import Iterable, Mapping, Sequence

from .causes import cause
from .episodes import GAP
from .tfl import CATEGORY
from .timeutil import epoch, month_bounds, month_index, month_name, month_start

SOURCE_LIVE = "a"
SOURCE_FOI = "f"
SOURCES = (SOURCE_LIVE, SOURCE_FOI)
ROW_FIELDS = (
    "line",
    "start",
    "end",
    "severity",
    "category",
    "planned",
    "source",
    "cause",
    "status",
    "reason",
    "end_estimated",
)


def split_months(start: int, end: int | None, now_ts: int | None = None) -> list[str]:
    """The months ("YYYY-MM") an episode from start to end is in: the month it starts in and each later month
    that begins before it ends. An open episode (end None) runs to the month of now_ts."""
    first = month_index(start)
    if end is None:
        if now_ts is None:
            raise ValueError("an open episode needs now_ts")
        last = max(first, month_index(now_ts))
    else:
        last = first
        while month_start(last + 1) < end:
            last += 1
    return [month_name(i) for i in range(first, last + 1)]


def clip_spans(spans: Iterable[Sequence[int]], a: int, b: int) -> list[list[int]]:
    """The parts of spans inside [a, b], dropping any of zero length."""
    return [[max(x, a), min(y, b)] for x, y in spans if min(y, b) > max(x, a)]


def merge_spans(spans: Iterable[Sequence[int]]) -> list[list[int]]:
    """Spans sorted, with those that overlap or touch joined."""
    out = []
    for a, b in sorted(spans):
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def extend_run(run: Sequence[int] | None, ts: int, gap: int = GAP) -> list[int]:
    """The watched run after a poll at ts: run extended if ts is no more than gap after its end, else a new run
    [ts, ts]. A poll before the run's end changes nothing."""
    if run and 0 <= ts - run[1] <= gap:
        return [run[0], ts]
    if not run or ts > run[1]:
        return [ts, ts]
    return list(run)


def run_segment(prev: Sequence[int] | None, run: Sequence[int]) -> list[int] | None:
    """The watched time one poll added, from the run before it (prev) and after it (run = extend_run(prev, ts)):
    [prev end, ts] when the poll extended prev, [ts, ts] when it began a new run, None when it changed nothing.

    Merge only this piece into the months it is in, `split_months(*piece)`, not the whole run: merge_spans joins
    touching spans, so every month ends up the same, but a poll touches one month (two across a month end) however
    long the run has lasted, and past months keep their revisions."""
    if prev and list(run) == list(prev):
        return None
    if prev and run[0] == prev[0]:
        return [prev[1], run[1]]
    return [run[0], run[1]]


def runs_of(poll_times: Iterable[int], gap: int = GAP) -> list[list[int]]:
    """Sorted poll times -> the runs of watched time, [first, last poll], polls in a run no more than gap apart."""
    runs: list[list[int]] = []
    for ts in poll_times:
        if runs and 0 <= ts - runs[-1][1] <= gap:
            runs[-1][1] = ts
        elif not runs or ts > runs[-1][1]:
            runs.append([ts, ts])
    return runs


def _ts(value) -> int:
    return epoch(value) if isinstance(value, str) else value


def month_episode(ep: Mapping, source: str) -> list:
    """An episode (OpenEpisode or ClosedEpisode; times as epochs or ISO 8601) as a month payload row, with its
    cause."""
    reason, status = (x if isinstance(x, str) else None for x in (ep.get("reason"), ep.get("status")))
    planned, end = bool(ep["planned"]), ep.get("end")
    return [
        ep["line"],
        _ts(ep["start"]),
        None if end is None else _ts(end),
        ep["severity"],
        ep.get("category") or CATEGORY(ep["severity"]),
        int(planned),
        source,
        cause(reason, planned, status),
        status,
        reason,
        int(bool(ep.get("end_estimated"))),
    ]


def row_dict(row: Sequence) -> dict:
    """A month payload row (8 or 11 fields) as a dict keyed by ROW_FIELDS, with planned and end_estimated as
    booleans."""
    out = dict(zip(ROW_FIELDS, row, strict=False))
    out["planned"] = bool(out["planned"])
    if "end_estimated" in out:
        out["end_estimated"] = bool(out["end_estimated"])
    return out


def episode_key(row: Sequence) -> tuple:
    """(line, severity, planned, start, source): one episode, in whichever months it is."""
    return row[0], row[3], int(bool(row[5])), row[1], row[6]


def _row_order(row: Sequence) -> tuple:
    return row[1], row[0], row[6], row[3], row[5], row[2] is None, row[2] or 0


def _overlaps(row: Sequence, a: int, b: int) -> bool:
    s, e = row[1], row[2]
    return s < b and (e is None or e > a or (e == a and s >= a))


def new_month(month: str) -> dict:
    month_bounds(month)  # checks the name
    return {"month": month, "episodes": [], "watched": {SOURCE_LIVE: [], SOURCE_FOI: []}}


def merge_month(
    payload: Mapping, add_episodes: Iterable = (), add_watched: Iterable[Sequence[int]] = (), source: str = SOURCE_LIVE
) -> dict:
    """A copy of a month payload with episodes and watched spans added; payload itself is not changed.

    add_episodes are rows or episode dicts (made into rows of `source`); those that do not overlap the month are
    left out. A row replaces the one with the same (line, severity, planned, start, source), except that an open
    copy never replaces a closed one. add_watched spans are clipped to the month and joined with `source`'s.
    Other sources' rows and spans are kept as they are."""
    month = payload["month"]
    a, b = month_bounds(month)
    rows: dict[tuple, list] = {}
    for row in payload.get("episodes") or ():
        rows[episode_key(row)] = list(row)
    for ep in add_episodes:
        row = list(ep) if isinstance(ep, (list, tuple)) else month_episode(ep, source)
        if not _overlaps(row, a, b):
            continue
        key = episode_key(row)
        have = rows.get(key)
        if have is not None and have[2] is not None and row[2] is None:
            continue  # a closed copy beats an open one
        rows[key] = row
    old = payload.get("watched") or {}
    watched = {src: [list(s) for s in old.get(src) or ()] for src in SOURCES}
    watched.update((src, [list(s) for s in spans]) for src, spans in old.items() if src not in watched)
    watched[source] = merge_spans(watched.get(source, []) + clip_spans(add_watched, a, b))
    return {**payload, "month": month, "episodes": sorted(rows.values(), key=_row_order), "watched": watched}
