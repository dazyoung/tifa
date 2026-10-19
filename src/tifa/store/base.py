"""The Store protocol, its data classes, and the document layout every backend shares.

Documents (paths are the same in every backend):

- `meta/state`: the poller's `State`, with `status` stored as gzip JSON.
- `months/{YYYY-MM}`: `{month, rev, payload: gzip JSON, bytes, sha256, updated_at}`.
- `polls/{YYYY-MM-DD}` (UTC day): `{at: [epochs], n, failed: [{slot, code}]}`, grown with array-union and increment.
- `snapshots/{epoch}`: `{hash, payload: gzip JSON}`.
- `guard/state` and `imports/{id}`: plain documents.

The helpers below turn writes into those documents, so the backends differ only in how they store them.
Every month write also rewrites `meta/state` (its `months` revision index) in the same transaction. A poll
transaction that reads `meta/state` therefore sees month revisions consistent with the month documents.
"""

from __future__ import annotations

import copy
import dataclasses
import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from tifa.store import codec

log = logging.getLogger("tifa.store")

SCHEMA = 1
APPLIED = "applied"
DUPLICATE = "duplicate"

MONTH_WARN_BYTES = 700_000  # a gzip month blob above this logs a warning
MONTH_MAX_BYTES = 1_040_000  # Firestore refuses documents over 1 MiB; fail early with a clear message

STATE_DOC = "meta/state"
GUARD_DOC = "guard/state"

_MONTH_RE = re.compile(r"\d{4}-(0[1-9]|1[0-2])")
_DAY_RE = re.compile(r"\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])")
_INT64 = 2**63
_MAX_DEPTH = 20  # Firestore's limit on nested maps and arrays


# ---------------------------------------------------------------- data classes


@dataclass
class State:
    """The poller's state (`meta/state`).

    `months` is the revision index of `months/{YYYY-MM}` (`{"rev": int, "n": episodes}`). The store maintains it:
    whatever a caller puts there is replaced by the stored index plus the months the same write upserts.
    """

    schema: int = SCHEMA
    last_poll: int | None = None
    last_slot: int | None = None
    run: list[int] | None = None
    open: list[dict[str, Any]] = field(default_factory=list)
    lines: dict[str, dict[str, Any]] = field(default_factory=dict)
    status: dict[str, Any] = field(default_factory=dict)
    status_etag: str = ""
    months: dict[str, dict[str, int]] = field(default_factory=dict)
    causes_sig: str = ""
    tfl_auth: str = "ok"
    updated_at: int = 0

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, doc: Mapping[str, Any]) -> State:
        """Build from a stored document; unknown fields are ignored and missing ones take their defaults."""
        names = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: copy.deepcopy(v) for k, v in doc.items() if k in names})


@dataclass(frozen=True)
class MonthDoc:
    month: str
    rev: int
    payload: dict[str, Any]
    sha256: str = ""  # of the canonical JSON, so equal content has an equal hash however it was compressed
    size: int = 0  # stored gzip bytes (the document's `bytes` field)


@dataclass(frozen=True)
class PollsDoc:
    day: str  # UTC "YYYY-MM-DD"
    at: list[int]  # poll times, ascending
    n: int  # polls recorded
    failed: list[dict[str, Any]]  # [{"slot": int, "code": str}]


@dataclass
class PollWrite:
    """What one `poll_transaction` commits, all or nothing.

    - `state`: the new state, or None to leave `meta/state` as it is (a failed slot). Set `last_slot = slot` for
      a successful poll; leave it for a failed one so a retry of the slot can still apply.
    - `months`: whole month payloads to store, each replacing the stored one and bumping its revision. Build
      them from `get_month()` and, inside `fn`, check `state.months[m]["rev"]` still matches the revision read.
    - `poll_at`: a poll time to add to `polls/{UTC day of poll_at}`.
    - `failed`: `{"slot": int, "code": str}`, added to `polls/{UTC day of slot * 60}`.
    - `snapshot`: `(epoch, payload)` for `snapshots/{epoch}`.
    """

    state: State | None = None
    months: dict[str, dict[str, Any]] = field(default_factory=dict)
    poll_at: int | None = None
    failed: dict[str, Any] | None = None
    snapshot: tuple[int, dict[str, Any]] | None = None


