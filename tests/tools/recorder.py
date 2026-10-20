"""A synthetic local recording for the tools' tests, in the format a local recorder writes.

A recorder samples a "truth" (disruptions as real-time intervals) at its own poll times, runs each poll through
tifa.core's observe() and advance(), and writes after each one: closed episodes appended to episodes.jsonl (ISO
times), the poll to polls.jsonl and the open episodes to open.json, plus tfl.json (current() wrapped with its fetch
time) after the last. Nothing here is real data: line ids and names are TfL's public ones, reasons are made up.
"""

from __future__ import annotations

import calendar
import json
from collections.abc import Iterable, Sequence
from pathlib import Path

from tifa.core.episodes import advance, observe
from tifa.core.tfl import current
from tifa.core.timeutil import iso

MIN, HOUR = 60, 3600
T0 = calendar.timegm((2026, 9, 30, 22, 0, 0))  # two hours before a UTC month ends

LINES = (
    ("bakerloo", "Bakerloo", "tube"),
    ("central", "Central", "tube"),
    ("district", "District", "tube"),
    ("jubilee", "Jubilee", "tube"),
    ("northern", "Northern", "tube"),
    ("victoria", "Victoria", "tube"),
    ("elizabeth", "Elizabeth line", "elizabeth-line"),
)

# (line, severity, description, reason, planned, from, to): in effect for from <= t < to
TRUTH = (
    ("central", 9, "Minor Delays", "Central Line: Minor delays due to an earlier signal failure at Bank.", False,
     T0 + 30 * MIN, T0 + 110 * MIN),
    ("northern", 6, "Severe Delays", "Northern Line: Severe delays due to a faulty train at Camden Town.", False,
     T0 + 90 * MIN, T0 + 160 * MIN),  # runs over midnight UTC into October
    ("district", 5, "Part Closure",
     "District Line: No service between Earl's Court and Wimbledon due to planned engineering work.", True,
     T0 - HOUR, T0 + 12 * HOUR),
    ("jubilee", 6, "Severe Delays", "Jubilee Line: Severe delays due to a customer incident at Stratford.", False,
     T0 + 280 * MIN, T0 + 12 * HOUR),
)  # fmt: skip


def status_entry(sev: int, desc: str, reason: str | None = None, planned: bool = False) -> dict:
    entry = {"statusSeverity": sev, "statusSeverityDescription": desc, "validityPeriods": []}
    if reason:
        entry["reason"] = reason
        period = {"fromDate": "2026-09-30T00:00:00Z", "toDate": "2026-10-31T00:00:00Z", "isNow": True}
        entry["validityPeriods"] = [period]
        entry["disruption"] = {"category": "PlannedWork" if planned else "RealTime", "description": reason}
    return entry


def payload(ts: int, truth: Iterable[Sequence] = TRUTH, lines: Iterable[Sequence] = LINES) -> list:
    """TfL's line-status response at ts for this truth."""
    out = []
    for lid, name, mode in lines:
        statuses = [
            status_entry(sev, desc, reason, planned)
            for line, sev, desc, reason, planned, a, b in truth
            if line == lid and a <= ts < b
        ]
        out.append(
            {
                "id": lid,
                "name": name,
                "modeName": mode,
                "lineStatuses": statuses or [status_entry(10, "Good Service")],
            }
        )
    return out


def schedule(start: int, end: int, every: int, skip: Iterable[tuple[int, int]] = ()) -> list[int]:
    """Poll times start, start + every, ... up to end, leaving out those inside any (a, b) of skip."""
    skip = list(skip)
    return [t for t in range(start, end + 1, every) if not any(a < t < b for a, b in skip)]


def episode_record(ep: dict) -> dict:
    return {
        "line": ep["line"],
        "name": ep["name"],
        "mode": ep["mode"],
        "severity": ep["severity"],
        "status": ep["status"],
        "category": ep["category"],
        "planned": ep["planned"],
        "start": iso(ep["start"]),
        "end": iso(ep["end"]),
        "end_estimated": ep["end_estimated"],
        "reason": ep["reason"],
    }


def record(directory: Path, polls: Iterable[int], truth: Iterable[Sequence] = TRUTH) -> dict:
    """Write a recording of truth sampled at polls into directory; returns the final core state."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    truth = list(truth)
    state: dict = {"last_poll": None, "open": []}
    raw = None
    with open(directory / "episodes.jsonl", "a") as eps, open(directory / "polls.jsonl", "a") as log:
        for ts in polls:
            raw = payload(ts, truth)
            obs, meta = observe(raw)
            state, closed, _ = advance(state, obs, meta, ts)
            for ep in closed:
                eps.write(json.dumps(episode_record(ep), separators=(",", ":")) + "\n")
            log.write(json.dumps({"at": iso(ts)}, separators=(",", ":")) + "\n")
            open_doc = {
                "version": 1,
                "last_poll": iso(ts),
                "open": [dict(e, start=iso(e["start"])) for e in state["open"]],
            }
            (directory / "open.json").write_text(json.dumps(open_doc, indent=1) + "\n")
    if raw is not None:
        last = iso(state["last_poll"])
        doc = {"ok": True, "fetched_at": last, "last_ok": last, "data": current(raw)}
        (directory / "tfl.json").write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n")
    return state


# the recording most tests use: every 2 minutes for four hours, a 30-minute gap, then half an hour more
LOCAL_POLLS = schedule(T0, T0 + 300 * MIN, 120, skip=[(T0 + 240 * MIN, T0 + 270 * MIN)])
LAST_POLL = LOCAL_POLLS[-1]
