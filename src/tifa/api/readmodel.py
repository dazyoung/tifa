"""The API's in-memory read model of the store.

The public API never reads the store per request: it reads a snapshot held in memory, refreshed from the store at
most every ``state_ttl`` seconds (30 by default). A refresh reads `meta/state` (one document) and reloads only the
months whose revision changed, so Firestore reads do not grow with traffic. Days of poll times (for /v1/coverage and
/v1/changes) and the guard document (for /health) are read on demand and kept for a while.

Snapshots are immutable once built and swapped in whole, so a request reads one consistent snapshot however many
refreshes happen meanwhile. One refresh runs at a time; other requests keep serving the current snapshot. A
snapshot's state and months always come from the same refresh: if any month cannot be read, the whole last good
snapshot stays (a newer state with an older month would lose the episodes closed in between).

A day of poll times is kept with the newest poll known to be recorded when it was read, so a copy read before a
snapshot's last poll is read again for that snapshot: /v1/changes never moves a cursor past polls it has not sent.
Finished days are read at most once per DAY_TTL, one read per day at a time, and within a budget of store reads.

When the store fails, the last good snapshot stays in service, marked with the error (``store_error``), and views
report the data as stale. Only the sources that may be published are kept: rows and watched spans of any other
source are dropped as months load, so nothing downstream can serve them by mistake.
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from tifa.api.ratelimit import TokenBucket
from tifa.config import Limits
from tifa.core import stats
from tifa.core.causes import CAUSES
from tifa.core.months import SOURCE_LIVE, month_episode
from tifa.core.tfl import CATEGORY_IDS, MODE_ORDER
from tifa.core.timeutil import DAY, iso, month_bounds, month_key, month_keys, utc_day
from tifa.store import codec
from tifa.store.base import PollsDoc, State, Store

log = logging.getLogger("tifa.api.readmodel")

STATE_TTL = 30.0  # seconds between reads of meta/state
DAY_TTL = 3600.0  # seconds a finished UTC day of poll times is kept before it is read again
GUARD_TTL = 30.0  # seconds the guard document is kept
POLL_DAYS_MAX = 400  # UTC days of poll times kept in memory
SETTLE = 600  # seconds after a UTC day ends after which nothing more is recorded in it (retries are within a minute)
DAY_READS_BURST = 200  # finished days of poll times read from the store at once, at most...
DAY_READS_PER_S = 0.25  # ...and then this many a second, about 22,000 a day whatever the requests. The current day
# is not counted: it is read at most once per snapshot (every state_ttl).
_DAY_LOCKS = 32  # stripes of the one-read-per-day locks

# Lines TfL's FOI release reports as one: they are never polled live, so their names come from here.
SHARED_LINES: dict[str, dict[str, Any]] = {
    "circle-hammersmith-city": {
        "name": "Circle and Hammersmith & City",
        "mode": "tube",
        "shared_with": ["circle", "hammersmith-city"],
    },
}


class StoreUnavailable(RuntimeError):
    """The store could not be read and nothing usable is held in memory."""


class HistoryBusy(StoreUnavailable):
    """Too many finished days of poll times were read from the store lately (a cost limit): try again soon."""


@dataclass(frozen=True)
class MonthEntry:
    """One month as loaded: its revision, the payload with only public sources, and a summary of it."""

    month: str
    rev: int
    payload: dict[str, Any]
    lines: frozenset[str]
    first: int | None  # the earliest time in it (an episode start or watched span start)
    content: bool  # any public episodes or watched time
    live_first: int | None = None  # the earliest time the live recording watched in it (its first poll there)
    _parsed: list = field(default_factory=list, repr=False, compare=False)  # stats.parse_month(payload), once

    def parsed(self) -> dict[str, Any] | None:
        """stats.parse_month() of the payload, kept with the entry: an unchanged month is parsed once, however
        many snapshots share it."""
        if not self._parsed:
            self._parsed.append(stats.parse_month(self.payload))
        return self._parsed[0]


@dataclass(frozen=True)
class Snapshot:
    """What the API serves, as of one refresh. Treat every field as read-only."""

    state: State | None
    months: Mapping[str, MonthEntry]
    public_sources: tuple[str, ...]
    version: str  # changes whenever the state or any month changes
    loaded_at: float  # wall-clock time of the refresh that produced it
    store_error: str | None = None  # why the last refresh failed, if it did (the data is then the last good)
    error_since: float | None = None
    # Some months could not be read on the very first load, so the state is newer than what is held of them. Only
    # then: a later failure keeps the last whole snapshot instead. Sync answers (/v1/changes) refuse such a one.
    partial: bool = False
    _memo: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    # ---- the state ------------------------------------------------------------------------------------------

    @property
    def last_poll(self) -> int | None:
        return self.state.last_poll if self.state else None

    @property
    def polls_from(self) -> int | None:
        """The earliest time a poll can have been recorded: the live recording's first watched time or the first
        time it saw a line. None when the live recording is not published or has not started."""

        def build() -> int | None:
            if SOURCE_LIVE not in self.public_sources:
                return None
            times = [e.live_first for e in self.months.values() if e.live_first is not None]
            if self.state is not None:
                for info in self.state.lines.values():
                    seen = _int(info.get("first_seen")) if isinstance(info, Mapping) else None
                    if seen is not None:
                        times.append(seen)
                if self.state.last_poll is not None:
                    times.append(self.state.last_poll)
            return min(times) if times else None

        return self._cached("polls_from", build)

    @property
    def open(self) -> list[dict[str, Any]]:
        """Open episodes (OpenEpisode), only when the live recording is public."""
        if self.state is None or SOURCE_LIVE not in self.public_sources:
            return []
        return [e for e in self.state.open if isinstance(e, Mapping)]

    @property
    def status(self) -> dict[str, Any]:
        """The current status (tfl.current()) as of the last poll; empty lists before the first poll."""
        raw = self.state.status if self.state else None
        if not isinstance(raw, Mapping) or SOURCE_LIVE not in self.public_sources:
            return {"lines": [], "disrupted": 0, "total": 0}
        return {
            "lines": list(raw.get("lines") or []),
            "disrupted": raw.get("disrupted", 0),
            "total": raw.get("total", 0),
        }

    @property
    def tfl_auth(self) -> str | None:
        return self.state.tfl_auth if self.state else None

    # ---- months ---------------------------------------------------------------------------------------------

    def payload(self, month: str) -> dict[str, Any] | None:
        entry = self.months.get(month)
        return entry.payload if entry is not None else None

    def open_rows(self) -> list[list]:
        """The open episodes as month rows with end None (source "a"), as the month payloads serve them."""
        return self._cached("open_rows", lambda: [month_episode(e, SOURCE_LIVE) for e in _valid_open(self.open)])

    def open_sig(self) -> str:
        """A digest of the open episodes: changes when one opens, closes or changes."""
        return self._cached("open_sig", lambda: codec.digest(self.open_rows()))

    def parsed(self, month: str) -> dict[str, Any] | None:
        """stats.parse_month() of a month with its open episodes added, cached for this snapshot."""

        def build() -> dict[str, Any] | None:
            a, b = month_bounds(month)
            entry = self.months.get(month)
            opens = [r for r in self.open_rows() if r[1] < b]
            if entry is None and not opens:
                return None
            base = entry.parsed() if entry is not None else {"eps": [], "watched": {}}
            if base is None:
                return None
            if opens:
                extra = stats.parse_month({"episodes": opens, "watched": {}}) or {"eps": []}
                base = {"eps": base["eps"] + extra["eps"], "watched": base["watched"]}
            return base

        return self._cached(f"parsed:{month}", build)

    # ---- lines and the months index -------------------------------------------------------------------------

    @property
    def lines(self) -> dict[str, dict[str, Any]]:
        """{id: {id, name, mode, shared_with, first_seen, last_seen}}: the lines polled live and the lines in the
        public months, in display order (mode, then name)."""
        return self._cached("lines", self._build_lines)

    def _build_lines(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        live = SOURCE_LIVE in self.public_sources
        if self.state is not None and live:
            for lid, info in self.state.lines.items():
                if not isinstance(lid, str) or not stats.LINE_ID.fullmatch(lid) or not isinstance(info, Mapping):
                    continue
                name, mode = info.get("name"), info.get("mode")
                out[lid] = {
                    "id": lid,
                    "name": name if isinstance(name, str) and name else lid,
                    "mode": mode if isinstance(mode, str) else None,
                    "shared_with": [],
                    "first_seen": _int(info.get("first_seen")),
                    "last_seen": _int(info.get("last_seen")),
                }
        for ep in self.open:
            lid = ep.get("line")
            if isinstance(lid, str) and stats.LINE_ID.fullmatch(lid) and lid not in out:
                name, mode = ep.get("name"), ep.get("mode")
                out[lid] = _line(lid, name if isinstance(name, str) else None, mode if isinstance(mode, str) else None)
        for entry in self.months.values():
            for lid in entry.lines:
                if lid not in out and stats.LINE_ID.fullmatch(lid):
                    shared = SHARED_LINES.get(lid)
                    if shared:
                        out[lid] = _line(lid, shared["name"], shared["mode"], list(shared["shared_with"]))
                    else:
                        out[lid] = _line(lid, None, None)
        ordered = sorted(out.values(), key=lambda x: (MODE_ORDER.get(x["mode"], 9), x["name"].casefold(), x["id"]))
        return {x["id"]: x for x in ordered}

    @property
    def first(self) -> int | None:
        """The start of the published history: the earliest public episode or watched time."""
        firsts = [e.first for e in self.months.values() if e.first is not None]
        return min(firsts) if firsts else None

    def month_names(self, now_ts: int) -> list[str]:
        """The months with public data: stored, overlapped by an open episode, or the current month while the live
        recording runs."""
        names = {m for m, e in self.months.items() if e.content}
        current = month_key(now_ts)
        if self.last_poll is not None and month_key(self.last_poll) == current:
            names.add(current)
        starts = [r[1] for r in self.open_rows()]
        if starts:
            names.update(month_keys(min(starts), max(now_ts, min(starts)) + 1))
        return sorted(n for n in names if n <= current)

    def index(self, now_ts: int) -> dict[str, Any]:
        """The months index (/v1/months)."""
        months = self.month_names(now_ts)
        return {
            "months": months,
            "first": iso(self.first) if self.first is not None else None,
            "lines": [
                {"id": x["id"], "name": x["name"], "mode": x["mode"], "shared_with": list(x["shared_with"])}
                for x in self.lines.values()
            ],
            "categories": list(CATEGORY_IDS),
            "causes": [{"code": code, "label": label} for code, label, _ in CAUSES],
        }

    def idx(self, now_ts: int) -> dict[str, Any]:
        """stats.parse_index() of the months index, for the period figures."""
        key = f"idx:{month_key(now_ts)}"
        return self._cached(key, lambda: stats.parse_index(self.index(now_ts)))

    def _cached(self, key: str, build: Callable[[], Any]) -> Any:
        # A race only builds the same value twice; dict assignment is atomic.
        try:
            return self._memo[key]
        except KeyError:
            value = self._memo[key] = build()
            return value


def _line(lid: str, name: str | None, mode: str | None, shared: list[str] | None = None) -> dict[str, Any]:
    return {
        "id": lid,
        "name": name or lid.replace("-", " ").title(),
        "mode": mode,
        "shared_with": shared or [],
        "first_seen": None,
        "last_seen": None,
    }


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _valid_open(entries: Iterable[Mapping]) -> list[Mapping]:
    out = []
    for e in entries:
        if (
            isinstance(e.get("line"), str)
            and _int(e.get("severity")) is not None
            and _int(e.get("start")) is not None
            and isinstance(e.get("planned"), bool | int)
        ):
            out.append(e)
    return out


def public_payload(payload: Mapping[str, Any], month: str, sources: Iterable[str]) -> dict[str, Any]:
    """A month payload with only the given sources' rows and watched spans."""
    keep = frozenset(sources)
    rows = [
        list(r)
        for r in payload.get("episodes") or ()
        if isinstance(r, list | tuple) and len(r) >= 7 and r[6] in keep and isinstance(r[0], str)
    ]
    watched_in = payload.get("watched") if isinstance(payload.get("watched"), Mapping) else {}
    watched = {
        src: [list(s) for s in spans if isinstance(s, list | tuple) and len(s) == 2]
        for src, spans in watched_in.items()
        if src in keep and isinstance(spans, list)
    }
    return {"month": month, "episodes": rows, "watched": watched}


