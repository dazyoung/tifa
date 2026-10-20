"""Three London days of TfL line status, polled once a minute, for the integration tests.

The days are Saturday 24 to Monday 26 October 2026 in London. Sunday 25 October is 25 hours long: the clocks go back
at 01:00 UTC, when 02:00 BST becomes 01:00 GMT, so 01:00-02:00 happens twice. Cloud Scheduler runs in UTC, so the
poller's minute slots carry on one a minute through it.

- SPELLS is the "truth": which line statuses TfL reports, and when (London wall times, converted to epochs).
- payload(t) is TfL's answer at t in TfL's own shape: a real answer (tests/fixtures/core/tfl_status_sample.json)
  with every line's statuses replaced by those in effect at t, or Good Service.
- EVENTS says what goes wrong in some slots (TfL failures, bad payloads, a retry within the minute); DUPLICATES and
  LATE are Scheduler deliveries of a slot that is already recorded.
- expected() works out from the truth and the successful poll times alone what Tifa should have recorded: the
  episodes (the GAP rule included), the watched runs and the open episodes at the end. It never calls tifa.core.

Reasons are written the way TfL writes them ("Central Line: ..." with a trailing space); `text` is what is left once
the line's name is taken off the front, which is what Tifa stores.
"""

from __future__ import annotations

import copy
import datetime as dt
import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tifa.core.episodes import GAP
from tifa.core.tfl import SEVERITY_NAMES
from tifa.core.timeutil import LONDON
from tifa.poller.tfl_client import FetchResult, TflError

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "core" / "tfl_status_sample.json"
MIN, HOUR, DAY = 60, 3600, 86400


def ldn(day: int, hour: int, minute: int = 0, *, fold: int = 0, month: int = 10) -> int:
    """A London wall time in 2026 as epoch seconds; fold=1 is the second 01:xx on 25 October (GMT)."""
    return int(dt.datetime(2026, month, day, hour, minute, tzinfo=LONDON, fold=fold).timestamp())


START = ldn(24, 0)  # 23 October 23:00 UTC: the first slot
END = ldn(27, 0)  # 27 October 00:00 UTC: the slot after the last
DAYS = ("2026-10-24", "2026-10-25", "2026-10-26")
CLOCKS_BACK = ldn(25, 1, fold=1)  # 01:00 UTC on 25 October: 02:00 BST becomes 01:00 GMT
NOW = END + 20  # the API's clock once the three days are recorded


def jitter(slot: int) -> int:
    """Seconds after its minute that a slot's poll happens (Scheduler fires on the minute; the poll takes a few)."""
    return 1 + slot % 7


@dataclass(frozen=True)
class Spell:
    """A status TfL reports on a line for start <= t < end, and what Tifa should make of it."""

    line: str
    severity: int
    start: int
    end: int
    text: str | None  # the reason without TfL's "<line> Line: " prefix; None when TfL gives no reason
    planned: bool = False
    kind: str = "RealTime"  # TfL's disruption category: RealTime, PlannedWork or Information
    cause: str | None = None  # the cause code Tifa should give it (None: unknown)
    category: str = "delays"
    is_now: bool = True  # False: listed with a validity period that is not now (works planned for later)

    @property
    def key(self) -> tuple[str, int, bool]:
        return self.line, self.severity, self.planned

    def on(self, t: int) -> bool:
        return self.start <= t < self.end


