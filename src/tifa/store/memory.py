"""In-memory Store for tests and local runs.

It keeps the same documents as the Firestore store (built by the same helpers in `base`) and gives the same
transactional guarantees: one lock serialises every operation, and a write's documents are all built before any
is stored, so a failure leaves nothing behind.
"""

from __future__ import annotations

import copy
import threading
import time
from collections.abc import Callable
from typing import Any

from tifa.store.base import (
    APPLIED,
    DUPLICATE,
    GUARD_DOC,
    STATE_DOC,
    MonthDoc,
    Plan,
    PollsDoc,
    PollWrite,
    State,
    check_day,
    check_doc_id,
    check_month,
    checked_doc,
    is_duplicate,
    month_from_doc,
    plain,
    plan_months,
    plan_poll,
    polls_from_doc,
    snapshot_doc,
    snapshot_from_doc,
    state_from_doc,
)


class MemoryStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()  # re-entrant, so `fn` may read the store inside a poll transaction
        self._docs: dict[str, dict[str, Any]] = {}

    # documents in and out are copies, as they would be over the wire
    def _get(self, path: str) -> dict[str, Any] | None:
        with self._lock:
            doc = self._docs.get(path)
            return plain(doc) if doc is not None else None

    def _state(self) -> State | None:
        doc = self._get(STATE_DOC)
        return state_from_doc(doc) if doc is not None else None

    def get_state(self) -> State | None:
        return self._state()

    def poll_transaction(self, slot: int, fn: Callable[[State | None], PollWrite | None]) -> str:
        with self._lock:
            current = self._state()
            if is_duplicate(current, slot):
                return DUPLICATE
            write = fn(copy.deepcopy(current))
            if write is None:
                return DUPLICATE
            self._apply(plan_poll(current, write, int(time.time())))
            return APPLIED

    def _apply(self, plan: Plan) -> None:
        if plan.state is not None:
            self._docs[STATE_DOC] = plan.state
        for month, doc in plan.months.items():
            self._docs[f"months/{month}"] = doc
        for day, (times, failed) in plan.polls.items():
            doc = self._docs.setdefault(f"polls/{day}", {})
            if times:  # array-union and increment, as Firestore applies them
                doc["at"] = doc.get("at", []) + [t for t in dict.fromkeys(times) if t not in doc.get("at", [])]
                doc["n"] = doc.get("n", 0) + len(times)
            if failed:
                have = doc.setdefault("failed", [])
                have.extend(r for r in failed if r not in have)
        if plan.snapshot is not None:
            ts, doc = plan.snapshot
            self._docs[f"snapshots/{ts}"] = doc

    def get_month(self, month: str) -> MonthDoc | None:
        doc = self._get(f"months/{check_month(month)}")
        return month_from_doc(doc) if doc is not None else None

    def list_months(self) -> dict[str, int]:
        state = self._state()
        return {m: v["rev"] for m, v in sorted(state.months.items())} if state else {}

    def put_month(self, month: str, payload: dict) -> int:
        with self._lock:
            current = self._state()
            state, docs = plan_months(current, None, {check_month(month): payload}, int(time.time()))
            self._docs[STATE_DOC] = state
            self._docs[f"months/{month}"] = docs[month]
            return docs[month]["rev"]

    def get_polls(self, day: str) -> PollsDoc | None:
        doc = self._get(f"polls/{check_day(day)}")
        return polls_from_doc(day, doc) if doc is not None else None

    def add_snapshot(self, ts: int, payload: dict) -> None:
        with self._lock:
            self._docs[f"snapshots/{int(ts)}"] = snapshot_doc(payload)

    def get_snapshot(self, ts: int) -> dict | None:
        doc = self._get(f"snapshots/{int(ts)}")
        return snapshot_from_doc(doc) if doc is not None else None

    def get_guard(self) -> dict | None:
        return self._get(GUARD_DOC)

    def put_guard(self, doc: dict) -> None:
        with self._lock:
            self._docs[GUARD_DOC] = checked_doc(doc, GUARD_DOC)

    def record_import(self, import_id: str, doc: dict) -> None:
        path = f"imports/{check_doc_id(import_id)}"
        with self._lock:
            self._docs[path] = checked_doc(doc, path)

    def get_import(self, import_id: str) -> dict | None:
        return self._get(f"imports/{check_doc_id(import_id)}")
