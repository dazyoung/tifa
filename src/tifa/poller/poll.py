"""One poll of TfL, recorded in one store transaction (contracts: "Poller").

    result = run_poll(store, client, slot, now_ts)

1. A slot already recorded (or a poll not after the last one) is a no-op: TfL is not even asked.
2. TfL is fetched outside the transaction and checked with `validate_payload()` against the lines seen in the last
   day. A failed fetch or a bad payload records a failed slot (`polls/{day}.failed`) and leaves the state alone;
   the result asks for HTTP 502 so Cloud Scheduler retries within the minute.
3. A good payload goes through `observe()` outside the transaction, then, inside it, `advance()` against the
   transaction's own state. The months touched are those of the episodes this poll closed and of the watched time
   it added (`run_segment()`, so a poll touches at most two months however long the run is). Each is merged with
   `merge_month()` into the stored payload and upserted only if it changed.
4. The new state carries `current()` and its ETag, the lines seen, the run and the TfL auth state. A snapshot
   (the parts of TfL's answer that `observe()` and `current()` read) is written only when the status changed or
   an episode opened or closed, so history can be recomputed from snapshots alone.

The transaction function has no side effects and may run more than once (Firestore retries it on contention). Month
payloads are read before the transaction and re-read inside it only when the state's revision index says they
changed in between.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from tifa.core.causes import CAUSES_SIG
from tifa.core.episodes import advance, observe
from tifa.core.months import SOURCE_LIVE, extend_run, merge_month, new_month, run_segment, split_months
from tifa.core.tfl import PayloadError, current, recent_line_ids, validate_payload
from tifa.core.timeutil import iso
from tifa.poller.tfl_client import FetchResult, TflError
from tifa.store import codec
from tifa.store.base import APPLIED, DUPLICATE, SCHEMA, MonthDoc, PollWrite, State, Store, is_duplicate

log = logging.getLogger("tifa.poller")

FAILED = "failed"  # with APPLIED and DUPLICATE, the outcomes of a poll
SNAPSHOT_SCHEMA = 1
MONTH_ATTEMPTS = 3  # transactions retried when a month changed under the poll (an import running alongside)

# The parts of TfL's answer that observe() and current() read: what a snapshot keeps.
_LINE_FIELDS = ("id", "name", "modeName")
_STATUS_FIELDS = ("statusSeverity", "statusSeverityDescription", "reason")
_PERIOD_FIELDS = ("fromDate", "toDate", "isNow")


class Fetcher(Protocol):
    def fetch_status(self) -> FetchResult: ...


class MonthConflict(RuntimeError):
    """A month's stored revision does not match the state's index: retry the transaction."""


@dataclass(frozen=True)
class PollResult:
    outcome: str  # "applied" | "duplicate" | "failed"
    slot: int
    poll_at: int | None = None
    code: str | None = None  # why a slot failed: a TflError code, a validate_payload() code, "store" or "late"
    reason: str | None = None  # "recorded" for duplicates of a recorded slot, "late" for a poll not after the last
    tfl_auth: str | None = None
    open: int = 0
    opened: int = 0
    closed: int = 0
    months: tuple[str, ...] = ()
    snapshot: bool = False
    fetch_ms: int | None = None
    lines: int = 0

    @property
    def ok(self) -> bool:
        return self.outcome != FAILED

    @property
    def http_status(self) -> int:
        """200 for applied and duplicate slots; 502 for a failed TfL fetch or payload, 503 for a store failure."""
        if self.outcome != FAILED:
            return 200
        return 503 if self.code == "store" else 502

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"outcome": self.outcome, "slot": self.slot}
        if self.poll_at is not None:
            out["poll_at"] = iso(self.poll_at)
        for name in ("code", "reason", "tfl_auth", "fetch_ms"):
            value = getattr(self, name)
            if value is not None:
                out[name] = value
        if self.outcome == APPLIED:
            out |= {
                "open": self.open,
                "opened": self.opened,
                "closed": self.closed,
                "months": list(self.months),
                "snapshot": self.snapshot,
                "lines": self.lines,
            }
        return out


@dataclass
class _Prepared:
    """What a good fetch gives, before the transaction: pure inputs to the transaction function."""

    slot: int
    now_ts: int
    raw: list
    obs: dict
    meta: dict
    status: dict
    etag: str
    auth: str
    months: dict[str, MonthDoc | None] = field(default_factory=dict)  # read before (or during) the transaction


def run_poll(store: Store, client: Fetcher, slot: int, now_ts: int) -> PollResult:
    """Record one poll for minute `slot` made at `now_ts` (epoch seconds). Never raises for TfL or payload
    problems; store errors while applying are reported as outcome "failed" with code "store"."""
    try:
        before = store.get_state()
    except Exception:
        log.exception("poll slot %d: could not read the state", slot)
        return PollResult(FAILED, slot, code="store")
    if is_duplicate(before, slot):
        return PollResult(DUPLICATE, slot, reason="recorded")
    if before is not None and before.last_poll is not None and now_ts <= before.last_poll:
        return PollResult(DUPLICATE, slot, reason="late")

    try:
        fetched = client.fetch_status()
    except TflError as exc:
        log.warning("poll slot %d: %s (%s)", slot, exc, exc.code)
        return _record_failure(store, slot, exc.code, auth=exc.auth)

    ok, code = validate_payload(fetched.raw, recent_line_ids(before.lines if before else {}, now_ts))
    if ok:
        try:
            status = current(fetched.raw)
            obs, meta = observe(fetched.raw)
        except PayloadError:  # validate_payload() should make this impossible
            ok, code = False, "bad_payload"
    if not ok:
        log.warning("poll slot %d: TfL's answer was refused (%s)", slot, code)
        return _record_failure(store, slot, code or "bad_payload", auth=fetched.auth, fetch_ms=fetched.elapsed_ms)

    prep = _Prepared(
        slot=slot,
        now_ts=now_ts,
        raw=fetched.raw,
        obs=obs,
        meta=meta,
        status=status,
        etag=codec.digest(status),
        auth=fetched.auth,
    )
    outcome: dict[str, Any] = {}
    for attempt in range(1, MONTH_ATTEMPTS + 1):
        try:
            _prefetch(store, prep, before)
            done = store.poll_transaction(slot, _transaction(store, prep, outcome))
            break
        except MonthConflict:
            log.info("poll slot %d: a month changed during the poll (attempt %d); retrying", slot, attempt)
            before = _safe_state(store) or before
        except Exception:
            log.exception("poll slot %d: the transaction failed", slot)
            return PollResult(FAILED, slot, code="store", tfl_auth=fetched.auth, fetch_ms=fetched.elapsed_ms)
    else:
        log.error("poll slot %d: months kept changing; giving up for this attempt", slot)
        return PollResult(FAILED, slot, code="store", tfl_auth=fetched.auth, fetch_ms=fetched.elapsed_ms)

    if done != APPLIED:
        reason = outcome.get("reason", "recorded")
        return PollResult(DUPLICATE, slot, reason=reason, tfl_auth=fetched.auth, fetch_ms=fetched.elapsed_ms)
    result = PollResult(
        APPLIED,
        slot,
        poll_at=now_ts,
        tfl_auth=fetched.auth,
        open=outcome["open"],
        opened=outcome["opened"],
        closed=outcome["closed"],
        months=tuple(outcome["months"]),
        snapshot=outcome["snapshot"],
        fetch_ms=fetched.elapsed_ms,
        lines=len(meta),
    )
    log.info(
        "poll slot %d applied: %d open, %d opened, %d closed, months %s, snapshot %s, tfl_auth %s, %d ms",
        slot,
        result.open,
        result.opened,
        result.closed,
        ",".join(result.months) or "-",
        "yes" if result.snapshot else "no",
        result.tfl_auth,
        fetched.elapsed_ms,
    )
    return result


def _safe_state(store: Store) -> State | None:
    try:
        return store.get_state()
    except Exception:
        return None


def _record_failure(store: Store, slot: int, code: str, *, auth: str | None, fetch_ms: int | None = None) -> PollResult:
    """Add {slot, code} to the day's failed slots, leaving the state alone (a retry of the slot can still apply)."""
    try:
        done = store.poll_transaction(slot, lambda state: PollWrite(failed={"slot": slot, "code": code}))
    except Exception:
        log.exception("poll slot %d: could not record the failed slot", slot)
    else:
        if done == DUPLICATE:  # another attempt recorded the slot meanwhile: nothing for Scheduler to retry
            return PollResult(DUPLICATE, slot, reason="recorded", tfl_auth=auth, fetch_ms=fetch_ms)
    return PollResult(FAILED, slot, code=code, tfl_auth=auth, fetch_ms=fetch_ms)