SPELLS: tuple[Spell, ...] = (
    # weekend engineering works, under way before the first poll; District's run through the clock change
    Spell("district", 5, START - 6 * HOUR, ldn(26, 4, 30),
          "No service between Earl's Court and Wimbledon due to planned engineering work. Replacement buses operate.",
          planned=True, kind="PlannedWork", cause="engineering", category="closure"),
    Spell("mildmay", 5, START - 6 * HOUR, ldn(25, 0, 30),
          "No service between Stratford and Richmond due to planned works.",
          planned=True, kind="PlannedWork", cause="engineering", category="closure"),
    # Saturday: one episode whose reason TfL rewrites while it lasts
    Spell("central", 9, ldn(24, 7, 41), ldn(24, 8, 10),
          "Minor delays due to an earlier signal failure at Liverpool Street. Tickets are being accepted on London "
          "Buses.", cause="signal"),
    Spell("central", 9, ldn(24, 8, 10), ldn(24, 8, 57),
          "Minor delays while we fix a signal failure at Liverpool Street.", cause="signal"),
    # two statuses at once on one line: planned works and an unplanned delay
    Spell("metropolitan", 5, ldn(24, 10), ldn(24, 13),
          "No service between Harrow-on-the-Hill and Amersham due to planned engineering work.",
          planned=True, kind="PlannedWork", cause="engineering", category="closure"),
    Spell("metropolitan", 9, ldn(24, 11), ldn(24, 11, 30),
          "Minor delays due to a person ill on a train at Baker Street.", cause="customer"),
    # severe delays through a five-minute TfL outage (17:30-17:34), then minor delays from the same poll on
    Spell("northern", 6, ldn(24, 17, 5), ldn(24, 18, 20),
          "Severe delays between Camden Town and Morden due to a faulty train at Kennington.", cause="train"),
    Spell("northern", 9, ldn(24, 18, 20), ldn(24, 18, 47),
          "Minor delays while we recover from an earlier faulty train.", cause="train"),
    # Sunday night: across the clock change. 00:50 BST to 01:40 GMT is 110 minutes; 01:20 BST to 01:10 GMT is 50
    Spell("jubilee", 6, ldn(25, 0, 50), ldn(25, 1, 40, fold=1),
          "Severe delays due to a customer incident at Stratford.", cause="customer"),
    Spell("hammersmith-city", 9, ldn(25, 1, 20), ldn(25, 1, 10, fold=1),
          "Minor delays due to an obstruction on the track at Paddington.", cause="obstruction"),
    # closed on Sundays: Service Closed is never an episode
    Spell("waterloo-city", 20, ldn(25, 0), ldn(26, 6), "Train service will resume at 06:00 on Monday.",
          kind="RealTime", category="closure"),
    Spell("elizabeth", 9, ldn(25, 9, 12), ldn(25, 10, 3),
          "Minor delays between Paddington and Reading due to a points failure at Slough.", cause="track"),
    # information only: recorded, never counted as disruption; split by the twenty-minute outage at 15:10
    Spell("piccadilly", 19, ldn(25, 12), ldn(26, 12), "No step-free access at Cockfosters due to a faulty lift.",
          kind="Information", cause="equipment", category="info"),
    # Monday
    Spell("dlr", 7, ldn(26, 6, 30), ldn(26, 7, 20), "Reduced service due to a shortage of staff.", cause="staff"),
    Spell("victoria", 2, ldn(26, 8, 15), ldn(26, 8, 44),
          "Suspended due to a fire alert at Oxford Circus. Tickets are being accepted on London Buses.",
          cause="fire", category="suspended"),
    Spell("liberty", 9, ldn(26, 16, 40), ldn(26, 17, 25), None, cause=None),  # no reason given: cause unknown
    Spell("metropolitan", 4, ldn(26, 20), ldn(26, 22),
          "No service between Harrow-on-the-Hill and Aldgate on Saturday 31 October due to planned engineering work.",
          planned=True, kind="PlannedWork", cause="engineering", category="closure", is_now=False),
    # still going on at the end
    Spell("bakerloo", 9, ldn(26, 23, 30), END + 3 * HOUR,
          "Minor delays due to an earlier signal failure at Queen's Park.", cause="signal"),
    Spell("elizabeth", 4, ldn(26, 23, 45), END + 5 * HOUR,
          "No service between Paddington and Abbey Wood due to planned engineering work.",
          planned=True, kind="PlannedWork", cause="engineering", category="closure"),
)  # fmt: skip