@runtime_checkable
class Store(Protocol):
    def get_state(self) -> State | None: ...

    def poll_transaction(self, slot: int, fn: Callable[[State | None], PollWrite | None]) -> str:
        """Run `fn` against the current state in one transaction and commit what it returns.

        Returns "duplicate" without calling `fn` when `state.last_slot >= slot`, and "duplicate" when `fn`
        returns None; otherwise "applied". `fn` gets its own copy of the state and must not have side effects:
        a backend may call it again when a concurrent transaction wins. An exception aborts with nothing written.
        """
        ...

    def get_month(self, month: str) -> MonthDoc | None: ...

    def list_months(self) -> dict[str, int]:
        """month -> rev, from the index in `meta/state`."""
        ...

    def put_month(self, month: str, payload: dict) -> int:
        """Store a month outside polls (imports); returns the new revision."""
        ...

    def get_polls(self, day: str) -> PollsDoc | None: ...

    def add_snapshot(self, ts: int, payload: dict) -> None: ...

    def get_guard(self) -> dict | None: ...

    def put_guard(self, doc: dict) -> None: ...

    def record_import(self, import_id: str, doc: dict) -> None: ...


# ---------------------------------------------------------------- validation


def check_month(month: str) -> str:
    if not isinstance(month, str) or not _MONTH_RE.fullmatch(month):
        raise ValueError(f"month must be YYYY-MM, got {month!r}")
    return month


def check_day(day: str) -> str:
    if not isinstance(day, str) or not _DAY_RE.fullmatch(day):
        raise ValueError(f"day must be YYYY-MM-DD, got {day!r}")
    return day


def check_doc_id(doc_id: str) -> str:
    """A usable document id: Firestore refuses '/', '.', '..', '__name__'-style ids and empty ones."""
    if (
        not isinstance(doc_id, str)
        or not doc_id
        or "/" in doc_id
        or doc_id in (".", "..")
        or (doc_id.startswith("__") and doc_id.endswith("__"))
        or len(doc_id.encode()) > 1500
    ):
        raise ValueError(f"invalid document id {doc_id!r}")
    return doc_id


def check_value(value: Any, path: str = "doc", depth: int = 0, in_array: bool = False) -> None:
    """Refuse what Firestore cannot store natively, so the in-memory store fails the same way:
    non-string or empty map keys, arrays directly inside arrays, integers beyond int64, deep nesting and
    non-JSON types (bytes are allowed)."""
    if depth > _MAX_DEPTH:
        raise ValueError(f"{path}: nested more than {_MAX_DEPTH} levels")
    if value is None or isinstance(value, bool | str | bytes | float):
        return
    if isinstance(value, int):
        if not -_INT64 <= value < _INT64:
            raise ValueError(f"{path}: integer out of int64 range")
    elif isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or not key:
                raise ValueError(f"{path}: map keys must be non-empty strings, got {key!r}")
            check_value(item, f"{path}.{key}", depth + 1)
    elif isinstance(value, list | tuple):
        if in_array:
            raise ValueError(f"{path}: an array cannot hold an array in Firestore")
        for i, item in enumerate(value):
            check_value(item, f"{path}[{i}]", depth + 1, in_array=True)
    else:
        raise ValueError(f"{path}: {type(value).__name__} cannot be stored")


def plain(value: Any) -> Any:
    """A deep copy in the shapes Firestore hands back (tuples become lists)."""
    if isinstance(value, dict):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [plain(v) for v in value]
    return value


def utc_day(ts: int) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d")


def is_duplicate(state: State | None, slot: int) -> bool:
    return state is not None and state.last_slot is not None and state.last_slot >= slot


# ---------------------------------------------------------------- documents


def state_to_doc(state: State) -> dict[str, Any]:
    doc = state.to_dict()
    doc["status"] = codec.encode(doc["status"])
    check_value(doc, STATE_DOC)
    return plain(doc)


def state_from_doc(doc: Mapping[str, Any]) -> State:
    doc = dict(doc)
    if isinstance(doc.get("status"), bytes | bytearray | memoryview):
        doc["status"] = codec.decode(doc["status"])
    return State.from_dict(doc)