# ---------------------------------------------------------------- the transaction


def _transaction(store: Store, prep: _Prepared, outcome: dict[str, Any]) -> Callable[[State | None], PollWrite | None]:
    """The function poll_transaction runs. It reads only `state`, `prep` and month payloads, and writes only its
    return value and `outcome` (overwritten on every call, so a rerun leaves what the last call computed)."""

    def fn(state: State | None) -> PollWrite | None:
        outcome.clear()
        if is_duplicate(state, prep.slot):
            outcome["reason"] = "recorded"
            return None
        if state is not None and state.last_poll is not None and prep.now_ts <= state.last_poll:
            outcome["reason"] = "late"  # another slot recorded a later poll first (clocks a second or two apart)
            return None
        write, info = build_write(state, prep, lambda month, rev: _month(store, prep, month, rev))
        outcome.update(info)
        return write

    return fn


def build_write(
    state: State | None, prep: _Prepared, month_of: Callable[[str, int | None], dict[str, Any]]
) -> tuple[PollWrite, dict[str, Any]]:
    """The PollWrite for a good poll against `state`, and a summary. Pure apart from `month_of(month, rev)`, which
    returns the stored payload of a month at revision `rev` (None: not stored yet)."""
    now = prep.now_ts
    last_poll = state.last_poll if state else None
    episodes = {"last_poll": last_poll, "open": state.open if state else []}
    moved, closed, opened = advance(episodes, prep.obs, prep.meta, now)

    prev_run = state.run if state and state.run else None
    if prev_run is None and last_poll is not None:
        prev_run = [last_poll, last_poll]  # a state written without its run (an import) still links to this poll
    run = extend_run(prev_run, now)
    piece = run_segment(prev_run, run)

    adds: dict[str, tuple[list, list]] = {}
    if piece is not None:
        for month in split_months(piece[0], piece[1]):
            adds.setdefault(month, ([], []))[1].append(list(piece))
    for ep in closed:
        for month in split_months(ep["start"], ep["end"]):
            adds.setdefault(month, ([], []))[0].append(ep)

    index = state.months if state else {}
    upserts: dict[str, dict[str, Any]] = {}
    for month, (eps, spans) in sorted(adds.items()):
        rev = (index.get(month) or {}).get("rev")
        base = month_of(month, rev) if rev is not None else new_month(month)
        merged = merge_month(base, eps, spans, SOURCE_LIVE)
        if merged != base:
            upserts[month] = merged

    lines = {k: dict(v) for k, v in (state.lines if state else {}).items()}
    for line_id, (name, mode) in prep.meta.items():
        entry = lines.get(line_id) or {"first_seen": now}
        entry.update(name=name, mode=mode, last_seen=now)
        entry.setdefault("first_seen", now)
        lines[line_id] = entry

    changed = (state is None or state.status_etag != prep.etag) or bool(closed) or bool(opened)
    new_state = State(
        schema=SCHEMA,
        last_poll=now,
        last_slot=prep.slot,
        run=run,
        open=moved["open"],
        lines=lines,
        status=prep.status,
        status_etag=prep.etag,
        months=dict(index),  # the store replaces it with the stored index plus the months upserted
        causes_sig=(state.causes_sig if state and state.causes_sig else CAUSES_SIG),
        tfl_auth=prep.auth,
        updated_at=now,
    )
    write = PollWrite(
        state=new_state,
        months=upserts,
        poll_at=now,
        snapshot=(now, snapshot_payload(prep.raw)) if changed else None,
    )
    info = {
        "open": len(moved["open"]),
        "opened": len(opened),
        "closed": len(closed),
        "months": sorted(upserts),
        "snapshot": changed,
    }
    return write, info


