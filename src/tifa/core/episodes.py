"""Disruption episodes from successive polls of TfL's line status.

TfL keeps no history, so each good poll records what is in effect now. An observation is a lineStatuses entry in
effect now (a validity period says isNow, or it has none), except Good Service, No Issues and Service Closed. An
episode is a run of polls that saw the same (line, severity, planned), planned meaning that disruption.category is
PlannedWork: it starts at the first poll that saw it and ends at the first that did not. Polls more than GAP apart
end what was open at the last poll, marked end_estimated, because nobody was watching in between.
"""

import math
from collections.abc import Iterable, Mapping
from typing import TypedDict

from .tfl import CATEGORY, CATEGORY_IDS, NOT_RECORDED, clean_reason

GAP = 900  # seconds: polls further apart than this break continuity
REASON_MAX = 400  # characters kept of an observation's reasons

Key = tuple[str, int, bool]  # (line, severity, planned)


class OpenEpisode(TypedDict):
    line: str
    name: str
    mode: str | None
    severity: int
    status: str
    category: str
    planned: bool
    start: int
    reason: str | None


class ClosedEpisode(OpenEpisode):
    end: int
    end_estimated: bool


OPEN_FIELDS = ("line", "name", "mode", "severity", "status", "category", "planned", "start", "reason")


def key_of(ep: Mapping) -> Key:
    return ep["line"], ep["severity"], ep["planned"]


def _open_order(ep: Mapping) -> tuple:
    return ep["start"], ep["line"], ep["severity"], ep["planned"]


def in_effect(status: dict) -> bool:
    """In effect now: a validity period says isNow, or there are none. Planned works for later are not."""
    periods = status.get("validityPeriods")
    if not periods:
        return True
    return isinstance(periods, list) and any(isinstance(p, dict) and p.get("isNow") is True for p in periods)


def observe(raw: list) -> tuple[dict[Key, dict], dict[str, tuple[str, str | None]]]:
    """TfL's lines -> ({(line, severity, planned): {"status", "reason"}}, {line: (name, mode)}).

    meta lists every line, disrupted or not. Two entries with the same key on one line (a Part Closure on two
    stretches) are one observation, with their distinct reasons joined and capped at REASON_MAX characters."""
    obs, meta = {}, {}
    for line in raw:
        if not isinstance(line, dict):
            continue
        line_id = line.get("id")
        name = line.get("name") or line_id
        if not isinstance(line_id, str) or not line_id or not isinstance(name, str):
            continue
        mode = line.get("modeName")
        meta[line_id] = (name, mode if isinstance(mode, str) else None)
        statuses = line.get("lineStatuses")
        for s in statuses if isinstance(statuses, list) else ():
            if not isinstance(s, dict):
                continue
            sev = s.get("statusSeverity")
            if not isinstance(sev, int) or isinstance(sev, bool) or sev in NOT_RECORDED or not in_effect(s):
                continue
            disruption = s.get("disruption")
            planned = isinstance(disruption, dict) and disruption.get("category") == "PlannedWork"
            o = obs.get((line_id, sev, planned))
            if o is None:
                desc = " ".join(str(s.get("statusSeverityDescription") or "").split())
                o = obs[(line_id, sev, planned)] = {"status": desc or f"Severity {sev}", "reasons": []}
            text = clean_reason(s["reason"], name) if s.get("reason") else ""
            if text and text.casefold() not in {r.casefold() for r in o["reasons"]}:
                o["reasons"].append(text)
    for o in obs.values():
        text = " ".join(o.pop("reasons"))
        o["reason"] = (text if len(text) <= REASON_MAX else text[: REASON_MAX - 1].rstrip() + "…") or None
    return obs, meta


def _closed(ep: Mapping, end: int, estimated: bool) -> ClosedEpisode:
    return {
        "line": ep["line"],
        "name": ep["name"],
        "mode": ep["mode"],
        "severity": ep["severity"],
        "status": ep["status"],
        "category": ep["category"],
        "planned": ep["planned"],
        "start": ep["start"],
        "end": end,
        "end_estimated": estimated,
        "reason": ep["reason"],
    }


def empty_state() -> dict:
    return {"last_poll": None, "open": []}