def month_to_doc(month: str, payload: dict[str, Any], rev: int, now: int) -> dict[str, Any]:
    check_month(month)
    if not isinstance(payload, dict):
        raise ValueError(f"month {month}: payload must be a dict")
    if payload.get("month", month) != month:
        raise ValueError(f"month {month}: payload is for {payload.get('month')!r}")
    raw = codec.dumps(payload)
    blob = codec.compress(raw)
    if len(blob) > MONTH_MAX_BYTES:
        raise ValueError(f"month {month}: {len(blob)} bytes gzipped is over the {MONTH_MAX_BYTES} byte limit")
    if len(blob) > MONTH_WARN_BYTES:
        log.warning(
            "month %s is %d bytes gzipped (warning above %d; Firestore's limit is 1 MiB)",
            month,
            len(blob),
            MONTH_WARN_BYTES,
        )
    return {
        "month": month,
        "rev": rev,
        "payload": blob,
        "bytes": len(blob),
        "sha256": codec.sha256(raw),
        "updated_at": now,
    }


def month_from_doc(doc: Mapping[str, Any]) -> MonthDoc:
    blob = bytes(doc["payload"])
    return MonthDoc(
        month=doc["month"],
        rev=doc["rev"],
        payload=codec.decode(blob),
        sha256=doc.get("sha256", ""),
        size=doc.get("bytes", len(blob)),
    )


def polls_from_doc(day: str, doc: Mapping[str, Any]) -> PollsDoc:
    return PollsDoc(day=day, at=sorted(doc.get("at") or []), n=doc.get("n", 0), failed=list(doc.get("failed") or []))


def snapshot_doc(payload: dict[str, Any]) -> dict[str, Any]:
    return {"hash": codec.digest(payload), "payload": codec.encode(payload)}


def snapshot_from_doc(doc: Mapping[str, Any]) -> dict[str, Any]:
    return codec.decode(doc["payload"])


def checked_doc(doc: dict[str, Any], path: str) -> dict[str, Any]:
    if not isinstance(doc, dict):
        raise ValueError(f"{path}: document must be a dict")
    check_value(doc, path)
    return plain(doc)


# ---------------------------------------------------------------- writes


@dataclass(frozen=True)
class Plan:
    """The documents one write produces, all built (and validated) before anything is written."""

    state: dict[str, Any] | None  # meta/state, or None to leave it
    months: dict[str, dict[str, Any]]  # month -> months/{month}
    polls: dict[str, tuple[list[int], list[dict[str, Any]]]]  # UTC day -> (poll times, failed records) to add
    snapshot: tuple[str, dict[str, Any]] | None  # (epoch id, snapshots/{epoch})


def plan_months(
    current: State | None, base: State | None, months: Mapping[str, dict[str, Any]], now: int
) -> tuple[dict[str, Any] | None, dict[str, dict[str, Any]]]:
    """Month documents with their next revisions, and the state document (`base`, or the current state when a
    month is written without one) carrying the updated index."""
    index = copy.deepcopy(current.months) if current else {}
    docs = {}
    for month, payload in sorted(months.items()):
        rev = int((index.get(month) or {}).get("rev", 0)) + 1
        docs[month] = month_to_doc(month, payload, rev, now)
        index[month] = {"rev": rev, "n": len(payload.get("episodes") or [])}
    if base is None and docs:
        base = current or State()
    state = state_to_doc(dataclasses.replace(base, months=index)) if base is not None else None
    return state, docs


def plan_poll(current: State | None, write: PollWrite, now: int) -> Plan:
    if not isinstance(write, PollWrite):
        raise TypeError(f"fn must return a PollWrite or None, not {type(write).__name__}")
    state, months = plan_months(current, write.state, write.months, now)
    polls: dict[str, tuple[list[int], list[dict[str, Any]]]] = {}
    if write.poll_at is not None:
        at = int(write.poll_at)
        polls.setdefault(utc_day(at), ([], []))[0].append(at)
    if write.failed is not None:
        record = {"slot": int(write.failed["slot"]), "code": str(write.failed["code"])}
        polls.setdefault(utc_day(record["slot"] * 60), ([], []))[1].append(record)
    snapshot = None
    if write.snapshot is not None:
        ts, payload = write.snapshot
        snapshot = (str(int(ts)), snapshot_doc(payload))
    return Plan(state, months, polls, snapshot)