SHORT_OUTAGE = [ldn(24, 17, 30) // 60 + i for i in range(5)]  # TfL answers 503: under GAP, nothing breaks
LONG_OUTAGE = [ldn(25, 15, 10) // 60 + i for i in range(20)]  # TfL times out for 20 minutes: over GAP
MISSING_LINES = ldn(24, 12) // 60  # TfL's answer lacks most lines; Scheduler's retry 25 s later is good
NOT_LIST = ldn(26, 3) // 60  # TfL answers with an error object
EVENTS: dict[int, str] = {
    **{s: "http_503" for s in SHORT_OUTAGE},
    **{s: "timeout" for s in LONG_OUTAGE},
    MISSING_LINES: "missing_lines",
    NOT_LIST: "not_list",
}
RETRY_AT = 25  # seconds into the minute of a retry within the slot
# Scheduler delivers a recorded slot again (at least once delivery), at and after the clock change
DUPLICATES = frozenset({ldn(25, 1, 59) // 60, CLOCKS_BACK // 60, ldn(25, 1, 30, fold=1) // 60})
LATE = frozenset({ldn(26, 9) // 60})  # an old attempt for this slot arrives after the next slot is recorded
SLOTS = range(START // 60, END // 60)


# ---------------------------------------------------------------- the end of October
# Six hours from 21:00 UTC on Saturday 31 October to 03:00 UTC on Sunday 1 November (London is on GMT, so London and
# UTC agree): the night a UTC month ends, for what crosses it. TfL answers 503 from 23:58 to 00:02, under GAP, so
# the watched run carries on across midnight and the poll at 00:03 extends it from 23:57, in both months at once.

MONTH_END = ldn(1, 0, month=11)  # 00:00 UTC on 1 November
ME_START, ME_END = ldn(31, 21), ldn(1, 3, month=11)
ME_NOW = ME_END + 20
ME_SPELLS: tuple[Spell, ...] = (
    # planned works under way all night, still on at the end: open, so in both months
    Spell("district", 5, ME_START - 3 * HOUR, ME_END + 6 * HOUR,
          "No service between Earl's Court and Richmond due to planned engineering work.",
          planned=True, kind="PlannedWork", cause="engineering", category="closure"),
    Spell("northern", 6, ldn(31, 22), ldn(31, 22, 45), "Severe delays due to a faulty train at Euston.",
          cause="train"),  # October only
    # starts in October, through the outage at midnight, ends in November: in both months, closed
    Spell("central", 9, ldn(31, 23, 20), ldn(1, 0, 40, month=11),
          "Minor delays due to an earlier signal failure at Holborn.", cause="signal"),
    Spell("victoria", 9, ldn(1, 1, month=11), ldn(1, 1, 30, month=11),
          "Minor delays due to a customer incident at Brixton.", cause="customer"),  # November only
)  # fmt: skip
ME_SLOTS = range(ME_START // 60, ME_END // 60)
ME_OUTAGE = [MONTH_END // 60 - 2 + i for i in range(5)]  # 23:58 to 00:02: TfL answers 503
ME_EVENTS: dict[int, str] = dict.fromkeys(ME_OUTAGE, "http_503")


def month_end_attempts() -> list[Attempt]:
    """Every delivery of the month-end night, in order."""
    return attempts(ME_SLOTS, ME_EVENTS, frozenset(), frozenset())


# ---------------------------------------------------------------- TfL's answers

_SAMPLE = json.loads(FIXTURE.read_text(encoding="utf-8"))
LINES = [(x["id"], x["name"], x["modeName"]) for x in _SAMPLE]
NAMES = {lid: name for lid, name, _ in LINES}
MODES = {lid: mode for lid, _, mode in LINES}
_GOOD = copy.deepcopy(_SAMPLE[0]["lineStatuses"][0])  # a real Good Service entry
_STATUS_TYPE = "Tfl.Api.Presentation.Entities.LineStatus, Tfl.Api.Presentation.Entities"
_PERIOD_TYPE = "Tfl.Api.Presentation.Entities.ValidityPeriod, Tfl.Api.Presentation.Entities"
_DISRUPTION_TYPE = "Tfl.Api.Presentation.Entities.Disruption, Tfl.Api.Presentation.Entities"


def _tfl_time(ts: int) -> str:
    return dt.datetime.fromtimestamp(ts, dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def prefix(line: str) -> str:
    """How TfL starts a line's reasons."""
    name = NAMES[line]
    if MODES[line] == "tube":
        return f"{name} Line: "
    if line == "dlr":
        return "DLR: "
    return f"{name} line: " if line == "elizabeth" else f"{name.upper()} LINE: "


def status_entry(spell: Spell) -> dict[str, Any]:
    reason = f"{prefix(spell.line)}{spell.text} " if spell.text else None
    period_from, period_to = (spell.start, spell.end) if spell.is_now else (ldn(31, 0), ldn(2, 0, month=11))
    entry: dict[str, Any] = {
        "$type": _STATUS_TYPE,
        "id": 0,
        "lineId": spell.line,
        "statusSeverity": spell.severity,
        "statusSeverityDescription": SEVERITY_NAMES[spell.severity],
        "created": "0001-01-01T00:00:00",
        "validityPeriods": [
            {
                "$type": _PERIOD_TYPE,
                "fromDate": _tfl_time(period_from),
                "toDate": _tfl_time(period_to),
                "isNow": spell.is_now,
            }
        ],
    }
    if reason is not None:
        entry["reason"] = reason
        entry["disruption"] = {
            "$type": _DISRUPTION_TYPE,
            "category": spell.kind,
            "categoryDescription": spell.kind,
            "description": reason,
            "affectedRoutes": [],
            "affectedStops": [],
        }
    return entry


def in_payload(t: int, spells: Iterable[Spell] = SPELLS) -> list[Spell]:
    """The spells listed in TfL's answer at t: those in effect, and works planned for later while announced."""
    return [s for s in spells if s.on(t)]


def payload(t: int, spells: Iterable[Spell] = SPELLS) -> list[dict[str, Any]]:
    """TfL's line-status answer at t."""
    out = []
    for line in _SAMPLE:
        item = copy.deepcopy(line)
        statuses = [status_entry(s) for s in in_payload(t, spells) if s.line == line["id"]]
        item["lineStatuses"] = statuses or [copy.deepcopy(_GOOD)]
        out.append(item)
    return out


def bad_payload(code: str, t: int, spells: Iterable[Spell] = SPELLS) -> Any:
    if code == "missing_lines":
        return payload(t, spells)[:3]
    if code == "not_list":
        return {"$type": "Tfl.Api.Presentation.Entities.ApiError", "httpStatusCode": 500, "message": "Internal error"}
    raise ValueError(code)


class FakeTfl:
    """A TfL client answering for the time it is told (`at`) and, through `fail`, failing as told."""

    def __init__(self, spells: Iterable[Spell] = SPELLS) -> None:
        self.spells = tuple(spells)
        self.at: int | None = None
        self.fail: str | None = None  # a TflError code, or a validate_payload() code for a bad payload
        self.fetches = 0

    def fetch_status(self) -> FetchResult:
        assert self.at is not None
        self.fetches += 1
        if self.fail in ("http_503", "timeout"):
            raise TflError(self.fail, f"TfL failed ({self.fail})", status=503 if self.fail == "http_503" else None)
        raw = bad_payload(self.fail, self.at, self.spells) if self.fail else payload(self.at, self.spells)
        return FetchResult(raw=raw, auth="ok", status=200, size=len(json.dumps(raw)), elapsed_ms=180)


# ---------------------------------------------------------------- the schedule


@dataclass(frozen=True)
class Attempt:
    """One delivery of a slot to the poller: when, and what should come of it."""

    slot: int
    at: int
    fail: str | None  # what TfL does: None for a good answer
    expect: str  # "applied", "failed" or "duplicate"


def attempts(
    slots: Iterable[int] = SLOTS,
    events: dict[int, str] = EVENTS,
    duplicates: frozenset[int] = DUPLICATES,
    late: frozenset[int] = LATE,
) -> list[Attempt]:
    """Every delivery of every slot, in order (of the three days unless told otherwise)."""
    out: list[Attempt] = []
    for slot in slots:
        at = slot * 60 + jitter(slot)
        event = events.get(slot)
        if event is None:
            out.append(Attempt(slot, at, None, "applied"))
        elif event == "missing_lines":
            out.append(Attempt(slot, at, event, "failed"))
            out.append(Attempt(slot, slot * 60 + RETRY_AT, None, "applied"))
        else:
            out.append(Attempt(slot, at, event, "failed"))
        if slot in duplicates:
            out.append(Attempt(slot, at + 3, None, "duplicate"))
        if slot - 1 in late:
            out.append(Attempt(slot - 1, at + 5, None, "duplicate"))
    return out


def poll_times(schedule: Iterable[Attempt] | None = None) -> list[int]:
    """The times of the polls that record (of the three days unless given a schedule)."""
    return [a.at for a in (attempts() if schedule is None else schedule) if a.expect == "applied"]


def failed_records() -> list[dict[str, Any]]:
    return [{"slot": a.slot, "code": a.fail} for a in attempts() if a.expect == "failed"]


# ---------------------------------------------------------------- what Tifa should record


@dataclass
class Expected:
    closed: list[dict[str, Any]]  # in the order they closed
    open: list[dict[str, Any]]
    runs: list[list[int]]
    changed: list[int] = field(default_factory=list)  # polls at which an episode opened or closed


def expected(polls: Iterable[int], spells: Iterable[Spell] = SPELLS) -> Expected:
    """Episodes from the truth sampled at the polls, by the rules Tifa documents: an episode starts at the first poll
    that saw its (line, severity, planned) in effect and ends at the first that did not; polls more than GAP apart
    end what was open at the last poll, with end_estimated; Service Closed is never recorded; the reason is the
    latest one TfL gave. The watched runs are the runs of polls no more than GAP apart."""
    spells = [s for s in spells if s.is_now and s.severity != 20]
    closed: list[dict[str, Any]] = []
    opened: dict[tuple, dict[str, Any]] = {}
    runs: list[list[int]] = []
    changed: list[int] = []
    prev = None
    for t in polls:
        before = len(closed), set(opened)
        if prev is not None and t - prev > GAP:
            closed += [dict(ep, end=prev, end_estimated=True) for ep in opened.values()]
            opened = {}
        if runs and t - runs[-1][1] <= GAP:
            runs[-1][1] = t
        else:
            runs.append([t, t])
        now = {}
        for s in spells:
            if s.on(t):
                now.setdefault(s.key, s)
        for key in [k for k in opened if k not in now]:
            closed.append(dict(opened.pop(key), end=t, end_estimated=False))
        for key, s in now.items():
            ep = opened.get(key)
            if ep is None:
                ep = opened[key] = {
                    "line": s.line,
                    "severity": s.severity,
                    "planned": s.planned,
                    "start": t,
                    "status": SEVERITY_NAMES[s.severity],
                    "category": s.category,
                    "cause": s.cause,
                    "reason": None,
                }
            if s.text:
                ep["reason"] = s.text
        if (len(closed), set(opened)) != before:
            changed.append(t)
        prev = t
    return Expected(
        closed=closed, open=sorted(opened.values(), key=lambda e: (e["start"], e["line"])), runs=runs, changed=changed
    )


def month_row(ep: dict[str, Any]) -> list:
    """An expected episode as a month payload row (end None while open)."""
    return [
        ep["line"],
        ep["start"],
        ep.get("end"),
        ep["severity"],
        ep["category"],
        int(ep["planned"]),
        "a",
        ep["cause"],
        ep["status"],
        ep["reason"],
        int(bool(ep.get("end_estimated"))),
    ]


def row_order(row: list) -> tuple:
    return row[1], row[0], row[6], row[3], row[5], row[2] is None, row[2] or 0