def month_entry(month: str, rev: int, payload: Mapping[str, Any], sources: Iterable[str]) -> MonthEntry:
    public = public_payload(payload, month, sources)
    starts = [r[1] for r in public["episodes"] if _int(r[1]) is not None]
    starts += [s[0] for spans in public["watched"].values() for s in spans if _int(s[0]) is not None and s[1] > s[0]]
    content = bool(public["episodes"]) or any(
        s[1] > s[0] for spans in public["watched"].values() for s in spans if _int(s[0]) is not None
    )
    # every poll lies in a watched span of the live recording (a run of one poll is a span of no length)
    live = [s[0] for s in public["watched"].get(SOURCE_LIVE, ()) if _int(s[0]) is not None and _int(s[1]) is not None]
    return MonthEntry(
        month=month,
        rev=rev,
        payload=public,
        lines=frozenset(r[0] for r in public["episodes"]),
        first=min(starts) if starts else None,
        content=content,
        live_first=min(live) if live else None,
    )


def _version(state: State | None, months: Mapping[str, MonthEntry]) -> str:
    if state is None:
        basis: Any = ["none", sorted((m, e.rev) for m, e in months.items())]
    else:
        basis = [
            state.last_poll,
            state.last_slot,
            state.updated_at,
            state.status_etag,
            state.tfl_auth,
            codec.digest(state.open) if state.open else "",
            sorted((m, e.rev) for m, e in months.items()),
        ]
    return codec.digest(basis)[:20]