def _int(value) -> int | None:
    """An epoch as an int, or None for anything that is not a finite number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return int(value)


def clean_open(entries) -> tuple[list[OpenEpisode], int]:
    """Stored open episodes made safe to advance: (episodes with every OPEN_FIELDS field, how many were dropped).

    An entry needs a line id, an integer severity, planned (a bool, or 0 or 1) and a start time, or it is dropped:
    nothing can be recorded for it. Other fields that are missing or of the wrong type get what a new episode would
    have (the line id as name, no mode or reason, status "Severity N", the severity's category), and the next poll
    that sees the episode updates them. So one damaged entry never stops recording."""
    kept: list[OpenEpisode] = []
    dropped = 0
    for ep in entries if isinstance(entries, (list, tuple)) else ():
        if not isinstance(ep, Mapping):
            dropped += 1
            continue
        line, sev, planned, start = ep.get("line"), ep.get("severity"), ep.get("planned"), _int(ep.get("start"))
        sev_ok = isinstance(sev, int) and not isinstance(sev, bool)
        # planned may be stored as 0 or 1, which compare equal to False and True
        if not (isinstance(line, str) and line and sev_ok and planned in (True, False) and start is not None):
            dropped += 1
            continue
        name, mode, status, cat, reason = (ep.get(k) for k in ("name", "mode", "status", "category", "reason"))
        kept.append(
            {
                "line": line,
                "name": name if isinstance(name, str) and name else line,
                "mode": mode if isinstance(mode, str) else None,
                "severity": sev,
                "status": status if isinstance(status, str) else f"Severity {sev}",
                "category": cat if cat in CATEGORY_IDS else CATEGORY(sev),
                "planned": bool(planned),
                "start": start,
                "reason": reason if isinstance(reason, str) else None,
            }
        )
    return kept, dropped


def advance(
    state: Mapping | None, obs: Mapping[Key, Mapping], meta: Mapping[str, Iterable], now_ts: int
) -> tuple[dict, list[ClosedEpisode], list[OpenEpisode]]:
    """Move the open episodes on to a poll at now_ts: (new state, episodes that closed, episodes that opened).

    state is {"last_poll": epoch or None, "open": [OpenEpisode]} (other keys are kept); it is not changed. If
    now_ts is over GAP after the last poll, every open episode first closes at that poll with end_estimated. Then
    an open key missing from obs closes at now_ts, a new key opens at now_ts, and open episodes take the latest
    name, mode, status and reason (a reason is kept while TfL gives none). A poll not after the last one records
    nothing. Stored open episodes pass through clean_open() first, so a damaged entry is dropped or repaired rather
    than stopping every later poll. Closed episodes come by (end, start, line, severity), open ones by (start, line,
    severity, planned)."""
    state = state or {}
    last = _int(state.get("last_poll"))
    if last is not None and now_ts <= last:
        return dict(state), [], []
    opened: dict[Key, dict] = {}
    for ep in sorted(clean_open(state.get("open"))[0], key=_open_order):
        opened[key_of(ep)] = ep
    closed: list[ClosedEpisode] = []
    if last is not None and now_ts - last > GAP:  # not watching in between: end what was open when last seen
        closed += [_closed(ep, last, True) for ep in opened.values()]
        opened = {}
    for key in [k for k in opened if k not in obs]:
        closed.append(_closed(opened.pop(key), now_ts, False))
    new = []
    for key, o in obs.items():
        ep = opened.get(key)
        if ep is None:
            ep = opened[key] = {
                "line": key[0],
                "name": key[0],
                "mode": None,
                "severity": key[1],
                "status": "",
                "category": CATEGORY(key[1]),
                "planned": key[2],
                "start": now_ts,
                "reason": None,
            }
            new.append(ep)
        ep["name"], ep["mode"] = meta[key[0]]
        ep["status"] = o["status"]
        if o["reason"]:
            ep["reason"] = o["reason"]
    closed.sort(key=lambda r: (r["end"], r["start"], r["line"], r["severity"]))
    out = dict(state, last_poll=now_ts, open=sorted(opened.values(), key=_open_order))
    return out, closed, [dict(ep) for ep in sorted(new, key=_open_order)]