def _prefetch(store: Store, prep: _Prepared, state: State | None) -> None:
    """Read the months a poll against `state` would touch, so the transaction usually reads none itself."""
    wanted: list[tuple[str, int]] = []

    def note(month: str, rev: int | None) -> dict[str, Any]:
        if rev is not None:
            wanted.append((month, rev))
        return new_month(month)

    try:
        build_write(state, prep, note)
    except Exception:  # a guess only: the transaction computes the real write
        return
    for month, rev in wanted:
        doc = prep.months.get(month)
        if doc is None or doc.rev != rev:
            prep.months[month] = store.get_month(month)


def _month(store: Store, prep: _Prepared, month: str, rev: int) -> dict[str, Any]:
    """The payload of `month` at revision `rev`: from the prefetch when it matches, else read again."""
    doc = prep.months.get(month)
    if doc is None or doc.rev != rev:
        doc = store.get_month(month)
        prep.months[month] = doc  # a cache of stored documents: correct on any later call too
    if doc is None or doc.rev != rev:
        raise MonthConflict(month)
    return doc.payload


# ---------------------------------------------------------------- snapshots


def snapshot_payload(raw: list) -> dict[str, Any]:
    """{"schema": 1, "raw": [...]}: each line with only the fields observe() and current() read."""
    return {"schema": SNAPSHOT_SCHEMA, "raw": [_slim_line(line) for line in raw if isinstance(line, Mapping)]}


def _slim_line(line: Mapping) -> dict[str, Any]:
    out = {k: line[k] for k in _LINE_FIELDS if k in line}
    statuses = []
    for s in line.get("lineStatuses") or ():
        if not isinstance(s, Mapping):
            continue
        slim = {k: s[k] for k in _STATUS_FIELDS if k in s}
        periods = s.get("validityPeriods")
        if isinstance(periods, list):
            slim["validityPeriods"] = [
                {k: p[k] for k in _PERIOD_FIELDS if k in p} for p in periods if isinstance(p, Mapping)
            ]
        disruption = s.get("disruption")
        if isinstance(disruption, Mapping) and "category" in disruption:
            slim["disruption"] = {"category": disruption["category"]}
        statuses.append(slim)
    out["lineStatuses"] = statuses
    return out