@dataclass
class _Cached:
    value: Any
    at: float  # monotonic


@dataclass(frozen=True)
class _PollDay:
    """One UTC day of poll times as read, and what the read is known to hold."""

    doc: PollsDoc | None
    at: float  # monotonic time of the read
    # A poll recorded before the read: the copy holds every poll up to this time. Polls are recorded in order,
    # each with the state in one transaction, so the newest last_poll in service when the read began qualifies.
    through: int | None
    settled: bool  # read once the day was over and settled: nothing more will be recorded in it

    def covers(self, need: int | None, day_end: int) -> bool:
        """Whether the copy holds every poll of its day up to `need` (a snapshot's last poll)."""
        if need is None or self.settled:
            return True
        return self.through is not None and (self.through >= need or self.through >= day_end)


def day_start(day: str) -> int:
    """00:00 UTC of a day "YYYY-MM-DD"."""
    d = dt.date.fromisoformat(day)
    return int(dt.datetime(d.year, d.month, d.day, tzinfo=dt.UTC).timestamp())


class ReadModel:
    """The API's view of the store. Thread-safe: refreshes may run in any thread while others read snapshots."""

    def __init__(
        self,
        store: Store | Callable[[], Store],
        *,
        public_sources: Iterable[str] = (SOURCE_LIVE,),
        limits: Limits | None = None,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        state_ttl: float = STATE_TTL,
        day_reads: tuple[float, float] | None = None,
    ) -> None:
        """`store` is a Store, or a function that makes one (called on the first refresh, so building the app
        never touches the network). `clock` gives wall-clock epoch seconds; tests pass a fake one. `day_reads` is
        the budget of settled days read from the store, (burst, per second); default (DAY_READS_BURST,
        DAY_READS_PER_S)."""
        burst, per_s = day_reads if day_reads is not None else (DAY_READS_BURST, DAY_READS_PER_S)
        self._store_or_factory = store
        self._store: Store | None = None
        self.public_sources = tuple(dict.fromkeys(public_sources))
        self.limits = limits or Limits()
        self._clock = clock
        self._mono = monotonic
        self.state_ttl = state_ttl
        self._snap = Snapshot(None, {}, self.public_sources, _version(None, {}), 0.0)
        self._loaded = False
        self._last_attempt: float | None = None  # monotonic
        self._refresh_lock = threading.Lock()
        self._lock = threading.Lock()  # guards the small caches below
        self._spare: dict[str, MonthEntry] = {}  # months read by a refresh that failed elsewhere, for the next one
        self._polls: OrderedDict[str, _PollDay] = OrderedDict()
        self._day_locks = [threading.Lock() for _ in range(_DAY_LOCKS)]
        self._day_reads = TokenBucket(burst, per_s, monotonic())
        self._guard: _Cached | None = None

    # ---- time -----------------------------------------------------------------------------------------------

    def now(self) -> int:
        """Wall-clock epoch seconds."""
        return int(self._clock())

    # ---- snapshots ------------------------------------------------------------------------------------------

    def snapshot(self) -> Snapshot:
        """The current snapshot. On the first call, loads it (blocking) if no refresh has run yet."""
        if not self._loaded:
            self.refresh()
        return self._snap

    def peek(self) -> Snapshot:
        """The current snapshot without loading anything (empty before the first refresh)."""
        return self._snap

    @property
    def version(self) -> str:
        return self._snap.version

    def due(self) -> bool:
        """Whether a refresh would read the store now."""
        return self._last_attempt is None or self._mono() - self._last_attempt >= self.state_ttl

    def store(self) -> Store:
        if self._store is None:
            s = self._store_or_factory
            # anything with get_state is a store (wrappers included); otherwise a function that makes one
            self._store = s if hasattr(s, "get_state") else s()  # type: ignore[operator,assignment]
        return self._store

    def refresh(self, force: bool = False) -> Snapshot:
        """Read the state (and changed months) if due or forced. Never raises for store errors: the last good
        snapshot stays, marked with the error. Returns the snapshot now in service."""
        if self._loaded and not force and not self.due():
            return self._snap
        blocking = not self._loaded  # nothing to serve yet: wait for the refresh in flight
        if not self._refresh_lock.acquire(blocking=blocking):
            return self._snap
        try:
            if blocking and self._loaded:  # another thread's first load finished while we waited
                return self._snap
            if self._loaded and not force and not self.due():  # another thread refreshed meanwhile
                return self._snap
            self._last_attempt = self._mono()
            self._snap = self._load(self._snap)
            self._loaded = True
            return self._snap
        finally:
            self._refresh_lock.release()

    def _load(self, old: Snapshot) -> Snapshot:
        now = self._clock()
        try:
            store = self.store()
            state = store.get_state()
        except Exception as exc:  # the store is down: keep serving what we have
            return self._failed(old, exc, now)
        index = dict(state.months) if state is not None else {}
        months: dict[str, MonthEntry] = {}
        error: Exception | None = None
        for month, info in sorted(index.items()):
            rev = info.get("rev") if isinstance(info, Mapping) else None
            have = old.months.get(month)
            if have is None or have.rev != rev:
                have = self._spare.get(month)
            if have is not None and have.rev == rev:
                months[month] = have
                continue
            try:
                doc = store.get_month(month)
            except Exception as exc:
                error = exc
                continue
            if doc is None:
                continue
            months[month] = month_entry(month, doc.rev, doc.payload, self.public_sources)
        if error is not None:
            # The new state with an old revision of a month would lose the episodes the polls in between closed:
            # no longer open (new state), not yet closed (old month), and a sync client's cursor past them. So the
            # last whole snapshot stays, and the months read now wait for the next refresh.
            self._spare = {m: e for m, e in months.items() if old.months.get(m) is not e}
            if old.state is not None:
                return self._failed(old, error, now)
            log.warning("store read failed (%s) on the first load; serving what could be read", type(error).__name__)
            return Snapshot(
                state,
                months,
                self.public_sources,
                _version(state, months),
                now,
                store_error=type(error).__name__,
                error_since=now,
                partial=True,
            )
        self._spare = {}
        return Snapshot(state, months, self.public_sources, _version(state, months), now)

    def _failed(self, old: Snapshot, exc: Exception, now: float) -> Snapshot:
        """The last good snapshot, whole (its state with its months), marked with the error."""
        reason = type(exc).__name__
        log.warning("store read failed (%s); serving the last good data", reason)
        return Snapshot(
            old.state,
            old.months,
            self.public_sources,
            old.version,
            old.loaded_at if old.state is not None else now,
            store_error=reason,
            error_since=old.error_since if old.store_error else now,
            partial=old.partial,
            _memo=old._memo,  # the same state and months: what was worked out from them still holds
        )

    # ---- poll times and the guard ---------------------------------------------------------------------------

    def polls(self, day: str, need: int | None = None, *, strict: bool = False, budget: bool = True) -> PollsDoc | None:
        """The poll times and failed slots of a UTC day ("YYYY-MM-DD").

        `need` is the last poll of the snapshot being answered: a copy read before that poll was recorded is read
        again, so the copy holds every poll of the day up to it. A copy is otherwise kept for state_ttl, or for
        DAY_TTL once read after the day settled. One read per day at a time. With `budget`, settled days are read
        within the budget of reads (HistoryBusy when it is spent and nothing is kept); callers whose days are few
        and bounded anyway (sync answers: the last 35 days) pass False, so a busy budget never stops them.

        When the store fails the last copy is served, unless `strict` and it does not hold `need`. Raises
        StoreUnavailable when there is nothing to serve."""
        end = day_start(day) + DAY
        hit = self._poll_hit(day, need, end)
        if hit is not None:
            return hit.doc
        with self._day_locks[hash(day) % _DAY_LOCKS]:  # one read of a day at a time; the others wait and reuse it
            hit = self._poll_hit(day, need, end)
            if hit is not None:
                return hit.doc
            with self._lock:
                old = self._polls.get(day)
            usable = old is not None and (not strict or old.covers(need, end))  # what to serve if no read is made
            settled = self._clock() >= end + SETTLE
            if settled and budget and not self._take_day_read():
                if old is not None and usable:  # a settled copy is complete anyway: only imports could add to it
                    return old.doc
                raise HistoryBusy("too many days of poll times were read just now")
            known = [t for t in (need, self._snap.last_poll) if t is not None]
            through = max(known) if known else None  # both recorded before the read begins
            try:
                doc = self.store().get_polls(day)
            except Exception as exc:
                if old is not None and usable:
                    log.warning("polls read failed (%s); serving the last good copy", type(exc).__name__)
                    return old.doc
                raise StoreUnavailable("the store could not be read") from exc
            with self._lock:
                self._polls[day] = _PollDay(doc, self._mono(), through, settled)
                self._polls.move_to_end(day)
                while len(self._polls) > POLL_DAYS_MAX:
                    self._polls.popitem(last=False)
            return doc

    def _poll_hit(self, day: str, need: int | None, end: int) -> _PollDay | None:
        with self._lock:
            hit = self._polls.get(day)
            if hit is None:
                return None
            ttl = DAY_TTL if hit.settled else self.state_ttl
            if self._mono() - hit.at >= ttl or not hit.covers(need, end):
                return None
            self._polls.move_to_end(day)
            return hit

    def _take_day_read(self) -> bool:
        with self._lock:
            return self._day_reads.take(1, self._mono())

    def poll_days(self, a: int, b: int) -> list[str]:
        """The UTC days overlapping [a, b)."""
        days, t = [], a - a % DAY
        while t < b:
            days.append(utc_day(t))
            t += DAY
        return days

    def guard(self) -> dict[str, Any] | None:
        """The guard document, kept for GUARD_TTL seconds; None if missing or unreadable."""
        with self._lock:
            hit = self._guard
            if hit is not None and self._mono() - hit.at < GUARD_TTL:
                return hit.value
        try:
            doc = self.store().get_guard()
        except Exception as exc:
            log.warning("guard read failed (%s)", type(exc).__name__)
            return hit.value if hit is not None else None
        with self._lock:
            self._guard = _Cached(doc, self._mono())
        return doc
